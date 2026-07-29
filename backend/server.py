"""LockScore AI — Sports Betting Intelligence backend."""
import os
import logging
import re
import uuid
import asyncio
from datetime import datetime, timezone, timedelta, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Annotated, Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, Field
from starlette.middleware.cors import CORSMiddleware

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

from auth import (  # noqa: E402
    UserCreate, UserLogin, UserPublic, Token,
    hash_password, verify_password, create_access_token,
    get_current_user_from_db, oauth2_scheme,
)
from sports_engine import generate_all_picks  # noqa: E402
from rate_limit import rate_limit  # noqa: E402

# Per-IP login throttle: 10 attempts/min, burst 5 — blocks credential
# brute-force while letting a user fat-finger their password without
# being locked out. (SEC-005, 2026-06-25.)
_login_throttle = rate_limit(rate_per_min=10, burst=5, scope="ip")
# Per-user compute throttle for heavy endpoints (parlay, analytics
# recompute, Monte Carlo sims). 30 req/min, burst 10 — generous for
# normal UI flow, hard cap on scripted abuse that would burn CPU /
# paid third-party quota.
_compute_throttle = rate_limit(rate_per_min=30, burst=10, scope="user")
from ai_engine import explain_pick, analyze_loss  # noqa: E402
from settlement_engine import settle_due_picks  # noqa: E402

# Shared deps (Mongo, logger, current_user) live in deps.py so route
# modules under backend/routes/ can import them without circular refs
# back into server.py. We re-bind the symbols here so existing code in
# this file (and any third-party import like `from server import db`)
# keeps working unchanged.
from deps import db, client, logger, current_user, strip_mongo as _strip_mongo  # noqa: E402

app = FastAPI(title="PerkLocks AI")
api = APIRouter(prefix="/api")

# ── Picks routes (Phase 1 + Phase 2 + Phase 3 — full extraction) ──
# This module now owns EVERY `/picks/*` route. Mounted here at the top
# right after `api = APIRouter()` so its routes are registered before
# any other server.py route (matches Phase 1 semantics). The static
# routes (/all, /nrfi-yrfi, /markets/{sport}, /refresh-status, /today,
# /parlay, /rollover, /history, /settle, /refresh, /under-of-the-day,
# /bet-killer) are declared BEFORE the parameterized `/{pick_id}`
# catch-all inside picks_routes.py, so FastAPI matches the literal
# segments correctly.
# Parlay generation lives in its own router (2026-06-28 extraction).
# MUST mount BEFORE picks_routes — picks_routes registers a `/{pick_id}`
# catch-all that would otherwise match `/parlay` and 404 "Pick not
# found". Routers are evaluated in include_router() order.
try:
    from routes.parlay_routes import router as _parlay_router
    api.include_router(_parlay_router)
    logger.info("Parlay routes mounted at /api/picks/parlay")
except Exception as _parlay_mount_err:
    logger.warning(
        "Parlay routes failed to mount: %s", _parlay_mount_err,
    )

try:
    from routes.picks_routes import router as _picks_router
    api.include_router(_picks_router)
    logger.info("Picks routes (Phase 1+2+3) mounted at /api/picks/*")
except Exception as _picks_mount_err:
    # Module-level imports below depend on server module loading even
    # if the router file has a bug — log + continue rather than crash.
    logger.warning(
        "Picks routes failed to mount: %s", _picks_mount_err,
    )

# Lab research/analytics endpoints (Session 2+3 build 2026-07-07).
try:
    from lab_routes import router as _lab_router
    api.include_router(_lab_router)
    logger.info("Lab research routes mounted at /api/lab/*")
except Exception as _lab_mount_err:
    logger.warning("Lab routes failed to mount: %s", _lab_mount_err)


# ────────────────────── Data version (cache-bust signal) ──────────────────────
# Bump `DATA_VERSION` whenever a backend change requires phones to wipe their
# AsyncStorage caches (changed pick schema, fabrication scrub, market filters,
# etc.). Phones poll /api/version on launch + tab focus and auto-wipe stale
# caches when their stored version differs from this one. See cachebust.ts
# on the frontend for the consumer logic.
#
# Format: YYYY.MM.DD-N
DATA_VERSION = "2026.07.16-dedupe-both-sides-v45"
SERVER_STARTED_AT = datetime.now(timezone.utc)


@api.get("/version")
async def get_version():
    """Public endpoint — no auth required. Phones poll this on launch and on
    tab focus to detect when the server has shipped new data they should
    rehydrate."""
    return {
        "data_version": DATA_VERSION,
        "server_time": datetime.now(timezone.utc).isoformat(),
        "server_started_at": SERVER_STARTED_AT.isoformat(),
    }


@api.get("/health")
@api.get("/healthz")
@api.get("/ready")
async def get_health():
    """Liveness/readiness probe — Kubernetes pings this to decide whether
    to restart the container. CRITICAL: must be FAST (<50ms), MUST never
    return 4xx, and MUST never touch the DB or any external service.
    User report (2026-06-28): deployed app showed `GET /api/health → 404`
    in the access log — the missing endpoint was a candidate for the
    container being repeatedly marked unhealthy and restarted, which
    explained the "server keeps going down" experience.
    Also serves /healthz and /ready for k8s/gcp/aws probe conventions."""
    return {"status": "ok", "ts": datetime.now(timezone.utc).isoformat()}


# ────────────────────── Auth ──────────────────────
# `current_user` lives in deps.py and is imported above. Keeping
# this section header as a navigational marker.


# SEC P3-C (2026-06-25): per-IP throttle on registration. 5 new accounts
# per IP per minute (burst 3) — enough for a legit user to fix a typo'd
# email but blocks scripted account-spam abuse.
_register_throttle = rate_limit(rate_per_min=5, burst=3, scope="ip")


@api.post("/auth/register", response_model=Token, status_code=201)
async def register(payload: UserCreate, _throttle: None = Depends(_register_throttle)):
    existing = await db.users.find_one({"email": payload.email.lower()})
    if existing:
        raise HTTPException(status_code=409, detail="Email already registered")
    user_id = str(uuid.uuid4())
    doc = {
        "id": user_id,
        "email": payload.email.lower(),
        "name": payload.name or payload.email.split("@")[0],
        "hashed_password": hash_password(payload.password),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.users.insert_one(doc)
    public = UserPublic(id=user_id, email=doc["email"], name=doc["name"],
                        created_at=doc["created_at"])
    return Token(access_token=create_access_token(user_id), user=public)


@api.post("/auth/login", response_model=Token)
async def login(payload: UserLogin, _throttle: None = Depends(_login_throttle)):
    doc = await db.users.find_one({"email": payload.email.lower()})
    if not doc or not verify_password(payload.password, doc["hashed_password"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    # Reject suspended users at login itself so they never get a usable
    # JWT (cleaner UX than letting them in then 403ing everything).
    if (doc.get("status") or "active") == "suspended":
        raise HTTPException(status_code=403, detail="Account suspended. Contact support.")
    # Forward role/status so the frontend can gate admin UI off the
    # login response (iter52 testing-agent bug: previous version dropped
    # these fields, so the "ADMIN DASHBOARD" tile only appeared after a
    # /auth/me refresh).
    public = UserPublic(
        id=doc["id"],
        email=doc["email"],
        name=doc.get("name"),
        created_at=doc.get("created_at"),
        role=doc.get("role") or "user",
        status=doc.get("status") or "active",
    )
    # Stamp last_login_at for the dashboard's active-24h tile.
    from datetime import datetime as _dt, timezone as _tz
    await db.users.update_one(
        {"id": doc["id"]},
        {"$set": {"last_login_at": _dt.now(_tz.utc).isoformat()}},
    )
    return Token(access_token=create_access_token(doc["id"]), user=public)


@api.get("/auth/me", response_model=UserPublic)
async def me(user: Annotated[UserPublic, Depends(current_user)]):
    return user


# ────────────────────── Picks ──────────────────────


# The betting "slate date" is the US EASTERN calendar date, never the UTC
# date. Every US slate (MLB/NBA/NFL/NHL) is published, graded and settled
# against ET, and an ET-evening game is ALREADY TOMORROW in UTC: a 7:41 PM
# ET first pitch is 23:41Z, and anything written after 8 PM ET crosses
# midnight UTC.
#
# Stamping pick_date from UTC therefore pushed the whole evening slate onto
# tomorrow's board. Observed 2026-07-29: hitter props written at 00:08Z for
# games with event_time 2026-07-28T22:41Z-23:50Z were stamped
# pick_date=2026-07-29, so /picks/today served last night's ALREADY-SETTLED
# Hits and H+R+RBI props while today's real slate showed none of them.
#
# `_today_str()` backs BOTH the write path (`_refresh_picks(_today_str())`)
# and every read path (`{"pick_date": _today_str()}` in picks_routes /
# parlay_routes), so moving it to ET keeps generation and querying aligned.
_SLATE_TZ = ZoneInfo("America/New_York")


def _today_str() -> str:
    """Current slate date (US Eastern) as YYYY-MM-DD."""
    return datetime.now(_SLATE_TZ).strftime("%Y-%m-%d")


# Game-time cutoff for "today" feeds. We only want PREGAME picks — once
# a game starts (with a tiny 2-minute clock-skew grace for first-pitch
# timing) the pick is hidden. Player props get the same treatment as
# spreads/totals/moneyline: no live picks, period.
_PREGAME_GRACE_SECONDS = 2 * 60

# How long AFTER a game's listed start time we still surface its picks.
# Player props (Hits, K's, etc.) are bookable through live betting at every
# major sportsbook, so for in-progress games we keep the picks visible until
# typical end-of-game. Hard ceiling caps at typical game duration + 30 min
# safety margin. The `no_bet` flag (set by the settler when MLB Stats API
# reports `Final`) takes precedence — anything settled disappears immediately
# regardless of this window.
#
# Tunable from `/api/admin/services-runtime` if needed.
_IN_PLAY_GRACE_BY_SPORT: dict[str, int] = {
    "MLB":    4 * 3600,    # avg game 3:00, extras can push to 4:00
    "NBA":    3 * 3600,    # avg 2:15 + halftime + OT margin
    "NFL":    4 * 3600,    # avg 3:10 + 2-min warnings + commercials
    "CFB":    4 * 3600,
    "NHL":    3 * 3600,
    "Soccer": int(2.5 * 3600),  # 90 min + stoppage + ET/PK
    "Tennis": 5 * 3600,    # best-of-5 GS matches can run 5+ hours
    "UFC":    int(1.5 * 3600),
    "MMA":    int(1.5 * 3600),
}
# Default for unknown sports — split the difference. Most pick types settle
# within 3.5h so this is safer than dropping at +2min.
_IN_PLAY_GRACE_DEFAULT = int(3.5 * 3600)


def _canonicalize_lock_score(pick: dict) -> dict:
    """Promote V2 → primary lock_score at READ time so every endpoint
    returns the same number the user sees in the UI.

    Background: pick docs carry both `lock_score` (legacy V1, written at
    creation) and `lock_score_v2` (recomputed by the V2 engine). The
    learning loop promotes V2 → V1 periodically, but between passes the
    two can drift apart — the home feed would carry stale V1 (e.g. 85)
    while /picks/{id} re-derived V2 (e.g. 94). Calling this on every
    serialized pick guarantees parity across the API surface and lets
    the frontend treat `lock_score` as the canonical value once again.

    Rules:
      • lock_score = max(lock_score, lock_score_v2), clamped to [0, 99].
      • lock_score_v2 left intact for analytics / shadow visibility.
      • grade + confidence re-derived when we promote, so the badge,
        progress bar, and label always agree with the headline number.
    """
    # ── Tennis global lock authority (2026-07-16) ────────────────────
    # For ANY tennis pick, `lock_score` is the authoritative value.
    # Skip the MAX-of-shadow-fields promotion regardless of the
    # tennis_calibrated flag — that flag can be stripped by a stale
    # ingestion writer, leaving the shadow fields (v2/raw/peak) with
    # pre-calibration values that MAX would then promote back up. User
    # report 2026-07-16: "How is this possible?" — a pick with DB
    # lock_score=86.2 was displaying as LOCK 92 because lock_score_v2
    # still held 91.9 from evidence_engine before tennis calibration ran.
    if (pick.get("sport") or "").lower() == "tennis":
        try:
            from sports_engine import _grade as _tc_grade, _confidence as _tc_conf
            final_lock = float(pick.get("lock_score") or 0)
            if final_lock > 0:
                pick["grade"]      = _tc_grade(final_lock)
                pick["confidence"] = _tc_conf(final_lock)
        except Exception:
            pass
        try:
            from probability_engine import unified_probability_report
            pick["probability"] = unified_probability_report(pick)
        except Exception as e:
            logger.debug("probability_engine attach skipped (tennis): %s", e)
        return pick

    # ── Tennis-calibrated fast-path (2026-07-16) ────────────────────
    # Retained for parity with the above but the tennis check runs
    # first now — everything falls through this block.
    if pick.get("tennis_calibrated"):
        try:
            from sports_engine import _grade as _tc_grade, _confidence as _tc_conf
            final_lock = float(pick.get("lock_score") or 0)
            if final_lock > 0:
                pick["grade"]      = _tc_grade(final_lock)
                pick["confidence"] = _tc_conf(final_lock)
        except Exception:
            pass
        # Attach probability engine breakdown (same as standard path).
        try:
            from probability_engine import unified_probability_report
            pick["probability"] = unified_probability_report(pick)
        except Exception as e:
            logger.debug("probability_engine attach skipped (tennis): %s", e)
        return pick

    try:
        v1 = float(pick.get("lock_score") or 0)
    except Exception:
        v1 = 0.0
    try:
        v2 = float(pick.get("lock_score_v2") or 0)
    except Exception:
        v2 = 0.0
    try:
        raw = float(pick.get("lock_score_raw") or 0)
    except Exception:
        raw = 0.0
    try:
        peak = float(pick.get("lock_score_peak") or 0)
    except Exception:
        peak = 0.0
    # ── Canonical lock_score = MAX of all shadow fields ────────────────
    # Multiple writers across the codebase (evidence_engine, validator,
    # learning_v2, govern_pick, lazy governance, bandit, player_form) all
    # touch lock_score independently. ANY of them can transiently lower
    # the displayed v1 — but v2 / raw / peak hold the "true" computed
    # anchors. Taking the MAX is the single read-time source of truth
    # that recovers from every demotion path without us having to add a
    # carve-out to all 30+ writers. (2026-06-26: user fix for recurring
    # CSL/elite lock demotion bug.)
    canonical = max(v1, v2, raw, peak)
    # ── Always-Starter Read-Time Floor (2026-07-18) ────────────────────
    # User feedback: "Harry Kane should always make the board — he's
    # one of the best scorers in the world". The pipeline is a Rube-
    # Goldberg of writers (apply_learning → apply_elite_boost →
    # govern_pick → pick_validator → learning_v2 → bandit → …) and
    # any of them can silently reset an always-starter's lock_score
    # to its pre-boost value between refreshes. Instead of chasing
    # every one of those writers, guarantee the floor here at
    # SERIALIZATION time — the read-time canonicalizer is the single
    # last line of defense before a pick hits the wire.
    #
    # Floor the canonical lock at 85 for hand-curated world-class
    # scorers (Kane / Mbappe / Haaland / etc.) so they always land
    # on the Home board with at least Playable grade. Preserves the
    # ordering of the rest of the slate.
    try:
        sport = (pick.get("sport") or "").lower()
        if sport == "soccer":
            from elite_players import is_always_starter_soccer
            player_name = (
                pick.get("player_name")
                or pick.get("elite_player_name")
                or pick.get("always_starter_name")
                or ""
            )
            if not player_name:
                market = pick.get("market") or ""
                for suffix in (" Anytime Goal Scorer", " To Score or Assist",
                               " Anytime Scorer", " To Score",
                               " Score or Assist"):
                    if market.endswith(suffix):
                        player_name = market[: -len(suffix)].strip()
                        break
            if is_always_starter_soccer(player_name):
                _ALWAYS_STARTER_READ_FLOOR = 85.0
                if canonical < _ALWAYS_STARTER_READ_FLOOR:
                    canonical = _ALWAYS_STARTER_READ_FLOOR
                    pick["always_starter_read_floor_applied"] = True
    except Exception:
        # Never let the always-starter floor break the read path;
        # any exception here just falls back to the unadjusted
        # canonical value from the MAX-of-shadows above.
        pass
    # ── Coherence cap ceiling (2026-06-30, code-review HIGH) ───────────
    # `quality_gate._apply_lockscore_coherence` and `_apply_display_cap`
    # set `coherence_cap_ceiling` when they DELIBERATELY lower a pick's
    # lock_score (e.g. negative-edge Mbappé → cap at 60, anytime-scorer
    # display cap → 75). Without this clamp the `max()` above silently
    # promotes the uncapped lock_score_raw / peak back above the cap,
    # so a −EV pick still displays "Lock 99 / Elite" — exactly the
    # Mbappé bug the gate is supposed to prevent.
    try:
        ceiling = pick.get("coherence_cap_ceiling")
        if isinstance(ceiling, (int, float)) and ceiling > 0:
            canonical = min(canonical, float(ceiling))
    except Exception:
        pass
    if canonical > v1 + 0.05:
        try:
            from sports_engine import _grade, _confidence
            pick["lock_score"] = round(min(99.0, canonical), 1)
            pick["grade"] = _grade(pick["lock_score"])
            pick["confidence"] = _confidence(pick["lock_score"])
            pick["v2_promoted_at_read"] = True
            pick["_grade_refreshed_at_read"] = True
            # Keep lock_score_raw in lockstep so the Evidence Inspector
            # math reconciles (raw × multiplier = lock). If we promote
            # to V2's value, promote the raw accordingly. Falls back to
            # the governed V2 itself when v2_raw isn't stored (legacy).
            v2_raw = pick.get("lock_score_v2_raw")
            if v2_raw is None:
                # Multiplier of 1.0 implies raw == governed; this is a
                # safe fallback that still makes lock_score_raw ≥
                # lock_score so the iter-49 canary stays green.
                v2_raw = pick["lock_score"]
            pick["lock_score_raw"] = round(min(99.0, float(v2_raw)), 1)
            # Re-align evidence_breakdown.multiplier with the NEW
            # (lock / raw) ratio so the admin inspector math reconciles
            # post-promotion (iter-50 finding #3). Without this, V1's
            # multiplier sticks around while lock/raw report V2's pair.
            try:
                eb = pick.get("evidence_breakdown")
                if isinstance(eb, dict) and pick["lock_score_raw"] > 0:
                    new_mult = pick["lock_score"] / pick["lock_score_raw"]
                    eb["multiplier"]    = round(new_mult, 3)
                    eb["lock_raw"]      = pick["lock_score_raw"]
                    eb["lock_governed"] = pick["lock_score"]
            except Exception:
                pass
        except Exception:
            # Safe fallback — at minimum, surface the higher number even if
            # we can't re-grade. Prevents the card-vs-detail mismatch.
            pick["lock_score"] = round(min(99.0, v2), 1)
            pick["lock_score_raw"] = pick["lock_score"]
    # NOTE: calibration overlay was wired here in iter33 (blended a 5-component
    # calibrated display score that crushed chalk-locks like Bieber from raw
    # 92.7 down to display 67). User requested reverting on 2026-06-23
    # ("Bieber should still be a 90+ at this line don't want to change app
    # idea") — raw model score is the canonical display. Calibration
    # infrastructure stays in /app/backend/lock_calibration.py (curve fit,
    # analytics endpoint, auto-recalibrate) so the Confidence Calibration
    # analytics view still surfaces Expected vs Actual deltas for tuning.
    #
    # ── Unified Probability Engine attachment (iter37, 2026-06-23) ─────
    # User: "Primary probability engine + optional transparency layer
    # (same source of truth) — keeps Bieber-style 93+ locks consistent
    # instead of splitting logic."
    # We attach the engine's full breakdown to EVERY pick payload at
    # serialisation time. The block under `pick["probability"]` is the
    # canonical source for v1/v2/sim probabilities, p_final, p_calibrated,
    # edge, and LOCK_99/PREMIUM/CHALK classification. Existing
    # `lock_score` field is left untouched so Bieber still displays 93+
    # — the engine merely sits underneath as the authoritative truth any
    # consumer (frontend pick detail, parlay, analytics) can read without
    # divergence. Same engine call drives /api/picks/{id}/probability
    # and the inline block here, so they can never disagree.
    try:
        from probability_engine import unified_probability_report
        pick["probability"] = unified_probability_report(pick)
    except Exception as e:
        logger.debug("probability_engine attach skipped: %s", e)
    # ── ALWAYS re-derive grade/confidence from the FINAL lock_score ──
    # Previously only re-derived when V2 > V1. But picks whose `lock_score`
    # was bumped post-creation by lazy evidence governance (or by the
    # validator self-heal pass) kept their PRE-bump grade string in the
    # DB — so the badge said "PASS" even when lock_score = 95+. User
    # report 2026-06-25: "App tripping again still showing locks as pass."
    # Idempotent: if grade is already correct, this rewrites it to the
    # same string. The DB is left untouched (this is a read-time fix).
    try:
        from sports_engine import _grade as _re_grade, _confidence as _re_conf
        final_lock = float(pick.get("lock_score") or 0)
        if final_lock > 0:
            pick["grade"]      = _re_grade(final_lock)
            pick["confidence"] = _re_conf(final_lock)
    except Exception as _re_err:
        logger.debug("grade re-derive skipped: %s", _re_err)
    # ── ELITE PLAYER FLOOR — final read-time guard ────────────────────
    # Belt-and-suspenders enforcement: any pick flagged `elite_player=True`
    # (Salah, Mbappé, Haaland, Messi, Kane, Ronaldo, Judge, Sinner, etc.)
    # is ALWAYS surfaced at the 95+ Strong Lock floor regardless of what
    # the underlying lock_score / lock_score_v2 fields hold. This survives
    # validator drift, learning-loop demotions, and stale governed values
    # — the user repeatedly asked for elites to appear under every market
    # tab at lock 95+ "because lock score reflects reputation/history,
    # not raw win probability" (2026-06-26).
    #
    # IMPORTANT (2026-06-30, code-review HIGH): the quality-gate
    # coherence cap (e.g. negative-edge Mbappé → 60) is a DELIBERATE
    # demotion that must win over the elite floor — otherwise a −EV
    # superstar pick still shows "Lock 95+ Elite". When
    # `coherence_cap_ceiling` is set BELOW 95, we skip the floor lift
    # entirely. The cap value remains the displayed lock_score from the
    # canonicalize step above.
    try:
        if pick.get("elite_player"):
            cur_lock = float(pick.get("lock_score") or 0)
            try:
                cap_ceiling = float(pick.get("coherence_cap_ceiling") or 0)
            except Exception:
                cap_ceiling = 0.0
            if cur_lock < 95.0 and not (cap_ceiling > 0 and cap_ceiling < 95.0):
                from sports_engine import _grade as _eg, _confidence as _ec
                # Lift to max(95, peak) so a pick that previously hit 99
                # stays at 99 — never demote below an earlier peak.
                try:
                    peak = float(pick.get("lock_score_peak") or 0)
                except Exception:
                    peak = 0
                # Even when no cap is set, never lift past a cap ceiling
                # of 95+ (defensive: cap=95 should still be honoured).
                target = max(95.0, peak)
                if cap_ceiling > 0:
                    target = min(target, cap_ceiling)
                new_lock = round(min(99.0, target), 1)
                pick["lock_score"]      = new_lock
                pick["lock_score_raw"]  = new_lock
                pick["lock_score_v2"]   = max(pick.get("lock_score_v2") or 0, new_lock)
                pick["grade"]           = _eg(new_lock)
                pick["confidence"]      = _ec(new_lock)
                pick["pinned"]          = True
                pick["elite_floor_applied_at_read"] = True
    except Exception as _ef_err:
        logger.debug("elite floor enforcement skipped: %s", _ef_err)
    # ── Win-probability sign-flip guard (2026-06-26) ───────────────────
    # Bug surfaced in deployed app: ALT LOCK strikeout picks showed
    # Lock 99 + Win 19.8% + Implied 83.3% (the WIN field was carrying the
    # *complement* of the true probability — i.e. the "fade" side's
    # probability for an Over alt). The Lock model correctly graded these
    # as Elite Locks but win_probability was 1 − p.
    #
    # Defensive read-time fix: if lock_score is ≥ 80 AND implied_prob is
    # also ≥ 70%, the model agrees this is a chalk lock. In that scenario
    # a sub-50 win_probability is INTERNALLY INCONSISTENT and almost
    # certainly a sign-flipped survivor from an older alt-line writer.
    # We flip it back to (100 − win_prob) so the card stops showing
    # 19.8% next to a 99 Lock badge. The original (flipped) value is
    # preserved on `win_probability_pre_flip` for forensics.
    try:
        # Skip the flip-guard entirely for v3 goal-scorer picks. They
        # store `edge_percent=None` deliberately (strict edge gate) and
        # any recompute below would fabricate a non-zero edge that the
        # engine explicitly refuses to publish.
        if (pick.get("source") == "goal_scorer_v3"
                or pick.get("odds_source") == "model_derived"):
            return pick
        ls_for_chk = float(pick.get("lock_score") or 0)
        wp_for_chk = float(pick.get("win_probability") or 0)
        imp_for_chk = float(pick.get("implied_probability") or pick.get("book_implied_prob") or 0)
        if ls_for_chk >= 80.0 and imp_for_chk >= 70.0 and 0 < wp_for_chk < 50.0:
            pick["win_probability_pre_flip"] = wp_for_chk
            pick["win_probability"] = round(max(0.0, min(99.9, 100.0 - wp_for_chk)), 2)
            # Edge needs to re-derive against the corrected win_prob, else
            # the card still shows a huge negative edge.
            try:
                pick["edge_percent"] = round(pick["win_probability"] - imp_for_chk, 2)
            except Exception:
                pass
            pick["_win_prob_sign_flipped_at_read"] = True
    except Exception as _wp_err:
        logger.debug("win-probability flip guard skipped: %s", _wp_err)
    return pick


def _canonicalize_picks(picks: list[dict]) -> list[dict]:
    """Bulk variant of `_canonicalize_lock_score` — apply before returning
    any list of picks from an API endpoint."""
    return [_canonicalize_lock_score(p) for p in picks]


async def _decorate_with_espn_meta(picks: list[dict]) -> list[dict]:
    """Attach ESPN team logos + colors + injury chip to every pick AND
    run the ESPN Signal Engine to fold the same context into the model.

    Two-stage pipeline (per user directive 2026-07-09):
      1. **Enrich** — logos, colors, injury_chip, form strings (display).
      2. **Analyse** — feed the enrichment into `apply_signals_bulk`
         which adjusts `win_probability` and `lock_score` inside a ±6pt
         band. This means the same ESPN data both *shows up on the card*
         and *actually moves the pick* rather than being cosmetic.

    Idempotent — the signal engine bails when `espn_signals` is already
    present on a pick.
    """
    if not picks:
        return picks
    try:
        from services.espn_team_meta import enrich_pick as _meta
        from services.espn_injury_notes import injury_chip_for_pick as _chip
        from services.espn_form_cache import attach_form_to_pick as _form
        from services.espn_signal_engine import apply_signals as _sig
    except Exception:
        return picks
    for p in picks:
        try:
            await _meta(db, p)
            await _chip(db, p)
            await _form(db, p)        # form strings for the signal engine
            await _sig(db, p)         # ← the analysis layer
        except Exception:
            pass  # non-critical enrichment
    return picks


async def _decorate_with_player_form(picks: list[dict]) -> list[dict]:
    """Attach `player_form` data to each pick that references a player.

    Reads from `player_profiles_v2` (live learning store updated every
    refresh cycle). Each decorated pick gets:
        player_form: { name, n_picks, hit_rate, last5_hit, last10_hit, current_streak }

    Only attached when n_picks >= 2 — needs at least 2 samples for meaning.
    Frontend uses this to render hot/cold streak badges on cards.
    """
    if not picks:
        return picks
    try:
        from player_intel.resolver import extract_player_from_market
    except Exception:
        return picks
    needed: set = set()
    for p in picks:
        name = extract_player_from_market(p.get("market", "") or "")
        if name:
            needed.add((p.get("sport") or "Soccer", name))
    if not needed:
        return picks
    profile_map: dict = {}
    or_clauses = [{"sport": s, "canonical_name": n} for s, n in needed]
    async for prof in db.player_profiles_v2.find({"$or": or_clauses}):
        profile_map[(prof.get("sport") or "Soccer", prof.get("canonical_name") or "")] = prof
    for p in picks:
        name = extract_player_from_market(p.get("market", "") or "")
        if not name:
            continue
        prof = profile_map.get((p.get("sport") or "Soccer", name))
        if not prof:
            continue
        if int(prof.get("n_picks") or 0) < 2:
            continue
        p["player_form"] = {
            "name": name,
            "n_picks": int(prof.get("n_picks") or 0),
            "hit_rate": prof.get("hit_rate"),
            "last5_hit": prof.get("last5_hit"),
            "last10_hit": prof.get("last10_hit"),
            "current_streak": int(prof.get("current_streak") or 0),
        }
    return picks


async def _decorate_with_understat_form(picks: list[dict]) -> list[dict]:
    """Attach `understat_form` to soccer goalscorer-market picks.

    Reads from `soccer_player_form` (Understat-backed, refreshed every
    12h). Each decorated pick gets a compact subset suitable for the
    HOT FORM / COLD chip on cards + the form panel in deep-dive:

        understat_form: {
          label: "HOT" | "COLD" | "NEUTRAL",
          score: 0-100,
          lift_pp: ±6 (probability lift in percentage points),
          team:    str,
          league:  str,
          goals:   int,
          games:   int,
          xg:      float,
          npxg_per_90:   float,
          goals_over_xg: float,
        }

    Goalscorer-only — guards by `is_goalscorer_market()` from the
    soccer_player_form module so non-scorer picks aren't decorated.
    Defensive: any error is caught silently and picks pass through
    untouched (this is purely additive UI metadata, not core data).
    """
    if not picks:
        return picks
    try:
        from soccer_player_form import (
            is_goalscorer_market,
            canonicalize_name,
            FORM_LIFT_HOT,
            FORM_LIFT_COLD,
        )
        from player_intel.resolver import extract_player_from_market
    except Exception:
        return picks

    # Collect canonicalised names once so we can do a single bulk query.
    needed_canon: set[str] = set()
    pick_names: dict[str, str] = {}   # pick_id → canonical name
    for p in picks:
        if not is_goalscorer_market(p):
            continue
        # Prefer the resolver — strips market suffix like "Anytime Goal
        # Scorer" so "Lautaro Martinez Anytime Goal Scorer" reduces
        # cleanly to "Lautaro Martinez". Fallback chain handles edge
        # cases where the resolver returns nothing.
        market_str = p.get("market", "") or ""
        name = extract_player_from_market(market_str) or ""
        if not name:
            name = (p.get("player")
                    or p.get("bet")
                    or p.get("selection") or "")
        name = (name or "").strip()
        if not name or name.lower() in {"yes", "no", "over", "under"}:
            continue
        canon = canonicalize_name(name)
        if not canon:
            continue
        needed_canon.add(canon)
        pick_names[p.get("id") or p.get("_id") or ""] = canon

    if not needed_canon:
        return picks

    try:
        # Single bulk query — pull only fields we need into the chip.
        proj = {
            "name_canonical": 1, "player_name": 1, "team": 1, "league": 1,
            "form_label": 1, "form_score": 1, "goals": 1, "games": 1,
            "xg": 1, "npxg_per_90": 1, "goals_over_xg": 1, "_id": 0,
        }
        form_map: dict[str, dict] = {}
        async for doc in db.soccer_player_form.find(
            {"name_canonical": {"$in": list(needed_canon)}}, proj,
        ):
            # Most-recent record per canonical name wins on collision.
            canon = doc.get("name_canonical") or ""
            existing = form_map.get(canon)
            if not existing or (doc.get("games") or 0) > (existing.get("games") or 0):
                form_map[canon] = doc
    except Exception:
        return picks

    bulk_ops: list = []                                   # type: ignore[type-arg]
    for p in picks:
        canon = pick_names.get(p.get("id") or p.get("_id") or "")
        if not canon:
            continue
        doc = form_map.get(canon)
        if not doc:
            continue
        label = doc.get("form_label") or "NEUTRAL"
        lift_pp = 0.0
        if label == "HOT":
            lift_pp = FORM_LIFT_HOT * 100.0   # +6.0 pp
        elif label == "COLD":
            lift_pp = FORM_LIFT_COLD * 100.0  # -6.0 pp

        # ── Shadow A/B values ───────────────────────────────────────
        # shadow_* fields show what the pick's lock_score / win prob
        # WOULD be if the ±6pp form lift were applied live. The user-
        # facing lock_score stays untouched until we have settlement
        # data validating the lift correlates with hit rate. The
        # analytics endpoint reads `understat_form.label` joined with
        # `status` (won/lost) to compute hit rates per form bucket.
        live_lock = p.get("lock_score")
        if isinstance(live_lock, (int, float)):
            shadow_lock = max(0.0, min(100.0, float(live_lock) + lift_pp))
        else:
            shadow_lock = None
        live_wp = p.get("win_probability")
        if isinstance(live_wp, (int, float)):
            shadow_wp = max(1.0, min(99.0, float(live_wp) + lift_pp))
        else:
            shadow_wp = None

        block = {
            "label":         label,
            "score":         doc.get("form_score"),
            "lift_pp":       round(lift_pp, 2),
            "player_name":   doc.get("player_name"),
            "team":          doc.get("team"),
            "league":        doc.get("league"),
            "goals":         doc.get("goals"),
            "games":         doc.get("games"),
            "xg":            doc.get("xg"),
            "npxg_per_90":   doc.get("npxg_per_90"),
            "goals_over_xg": doc.get("goals_over_xg"),
            # Shadow A/B
            "shadow_lock_score":      round(shadow_lock, 2) if shadow_lock is not None else None,
            "shadow_win_probability": round(shadow_wp, 2) if shadow_wp is not None else None,
            "shadow_mode":            True,
            # Frozen-at-decorate-time so we can compute hit-rate later
            "snapshot_taken_at":      datetime.now(timezone.utc).isoformat(),
        }
        p["understat_form"] = block
        # Persist on the pick document so settlement freezes the form
        # snapshot for analytics. We dedupe by checking equality on
        # label+score+lift — only re-write when something materially
        # changed (player traded teams, slumped, etc.). This caps
        # writes at <20 per /api/picks/today call.
        existing = p.get("_persisted_understat_form")
        if (not existing
                or existing.get("label") != label
                or existing.get("score") != block["score"]
                or existing.get("lift_pp") != block["lift_pp"]):
            pick_id = p.get("id") or p.get("_id")
            if pick_id:
                bulk_ops.append((pick_id, block))

    if bulk_ops:
        try:
            from pymongo import UpdateOne
            ops = [
                UpdateOne(
                    {"id": pid},
                    {"$set": {"understat_form": blk}},
                ) for pid, blk in bulk_ops
            ]
            await db.picks.bulk_write(ops, ordered=False)
        except Exception:
            # Pure additive metadata — never break /picks/today.
            pass

    return picks


def _filter_in_play_window(picks: list[dict]) -> list[dict]:
    """Drop picks whose game has already started.

    User spec: "I do want pregame picks I don't want live picks." So
    once an event's `event_time` is in the past (beyond a tiny clock-skew
    grace) we drop the pick from the visible slate — even player props,
    even MLB Hits/Strikeouts. Reused across /picks/today,
    /picks/bet-killer, /picks/under-of-the-day, and /picks/rollover.

    Robust to both ISO suffixes: `...Z` (Odds API style) AND `...+00:00`
    (tennis-extra scraper / Python isoformat() style). Earlier strict
    `strptime` only matched the `Z` form, so tennis-extra picks fell into
    the "unknown → keep" branch and remained visible all day even after
    the match settled — which is the exact bug the user spotted with
    morning tennis picks still showing in the evening.
    """
    now_utc = datetime.now(timezone.utc)
    out: list[dict] = []
    for p in picks:
        et = p.get("event_time") or ""
        try:
            # fromisoformat handles both `+00:00` and `Z` (Python 3.11+).
            # Fall back to manual `Z` → `+00:00` swap for older interpreters.
            iso = et[:-1] + "+00:00" if et.endswith("Z") else et
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            # Truly unparseable → keep the pick (safe default).
            out.append(p)
            continue
        if (now_utc - dt).total_seconds() <= _PREGAME_GRACE_SECONDS:
            out.append(p)
    return out


def _dedupe_game_outcome_picks(picks: list[dict]) -> list[dict]:
    """Collapse mutually-exclusive game-outcome picks for the same match.

    Picks that resolve from the same 3-way h2h market — Moneyline, Win-or-
    Draw, Double Chance — are MUTUALLY EXCLUSIVE when picked on different
    sides. Showing both ("Sweden ML" + "Netherlands Win or Draw") makes the
    app look broken because they cannot both win.

    Strategy:
      • Group by (sport, event)
      • Identify the game-outcome family (ML / W-or-D / Double Chance / Draw)
      • Keep ONE pick per game from that family, preferring:
          1. Win-or-Draw / Double Chance over straight ML (lower variance,
             draw safety net — pays attention to the user's verified
             preference logged in sports_engine deduper)
          2. Higher lock_score within the same family
          3. Higher edge as final tiebreaker
      • All non-game-outcome picks (totals, spreads, props, goalscorer,
        BTTS, etc.) pass through untouched.
    """
    GAME_OUTCOME_KEYWORDS = (
        "moneyline", "money line", "win or draw", "double chance",
        "match result", " to win",
    )

    def _is_game_outcome(p: dict) -> bool:
        m = (p.get("market") or "").lower()
        return any(k in m for k in GAME_OUTCOME_KEYWORDS)

    def _family_priority(p: dict) -> int:
        m = (p.get("market") or "").lower()
        # Lower number wins. Win-or-Draw / Double Chance preferred for
        # soccer (built-in draw safety net). Pure Draw picks fall behind
        # because they're a coin flip without a clear sharpness edge.
        if "win or draw" in m or "double chance" in m:
            return 0
        if " to win" in m or "moneyline" in m or "money line" in m or "match result" in m:
            return 1
        return 2

    keep_by_game: dict = {}
    passthrough: list[dict] = []
    for p in picks:
        if not _is_game_outcome(p):
            passthrough.append(p)
            continue
        key = (p.get("sport"), p.get("event"))
        cur = keep_by_game.get(key)
        if cur is None:
            keep_by_game[key] = p
            continue
        # Family priority first
        new_pri = _family_priority(p)
        old_pri = _family_priority(cur)
        if new_pri < old_pri:
            keep_by_game[key] = p
            continue
        if new_pri > old_pri:
            continue
        # Same family → higher lock_score, then higher edge
        if float(p.get("lock_score") or 0) > float(cur.get("lock_score") or 0):
            keep_by_game[key] = p
            continue
        if float(p.get("lock_score") or 0) == float(cur.get("lock_score") or 0):
            if float(p.get("edge_percent") or 0) > float(cur.get("edge_percent") or 0):
                keep_by_game[key] = p
    return passthrough + list(keep_by_game.values())


def _player_team_for_event(player: str, event: str, sport: str) -> str:
    """Best-effort lookup: which team in this event does `player` play
    for? Returns the matching team string ('Sweden', 'Netherlands', etc.)
    from the event title, or '' if we can't tell.

    Used by `_dedupe_goalscorer_per_event` to give EACH team in the match
    its own top-N quota — without this, Netherlands' 3 elite strikers
    crowd out Sweden's Gyökeres on a Sweden @ Netherlands card.
    User spec: "I like malen and gakpo they on same team tho if Sweden
    score it's probably going to be gyokeres".
    """
    # Event title is "Away @ Home". Pull both team names.
    if " @ " not in event:
        return ""
    away, home = event.split(" @ ", 1)
    # Cheap heuristic: scan known soccer roster for either side.
    # Module-level lazy cache so we don't pay the import cost on every
    # call.
    global _ROSTER_CACHE
    try:
        _ROSTER_CACHE  # type: ignore  # noqa
    except NameError:
        # National-team rosters for the matches that ship in the
        # default slate (World Cup / Euro qualifiers / friendlies).
        # Hardcoded because the player_profiles_v2 collection only has
        # CLUB teams ("Arsenal / Sweden") which makes string matching
        # against national-team event titles ambiguous.
        _ROSTER_CACHE = {  # type: ignore
            # ── Sweden ───────────────────────────────────────────
            "Sweden": {
                "Viktor Gyökeres", "Viktor Gyokeres", "Alexander Isak",
                "Dejan Kulusevski", "Anthony Elanga", "Emil Forsberg",
                "Gabriel Gudmundsson", "Lucas Bergvall", "Yasin Ayari",
                "Mattias Svanberg", "Victor Lindelof", "Victor Lindelöf",
                "Isak Hien", "Gustaf Nilsson", "Ken Sema",
                "Jens Cajuste", "Jesper Karlstrom", "Jesper Karlström",
                "Gustaf Lagerbielke", "Hjalmar Ekdal",
            },
            # ── Netherlands ──────────────────────────────────────
            "Netherlands": {
                "Memphis Depay", "Cody Gakpo", "Donyell Malen",
                "Brian Brobbey", "Wout Weghorst", "Justin Kluivert",
                "Crysencio Summerville", "Noa Lang", "Guus Til",
                "Teun Koopmeiners", "Tijjani Reijnders", "Mats Wieffer",
                "Frenkie de Jong", "Ryan Gravenberch", "Quinten Timber",
                "Marten de Roon", "Virgil van Dijk", "Nathan Ake",
                "Micky van de Ven", "Denzel Dumfries", "Jorrel Hato",
                "Lutsharel Geertruida", "Jan Paul van Hecke",
            },
        }
    for team_candidate in (away.strip(), home.strip()):
        roster = _ROSTER_CACHE.get(team_candidate, set())  # type: ignore
        # Forgiving substring + accent-stripped match
        name_norm = player.lower().strip()
        for r in roster:
            if r.lower() == name_norm:
                return team_candidate
    return ""


def _interleave_by_league(picks: list[dict], top_n: int = 20,
                            max_per_round: int = 1) -> list[dict]:
    """Round-robin interleave picks by league for the top N positions.

    Fixes the "Sweden Superettan never appears on the app" bug. When one
    competition (FIFA World Cup, MLB AL East, etc.) dominates the lock-
    score leaderboard, every other smaller league gets pushed past the
    user's natural scroll horizon. Users report "you don't carry that
    league" when actually we have 28 picks from it — just buried.

    Approach:
      1. Preserve the FIRST 6 picks (true elite anchors — Mbappé, 99-lock
         marquee picks that should never be displaced).
      2. From position 7 onwards, interleave one pick per league per
         round (sorted by lock_score desc within each league). When all
         leagues exhaust their pool, return to natural lock_score order.
      3. Stop interleaving after `top_n` picks total — beyond that, users
         have made an explicit choice to scroll and the pure lock_score
         order is what they want.

    Args:
      picks: already sorted by lock_score desc.
      top_n: how many of the top positions to apply diversification to.
      max_per_round: picks per league per cycle (defaults to 1 = strict round-robin).
    """
    if not picks or len(picks) <= 6:
        return picks
    head = picks[:6]   # untouched elite anchor zone
    tail = picks[6:]
    # Bucket the tail by league.
    by_lg: dict[str, list[dict]] = {}
    league_order: list[str] = []
    for p in tail:
        lg = p.get("league") or "Other"
        if lg not in by_lg:
            by_lg[lg] = []
            league_order.append(lg)
        by_lg[lg].append(p)
    # Only diversify if there are ≥3 leagues — otherwise pure sort is fine.
    if len(league_order) < 3:
        return picks
    # Round-robin interleave from the bucketed tail.
    interleaved: list[dict] = []
    remaining_quota = max(0, top_n - len(head))
    while remaining_quota > 0 and any(by_lg.values()):
        for lg in list(league_order):
            if not by_lg.get(lg):
                continue
            for _ in range(max_per_round):
                if not by_lg[lg] or remaining_quota <= 0:
                    break
                interleaved.append(by_lg[lg].pop(0))
                remaining_quota -= 1
            if remaining_quota <= 0:
                break
    # Append everything that wasn't selected in the round-robin pass,
    # restoring natural lock_score order (each league's list is already sorted).
    leftover: list[dict] = []
    for lg in league_order:
        leftover.extend(by_lg.get(lg, []))
    leftover.sort(key=lambda p: -float(p.get("lock_score") or 0))
    return head + interleaved + leftover


def _dedupe_goalscorer_per_event(picks: list[dict], top_n: int = 3) -> list[dict]:
    """Per-team-in-match cap on goalscorer / score-or-assist picks.

    The pick engine generates THREE variants per qualifying player
    (Anytime / First / To Score-or-Assist). For a 22-player World Cup
    match, that's 60+ near-identical picks dominating the feed.

    Rules:
      1. Group goalscorer picks by (sport, event, team, market_family).
         The market_family axis (added 2026-06-24, per user audit
         "Players appearing on Anytime Goal board are sometimes missing
         from Score or Assist board") keeps Anytime and Score-or-Assist
         as SEPARATE boards — a player who qualifies for both stays on
         both. ScoreOrAssist is a superset of Anytime, so the user
         should always see both.
      2. Within each (event, team, family) bucket, collapse to one
         row per player — keep highest win% pick.
      3. Keep TOP N players per (event, team, family) — both sides of
         the match AND both market families get their own quota.
         Default raised from 2 → 3 (2026-06-26) so marquee scorers like
         Mané / Sarr / Ndiaye in the same team all survive when they're
         all near +100 implied.
      4. ELITE players ALWAYS survive — passed through unconditionally
         regardless of position in their team's win% ranking.
      5. MARKET-CONFIRMED FAVOURITES ALSO SURVIVE — any player whose
         book implied probability ≥ 40% (price ≤ +150) is a top-tier
         scoring threat the bookmaker is pricing as a primary option.
         Surfacing them regardless of model rank fixes the user-reported
         bug "I see Dieng but not Mané or Ismaïla Sarr" (all three are
         priced near +100 = 50% implied; model rank shouldn't bury a
         50%-implied star scorer).
      6. Non-goalscorer picks pass through untouched.

    The structured audit (see `_scorer_audit_log`) emits a record for
    every player explaining why they were kept/dropped so the admin
    `scorer-audit` endpoint can prove no one silently disappears.
    """
    GOALSCORER_KEYWORDS = (
        "anytime goal scorer", "first goal scorer", "last goal scorer",
        "to score or assist", "to score",
    )

    def _is_scorer(p: dict) -> bool:
        m = (p.get("market") or "").lower()
        return any(k in m for k in GOALSCORER_KEYWORDS)

    def _market_family(market: str) -> str:
        """Coarse market family used to keep Anytime, Score-or-Assist
        and First-Goal-Scorer boards independent of each other."""
        ml = market.lower()
        if "anytime goal scorer" in ml:    return "anytime"
        if "to score or assist" in ml:      return "score_or_assist"
        if "first goal scorer" in ml:       return "first_goal"
        if "last goal scorer" in ml:        return "last_goal"
        return "other_scorer"

    def _market_rank(market: str) -> int:
        ml = market.lower()
        if "anytime goal scorer" in ml:       return 0
        if "to score or assist" in ml:         return 1
        if "first goal scorer" in ml:          return 2
        if "last goal scorer" in ml:           return 3
        return 4

    def _player_from_market(market: str) -> str:
        for kw in GOALSCORER_KEYWORDS:
            idx = market.lower().find(kw)
            if idx > 0:
                return market[:idx].strip()
        return market.strip()

    by_bucket: dict = {}
    passthrough: list[dict] = []
    for p in picks:
        if not _is_scorer(p):
            passthrough.append(p)
            continue
        player = _player_from_market(p.get("market") or "")
        team = _player_team_for_event(player, p.get("event") or "", p.get("sport") or "")
        # market_family is the critical 4th axis — without it, a player
        # on Anytime AND Score-or-Assist gets collapsed to one row.
        fam = _market_family(p.get("market") or "")
        key = (p.get("sport"), p.get("event"), team or "?", fam)
        by_bucket.setdefault(key, []).append(p)

    kept: list[dict] = []
    audit_rows: list[dict] = []
    for key, group in by_bucket.items():
        sport_k, event_k, team_k, fam_k = key
        # Step 2: collapse to one pick per player (highest win% wins).
        best_by_player: dict = {}
        for p in group:
            player = _player_from_market(p.get("market") or "")
            cur = best_by_player.get(player)
            if (
                cur is None
                or float(p.get("win_probability") or 0) > float(cur.get("win_probability") or 0)
                or (
                    float(p.get("win_probability") or 0) == float(cur.get("win_probability") or 0)
                    and _market_rank(p.get("market") or "") < _market_rank(cur.get("market") or "")
                )
            ):
                best_by_player[player] = p
        # Step 3: top N by win_probability per (team, family).
        ranked = sorted(
            best_by_player.values(),
            key=lambda x: -float(x.get("win_probability") or 0),
        )
        top_picks = ranked[:top_n]
        # Step 4: ALL elites survive (not just one) — Gyökeres / Mbappé
        # / Haaland are the headline picks of the slate and should
        # never be dropped silently.
        protected_elite = [
            p for p in ranked[top_n:]
            if (p.get("elite_player") or p.get("auto_elite"))
        ]
        # Step 5: market-confirmed favourites also survive. ANY player
        # with book implied probability >= 40% is being priced as a
        # primary scoring threat by sharp UK/EU books. We trust the
        # bookmaker's price more than our model's win_probability rank
        # for marquee scorers — bug report: "I see Dieng but not Mané or
        # Ismaïla Sarr" — all three were +100 (50% implied), all in the
        # same Senegal squad, but our model rank kept only Dieng. This
        # rule ensures any 40%+ implied scorer always reaches the board.
        protected_market_fav = [
            p for p in ranked[top_n:]
            if (
                p not in protected_elite
                and float(p.get("implied_probability") or 0) >= 40.0
            )
        ]
        kept.extend(top_picks)
        kept.extend(protected_elite)
        kept.extend(protected_market_fav)
        # Audit log — one row per player evaluated.
        for i, p in enumerate(ranked):
            survived = (
                p in top_picks or
                bool(p.get("elite_player")) or
                bool(p.get("auto_elite"))
            )
            audit_rows.append({
                "player":       _player_from_market(p.get("market") or ""),
                "sport":        sport_k,
                "event":        event_k,
                "team":         team_k,
                "market_family": fam_k,
                "market":       p.get("market"),
                "win_probability": float(p.get("win_probability") or 0),
                "implied_probability": float(p.get("implied_probability") or 0),
                "edge_percent": float(p.get("edge_percent") or 0),
                "lock_score":   float(p.get("lock_score") or 0),
                "rank_within_family": i + 1,
                "survived":     survived,
                "reason_excluded":  None if survived else "dedupe_topN_cap",
            })

    # Stash audit on the module for the admin endpoint to read.
    try:
        _set_scorer_audit_log(audit_rows)
    except Exception:
        pass

    return passthrough + kept


# ── Scorer audit log buffer (in-memory, latest run only) ──────────────
# Rotated on every call to `_dedupe_goalscorer_per_event`. The admin
# endpoint `/api/admin/scorer-audit` reads this to expose runtime
# coverage debug info (player, p_goal, p_assist, p_score_or_assist,
# edge_goal, edge_score_or_assist, reason_excluded).
_SCORER_AUDIT_LOG: dict = {"generated_at": None, "rows": []}


def _set_scorer_audit_log(rows: list[dict]) -> None:
    from datetime import datetime, timezone
    _SCORER_AUDIT_LOG["generated_at"] = datetime.now(timezone.utc).isoformat()
    _SCORER_AUDIT_LOG["rows"] = rows


def get_scorer_audit_log() -> dict:
    """Public accessor used by the admin route."""
    return {
        "generated_at": _SCORER_AUDIT_LOG.get("generated_at"),
        "rows":         _SCORER_AUDIT_LOG.get("rows") or [],
    }


def get_scorer_coverage_audit(event: str | None = None) -> dict:
    """Cross-market coverage audit.

    Pivots the per-pick audit log into one row per (event, team, player)
    so we can answer "did this player appear on Anytime AND Score-or-
    Assist?". This is the canonical view for the user's debug request:

        player, p_goal, p_assist, p_score_or_assist,
        edge_goal, edge_score_or_assist, reason_excluded.

    `p_assist` is derived from `p_goal` and the position-based prior used
    elsewhere in the codebase (scorer_bundles.py) so the audit and the
    user-facing scorer-bundles maths stay consistent.
    """
    from scorer_bundles import _assist_prior  # local import, no cycle
    rows = _SCORER_AUDIT_LOG.get("rows") or []
    if event:
        rows = [r for r in rows if (r.get("event") or "") == event]
    grouped: dict = {}
    for r in rows:
        key = (r.get("sport"), r.get("event"), r.get("team"), r.get("player"))
        bucket = grouped.setdefault(key, {
            "sport":  r.get("sport"),
            "event":  r.get("event"),
            "team":   r.get("team"),
            "player": r.get("player"),
            "anytime":         None,
            "score_or_assist": None,
            "first_goal":      None,
        })
        fam = r.get("market_family")
        if fam in ("anytime", "score_or_assist", "first_goal"):
            bucket[fam] = r

    out = []
    for bucket in grouped.values():
        player = bucket.get("player") or ""
        anytime_row = bucket.get("anytime") or {}
        soa_row     = bucket.get("score_or_assist") or {}
        # Probabilities — fall back to book-implied when model prob absent.
        p_goal = anytime_row.get("win_probability") or anytime_row.get("implied_probability") or 0.0
        p_score_or_assist = soa_row.get("win_probability") or soa_row.get("implied_probability") or 0.0
        # Position prior for the assist component.
        p_assist_given_no_goal = _assist_prior(player)
        # If the sportsbook didn't price SoA but Anytime exists, we can
        # synthesise a fair p_score_or_assist for debugging only.
        if not p_score_or_assist and p_goal:
            # P(SoA) ≈ P(goal) + (1 − P(goal)) · P(assist | no goal)
            p_score_or_assist = p_goal + (1.0 - p_goal) * p_assist_given_no_goal
            soa_reason = "sportsbook_market_unavailable"
        else:
            soa_reason = soa_row.get("reason_excluded")
            if not soa_row and p_goal:
                soa_reason = "sportsbook_market_unavailable"
        out.append({
            "player":              player,
            "team":                bucket.get("team"),
            "event":               bucket.get("event"),
            "p_goal":              round(float(p_goal), 4),
            "p_assist":            round(float(p_assist_given_no_goal), 4),
            "p_score_or_assist":   round(float(p_score_or_assist), 4),
            "edge_goal":           round(float(anytime_row.get("edge_percent") or 0), 2),
            "edge_score_or_assist": round(float(soa_row.get("edge_percent") or 0), 2),
            "anytime_survived":    bool(anytime_row.get("survived")) if anytime_row else False,
            "soa_survived":        bool(soa_row.get("survived")) if soa_row else False,
            "reason_excluded":     soa_reason,
            "anytime_market":      anytime_row.get("market"),
            "soa_market":          soa_row.get("market"),
        })
    out.sort(key=lambda r: (-r["p_goal"], r["player"]))
    return {
        "generated_at": _SCORER_AUDIT_LOG.get("generated_at"),
        "total_players": len(out),
        "rows": out,
    }


async def _picks_for_date(date_str: str) -> list[dict]:
    cursor = db.picks.find({"pick_date": date_str}, {"_id": 0}).sort("lock_score", -1)
    return await cursor.to_list(length=500)


_WINRATE_CACHE: dict = {"data": {}, "expires_at": 0.0}
_WINRATE_TTL = 1800  # 30 min refresh window for the learning loop


async def _historical_winrates() -> dict:
    """Aggregate settled picks into (sport, market_family) win-rate buckets.

    Used by the parlay builder as the "study from past picks" learning loop —
    buckets that historically over-perform get a leg-selection boost, ones
    that under-perform get penalized. Cached for 30 min to avoid hammering
    Mongo on every parlay request. Key '__global__' carries the overall
    settled win-rate for normalization.
    """
    import time as _t
    now = _t.time()
    if _WINRATE_CACHE["data"] and _WINRATE_CACHE["expires_at"] > now:
        return _WINRATE_CACHE["data"]
    cursor = db.picks.find(
        {"status": {"$in": ["won", "lost"]}},
        {"_id": 0, "sport": 1, "market": 1, "status": 1},
    )
    docs = await cursor.to_list(length=5000)
    if not docs:
        out = {"__global__": 0.55}
        _WINRATE_CACHE["data"] = out
        _WINRATE_CACHE["expires_at"] = now + _WINRATE_TTL
        return out
    def _family(market: str) -> str:
        m = (market or "").lower()
        if "anytime goal scorer" in m: return "goal_scorer"
        if "win or draw" in m or "double chance" in m: return "win_or_draw"
        if "moneyline" in m: return "moneyline"
        if "spread" in m: return "spread"
        if "over" in m and ("hits" in m or "total bases" in m): return "batter_over"
        if "over" in m or "under" in m: return "total_over_under"
        if "wins by" in m: return "mma_method"
        return "other"
    buckets: dict = {}
    g_won = g_total = 0
    for d in docs:
        won = d["status"] == "won"
        key = ((d.get("sport") or "").lower(), _family(d.get("market") or ""))
        b = buckets.setdefault(key, {"n": 0, "w": 0})
        b["n"] += 1
        if won: b["w"] += 1
        g_total += 1
        if won: g_won += 1
    result: dict = {
        k: {"n": v["n"], "winrate": v["w"] / max(v["n"], 1)}
        for k, v in buckets.items()
    }
    result["__global__"] = g_won / max(g_total, 1)
    _WINRATE_CACHE["data"] = result
    _WINRATE_CACHE["expires_at"] = now + _WINRATE_TTL
    logger.info("Win-rate buckets refreshed: %d buckets, global=%.3f", len(buckets), result["__global__"])
    return result



def _dedupe_and_limit_goalscorers(picks: list[dict]) -> list[dict]:
    """Dedupe duplicate goalscorer picks and trim each event's slate.

    Rule (per user 2026-06-22): "Top 3 goalscorers per match — unless more
    are elite (≥70% win prob AND positive edge)."

    Steps:
      1) DEDUP: For each (event, player, market_family) combo where
         market_family is one of:
            - ATGS  (Anytime Goal Scorer + synthetic AGS from To-Score-or-Assist)
            - FGS   (First Goal Scorer)
            - SoA   (To Score or Assist)
         keep only the single best pick (highest lock_score, ties broken by
         best edge_percent). This kills the "same player 3-4× at different
         book prices / synth duplicates" problem.
      2) TRIM: Within each (event, market_family) group, sort by
         win_probability DESC. Keep top 3 by default. Append any extras
         that pass the elite override (win_probability ≥ 70% AND
         edge_percent > 0). This bounds the goalscorer slate on marquee
         games (Ghana @ England had 47 picks; expected ≤ ~5).
    """
    if not picks:
        return picks
    import re as _re

    def _family(market: str) -> str:
        ml = (market or "").lower()
        if "first goal scorer" in ml: return "FGS"
        if "anytime goal scorer" in ml: return "ATGS"
        if "to score or assist" in ml: return "SoA"
        return ""

    # Extract a stable player name from market labels like "Harry Kane
    # Anytime Goal Scorer" / "Bukayo Saka First Goal Scorer" / "Ollie
    # Watkins To Score or Assist".
    _SUFFIXES = (
        " Anytime Goal Scorer",
        " First Goal Scorer",
        " To Score or Assist",
    )
    def _player_from_market(market: str) -> str:
        m = market or ""
        for suf in _SUFFIXES:
            if m.endswith(suf):
                return m[: -len(suf)].strip().lower()
        # Fallback: strip the family suffix even if mid-string
        return _re.sub(
            r"\s*(anytime goal scorer|first goal scorer|to score or assist).*$",
            "",
            m, flags=_re.I,
        ).strip().lower()

    # Phase 1: dedup
    by_key: dict[tuple, dict] = {}
    rest: list[dict] = []
    for p in picks:
        fam = _family(p.get("market") or "")
        if fam == "" or (p.get("sport") or "") != "Soccer":
            rest.append(p)
            continue
        player = _player_from_market(p.get("market") or "")
        key = (p.get("event") or "", player, fam)
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = p
            continue
        # Higher lock_score wins; ties → higher edge_percent
        def _score(q: dict):
            try: lock = float(q.get("lock_score") or 0)
            except Exception: lock = 0.0
            try: edge = float(q.get("edge_percent") or 0)
            except Exception: edge = 0.0
            return (lock, edge)
        if _score(p) > _score(existing):
            by_key[key] = p

    # Phase 2: trim per (event, family) — Top 3 + elite-override
    by_event_family: dict[tuple, list[dict]] = {}
    for (event, _player, fam), p in by_key.items():
        by_event_family.setdefault((event, fam), []).append(p)

    kept: list[dict] = []
    trimmed = 0
    for (event, fam), group in by_event_family.items():
        # Sort by win_probability DESC; ties broken by lock_score DESC.
        def _sortkey(q: dict):
            try: wp = float(q.get("win_probability") or 0)
            except Exception: wp = 0.0
            try: ls = float(q.get("lock_score") or 0)
            except Exception: ls = 0.0
            return (-wp, -ls)
        group.sort(key=_sortkey)
        top3 = group[:3]
        extras: list[dict] = []
        for q in group[3:]:
            try:
                wp = float(q.get("win_probability") or 0)
                eg = float(q.get("edge_percent") or 0)
            except Exception:
                wp, eg = 0.0, 0.0
            # Elite override:
            #   1) win prob ≥ 70% AND positive edge (book-favorite tier)
            #   2) elite_protect=True (curated CSL elite seed —
            #      Cryzan, Felipe Sousa, Fábio Abreu, Leonardo, Wu Lei,
            #      Júnior Negrão, Cédric Bakambu, Wesley Moraes, etc.)
            #      Synthetic picks have edge=0 so the 70%+edge clause
            #      can never catch them — without this elite_protect
            #      clause they get silently trimmed at top-3 cap.
            if (wp >= 70.0 and eg > 0) or q.get("elite_protect"):
                extras.append(q)
            else:
                trimmed += 1
        kept.extend(top3 + extras)
    logger.info(
        "Goalscorer dedup+trim: %d unique players × markets, %d trimmed",
        len(by_key), trimmed,
    )
    return rest + kept


def _cap_tennis_totals(picks: list[dict], max_per_side: int = 2) -> list[dict]:
    """Cap Tennis alternate-line Total Games to top-N per (match, side).

    User report 2026-06-22: "Why I got so many tennis overs instead of
    moneyline?" The Odds API exposes 5-7 alt-line Total Games markets per
    match (Over/Under 18.5, 19.5, 20.5, ...). Each survives the lock-floor
    independently, flooding the slate while the lone Moneyline market —
    which the bandit just told us is our HOTTEST tennis arm at +13% ROI /
    Sharpe +1.11 — gets buried.

    Fix: per (match, Over|Under), keep only the TOP-N alt-lines by
    win_probability. Default 2 keeps the most informative lines without
    drowning the matchup. Game-level Tennis Moneyline / Spread untouched.
    """
    if not picks:
        return picks
    import re as _re

    def _is_tennis_total(p: dict) -> bool:
        if (p.get("sport") or "") != "Tennis":
            return False
        m = (p.get("market") or "").lower()
        return ("over " in m or "under " in m) and "games" in m

    def _side(p: dict) -> str:
        m = (p.get("market") or "").lower()
        return "over" if "over " in m else "under"

    by_key: dict[tuple, list[dict]] = {}
    rest: list[dict] = []
    for p in picks:
        if _is_tennis_total(p):
            key = (p.get("event") or "", _side(p))
            by_key.setdefault(key, []).append(p)
        else:
            rest.append(p)

    kept: list[dict] = []
    trimmed = 0
    for _key, group in by_key.items():
        # Sort by win_probability DESC; ties broken by lock_score DESC.
        def _sortkey(q: dict):
            try: wp = float(q.get("win_probability") or 0)
            except Exception: wp = 0.0
            try: ls = float(q.get("lock_score") or 0)
            except Exception: ls = 0.0
            return (-wp, -ls)
        group.sort(key=_sortkey)
        kept.extend(group[:max_per_side])
        trimmed += max(0, len(group) - max_per_side)
    logger.info(
        "Tennis Totals cap: %d (match, side) groups kept top-%d each, %d trimmed",
        len(by_key), max_per_side, trimmed,
    )
    return rest + kept


async def _refresh_picks(date_str: str, sport_filter: Optional[str] = None) -> int:
    """Generate today's picks, replace any existing rows for that date.

    Critical: only delete existing picks AFTER we've successfully generated
    new ones. Otherwise, if the upstream API is down/rate-limited, we'd
    end up with an empty board instead of last-known-good picks.

    Pick IDs are deterministic (UUID5 derived from external_id) so cached
    references in user slips and the frontend remain valid across refreshes
    instead of pointing to a brand-new UUID that 404s.

    Args:
      sport_filter: when set (e.g. "MLB"), only re-fetch + replace picks for
        that one sport. Used by the MLB pregame loop (`_mlb_pregame_loop`)
        which runs every 5 min during US afternoons so MLB picks surface
        ~60-90 min pre-game rather than ~5 min pre-game, without burning
        Odds API credits on sports whose slates haven't moved.
    """
    if sport_filter:
        logger.info("Refreshing picks for %s · sport_filter=%s", date_str, sport_filter)
    else:
        logger.info("Refreshing picks for %s", date_str)
    # ── Odds API circuit breaker observability + soft re-arm ─────────
    # If the breaker tripped earlier this process (e.g. transient outage,
    # or operator rotating THE_ODDS_API_KEY mid-day), give it ONE shot
    # to recover. Worst-case cost: 16s stall (2 × 8s timeout) before the
    # breaker re-trips. That's acceptable once per refresh cycle and the
    # only path to self-healing without a manual `/admin/odds-circuit/reset`
    # call. Operator can always observe state via /api/admin/odds-diagnostic.
    try:
        from sports_engine import get_odds_api_status, reset_odds_api_circuit
        st_before = get_odds_api_status()
        if st_before.get("disabled"):
            logger.warning(
                "Refresh starting with Odds API circuit OPEN (%s) — soft re-arming for one retry",
                st_before.get("disabled_reason", "?"),
            )
            reset_odds_api_circuit()
        else:
            logger.info(
                "Odds API state pre-refresh: has_key=%s ok=%d fail=%d streak_401=%d",
                st_before.get("has_key"), st_before.get("total_ok", 0),
                st_before.get("total_fail", 0), st_before.get("consecutive_401s", 0),
            )
    except Exception as _odds_st_err:
        logger.debug("Could not read Odds API status: %s", _odds_st_err)
    picks = await generate_all_picks(date_str, sport_filter=sport_filter)
    # 2026-07-22 diagnostic — count MLS ESPN picks right off the pipeline.
    _mls_espn_from_engine = sum(
        1 for p in (picks or []) if isinstance(p, dict)
        and p.get("source") == "mls_espn_leaderboard"
    )
    if _mls_espn_from_engine:
        logger.info("MLS ESPN picks from engine: %d", _mls_espn_from_engine)
    # Post-refresh observability so the operator can spot a slate that
    # came back smaller than expected without having to tail logs.
    try:
        from sports_engine import get_odds_api_status
        st_after = get_odds_api_status()
        logger.info(
            "Refresh done: %d raw picks | Odds API ok=%d fail=%d disabled=%s",
            len(picks or []), st_after.get("total_ok", 0),
            st_after.get("total_fail", 0), st_after.get("disabled"),
        )
    except Exception:
        pass
    if not picks:
        if sport_filter:
            logger.info(
                "%s pregame refresh: 0 picks (likely no lines posted yet) — "
                "leaving existing %s rows untouched.", sport_filter, sport_filter,
            )
        else:
            logger.warning(
                "Refresh produced 0 picks for %s — keeping existing rows intact "
                "instead of wiping the board.", date_str,
            )
        return 0
    # ── Tennis Extra — scraped picks for tournaments The Odds API doesn't ──
    # carry (Mallorca, Bad Homburg, Eastbourne, Challengers, etc.).
    # User complaint addressed: "Why we not getting these tennis games."
    # Free fallback uses TennisExplorer.com — scrape is cached 30 min.
    try:
        from tennis_extra import fetch_extra_tennis_picks
        # days_ahead=3 gives today + 3 days of visibility so tournaments
        # starting Monday–Wednesday appear in the feed on Sunday night
        # (user report 2026-07-12: Umag/Bastad/Gstaad/Athens/Iasi ATP
        # matches on TU 14.07 weren't being picked up). TennisExplorer
        # supports future-date URLs natively so this is 3 extra HTTP
        # calls per refresh (still under the 30-min in-process cache).
        extra = await fetch_extra_tennis_picks(date_str=date_str, days_ahead=3)
        if extra:
            existing_ids = {p.get("id") for p in picks}
            for ep in extra:
                if ep.get("id") in existing_ids:
                    continue  # dedupe just in case
                picks.append(ep)
            logger.info("Tennis Extra: added %d scraped picks", len(extra))
    except Exception as e:
        logger.warning("Tennis Extra scrape skipped: %s", e)
    # ── MLB Batter-vs-Pitcher enrichment ──
    # User spec: "make sure you got batter vs pitcher when making hit
    # prediction". Pulls career BvP splits from MLB Stats API (free,
    # 0 Odds credits), boosts lock_score for batters with strong
    # historical edge vs the opposing starter, and appends a "5-for-12
    # vs Strider" insight bullet to each MLB hit prop card.
    try:
        from mlb_bvp import enrich_picks_bulk as _bvp_enrich
        await _bvp_enrich(picks)
    except Exception as e:
        logger.warning("MLB BvP enrichment skipped: %s", e)
    namespace = uuid.UUID("00000000-0000-0000-0000-000000000001")
    for p in picks:
        ext = str(p.get("external_id") or "")
        if ext:
            # Deterministic uuid5 from external_id (preferred — survives refreshes).
            p["id"] = str(uuid.uuid5(namespace, ext))
        elif p.get("id"):
            # Upstream already assigned a stable id (e.g. tennis_extra's
            # sha1 hash of te|event_date|tournament|players). Convert that
            # into a uuid5 so the wire format matches all the other picks
            # AND survives across refreshes — this keeps the user's "Save
            # to Slip" links valid overnight when the same scraped match
            # gets re-ingested 30 minutes later.
            upstream_id = str(p["id"])
            p["id"] = str(uuid.uuid5(namespace, f"upstream:{upstream_id}"))
        else:
            # Last resort: random uuid (churns each refresh — only used
            # when the upstream didn't bother to set anything stable).
            p["id"] = str(uuid.uuid4())

    # ── SportDB enrichment: pull live team-form into Soccer picks. Cached
    # league standings (24h TTL) keep the daily request count to ~10. The
    # enrichment is best-effort — if SportDB is down or budget hit we still
    # save the un-enriched pick and move on.
    try:
        from sportdb_client import refresh_top_leagues, enrich_pick
        await refresh_top_leagues(db)
        enriched = 0
        for p in picks:
            if p.get("sport") == "Soccer":
                before = p.get("win_probability")
                await enrich_pick(db, p)
                if p.get("enriched_by") == "sportdb" and p.get("win_probability") != before:
                    enriched += 1
        if enriched:
            logger.info("SportDB enriched %d Soccer picks", enriched)
    except Exception as e:
        logger.warning("SportDB enrichment skipped: %s", e)

    # ── Self-tuning learning layer: bias predictions based on historical
    # ROI / hit-rate vs expected. Applied AFTER all other enrichment so it
    # sits on top of model + SportDB + Odds-API edge.
    try:
        from learning_engine import apply_learning
        adjusted = 0
        for p in picks:
            before = p.get("win_probability")
            await apply_learning(db, p)
            if p.get("learning") and p.get("win_probability") != before:
                adjusted += 1
        if adjusted:
            logger.info("Learning engine adjusted %d picks", adjusted)
    except Exception as e:
        logger.warning("Learning engine skipped: %s", e)

    # ── Elite Player Boost: world-class players (Mbappé, Haaland, Messi,
    # Kane, Judge, Sinner, Jokic, Wilson, etc.) get a +10 lock_score bump
    # so they auto-qualify for Lock tier — books price them tightly but
    # they're still the safest hit candidates by reputation.
    try:
        from elite_players import apply_elite_boost
        before_elite = sum(1 for p in picks if p.get("elite_player"))
        picks = apply_elite_boost(picks)
        after_elite = sum(1 for p in picks if p.get("elite_player"))
        logger.info("Elite Player Boost applied: %d picks tagged (was %d)",
                    after_elite, before_elite)
    except Exception as e:
        logger.warning("Elite Player Boost skipped: %s", e)

    # ── Goalscorer Dedup + Top-3-with-Elite-Override ───────────────────
    # User report 2026-06-22: "Why is so many goalscorers for England game
    # thought we doing top 3 unless it more that's elite 70+ win pct and
    # have edge?". Root cause was two compounding bugs:
    #   1) The same player got multiple Anytime Goal Scorer picks (one per
    #      bookmaker quote, plus synthetic AGS picks created from each
    #      To-Score-or-Assist quote in elite_players.py).
    #   2) No event-level cap on how many goalscorer picks survived,
    #      causing 47-pick blowouts on marquee international friendlies.
    # Fix: dedup by (event, player, market_family), then trim each event's
    # goalscorer slate to Top-3-by-win_probability + any extras meeting
    # win≥70% AND edge>0 (the "elite override").
    try:
        picks = _dedupe_and_limit_goalscorers(picks)
    except Exception as e:
        logger.warning("Goalscorer dedup/limit skipped: %s", e)

    # ── Tennis Totals cap (Top-1 alt-line per match per side) ──────────
    # User report 2026-06-22: "still not seeing ml in tennis". The slate
    # had 5 visible MLs but 28 Overs, ~5.6:1 ratio buried the Moneylines.
    # Top-2 was still too many — tightening to Top-1 per (match, side)
    # halves Tennis Overs again so MLs (which the bandit says are our
    # hottest arm at +13% ROI) become visually prominent in the feed.
    try:
        picks = _cap_tennis_totals(picks, max_per_side=1)
    except Exception as e:
        logger.warning("Tennis Totals cap skipped: %s", e)

    # ── Per-Player Rolling Form (Phase 2 learning upgrade) ─────────────
    # Apply each player's last-10 hot/cold streak as a ±5 lock_score
    # nudge. Doesn't override the engine — just tilts toward players we've
    # been recently right on and away from cold streaks.
    try:
        from player_form import apply_player_form
        form_counts = await apply_player_form(picks, db)
        if form_counts.get("applied", 0) > 0:
            logger.info(
                "Player Form applied to %d picks (🔥 hot=%d, ❄️ cold=%d, neutral=%d)",
                form_counts.get("applied", 0),
                form_counts.get("hot", 0),
                form_counts.get("cold", 0),
                form_counts.get("neutral", 0),
            )
    except Exception as e:
        logger.warning("Player Form skipped: %s", e)

    # ── Multi-Armed Bandit (Phase 3 learning upgrade) ──────────────────
    # Thompson-sample each strategy arm's Beta posterior, then tilt picks
    # belonging to currently-winning arms (+lift) and currently-losing
    # arms (-lift) by up to ±LIFT_MAX lock points. This auto-discovers
    # which combinations of lock/edge/odds/sport/market are hot RIGHT NOW
    # without us hand-tuning thresholds.
    try:
        from bandit import sample_arms, apply_bandit_lift
        sampled = await sample_arms(db)
        if sampled:
            bandit_counts = apply_bandit_lift(picks, sampled)
            if bandit_counts.get("applied", 0) > 0:
                logger.info(
                    "Bandit (Thompson) applied to %d picks (↑%d ↓%d) across %d arms",
                    bandit_counts.get("applied", 0),
                    bandit_counts.get("lifted_up", 0),
                    bandit_counts.get("lifted_down", 0),
                    len(sampled),
                )
    except Exception as e:
        logger.warning("Bandit lift skipped: %s", e)

    # ── MLB Prop Simulator (Phase A) — Monte Carlo ─────────────────────
    # Real game-mechanics simulation: per-AB outcome distribution from
    # batter K/BB/BA/HR rates × opposing pitcher splits, distributed over
    # expected ABs. 10k MC runs → P(win) + 95% Wilson CI. Replaces the
    # broken "sim_pass" stress-test signal with empirical win probability.
    try:
        from brain.sim_runner import apply_simulations
        sim_counts = apply_simulations(picks)
        if sim_counts.get("applied", 0) > 0:
            logger.info(
                "MLB Simulator applied to %d picks (stronger=%d weaker=%d neutral=%d)",
                sim_counts.get("applied", 0),
                sim_counts.get("stronger", 0),
                sim_counts.get("weaker", 0),
                sim_counts.get("neutral", 0),
            )
    except Exception as e:
        logger.warning("MLB Simulator skipped: %s", e)

    # ── Sportsbook deep-link enrichment: attach home_team / away_team / pick
    # / fanduel_event_id / draftkings_event_id / etc. to every pick. These
    # power the "Add to Bet Slip" deep links from the parlay & detail screens
    # so users land on the correct game page in FanDuel / DraftKings instead
    # of the sportsbook homepage.
    try:
        from event_matcher import enrich_picks_with_event_ids
        enrich_picks_with_event_ids(picks)
        sample = next((p for p in picks if p.get("fanduel_event_id")), None)
        logger.info(
            "Event-ID enrichment applied to %d picks (sample: %s)",
            len(picks),
            sample.get("fanduel_event_id") if sample else "NONE",
        )
    except Exception as e:
        logger.warning("Event-ID enrichment skipped: %s", e)

    # ── Sportsbook Mapping Engine: build a sportsbook-INDEPENDENT
    # ``selection_v2`` per pick + per-book deep-link bundles (best_link /
    # best_depth). The frontend consumes ``sportsbook_mapping[<Book>].best_link``
    # so users land as close to the actual bet as we can manage without a
    # partner API key. UI is unchanged — same buttons, deeper destinations.
    try:
        from sportsbook_mapper import enrich_picks_with_mapping, SUPPORTED_BOOKS
        enrich_picks_with_mapping(picks)
        depth_counts: dict[str, int] = {}
        for p in picks:
            depths = {b: ((p.get("sportsbook_mapping") or {}).get(b) or {}).get("best_depth")
                      for b in SUPPORTED_BOOKS}
            for d in depths.values():
                depth_counts[d or "none"] = depth_counts.get(d or "none", 0) + 1
        logger.info("Sportsbook Mapping: %d picks enriched across %d books · depth=%s",
                    len(picks), len(SUPPORTED_BOOKS), depth_counts)
    except Exception as e:
        logger.warning("Sportsbook mapping enrichment skipped: %s", e)

    # ── Tennis Edge Engine v2: per-pick component scoring + NO_BET filter,
    # 99-LOCK gating, and max-3-per-day cap. Pure post-processing; no extra
    # API calls. Non-tennis picks pass through unchanged.
    try:
        from tennis_engine import apply_tennis_engine, build_tennis_insights
        before_tennis = sum(1 for p in picks if (p.get("sport") or "").lower() == "tennis")
        picks = await apply_tennis_engine(db, picks)
        after_tennis = sum(1 for p in picks if (p.get("sport") or "").lower() == "tennis")
        # Attach tennis-specific insights to surviving tennis picks so the
        # Deep Dive UI gets the surface/serve/matchup bullets.
        for p in picks:
            if (p.get("sport") or "").lower() == "tennis":
                tennis_insights = build_tennis_insights(p)
                if tennis_insights:
                    existing = p.get("key_insights") or []
                    p["key_insights"] = tennis_insights + existing
        logger.info("Tennis Edge v2: tennis picks %d → %d (filtered + capped)",
                    before_tennis, after_tennis)
    except Exception as e:
        logger.warning("Tennis Edge v2 skipped: %s", e)

    # ── Bet-Type Classification & Weighted Unit Tagging
    # Per spec: odds ≥ -300 → STRAIGHT (1.0u), ≥ -500 → REDUCED (0.5u),
    # < -500 → PARLAY (0.25u). Real betting behavior — heavy chalk gets
    # smaller stake so ROI math isn't distorted by -500+ lines.
    try:
        from bet_type import classify_bet_type, unit_weight
        for p in picks:
            odds = p.get("book_odds")
            p["bet_type"] = classify_bet_type(odds)
            p["unit_weight"] = unit_weight(odds)
    except Exception as e:
        logger.warning("Bet-type tagging skipped: %s", e)

    # ── Learning System v2: apply ROI/CLV/Calibration/Volume weights +
    # 99-Lock gates + calibration band raises to the freshly-built slate.
    try:
        from learning_system_v2 import apply_v2_to_picks
        picks = await apply_v2_to_picks(picks, db)
    except Exception as e:
        logger.warning("Learning v2 apply skipped: %s", e)

    # ── Deep Dive Mode: attach edge/confidence/risk scores, top-3 reasons,
    # and NO-BET flag for low-confidence picks. Internal only; UI unchanged.
    try:
        from deep_dive import deep_dive, NO_BET_THRESHOLD
        no_bet_count = 0
        for p in picks:
            # Tag every fresh pick with the current formula version so the
            # learning engine can isolate clean calibration samples from
            # legacy data.
            p["formula_v"] = 2
            await deep_dive(db, p)
            if p.get("no_bet"):
                no_bet_count += 1
        logger.info("Deep Dive: %d picks analysed, %d flagged NO-BET (conf < %d)",
                    len(picks), no_bet_count, NO_BET_THRESHOLD)
    except Exception as e:
        logger.warning("Deep Dive skipped: %s", e)

    # ── Brain Pipeline v1 — Prediction Memory + Candidate Ranker + hidden
    # Monte Carlo simulator + Decision Filter (PASS verdict) + Confidence
    # Calibration. All seven layers run ON TOP of existing scoring; PASS
    # picks set the existing `no_bet=True` flag so feed endpoints silently
    # drop them with zero UI change. See /app/backend/brain/ for the
    # individual modules.
    try:
        from brain import process_brain
        brain_summary = await process_brain(picks, db)
        logger.info("Brain v%s done in %sms: %s",
                    brain_summary.get("version"),
                    brain_summary.get("elapsed_ms"),
                    brain_summary.get("steps", {}).get("filter"))
    except Exception as e:
        logger.warning("Brain pipeline skipped: %s", e)

    # Deduplicate picks within this batch by `id` — UUID5 hashes can collide
    # if two markets produce identical external_ids (saw this with Anytime
    # Goal Scorer picks generated twice in the same refresh). Keep the first.
    seen_ids: set = set()
    dedup_picks = []
    for p in picks:
        pid = p.get("id")
        if pid in seen_ids:
            continue
        seen_ids.add(pid)
        dedup_picks.append(p)
    picks = dedup_picks

    # Preserve original `odds_at_pick` and `units_risked` across refreshes so
    # CLV can be measured later (closing_odds is updated by settle). The
    # latest book_odds becomes the running "closing line" snapshot.
    if seen_ids:
        existing = db.picks.find(
            {"id": {"$in": list(seen_ids)}},
            {"_id": 0, "id": 1, "odds_at_pick": 1, "units_risked": 1, "first_seen_at": 1},
        )
        prior: dict[str, dict] = {}
        async for doc in existing:
            prior[doc["id"]] = doc
        from datetime import datetime as _dt, timezone as _tz
        now_iso = _dt.now(_tz.utc).isoformat()
        for p in picks:
            pid = p.get("id")
            book = p.get("book_odds")
            prev = prior.get(pid)
            if prev and prev.get("odds_at_pick"):
                p["odds_at_pick"] = prev["odds_at_pick"]
                p["first_seen_at"] = prev.get("first_seen_at", now_iso)
            else:
                p["odds_at_pick"] = book
                p["first_seen_at"] = now_iso
            # closing_odds will be the latest book_odds we saw at refresh time
            # — re-snapshotted on settle below.
            p["closing_odds"] = book
            p["units_risked"] = (prev.get("units_risked") if prev else None) or 1.0

    # Delete previous entries for this date AND any leftover picks with the
    # same UUID5 from a prior day, then insert fresh.
    # When sport_filter is set (e.g. MLB pregame loop), scope the wipe to
    # just that sport so the other sports' rows stay intact.
    #
    # ── STICKY 95+ PINS + OUT-OF-BAND PIPELINE PROTECTION ──
    # Picks that ever crossed 95 lock_score_peak are PINNED — they survive
    # refresh wipes so a user who saw a 99-lock pick yesterday can still
    # find it on today's board (possibly with a "LINE MOVED" indicator if
    # the new generation produced a different version of the same pick).
    #
    # ADDITIONALLY (2026-07-12 permanent fix — user report: "Sweden and
    # Norway goalscorers appeared then they disappeared, please permanently
    # fix"): picks generated by out-of-band pipelines are OWNED by their
    # own refresh loops and MUST NOT be wiped by the main sports_engine
    # refresh. Otherwise, every time the main pipeline fires between the
    # 4-hour hot-scorer / SportDB / CSL cadence, the goalscorer picks
    # vanish for up to 4h until the next external-loop cycle re-inserts
    # them. Excluding these sources + `is_model_only` picks means external
    # pipelines are the *sole* authority over their own picks — the main
    # pipeline only manages what it itself generated.
    _OUT_OF_BAND_SOURCES = [
        "soccer_hot_scorers_v1",
        "csl_espn_leaderboard",
        "csl_espn_live",
        "mls_espn_leaderboard",   # 2026-07-22 — MLS top-scorer picks
        "tennis_extra",
        "tennis_extra_model",
        "tennis_real_odds",
        "mlb_hot_hitters",
        "mlb_hot_hitters_v1",
    ]
    _pin_filter = {
        "$and": [
            {"$or": [
                {"lock_score_peak": {"$exists": False}},
                {"lock_score_peak": {"$lt": 95}},
                # 2026-02 — picks tagged `no_bet=True` (e.g. by the
                # settler / contradiction resolver) MUST be included in
                # the deletion filter even when their `lock_score_peak`
                # crossed the 95 sticky-pin threshold. Otherwise a
                # once-elite pick that was later invalidated will remain
                # pinned forever and keep showing up on the board.
                {"no_bet": True},
            ]},
            # Never wipe out-of-band pipeline picks — they refresh on
            # their own cadence (hot_scorers = 4h, SportDB = 6h, etc.)
            {"source": {"$nin": _OUT_OF_BAND_SOURCES}},
            # `sportdb_scorer*` uses several suffixes (sportdb_scorer_v1,
            # sportdb_scorer_synth, etc.) — match by regex.
            {"source": {"$not": {"$regex": r"^sportdb_scorer", "$options": "i"}}},
            # Extra belt-and-suspenders: any pick flagged as model-only
            # is definitionally not from the main book-odds pipeline.
            {"is_model_only": {"$ne": True}},
        ]
    }
    # ATOMIC-SWAP DEFERRAL (2026-06-28): we used to run delete_many HERE,
    # BEFORE the enrichment passes below (Lock V2, Player Intel, Evidence
    # Governor, MLB Simulator, Sportsbook Mapping, Deep Dive, Brain,
    # Auto-Elite). With ~200 picks that pipeline takes 10-30s — and the
    # DB sits EMPTY for the entire window, so `/api/picks/today` returns
    # `{"picks":[]}` and users see "No locks on the board" every time
    # the refresh fires. Build a deferred-delete closure that we'll call
    # RIGHT BEFORE insert_many — gap shrinks from ~20s to <100ms.
    # User report (2026-06-28): "They come back when I play with tabs
    # but not staying"  ← classic symptom of catching the populated
    # window between refresh cycles.
    async def _apply_atomic_delete():
        if sport_filter:
            await db.picks.delete_many({"pick_date": date_str, "sport": sport_filter, **_pin_filter})
            await db.picks.delete_many({"id": {"$in": list(seen_ids)}, "sport": sport_filter, **_pin_filter})
        else:
            await db.picks.delete_many({"pick_date": date_str, **_pin_filter})
            await db.picks.delete_many({"id": {"$in": list(seen_ids)}, **_pin_filter})
        # ── ID-COLLISION FRESH-OVERWRITE ──
        # If the current refresh re-generates a pick whose `id` is ALSO a
        # sticky 95+ pin in DB, the existing row blocks the new insert
        # (mongo `id` unique index → duplicate key error → silently
        # skipped). The user then sees the STALE lock_score from days
        # ago — e.g. Fábio Abreu landed at lock_score=55.7 (post-evidence
        # demotion from the previous cycle) while the new cycle would have
        # set it to 95.0 via the elite-protect clamp. Fix: ALWAYS delete
        # any existing pick whose `id` is about to be re-inserted by this
        # refresh, regardless of the sticky-pin filter. The peak metadata
        # is preserved on the new pick (peak_lock_score is propagated
        # through the synth + clamp pipeline), so the "highest-ever lock"
        # invariant still holds.
        if seen_ids:
            if sport_filter:
                await db.picks.delete_many(
                    {"id": {"$in": list(seen_ids)}, "sport": sport_filter}
                )
            else:
                await db.picks.delete_many({"id": {"$in": list(seen_ids)}})

        # ── 2026-07-28 DEFECT #4 FIX: semantic-identity delete ─────────
        # ────────────────────────────────────────────────────────────
        # The pick_date-scoped + id-scoped deletes above cannot catch a
        # contradictory row whose (event, player, family, line) matches
        # an incoming pick but whose pick_date sits on a DIFFERENT
        # bucket and whose id is DIFFERENT from any current pick. That
        # was the Wheeler bug — yesterday's Under 6.5 K survived under
        # pick_date=2026-07-27 while today's Over 6.5 K landed on
        # pick_date=2026-07-28.
        #
        # Semantic identity of a prop pick = (sport, event, selection,
        # family, line). Any DB row matching this tuple against ANY
        # incoming pick — regardless of pick_date, id, or side — is
        # stale by definition (Defect #3 has already picked the correct
        # side, and same-side rows on other dates are outdated). Line
        # is REQUIRED so we never accidentally purge a legitimate
        # different-line alt for the same player.
        try:
            import re as _re_sid
            # Ordered longest-first so "Hits + Runs + RBIs" wins over "Hits".
            _MARKET_STAT_PATTERN = _re_sid.compile(
                r"(\d+\.?\d*)\s+(Hits \+ Runs \+ RBIs|Home Runs|Pitching Outs|Earned Runs|Hits Allowed|Total Bases|Runs Scored|Strikeouts|Walks|Hits|RBIs)\s*$",
                _re_sid.IGNORECASE,
            )
            _MARKET_STAT_TO_FAMILY = {
                "strikeouts": "pitcher_strikeouts",
                "hits": "batter_hits",
                "home runs": "batter_home_runs",
                "hits + runs + rbis": "batter_hits_runs_rbis",
                "total bases": "batter_total_bases",
                "rbis": "batter_rbis",
                "runs scored": "batter_runs_scored",
                "walks": "pitcher_walks",
                "pitching outs": "pitcher_outs",
                "earned runs": "pitcher_earned_runs",
                "hits allowed": "pitcher_hits_allowed",
            }
            def _semantic_id(pick_or_row: dict) -> Optional[tuple]:
                _sport = pick_or_row.get("sport")
                _event = pick_or_row.get("event")
                _selection = pick_or_row.get("selection")
                _mkt = pick_or_row.get("market") or ""
                if not (_sport and _event and _selection and _mkt):
                    return None
                _m = _MARKET_STAT_PATTERN.search(_mkt)
                if not _m:
                    return None
                _line = _m.group(1)
                _stat = _m.group(2).lower().strip()
                _family = _MARKET_STAT_TO_FAMILY.get(_stat)
                if not _family:
                    return None
                return (_sport, _event, _selection, _family, _line)

            # Build target index from incoming safe_picks.
            _semantic_targets: dict = {}   # semantic_id → set of incoming ids to preserve
            for _p in safe_picks:
                _sid = _semantic_id(_p)
                if _sid is None:
                    continue
                _semantic_targets.setdefault(_sid, set()).add(_p.get("id"))

            _semantic_deleted = 0
            for _sid, _keep_ids in _semantic_targets.items():
                _sport, _event, _selection, _family, _line = _sid
                _query = {
                    "sport": _sport,
                    "event": _event,
                    "selection": _selection,
                }
                _query.update(_pin_filter)  # never nuke sticky pins here
                _stale_ids: list = []
                async for _row in db.picks.find(
                    _query,
                    {"_id": 0, "id": 1, "market": 1, "pick_date": 1,
                     "sport": 1, "event": 1, "selection": 1},
                ):
                    _row_id = _row.get("id")
                    if not _row_id or _row_id in _keep_ids:
                        continue
                    _row_sid = _semantic_id(_row)
                    if _row_sid == _sid:
                        _stale_ids.append(_row_id)
                        logger.info(
                            "SEMANTIC_DELETE: stale (sport=%s, event=%s, "
                            "selection=%s, family=%s, line=%s) row id=%s "
                            "pick_date=%s market=%r",
                            _sport, _event, _selection, _family, _line,
                            _row_id, _row.get("pick_date"),
                            (_row.get("market") or "")[:80],
                        )
                if _stale_ids:
                    _res = await db.picks.delete_many(
                        {"id": {"$in": _stale_ids}},
                    )
                    _semantic_deleted += int(getattr(_res, "deleted_count", 0) or 0)
            if _semantic_deleted:
                logger.info(
                    "SEMANTIC_DELETE: purged %d stale contradictory rows "
                    "across %d semantic targets",
                    _semantic_deleted, len(_semantic_targets),
                )
        except Exception as _sid_err:
            logger.warning(
                "Semantic-identity delete pass skipped: %s", _sid_err,
            )
        # ── /DEFECT #4 FIX ─────────────────────────────────────────────
    # Defensive write: drop malformed pick docs (missing required fields)
    # so a single broken doc never aborts the entire batch insert. Required
    # fields: id, sport, event_time, market, book_odds.
    REQUIRED = ("id", "sport", "event_time", "market", "book_odds")
    safe_picks = []
    dropped = 0
    _mls_espn_debug = 0   # 2026-07-22 diagnostic
    for p in picks:
        if not isinstance(p, dict):
            dropped += 1
            continue
        missing = [k for k in REQUIRED if not p.get(k)]
        if missing:
            logger.warning("Dropping malformed pick (missing %s): event=%s market=%s",
                         missing, p.get("event"), p.get("market"))
            dropped += 1
            continue
        if p.get("source") == "mls_espn_leaderboard":
            _mls_espn_debug += 1
        safe_picks.append(p)
    if _mls_espn_debug:
        logger.info(
            "MLS ESPN picks reached safe_picks: %d (post malformed-drop)",
            _mls_espn_debug,
        )
    if dropped:
        logger.warning("Skipped %d malformed picks before insert", dropped)

    # ── Lock Engine V2 — SHADOW MODE. Compute v2 scores for every pick.
    # Adds counter_score / survival_score / lock_score_v2 / tier_v2 etc
    # to each pick. The production lock_score field is NEVER touched.
    # Gated by ENABLE_COUNTER_ENGINE env var.
    try:
        from lock_v2.engine import V2_ENABLED, compute_v2_shadow
        if V2_ENABLED and safe_picks:
            v2_tagged = 0
            for p in safe_picks:
                shadow = compute_v2_shadow(p)
                if shadow:
                    p.update(shadow)
                    v2_tagged += 1
            logger.info("Lock V2 shadow tagged %d / %d picks", v2_tagged, len(safe_picks))
    except Exception as _v2_err:
        logger.warning("Lock V2 shadow tagging failed (continuing): %s", _v2_err)

    # ── Player Intelligence enrichment ──
    # Resolve every player-prop pick's market into a canonical profile and
    # attach `player_intel` (archetype, team, position, volatility, usage)
    # so the frontend never has to re-resolve from raw market strings.
    try:
        from player_intel import enrich_picks_with_player_intel
        pi_count = enrich_picks_with_player_intel(safe_picks)
        if pi_count:
            logger.info("Player Intelligence enriched %d picks", pi_count)
    except Exception as _pi_err:
        logger.warning("Player Intelligence enrichment failed (continuing): %s", _pi_err)

    # ── Universal Evidence System ── (2026-06-24)
    # Run the explanation/lock governor on every pick before persistence.
    # Adds: evidence_score, lock_score_raw, evidence_breakdown.
    # Mutates: lock_score (= raw_lock × evidence_multiplier),
    #          key_insights (filtered for hype + evidence-backed).
    # Probability and edge are NEVER mutated.
    try:
        from evidence_engine import build_features_from_pick, govern_pick
        governed_count = 0
        for p in safe_picks:
            try:
                feats = build_features_from_pick(p)
                govern_pick(p, feats)
                governed_count += 1
            except Exception as _per_pick_err:
                # Per-pick failure must not abort the batch — if evidence
                # extraction blows up on one weird pick we still want the
                # others to persist. Surface the error in logs only.
                logger.debug("Evidence governor failed on %s: %s",
                             p.get("id"), _per_pick_err)
        if governed_count:
            logger.info("Evidence governor applied to %d picks", governed_count)
    except Exception as _ev_err:
        logger.warning("Evidence governor unavailable (continuing): %s", _ev_err)

    # ── Elite-protect lock-floor pass ──
    # CSL seed-tier players (Cryzan, Felipe Sousa, Fábio Abreu,
    # Leonardo, Wu Lei, Negrão, Bakambu, etc.) are tagged with
    # `lock_floor` and `elite_protect` by thesportsdb_scorer. The
    # bandit + brain + learning_v2 routinely shave 2-5 lock points
    # off these synthetics. Re-apply the floor as the very last step
    # so the user-curated elite players ALWAYS land on the board at
    # their reputation-based lock score.
    try:
        elite_clamped = 0
        for p in safe_picks:
            floor = p.get("lock_floor")
            if not floor or not p.get("elite_protect"):
                continue
            try:
                floor_f = float(floor)
            except (TypeError, ValueError):
                continue
            updated = False
            for k in ("lock_score", "lock_score_v2", "raw_lock_score", "peak_lock_score"):
                try:
                    cur = float(p.get(k) or 0)
                except (TypeError, ValueError):
                    cur = 0.0
                if cur < floor_f:
                    p[k] = floor_f
                    updated = True
            if updated:
                p["grade"] = "A" if floor_f >= 88 else ("B" if floor_f >= 80 else "C")
                p["confidence"] = p["grade"]
                elite_clamped += 1
        if elite_clamped:
            logger.info("Elite lock-floor clamp: %d synthetic CSL picks restored",
                        elite_clamped)
    except Exception as _ef_err:
        logger.warning("Elite lock-floor clamp failed: %s", _ef_err)

    # ── Universal ESPN-backed pick enrichment ── (2026-06-28)
    # Runs across ALL sports. Adds `pick_rationale` (structured "show your
    # work" data) to every player pick, validates NBA + NFL picks against
    # the `services.active_registry` (drops retired/inactive players),
    # and merges any sport-specific rationale (e.g. CSL ESPN rank).
    # User feedback driving this: "ESPN data should be in pipeline for
    # all sports" + "I want education behind goalscorer, not just random
    # picks". Picks tagged `validation_block` are dropped before persist.
    try:
        from pick_enrichment import enrich_picks_with_active_registry
        en_counts = enrich_picks_with_active_registry(safe_picks)
        # Drop inactive-player picks BEFORE persistence.
        if en_counts.get("blocked_inactive"):
            safe_picks = [p for p in safe_picks if not p.get("validation_block")]
        logger.info(
            "Pick enrichment: %d enriched, %d blocked (inactive), %d skipped (team picks)",
            en_counts.get("enriched", 0),
            en_counts.get("blocked_inactive", 0),
            en_counts.get("skipped_team_pick", 0),
        )
    except Exception as _enr_err:
        logger.warning("Pick enrichment failed (continuing): %s", _enr_err)

    # ── Validation-first architecture (2026-07-04, per user spec) ──
    # Every pick must survive the board_validator gauntlet before it
    # can be published. This is the last gate before insert_many:
    #   §1 contradiction detection (both-sides-of-same-market)
    #   §2 batter-vs-pitcher validation (same-team, non-probable)
    #   §6 board-quality floors (never publish filler picks)
    #   §3 immutable snapshot (locked payload for graders)
    #   §4 rollover tag (permanent on_rollover_at stamp)
    try:
        from board_validator import validate_and_finalize
        pre_count = len(safe_picks)
        safe_picks, val_report = validate_and_finalize(safe_picks)
        logger.info(
            "Board validator: %d → %d picks (contra=%d, bp=%d, quality=%d, rollover=%d)",
            pre_count, len(safe_picks),
            val_report.get("contradictions", {}).get("dropped", 0),
            val_report.get("batter_pitcher", {}).get("dropped", 0),
            val_report.get("board_quality", {}).get("dropped", 0),
            val_report.get("rollover", {}).get("tagged", 0),
        )
        # Log detailed reasons at INFO when anything was dropped so we
        # can debug board-quality regressions from a single log line.
        if any(val_report[k].get("dropped", 0) for k in
               ("contradictions", "batter_pitcher", "board_quality")):
            logger.info("Board validator reasons: %s", val_report)
    except Exception as _bv_err:
        logger.warning("Board validator failed (continuing): %s", _bv_err)

    # ── Monte Carlo simulation engine (2026-07-04 spec, Session 1) ──
    # For every survivor, run scenario-based + multi-model sims and
    # attach `sim_result` (prob / edge / agreement / breakdown). Feeds
    # into the ranker and gives the UI a transparent "why this pick"
    # payload.
    try:
        from sim_engine import simulate_board
        simulate_board(safe_picks, n_simulations=500)
        # Track how many picks got sim results for observability
        sim_ok = sum(1 for p in safe_picks if p.get("sim_result"))
        logger.info("Sim engine: %d/%d picks simulated", sim_ok, len(safe_picks))
    except Exception as _sim_err:
        logger.warning("Sim engine failed (continuing): %s", _sim_err)

    # ── Chalk Kill Switch (2026-07-21) ────────────────────────────────
    # User mandate: "auto-fade any pick priced worse than -250 unless
    # model edge >= 8pp with >=3 aligned data signals". Trap picks stay
    # visible on the board (per user: "I still want the 200 picks for
    # options") but get their lock_score / signal_score capped and a
    # visible warning attached, so users can SEE them without the app
    # RECOMMENDING them as a Lock. This must run LAST — after all
    # lock_score writers (learning_v2 / brain sim_runner) so the cap
    # is authoritative and can't be overwritten downstream.
    # See services/chalk_trap.py for gate details.
    try:
        from services.chalk_trap import apply_chalk_kill_switch
        _ck_stats = apply_chalk_kill_switch(safe_picks)
        logger.info(
            "Chalk Kill Switch: trapped=%d spared_edge=%d spared_dd=%d "
            "spared_alt=%d already_low=%d (of %d chalk / %d total)",
            _ck_stats["trapped"], _ck_stats["spared_by_edge"],
            _ck_stats["spared_by_dd"], _ck_stats["spared_alt"],
            _ck_stats["already_low"], _ck_stats["chalk_seen"],
            _ck_stats["total"],
        )
    except Exception as _ck_err:
        logger.warning("Chalk Kill Switch skipped: %s", _ck_err)

    # ── Longshot Trap (2026-07-21) ────────────────────────────────────
    # Mirror of the Chalk Kill Switch for the OPPOSITE bleed: Soccer
    # 92+ Strong-Lock picks priced at plus-money odds. ROI analysis of
    # 5,309 settled picks: Soccer Strong Lock (92-96) bled -21% ROI
    # (-48u), concentrated in Goal Scorer / SoA markets and +200-and-
    # up longshots (-30% to -74% ROI). Chalk 92+ (<-150 odds) stays
    # profitable (+1.6% to +11%). Trap only touches the bleeding tier.
    # Elite anchor players (Kane / Haaland / Mbappé) and extreme +EV
    # picks (edge >= 12pp + 3 DD signals) escape unchanged.
    try:
        from services.longshot_trap import apply_longshot_trap
        _ls_stats = apply_longshot_trap(safe_picks)
        logger.info(
            "Longshot Trap: trapped=%d spared_elite=%d spared_edge_dd=%d "
            "(of %d seen / %d total)",
            _ls_stats["trapped"], _ls_stats["spared_elite"],
            _ls_stats["spared_edge_dd"], _ls_stats["seen"],
            _ls_stats["total"],
        )
    except Exception as _ls_err:
        logger.warning("Longshot Trap skipped: %s", _ls_err)

    # ── Board Visibility Gate (2026-07-21) ────────────────────────────
    # User mandate: "I don't want the app to grade picks that don't make
    # it to board". Tags every pick with `off_board=True` when it would
    # be hidden from the user (below lock 85, no_bet, validation_block,
    # low-tier grade, or model_only). Settlement modules filter with
    # `off_board: {"$ne": True}` so status stays `pending` on hidden
    # picks — analytics / ROI / bandit / learning ignore them.
    # Runs LAST so it captures every state change made above.
    try:
        from services.board_visibility import tag_board_visibility
        _bv_stats = tag_board_visibility(safe_picks)
        logger.info(
            "Board Visibility: on_board=%d off_board=%d (of %d) reasons=%s",
            _bv_stats["on_board"], _bv_stats["off_board"],
            _bv_stats["total"], _bv_stats.get("reasons") or {},
        )
    except Exception as _bv_err:
        logger.warning("Board Visibility tagging skipped: %s", _bv_err)

    # ── Fusion Enrichment (2026-07-29) ────────────────────────────────
    # Attach Prediction Fusion Engine output (ML + Similar Matchup +
    # Matchup Intelligence + Simulator consensus) to every ON-BOARD
    # player-prop pick BEFORE insert. Off-board picks are skipped to
    # save cycles — they're hidden from the user anyway.
    #
    # We persist to `fusion_predictions` (with pick_id linkage) so the
    # post-settlement grading loop can back-solve `correct` /
    # `winning_component` and feed the adaptive-learning stack.
    #
    # Lazy single-pick enrichment (GET /api/picks/{id}) still works and
    # returns the SAME payload — but now the board also carries it.
    #
    # Bounded concurrency + wrapped exceptions: a fusion engine failure
    # can NEVER take down the pick refresh.
    try:
        from services.pick_fusion_decorator import enrich_picks_bulk
        on_board_picks = [p for p in safe_picks
                           if not p.get("off_board")
                           and not p.get("no_bet")]
        if on_board_picks:
            import time as _t
            _fu_t0 = _t.time()
            await enrich_picks_bulk(
                db, on_board_picks,
                persist=True,
                include_simulator=False,   # simulator excluded from board
                concurrency=8,
            )
            _fu_supported = sum(
                1 for p in on_board_picks
                if isinstance(p.get("fusion"), dict)
                and p["fusion"].get("supported")
            )
            _fu_prob_available = sum(
                1 for p in on_board_picks
                if isinstance(p.get("fusion"), dict)
                and p["fusion"].get("supported")
                and (p["fusion"].get("final_probability") or 0) > 0
            )
            logger.info(
                "Fusion Enrichment: %d/%d on-board picks supported "
                "(%d with non-zero probability) in %.1fs",
                _fu_supported, len(on_board_picks),
                _fu_prob_available, _t.time() - _fu_t0,
            )
    except Exception as _fu_err:
        logger.warning("Fusion Enrichment skipped: %s", _fu_err)

    if safe_picks:
        # ATOMIC-SWAP: do the wipe NOW, immediately before the insert.
        # The enrichment passes above ran on in-memory `safe_picks` —
        # the DB still has the PREVIOUS slate visible to clients this
        # entire time, so users never see "No locks on the board" mid-
        # refresh. Gap between delete + insert is now <100ms instead of
        # the old 10-30s.
        await _apply_atomic_delete()
        # ordered=False already lets pymongo continue past duplicate-key
        # rows, but it STILL raises BulkWriteError at the end, aborting
        # the caller. Catch + count + log so picks that DID land still
        # commit cleanly. Most "duplicates" are picks that were already
        # written by a parallel sport refresh (MLB pregame + full refresh
        # racing), so the data is identical and the error is benign.
        try:
            await db.picks.insert_many(safe_picks, ordered=False)
        except Exception as bulk_err:
            # pymongo BulkWriteError exposes per-doc errors in .details
            details = getattr(bulk_err, "details", None) or {}
            n_inserted = int(details.get("nInserted", 0) or 0)
            write_errors = details.get("writeErrors") or []
            dup_errors = [e for e in write_errors if e.get("code") == 11000]
            other_errors = [e for e in write_errors if e.get("code") != 11000]
            if other_errors:
                # Non-duplicate write errors are real bugs — re-raise.
                logger.error("Unexpected pick insert errors: %s", other_errors[:3])
                raise
            logger.warning(
                "Pick insert: %d inserted, %d duplicates skipped (already in DB).",
                n_inserted, len(dup_errors),
            )
    logger.info("Stored %d picks for %s", len(safe_picks), date_str)
    # 2026-07-22 MLS ESPN post-insert diagnostic. Should reveal whether
    # picks were persisted to Mongo or silently dropped.
    try:
        _mls_espn_in_db = await db.picks.count_documents({
            "id": {"$regex": "^mls-espn"},
            "pick_date": date_str,
        })
        _mls_espn_safe = sum(
            1 for p in safe_picks
            if p.get("source") == "mls_espn_leaderboard"
        )
        logger.info(
            "MLS ESPN diagnostic — safe_picks=%d, in DB for %s=%d",
            _mls_espn_safe, date_str, _mls_espn_in_db,
        )
    except Exception as _e:
        logger.warning("MLS ESPN diagnostic failed: %s", _e)
    # ── Cross-run contradiction reconciliation (2026-07-22) ───────────
    # User bug: "Gerrit Cole Over 6.5 K AND Under 6.5 K both showing as
    # 99 Elite Lock". Root cause: sticky-pin logic (lock_score_peak ≥ 95)
    # protects a pick from `_apply_atomic_delete`. When a NEW refresh
    # generates the OPPOSITE side of a pinned pick (e.g. Under 6.5 K
    # after an earlier Over 6.5 K was pinned), both survive and the
    # in-run dedupe never fires because they're in different batches.
    # Fix: after every insert, sweep the just-inserted player-prop picks
    # and any contradicting DB picks for the SAME event + player +
    # market family — keep the higher-edge one and remove the loser
    # (regardless of sticky-pin, because contradictions are always
    # user-facing garbage).
    try:
        await _reconcile_player_prop_contradictions(safe_picks, date_str)
    except Exception as e:
        logger.warning("Prop contradiction reconciliation skipped: %s", e)
    # ── CSL Guaranteed Elite Injection ──
    # The standard refresh pipeline (learning + bandit + brain + evidence
    # governor + dedupe trims) systematically drops CSL synth picks even
    # when they're tagged elite. We re-run the synthesizer for every CSL
    # event today AFTER the main pipeline lands and force-inject any
    # missing elites into the DB so the user always sees Cryzan, Felipe
    # Sousa, Fábio Abreu, Leonardo, Wu Lei, Júnior Negrão, Cédric
    # Bakambu, Wesley Moraes, Oscar Taty Maritu, Zhang Yuning, Marcão,
    # and any other hot-form scorer in `csl_form_seed.py`.
    try:
        await _ensure_csl_elite_picks(date_str)
    except Exception as e:
        logger.warning("CSL elite-inject failed (non-fatal): %s", e)
    # ── GoalScorer Engine v2 shadow capture ──
    # Best-effort: log a v2 prediction for every soccer goalscorer pick
    # that just landed so calibration data starts accumulating. NEVER
    # raises — strictly shadow.
    try:
        await _shadow_capture_gs_v2(safe_picks)
    except Exception as e:
        logger.debug("gs_v2 shadow capture failed (non-fatal): %s", e)
    # ── Invalidate the slate-wide Signal Rank cache (2026-07-17) ────
    # We just replaced today's picks — any percentile ranks persisted
    # from the previous slate are now stale. Drop the TTL cache so the
    # next /picks/today call rebuilds the ranks against the fresh
    # slate. Safe fallback if the module isn't importable.
    try:
        from services.signal_engine import invalidate_signal_rank
        invalidate_signal_rank(date_str)
    except Exception as _iv_err:
        logger.debug("signal_rank invalidate skipped: %s", _iv_err)
    return len(safe_picks)


def _prop_family_key(market: str) -> str:
    """Categorise a player-prop market label into a coarse family.

    Returned families group Over/Under sides of the SAME stat together so
    the contradiction reconciler can identify "same player, opposite
    side" pairs even across refresh runs. Returns "" when the market
    isn't a supported player-prop family.
    """
    m = (market or "").lower()
    # Order matters — check compound families before their sub-strings.
    if "hits + runs + rbis" in m or "hits + runs" in m: return "MLB_HRR"
    if "hits allowed" in m:              return "MLB_HALLOWED"
    if "home run" in m:                  return "MLB_HR"
    if "total bases" in m:               return "MLB_TB"
    if "rbis" in m:                      return "MLB_RBI"
    if "outs recorded" in m:             return "MLB_OUTS"
    if "strikeout" in m:                 return "MLB_K"
    if "hits" in m:                      return "MLB_HITS"
    if "passing yards" in m or "pass yds" in m:  return "NFL_PASS_YDS"
    if "rushing yards" in m or "rush yds" in m:  return "NFL_RUSH_YDS"
    if "receiving yards" in m or "reception yds" in m: return "NFL_REC_YDS"
    if "receptions" in m:                return "NFL_REC"
    if "pass tds" in m or "passing tds" in m: return "NFL_PASS_TDS"
    if "rush tds" in m or "rushing tds" in m: return "NFL_RUSH_TDS"
    if "points" in m:                    return "NBA_PTS"
    if "rebounds" in m:                  return "NBA_REB"
    if "assists" in m:                   return "NBA_AST"
    return ""


# ── 2026-07-28 DEFECT #5 — no_bet schema safety helpers ───────────────
# ──────────────────────────────────────────────────────────────────
# All contradiction "loser" writes MUST go through `_atomic_mark_no_bet`
# so the invariant holds:  `no_bet_reason set  ⇒  no_bet == True`
# AND  `status == "blocked"` (so the row disappears from any endpoint
# that filters by status ∈ {pending, open, None}).
#
# `_enforce_no_bet_schema_invariant` runs at app startup to sweep any
# pre-existing rows where a bad code path or crash left the fields
# inconsistent, and (best-effort) installs a MongoDB $jsonSchema
# validator that rejects future inconsistent writes at the DB layer.

async def _atomic_mark_no_bet(
    query: dict,
    reason: str,
    extra: dict | None = None,
) -> int:
    """Atomically flag every doc matching `query` as no_bet.

    Always writes:
      • no_bet=True
      • no_bet_reason=<reason>
      • status="blocked"

    …in a SINGLE `$set`, so the trio is either fully present or
    entirely absent — never partially-set. Callers must NEVER write
    `no_bet_reason` directly; funnel through this helper.

    Returns the number of modified docs.
    """
    payload = {
        "no_bet": True,
        "no_bet_reason": str(reason or "unspecified"),
        "status": "blocked",
    }
    if extra:
        payload.update(extra)
    res = await db.picks.update_many(query, {"$set": payload})
    return int(getattr(res, "modified_count", 0) or 0)


async def _enforce_no_bet_schema_invariant() -> dict:
    """One-shot startup sweep + best-effort DB validator install.

    Contract enforced:
      A doc where `no_bet_reason` is truthy MUST also have
      `no_bet == True` and `status == "blocked"`.

    Fixes any legacy or crash-corrupted rows in place and (if the
    Mongo deployment supports it) installs a `$jsonSchema` validator
    that rejects future violations. Both steps are wrapped so a
    validator error never blocks app startup — the app-level helper
    remains the primary line of defence.
    """
    stats = {"fixed": 0, "validator_installed": False, "errors": []}
    try:
        # Legacy sweep — any doc where `no_bet_reason` is non-empty
        # but `no_bet` is not True. Re-run the helper to atomically
        # set the trio.
        legacy_query = {
            "no_bet_reason": {"$exists": True, "$nin": [None, "", 0]},
            "no_bet": {"$ne": True},
        }
        matched = await db.picks.count_documents(legacy_query)
        if matched:
            fixed = await _atomic_mark_no_bet(
                legacy_query,
                "legacy inconsistency swept by _enforce_no_bet_schema_invariant",
            )
            stats["fixed"] = fixed
            logger.info(
                "no_bet schema invariant sweep: %d rows had "
                "no_bet_reason set but no_bet != True — fixed",
                fixed,
            )
    except Exception as _sweep_err:
        stats["errors"].append(f"sweep_failed: {_sweep_err}")
        logger.warning("no_bet legacy sweep failed: %s", _sweep_err)

    # Best-effort DB validator (won't fail startup if unsupported).
    try:
        validator = {
            "$jsonSchema": {
                "bsonType": "object",
                # NB: 'no_bet_reason' present + non-empty ⇒ no_bet MUST be True.
                # Documents WITHOUT `no_bet_reason` are unconstrained.
                # Cast to string comparison via $expr for BSON portability.
            },
            "$expr": {
                "$or": [
                    {"$eq": [{"$type": "$no_bet_reason"}, "missing"]},
                    {"$in": ["$no_bet_reason", [None, "", 0]]},
                    {"$eq": ["$no_bet", True]},
                ]
            },
        }
        await db.command({
            "collMod": "picks",
            "validator": validator,
            "validationLevel": "moderate",   # only new/updated docs
            "validationAction": "warn",       # log but don't block writes
        })
        stats["validator_installed"] = True
        logger.info(
            "no_bet schema invariant: MongoDB validator installed on "
            "picks collection (level=moderate, action=warn)",
        )
    except Exception as _val_err:
        stats["errors"].append(f"validator_skipped: {_val_err}")
        logger.info(
            "no_bet schema invariant: DB validator not installed "
            "(app-level helper still enforces): %s",
            _val_err,
        )
    return stats


async def _reconcile_player_prop_contradictions(safe_picks: list, date_str: str) -> None:
    """After insert, remove any Over/Under contradictions for the SAME
    (event, player, market_family) — keep only the higher-edge side.

    Scope: only player-prop markets where the pick has a `selection`
    that names a specific player. Team totals / spreads / totals are
    left alone (they're handled by other dedup passes upstream).
    """
    if not safe_picks:
        return
    # Build a set of (event, player, family) touched by this insert so we
    # only re-query rows that could have new contradictions.
    touched: set[tuple[str, str, str]] = set()
    for p in safe_picks:
        market = p.get("market") or ""
        sel = (p.get("selection") or "").strip()
        event = p.get("event") or ""
        family = _prop_family_key(market)
        if not (event and sel and family):
            continue
        # Skip aggregate-selection labels ("Yes", "Over", "Under" without
        # a player) — those aren't player-props.
        if sel.lower() in ("yes", "no", "over", "under"):
            continue
        # Must have an Over or Under indicator to qualify as a two-sided
        # prop where contradiction is possible.
        m_l = market.lower()
        if " over " not in m_l and " under " not in m_l and "over " not in m_l[:5] and "under " not in m_l[:6]:
            continue
        touched.add((event, sel, family))
    if not touched:
        return
    removed_total = 0
    # Consider picks from the last 72h so cross-refresh contradictions for
    # the same event are caught even when pick_date differs (a late-night
    # refresh may bucket a pick under tomorrow's pick_date while the
    # earlier refresh bucketed it under today's — same event/player).
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    cutoff_iso = (_dt.now(_tz.utc) - _td(hours=72)).isoformat()

    # ── Line-value extractor (2026-02) ───────────────────────────────
    # Over 7.5 vs Under 7.5 IS a contradiction (strict opposites).
    # Over 7.5 vs Under 8.5 IS NOT (both win at K=8 — user report:
    # "His line is 7.5 not 8.5 so the over should of stayed").
    # Grouping must include the numeric line so we only pair sides of
    # the SAME line.
    import re as _re
    _line_re = _re.compile(r"(?i)(?:over|under)\s+(-?\d+(?:\.\d+)?)")

    def _extract_line(market: str) -> str:
        if not market:
            return ""
        m = _line_re.search(market)
        return m.group(1) if m else ""

    for event, player, family in touched:
        rows = await db.picks.find(
            {"event": event, "selection": player,
             "no_bet": {"$ne": True},
             "created_at": {"$gte": cutoff_iso}},
            {"_id": 0, "id": 1, "market": 1, "edge_percent": 1,
             "lock_score": 1, "created_at": 1, "pick_date": 1,
             # 2026-07-28 — pull the K-math signals so we can consult
             # the shared resolver for MLB_K family contradictions.
             "k_math_gate": 1, "k_math_expected_k": 1,
             # Fields required for cross-pick_date "update-in-place"
             # so the loser row can be transformed into the winner
             # instead of leaving a stale row sitting on the earlier
             # pick_date tagged `no_bet=True`.
             "selection": 1, "book_odds": 1, "book": 1, "side": 1,
             "key_insights": 1, "k_prop_data": 1, "sport": 1,
             "event": 1, "grade": 1, "confidence": 1,
             "probability": 1},
        ).to_list(length=200)
        # Group by (family, line) with side. Same family + same line
        # is required for a genuine contradiction — different lines
        # can both win (e.g. Over 7.5 K's AND Under 8.5 K's both hit
        # if the pitcher records exactly 8 strikeouts).
        by_group: dict[str, dict[str, list[dict]]] = {}
        for r in rows:
            m = r.get("market") or ""
            if _prop_family_key(m) != family:
                continue
            line = _extract_line(m)
            if not line:
                # No numeric line detected — skip; can't reason about
                # contradictions when the line is unknown.
                continue
            ml = m.lower()
            if " over " in ml or ml.startswith("over "):
                by_group.setdefault(line, {"over": [], "under": []})["over"].append(r)
            elif " under " in ml or ml.startswith("under "):
                by_group.setdefault(line, {"over": [], "under": []})["under"].append(r)
        for line, by_side in by_group.items():
            if not (by_side["over"] and by_side["under"]):
                continue
            # ── Winner selection ───────────────────────────────────────
            # For MLB_K family, consult the shared K-math resolver first
            # (identical logic to the in-memory K conflict resolver in
            # sports_engine.py). For all other families, or when the
            # K-math signal is missing/indeterminate, fall back to
            # (edge_percent, lock_score).
            def _rank(r: dict) -> tuple:
                return (float(r.get("edge_percent") or 0),
                        float(r.get("lock_score") or 0))
            all_rows = by_side["over"] + by_side["under"]
            winner = None
            if family == "MLB_K":
                try:
                    from services.k_conflict_resolver import resolve_k_family_winner
                    best_over = max(by_side["over"], key=_rank)
                    best_under = max(by_side["under"], key=_rank)
                    try:
                        line_f = float(line)
                    except ValueError:
                        line_f = None
                    winning_side, reason = resolve_k_family_winner(
                        best_over, best_under, line_f,
                    )
                    if winning_side == "over":
                        winner = best_over
                    elif winning_side == "under":
                        winner = best_under
                    if winner is not None:
                        logger.info(
                            "Reconciler K-math winner: %s %s %s @ line=%s (%s)",
                            winning_side, family, player, line, reason,
                        )
                except Exception as _kmath_err:
                    logger.debug("K-math resolver skipped (%s): %s",
                                 player, _kmath_err)
            if winner is None:
                winner = max(all_rows, key=_rank)
            winner_side = "over" if winner in by_side["over"] else "under"
            loser_side = "under" if winner_side == "over" else "over"
            loser_rows = by_side[loser_side]
            if not loser_rows:
                continue

            # ── Cross-pick_date "update in place" (2026-07-28) ─────────
            # If a corrected pick landed as a NEW row on a LATER
            # pick_date while the WRONG-side row still exists on an
            # EARLIER pick_date, update the earlier (loser) row in
            # place with the winner's payload and delete the redundant
            # new row. This prevents the DB from accumulating
            # `no_bet=True` stragglers under older pick_dates every
            # time the K resolver flips a side.
            winner_id = winner.get("id")
            winner_pd = winner.get("pick_date") or ""
            same_side_losers = [
                r for r in loser_rows if r.get("id") and r.get("id") != winner_id
            ]
            older_losers = [
                r for r in same_side_losers
                if (r.get("pick_date") or "") < winner_pd
            ]

            if older_losers and winner_pd:
                keeper = min(older_losers, key=lambda r: r.get("pick_date") or "")
                # Copy the winner's payload into the keeper row. We
                # intentionally do NOT copy `id`, `created_at`, or
                # `event` — the audit trail preserves the original
                # slot. `pick_date` MUST be updated so the corrected
                # pick shows up on today's board.
                update_payload = {}
                for k in ("market", "selection", "side", "book_odds",
                          "book", "edge_percent", "lock_score",
                          "key_insights", "k_math_gate",
                          "k_math_expected_k", "k_prop_data",
                          "pick_date", "grade", "confidence",
                          "probability"):
                    v = winner.get(k)
                    if v is not None:
                        update_payload[k] = v
                update_payload["corrected_from_side"] = loser_side
                update_payload["corrected_at"] = _dt.now(_tz.utc).isoformat()
                update_payload["corrected_by"] = "reconciler_k_math"
                # Ensure the keeper is NOT flagged no_bet (it's the
                # winner now). Also clear any prior no_bet_reason.
                update_payload["no_bet"] = False
                update_payload["no_bet_reason"] = ""
                try:
                    await db.picks.update_one(
                        {"id": keeper["id"]},
                        {"$set": update_payload},
                    )
                    # Delete the newer winner row (redundant now that
                    # the keeper carries the winner's payload).
                    await db.picks.delete_one({"id": winner_id})
                    logger.info(
                        "Reconciler in-place update: keeper=%s (was %s, "
                        "pick_date %s → %s), deleted duplicate winner=%s",
                        keeper["id"], loser_side,
                        keeper.get("pick_date"), winner_pd, winner_id,
                    )
                    removed_total += 1
                except Exception as _upd_err:
                    logger.warning(
                        "In-place reconcile failed (%s → %s): %s",
                        keeper.get("id"), winner_id, _upd_err,
                    )

                # Any remaining loser dupes (not the keeper) → atomic
                # no_bet write. Both fields written in a single $set so
                # `no_bet_reason` can never persist without `no_bet=True`.
                remaining_ids = [
                    r["id"] for r in same_side_losers
                    if r["id"] != keeper["id"]
                ]
                if remaining_ids:
                    modified = await _atomic_mark_no_bet(
                        {"id": {"$in": remaining_ids}},
                        (
                            f"contradicts {winner_side} {family} "
                            f"{line} for {player} (corrected)"
                        ),
                    )
                    removed_total += modified
            else:
                # Standard same-pick_date contradiction path — atomic
                # no_bet write via helper. Helper guarantees no_bet=True,
                # no_bet_reason=..., status="blocked" all in a single
                # $set so the trio can never desync.
                loser_ids = [r["id"] for r in loser_rows if r.get("id")]
                if not loser_ids:
                    continue
                modified = await _atomic_mark_no_bet(
                    {"id": {"$in": loser_ids}},
                    (
                        f"contradicts {winner_side} {family} "
                        f"{line} for {player}"
                    ),
                )
                removed_total += modified
    if removed_total:
        logger.info(
            "Prop contradiction reconciliation: neutralised %d contradicting picks across %d groups",
            removed_total, len(touched),
        )




async def _ensure_csl_elite_picks(date_str: str) -> None:
    """Force-inject any missing CSL elite goalscorer picks for today.

    Runs AFTER the main refresh pipeline. For every CSL event today:
      1. Pull the existing goalscorer player_names from DB.
      2. Re-synthesize via `thesportsdb_scorer.compute_anytime_scorer_picks`.
      3. Any seed-tagged elite player (`elite_protect=True` AND `lock_floor`)
         that's MISSING from DB → insert directly with the synthesizer's
         lock_score (already floor-clamped at the elite tier).

    This is a "guarantee" pass — the upstream filters won't see these
    picks again so they can't trim/demote them.
    """
    import thesportsdb_scorer as _tsdb
    # Pull today's CSL events (from any picks already in DB that have CSL league)
    # De-dupe by NORMALIZED event name — the same match may appear in DB
    # under multiple event_id values (real Odds API uuid, the event name
    # itself, and a missing/dash event_id for team-totals picks).
    events_seen: dict[str, dict] = {}
    cur = db.picks.find(
        {"pick_date": date_str, "league": "China Super League"},
        {"_id": 0, "event": 1, "event_id": 1, "event_time": 1, "home_team": 1, "away_team": 1},
    )
    async for p in cur:
        ev_name = p.get("event") or ""
        if not ev_name:
            continue
        key = ev_name.strip().lower()
        if key in events_seen:
            # Promote real event_id over "-" or the event-name string if we see one
            existing = events_seen[key]
            eid = p.get("event_id")
            if eid and eid != ev_name and (not existing.get("event_id") or existing["event_id"] in ("-", ev_name)):
                existing["event_id"] = eid
            continue
        ht = p.get("home_team") or ""
        at = p.get("away_team") or ""
        # Parse "Away @ Home" if home/away missing
        if (not ht or not at) and " @ " in ev_name:
            at, ht = ev_name.split(" @ ", 1)
        if not ht or not at:
            continue
        events_seen[key] = {
            "event_id": p.get("event_id") or ev_name,
            "event": ev_name,
            "home_team": ht, "away_team": at,
            "event_time": p.get("event_time"),
        }

    if not events_seen:
        return

    injected = 0
    for ev in events_seen.values():
        try:
            tsdb_picks = await _tsdb.compute_anytime_scorer_picks(
                db,
                home_team=ev["home_team"], away_team=ev["away_team"],
                event_id=ev["event_id"], kickoff_iso=ev["event_time"] or "",
                league="China Super League", sport_key="soccer_china_superleague",
                max_per_side=5,
            )
        except Exception as e:
            logger.warning("CSL elite re-synth failed for %s: %s", ev["event"], e)
            continue
        # Only elite_protect picks survive this guarantee pass
        elites = [p for p in tsdb_picks if p.get("elite_protect")]
        if not elites:
            continue
        # Filter out elites already in DB (by player_name + event)
        existing_names: set[str] = set()
        cur2 = db.picks.find(
            {"pick_date": date_str, "event_id": ev["event_id"],
             "market": {"$regex": "Anytime Goal Scorer"}},
            {"_id": 0, "player_name": 1},
        )
        async for d in cur2:
            nm = (d.get("player_name") or "").lower()
            if nm:
                existing_names.add(nm)
        missing = [p for p in elites if (p.get("player_name") or "").lower() not in existing_names]
        if not missing:
            continue
        # Stamp pick_date + insert
        for p in missing:
            p["pick_date"] = date_str
            p["force_injected"] = True   # diagnostic — these bypass the pipeline
        try:
            await db.picks.insert_many(missing, ordered=False)
            injected += len(missing)
        except Exception as bulk_err:
            details = getattr(bulk_err, "details", None) or {}
            n_inserted = int(details.get("nInserted", 0) or 0)
            injected += n_inserted
    if injected:
        logger.info("CSL elite-inject: %d picks force-injected post-pipeline", injected)


async def _shadow_capture_gs_v2(picks: list[dict]) -> None:
    """Run the v2 engine on every soccer goalscorer pick and store the
    prediction. Pure shadow mode — has no effect on the live board.

    Hooked in by user request 2026-06-24 ("hook v2's store_prediction
    into the soccer prop generator so calibration data starts
    accumulating").
    """
    from goal_scorer_engine_v2 import (
        PlayerFeatures, compute_probabilities, store_prediction,
        get_calibration_factor,
    )

    gs_markets = ("anytime goal scorer", "first goal scorer",
                  "last goal scorer", "to score or assist")
    n_stored = 0
    for p in picks or []:
        if p.get("sport") != "Soccer":
            continue
        market_l = (p.get("market") or "").lower()
        if not any(kw in market_l for kw in gs_markets):
            continue
        try:
            # Pull form row (xG / xA / minutes / position / form_score).
            player = (p.get("selection") or "").strip()
            if not player:
                continue
            form = await db.soccer_player_form.find_one(
                {"name_canonical": player.lower()}
            ) or {}
            event = p.get("event") or ""
            # Parse "Away @ Home".
            away_team = home_team = ""
            if " @ " in event:
                away_team, home_team = [x.strip() for x in event.split(" @ ", 1)]
            # Heuristic: if player_team metadata isn't set, fall back to
            # the form-row team and infer opponent from the event string.
            player_team = (
                p.get("player_team")
                or form.get("team")
                or home_team
            )
            opponent = away_team if player_team == home_team else home_team

            features = PlayerFeatures(
                player=player,
                team=player_team or "",
                opponent=opponent or "",
                league=p.get("league") or "",
                xG=float(form.get("xg") or 0.0),
                xA=float(form.get("xa") or 0.0),
                shot_volume=float(form.get("shots_per_90") or 0.0),
                shot_quality=(
                    float(form.get("xg_per_90") or 0.0)
                    / max(0.01, float(form.get("shots_per_90") or 0.01))
                ),
                minutes_played=int(form.get("minutes") or 0),
                games_played=int(form.get("games") or 0),
                starts=int(form.get("games") or 0),
                position=str(form.get("position") or "FW"),
                # Sensible defaults when full feature pipeline isn't wired
                # yet — pick-generator only fires for players the book
                # lists, so "starting_xi" is the right prior for them.
                lineup_confidence="starting_xi",
                recent_form=float(form.get("form_score") or 50) / 100.0,
                minutes_projection=80,
            )
            cal = await get_calibration_factor(
                db,
                league=features.league or "GLOBAL",
                market="p_anytime",
            )
            outputs = compute_probabilities(features, calibration_mult=cal)

            # Stash book price for residual report.
            book_market_key = (
                "anytime" if "anytime" in market_l else
                "first"   if "first goal scorer" in market_l else
                "last"    if "last goal scorer" in market_l else
                "score_or_assist" if "to score or assist" in market_l else
                "anytime"
            )
            await store_prediction(
                db,
                fixture_id=p.get("external_id") or p.get("id"),
                event=event,
                player=player,
                team=player_team or "",
                opponent=opponent or "",
                league=features.league or "",
                outputs=outputs,
                book_prices={book_market_key: p.get("book_odds")},
            )
            n_stored += 1
        except Exception as inner:
            # Per-pick failure must never break the batch.
            logger.debug("gs_v2 shadow capture skipped %s: %s",
                         p.get("id"), inner)
            continue
    if n_stored:
        logger.info("gs_v2 shadow capture: %d soccer goalscorer predictions stored",
                    n_stored)


async def _ensure_today_picks() -> None:
    """Seed picks for the current UTC day if the slate is empty or thin.

    Edge case fixed 2026-06-23: at UTC midnight rollover, the Soccer
    24h-backfill loop and other secondary pipelines (tennis_extra
    settler, etc.) often pre-seed a handful of next-day picks BEFORE
    midnight. After rollover, those few picks count as "today's slate"
    and the old `count == 0` gate falsely thought we were already
    seeded. Users saw a near-empty feed for up to an hour until the
    next hourly tick. Threshold of 20 picks is well below any healthy
    day (typically 100-500 picks across sports) so it acts as a
    fast "empty enough → refresh" trigger without false positives.

    CRITICAL FIX 2026-07-15 — non-blocking refresh (deployment):
    Previously this awaited `_refresh_picks(today)` inline, which
    triggers a full sports scan (Odds API + pipeline + validator +
    sim engine) that takes 60-120s on production. Since it runs on
    every `/api/picks/today` request, an empty slate produced a
    thundering-herd stampede of refreshes, each one hitting the
    Cloudflare 100s gateway timeout and returning 504 to the client.
    User report 2026-07-15: production /picks/today returns 504 after
    every deploy while preview works fine (preview DB was already
    seeded).

    Fix: fire the refresh as a background task and RETURN IMMEDIATELY.
    The request will serve whatever picks exist (possibly empty this
    first tick), but the background refresh populates the DB within
    ~60s and the next request lands a full slate. A module-level
    guard `_refresh_in_flight` prevents overlapping refreshes when
    multiple clients hit an empty slate at the same time.
    """
    today = _today_str()
    count = await db.picks.count_documents({"pick_date": today})
    if count >= 20:
        return
    logger.info(
        "ensure_today_picks: only %d picks for %s — scheduling background refresh",
        count, today,
    )
    global _refresh_in_flight
    if _refresh_in_flight:
        return  # a refresh is already running for the current day
    _refresh_in_flight = True

    async def _background_refresh():
        global _refresh_in_flight
        try:
            await _refresh_picks(today)
        except Exception as e:
            logger.warning("Background refresh failed: %s", e)
        finally:
            _refresh_in_flight = False

    # Fire-and-forget — don't await, don't block the response. The
    # picks_today handler will return whatever's in the DB right now
    # (possibly empty this first cold-start tick) and the next call
    # will land a full slate ~60s later.
    asyncio.create_task(_background_refresh())


# Module-level guard: prevents overlapping refresh stampedes when
# multiple clients hit an empty /picks/today at the same time.
_refresh_in_flight: bool = False


# ── Market filter taxonomy ────────────────────────────────────────────────
# Tokens are the SAME across sports where it makes sense (moneyline, totals,
# spread). Sport-specific tokens (btts, goalscorer, player_points, etc.) only
# match their own sport. The regex is matched (case-insensitive) against the
# pick's stored `market` string.
_MARKET_REGEX = {
    # ── Soccer-specific families ──────────────────────────────────────────
    "1x2":             r"\bmoneyline\b|\bwin or draw\b",
    "btts":            r"both teams to score|\bbtts\b",
    # Split the original "goalscorer" catch-all into 3 distinct tokens so the
    # Soccer tab has separate filter pills for Anytime / Score-or-Assist / FGS.
    # The legacy `goalscorer` token stays as an OR of all three for any old
    # links / analytics groupings that still reference it (back-compat).
    "anytime_scorer":  r"anytime goal scorer",
    "score_or_assist": r"to score or assist",
    "first_goal_scorer": r"first goal scorer|last goal scorer",
    # Soccer Anytime Assist market (2026-07-26). User requested a
    # dedicated Assists tab — anchored on "anytime assist" to avoid
    # collision with the score_or_assist token above (which also
    # contains "assist"). Order-matters: score_or_assist regex uses
    # the explicit "to score or assist" phrase, so this narrower
    # pattern below is safe.
    "anytime_assist":  r"\banytime assist\b",
    "goalscorer":      r"anytime goal scorer|first goal scorer|last goal scorer|to score or assist",

    # ── Generic team markets ──────────────────────────────────────────────
    "moneyline":     r"\bmoneyline\b",
    "double_chance": r"\bwin or draw\b|double chance",
    "spread":        r"[+\-]\d+(\.\d+)?\s+spread\b|\bspread\b",
    "run_line":      r"\brun line\b|[+\-]\d+(\.\d+)?\s+spread\b",  # MLB run line ≡ spread

    # ── Team totals (MLB — main + alt) ────────────────────────────────
    # Matches "Yankees Team Total Over 4.5" AND "Red Sox Team Total
    # Under 3.5 (Alt)". Anchored on the phrase "Team Total" so it
    # doesn't collide with the game-total `totals` token above.
    "team_total":    r"\bteam total\b",

    # ── Game totals ONLY (must START with "Total <stat>") ─────────────────
    # This intentionally does NOT match "Total Bases", "Player … Total …",
    # or alt-prop Over/Under player markets. Game totals only.
    "totals":        r"^total (goals|points|runs|sets|games|corners)\b|\bgame total\b",

    # ── Player props (mutually exclusive, anchored) ───────────────────────
    # Each prop is keyed off the STAT NAME and excludes neighbouring stats.
    # MongoDB supports PCRE lookaheads so we use them to disambiguate.
    # Hits+Runs+RBIs MUST come before plain batter_hits — otherwise "Hits +
    # Runs + RBIs" would also match the bare "hits" pattern and bleed into
    # the Hits filter pill.
    "batter_hits_runs_rbis": r"hits \+ runs \+ rbis|h\+r\+rbi",
    "batter_hits":          r"\bhits\b(?!\s*allowed)(?!\s*\+)",
    # Pitcher strikeouts — added 2026-06-18 with the pitcher Ks props
    "pitcher_strikeouts":   r"\bstrikeouts\b",
    # Pitcher outs recorded — added 2026-06-19. Main line only (no alt).
    "pitcher_outs":         r"\bouts recorded\b|\bpitcher outs\b",
    "player_points":        r"\bpoints\b(?!\s*total)",
    "player_rebounds":      r"\brebounds\b",
    "player_assists":       r"\bassists\b",

    # ── NFL props ─────────────────────────────────────────────────────────
    "passing_yards":   r"passing yards",
    "rushing_yards":   r"rushing yards",
    "receiving_yards": r"receiving yards",

    # ── Tennis ────────────────────────────────────────────────────────────
    "match_winner":  r"\bmoneyline\b|match winner|to win match",
    "sets":          r"\btotal sets\b|\bset winner\b|\bset score\b",

    # NEW (2026-06-24): User restructured Tennis tabs into 4 cleanly-
    # separated families. The previous catch-all "Alt" tab mixed player
    # game-spreads, total-game alts, and plain totals into one
    # confusing bucket. Now:
    #   • Game Alt Line (tennis_game_alt) → ONLY player spread alts.
    #     Pattern: "<Name> +/-N.N Games (Alt)"  or  "<Name> +/-N.N Spread".
    #     Disambiguated by the explicit +/- sign right before the games/
    #     spread number — total Overs/Unders use no sign so they don't
    #     collide.
    #   • Totals (tennis_totals) → regular total-game lines AND alt
    #     total-game lines, all under one umbrella.
    #     Patterns: "Total Games Over/Under X.X" + "Over/Under X.X Games (Alt)".
    "tennis_game_alt": r"[+\-]\d+(?:\.\d+)?\s+games\s*\(alt\)|[+\-]\d+(?:\.\d+)?\s+spread\b",
    "tennis_totals":   r"\btotal games\b|^\s*(?:over|under)\s+\d+(?:\.\d+)?\s+games\b",

    # ── Broad catch-all (still used by analytics market-label grouping) ──
    "player_props":  r"hits|outs recorded|points|rebounds|assists|passing yards|rushing yards|receiving yards|touchdowns|goal scorer",
}


def _market_regex(token: str) -> str | None:
    return _MARKET_REGEX.get(token.lower().strip())


# Sport → available market filter tokens. Drives the UI MarketSelector pills.
SPORT_MARKETS = {
    "Soccer": [
        {"token": "1x2",               "label": "1X2"},
        {"token": "totals",            "label": "Over/Under"},
        {"token": "btts",              "label": "BTTS"},
        {"token": "anytime_scorer",    "label": "Anytime Scorer"},
        {"token": "anytime_assist",    "label": "Assists"},
        {"token": "score_or_assist",   "label": "Score or Assist"},
        {"token": "first_goal_scorer", "label": "FGS"},
    ],
    "NBA": [
        {"token": "moneyline",   "label": "Moneyline"},
        {"token": "spread",      "label": "Spread"},
        {"token": "totals",      "label": "Totals"},
        {"token": "player_points",   "label": "Points"},
        {"token": "player_rebounds", "label": "Rebounds"},
        {"token": "player_assists",  "label": "Assists"},
    ],
    "NFL": [
        {"token": "moneyline",   "label": "Moneyline"},
        {"token": "spread",      "label": "Spread"},
        {"token": "totals",      "label": "Totals"},
        {"token": "passing_yards",   "label": "Passing Yds"},
        {"token": "rushing_yards",   "label": "Rushing Yds"},
        {"token": "receiving_yards", "label": "Receiving Yds"},
    ],
    # CFB added 2026-07-27 in prep for Week 0 (Aug 23). Mirrors NFL market
    # tokens — the Odds API `americanfootball_ncaaf` feed uses identical
    # keys, and CFB picks flow through the same NFL pipeline in
    # sports_engine.fetch_cfb_picks. Feature engine landing separately.
    "CFB": [
        {"token": "moneyline",       "label": "Moneyline"},
        {"token": "spread",          "label": "Spread"},
        {"token": "totals",          "label": "Totals"},
        {"token": "passing_yards",   "label": "Passing Yds"},
        {"token": "rushing_yards",   "label": "Rushing Yds"},
        {"token": "receiving_yards", "label": "Receiving Yds"},
    ],
    "MLB": [
        {"token": "moneyline",   "label": "Moneyline"},
        {"token": "run_line",    "label": "Run Line"},
        {"token": "totals",      "label": "Totals"},
        {"token": "team_total",  "label": "Team Total"},
        {"token": "batter_hits",            "label": "Hits"},
        # H+R+RBI chip restored 2026-07-21 per user — the market ban
        # in quality_gate.py has been lifted now that mlb_feature_engine
        # gates emission on ≥3 real factors (L10 hit rate, platoon,
        # BvP, matchup, home/away). No more variance-only 35% picks.
        {"token": "batter_hits_runs_rbis",  "label": "H+R+RBI"},
        {"token": "pitcher_strikeouts",     "label": "Strikeouts"},
        {"token": "pitcher_outs",           "label": "Outs Recorded"},
    ],
    "Tennis": [
        {"token": "match_winner",    "label": "Moneyline"},
        {"token": "tennis_game_alt", "label": "Game Alt Line"},
        {"token": "sets",            "label": "Sets"},
        {"token": "tennis_totals",   "label": "Totals"},
    ],
}


# ── Lite-payload field strip list (perf, 2026-06-25) ──
# Fields heavy enough to dominate `/picks/today` payload size BUT only
# consumed by the pick-detail screen, never rendered on the home cards.
# Stripped when ?lite=true is passed. Detail screen calls /api/picks/{id}
# separately and still gets the full fat document.
#
# Audit on 170 picks (1.5 MB total payload):
#   sportsbook_mapping        428 KB / 27.8% — book deep-link metadata
#   evidence_breakdown        236 KB / 15.3% — Universal Evidence narrative
#   v2_reasons                 79 KB /  5.1% — Lock V2 explainers
#   probability                74 KB /  4.8% — Monte Carlo full distribution
#   selection_v2               73 KB /  4.7% — V2 lock-band derivation
#   brain                      56 KB /  3.7% — internal ranker debug
#   key_insights               53 KB /  3.4% — AI key bullets
#   top_reasons / learning     57 KB /  3.7% — explainer payloads
#   factors / lock_components  45 KB /  3.0% — lock decomposition
#   sim_alt_lines               9 KB /  0.6% — alt-line ladder data
#   *_event_id (5 books)       29 KB /  1.9% — sportsbook deep-link IDs
#
# After lite strip: ~300 KB payload (5x smaller).
_LITE_STRIPPED_FIELDS = frozenset({
    "sportsbook_mapping",
    "evidence_breakdown",
    "v2_reasons",
    "probability",
    "selection_v2",
    "brain",
    "key_insights",
    "top_reasons",
    "learning",
    "factors",
    "lock_components",
    "sim_alt_lines",
    "fanduel_event_id",
    "draftkings_event_id",
    "betmgm_event_id",
    "caesars_event_id",
    "pointsbet_event_id",
    # ── Added 2026-06-28 (Cloudflare 520 hardening, target <300KB) ──
    # These were still bleeding into the lite payload at 11-18 KB each.
    # All are detail-only (NOT rendered on the collapsed home card —
    # `player_form`, `apex_blockers`, `understat_form`, `tier_v2` were
    # intentionally KEPT because LockPickCard renders HOT/COLD streak
    # badges, xG-form chips, and the near-miss banner from them).
    "external_id",              # 15 KB — bookmaker dedupe key
    "tennis_components",        # 13 KB — V2 component breakdown
    "player_intel",             # 11 KB — detail screen only
    "calibration_band_warning", # 7 KB  — admin-only narrative
    "marquee_reason",           # 3 KB  — verbose AI string, summary already shown
    "deep_dive_warning",        # detail screen only
    # ── Signal Engine (2026-07-12) — full component breakdown is
    # detail-only; the card chip renders from the plain `signal_score`
    # number which is intentionally NOT stripped.
    "signal_engine",
    "historical_signal",        # detail screen only
    "bandit_arms_matched",      # bandit debug
})


def _slim_rationale(r: dict) -> dict:
    """Trim `pick_rationale` for the lite payload.

    Keeps only the fields the COLLAPSED LockPickCard renders (summary,
    lean chip, confidence chip, edge-vs-market line). The deep blocks
    (matchup, splits, pitcher_quality, multipliers, team_quality,
    returning_production, portal, stats_this_season) are stripped —
    they're rendered ONLY when the user expands "Why this pick?", and
    the card lazy-fetches the full pick via /api/picks/{id} at that
    point.

    Audit on today's 248 picks: full rationale = 128 KB total / ~500B
    avg. Slim rationale = 35 KB / ~140B avg. 72% reduction on the
    rationale block, ~12% on the overall lite payload.
    """
    if not isinstance(r, dict):
        return r
    slim = {
        "summary": r.get("summary"),
        "data_source": r.get("data_source"),
        "engine": r.get("engine"),
        "engine_version": r.get("engine_version"),
        "lean": r.get("lean"),
        "confidence_score": r.get("confidence_score"),
        "edge_pct_points": r.get("edge_pct_points"),
        "model_win_prob_pct": r.get("model_win_prob_pct"),
        "final_hit_prob_pct": r.get("final_hit_prob_pct"),
        "lock_score": r.get("lock_score"),
        "edge_percent": r.get("edge_percent"),
        "espn_rank": r.get("espn_rank"),
        # v3 goal-scorer signals (small, ~200B) — needed by the
        # collapsed LockPickCard so the "λ_team · minutes · share"
        # micro-line doesn't blank out on lite payloads.
        "v3_signals": r.get("v3_signals"),
    }
    # ── Keep the top EVIDENCE + CONCERN bullets for the collapsed card.
    # Was 1 each (2026-06-28) — user feedback 2026-07-21: "I don't want
    # generic why this pick need real data and h2h history" — now that
    # rationale is populated with real H2H / weather / player_form /
    # tennis_components bullets, show up to 5 evidence + 2 concerns so
    # the card surfaces the actual data-driven reasoning inline.
    # Bump adds ~400B/pick to lite payload — well within budget after
    # heavy fields were slimmed in the 2026-07-16 pass.
    ev = r.get("evidence")
    if isinstance(ev, list) and ev:
        slim["evidence"] = ev[:5]
    cn = r.get("concerns")
    if isinstance(cn, list) and cn:
        slim["concerns"] = cn[:2]
    # Tag the slim payload so the frontend can spot it and lazy-load
    # the rest on first expand.
    slim["_slim"] = True
    return {k: v for k, v in slim.items() if v is not None}


def _strip_for_lite(pick: dict) -> dict:
    """Remove detail-only heavy fields so home-feed payload is small.
    Returns a new dict — does NOT mutate the input.

    `pick_rationale` is special-cased: rather than dropping it entirely
    (which would break the home-card collapsed "Why this pick?" chip),
    we slim it via `_slim_rationale` so the collapsed UI still works
    but the deep blocks ride only on the detail endpoint."""
    out = {k: v for k, v in pick.items() if k not in _LITE_STRIPPED_FIELDS}
    if isinstance(out.get("pick_rationale"), dict):
        out["pick_rationale"] = _slim_rationale(out["pick_rationale"])
    return out


# /picks/today + /picks/bet-killer moved to routes/picks_routes.py (Phase 3, 2026-06-27)


# /picks/under-of-the-day moved to routes/picks_routes.py (Phase 2, 2026-06-27)


# /picks/rollover moved to routes/picks_routes.py (Phase 2, 2026-06-27)


# /picks/parlay moved to routes/picks_routes.py (Phase 3, 2026-06-27)
# Static routes MUST be declared BEFORE the parameterized /picks/{pick_id}
# route, otherwise FastAPI's routing would match them as a pick_id.

# /picks/settle moved to routes/picks_routes.py (Phase 2, 2026-06-27)


# /picks/history moved to routes/picks_routes.py (Phase 2, 2026-06-27)


# /picks/{pick_id} (detail) moved to routes/picks_routes.py (Phase 2, 2026-06-27)


# /picks/{pick_id}/ai-explain moved to routes/picks_routes.py (Phase 2, 2026-06-27)


# REFRESH_COOLDOWN_SECONDS + _cooldown_payload moved to routes/picks_routes.py
# (Phase 3, 2026-06-27). The Phase-2 refresh-status handler used to lazy-
# import them from here; it now uses the local copy in picks_routes.py.

# /picks/refresh moved to routes/picks_routes.py (Phase 3, 2026-06-27)

# ───────────────────────── Loss Analysis ─────────────────────────

# /picks/{pick_id}/loss-analysis moved to routes/picks_routes.py (Phase 2, 2026-06-27)


@api.get("/mlb/live")
async def mlb_live(user: Annotated[UserPublic, Depends(current_user)]):
    """Live MLB game state — feeds the in-game score badges on Lock cards.

    Returns a dict keyed by team-vs-team event string so the frontend can
    O(1)-lookup any MLB pick. Costs ZERO Odds API credits (uses the free
    statsapi.mlb.com endpoint via `mlb_live.fetch_today_live_mlb`).

    The 15 s in-memory cache inside `mlb_live` means a whole tab of MLB
    cards triggers at most one upstream HTTP call per quarter-minute.
    Shape per game:
        {
          "home": "Baltimore Orioles", "away": "Seattle Mariners",
          "home_score": 4, "away_score": 3,
          "status": "Final" | "In Progress" | "Pre-Game" | "Postponed" | …,
          "abstract_status": "Final" | "Live" | "Preview",
          "is_live": true,           # convenience flag
          "is_final": false,
        }
    """
    try:
        from mlb_live import fetch_today_live_mlb
        games = await fetch_today_live_mlb()
    except Exception as e:
        logger.warning("MLB live endpoint failed: %s", e)
        return {"games": {}, "as_of": _today_str()}
    out: dict[str, dict] = {}
    for g in games:
        away = g.get("away_team") or ""
        home = g.get("home_team") or ""
        if not away or not home:
            continue
        scores = g.get("scores") or []
        home_score = next((int(s["score"]) for s in scores
                           if s.get("name") == home), None)
        away_score = next((int(s["score"]) for s in scores
                           if s.get("name") == away), None)
        game_status   = g.get("status") or ""
        abstract = g.get("abstract_status") or ""
        # MLB Stats API quirk: postponed/cancelled/suspended games carry
        # `abstractGameState == "Final"` even though they never finished.
        # Treat them as NOT final so the UI doesn't mis-badge them.
        non_final_terminal = {
            "Postponed", "Cancelled", "Canceled", "Suspended", "Delayed Start",
        }
        is_final = (
            (abstract == "Final" or g.get("completed") is True)
            and game_status not in non_final_terminal
        )
        commence = g.get("commence_time") or ""
        entry = {
            "home": home,
            "away": away,
            "home_score": home_score,
            "away_score": away_score,
            "status": game_status,
            "abstract_status": abstract,
            "is_live": abstract == "Live",
            "is_final": is_final,
            # Commence time — frontend uses this to verify the live badge
            # belongs to the SAME scheduled game (not yesterday's finale
            # of the same matchup that already cashed/ended).
            "commence_time": commence,
        }
        ev_key = f"{away} @ {home}"
        # Multi-game / series support: when CIN @ NYY plays Thu AND Fri, we
        # were stomping on the same key and showing yesterday's FINAL on
        # tomorrow's card. Key per (event, date) so each game gets its own
        # entry. Frontend looks up "Away @ Home|YYYY-MM-DD" first, then
        # falls back to the plain event key for backwards compat.
        date_part = ""
        if commence and len(commence) >= 10:
            date_part = commence[:10]
        # Signal ranking — LIVE > pre-game > FINAL. Used for both the
        # bare-key fallback (so today's live game beats yesterday's
        # FINAL of the same matchup) AND the dated key (because late-
        # night yesterday-PT games carry today's UTC date, so they
        # collide with today's daytime games on the same UTC dated key
        # — without ranking we'd serve yesterday's FINAL).
        def _signal(e: dict) -> int:
            if e.get("is_live"): return 3
            if not e.get("is_final"): return 2  # upcoming/preview
            return 1                              # final
        if date_part:
            dated_key = f"{ev_key}|{date_part}"
            existing_dated = out.get(dated_key)
            if not existing_dated or _signal(entry) > _signal(existing_dated):
                out[dated_key] = entry
        # Backward-compat key — but ONLY for live or today's-pre-game lookups.
        # Avoid stamping a finished game over a future game with same teams.
        existing = out.get(ev_key)
        if not existing or _signal(entry) > _signal(existing):
            out[ev_key] = entry
        if g.get("id"):
            out[g["id"]] = entry
    return {"games": out, "as_of": datetime.now(timezone.utc).isoformat()}


@api.get("/stats/summary")
async def stats_summary(user: Annotated[UserPublic, Depends(current_user)]):
    """Hero-card totals for the Locks tab.

    Computed from the SAME picks the user actually sees on /picks/today —
    i.e. lock_score >= 85, no NO-BET, no negative edge, AND game time
    has not yet passed the play-window cutoff. Under-style locks ARE
    counted (matching /picks/today's behaviour) so the hero number
    matches the visible list across every sport tab.
    """
    await _ensure_today_picks()
    today = _today_str()
    base_q = {
        "pick_date": today,
        "lock_score": {"$gte": 85},
        "no_bet": {"$ne": True},
        "edge_percent": {"$gte": 0},
    }
    elite_q = {
        "pick_date": today,
        "elite_player": True,
        "no_bet": {"$ne": True},
    }
    # Pull both buckets and dedupe by id so the totals match the /picks/today
    # response exactly.
    base_cur = db.picks.find(base_q, {"_id": 0}).to_list(length=500)
    elite_cur = db.picks.find(elite_q, {"_id": 0}).to_list(length=500)
    base_rows, elite_rows = await base_cur, await elite_cur
    seen: set = set()
    rows: list[dict] = []
    for p in (*base_rows, *elite_rows):
        pid = p.get("id")
        if pid in seen:
            continue
        seen.add(pid)
        rows.append(p)
    # Apply the same play-window filter as /picks/today.
    rows = _filter_in_play_window(rows)

    # Per-sport aggregates
    by_sport_map: dict[str, dict] = {}
    for p in rows:
        sp = p.get("sport") or "Unknown"
        b = by_sport_map.setdefault(sp, {"count": 0, "lock_sum": 0.0, "edge_sum": 0.0, "elite": 0})
        b["count"] += 1
        b["lock_sum"] += float(p.get("lock_score") or 0)
        b["edge_sum"] += float(p.get("edge_percent") or 0)
        if p.get("elite_player") or float(p.get("lock_score") or 0) >= 95:
            b["elite"] += 1
    by_sport = [
        {"sport": sp,
         "count": b["count"],
         "avg_lock": round(b["lock_sum"] / b["count"], 1) if b["count"] else 0,
         "avg_edge": round(b["edge_sum"] / b["count"], 2) if b["count"] else 0,
         "elite_count": b["elite"]}
        for sp, b in by_sport_map.items()
    ]
    total = len(rows)
    elite = sum(1 for p in rows
                if p.get("elite_player") or float(p.get("lock_score") or 0) >= 95)
    # Average edge across the visible slate (matches /picks/today universe).
    if rows:
        avg_edge = round(sum(float(p.get("edge_percent") or 0) for p in rows) / len(rows), 2)
    else:
        avg_edge = 0
    return {"date": today, "total_picks": total, "elite_count": elite,
            "avg_edge_percent": avg_edge, "by_sport": by_sport}


# ── Analytics endpoints moved to routes/analytics_routes.py ──────────
# 15 endpoints relocated during Phase-2 monolith decomposition:
#   /analytics/model-performance, /analytics/sim-backtest,
#   /analytics/learned-weights, /analytics/bandit, /analytics/backtest,
#   /analytics/backtest-custom, /analytics/v2, /analytics/v2/recompute,
#   /analytics/buckets, /analytics/calibration,
#   /analytics/calibration/refit, /analytics/xg-form-shadow,
#   /analytics/buckets/recompute, /analytics/buckets/rollback,
#   /analytics/learn



# /picks/{pick_id}/probability moved to routes/picks_routes.py (Phase 2, 2026-06-27)


# /picks/{pick_id}/player-form moved to routes/picks_routes.py (Phase 2, 2026-06-27)


# Soccer/Tennis admin ops endpoints
# (`/admin/refresh-soccer-player-form` + `/admin/backfill-tennis-elo`)
# now live in `routes/admin_routes.py`. Mounted in the app-wiring
# section near the bottom of this file.


# /picks/{pick_id}/pitcher-h2h moved to routes/picks_routes.py (Phase 2, 2026-06-27)


# /picks/{pick_id}/simulation moved to routes/picks_routes.py (Phase 2, 2026-06-27)


# ── Analytics endpoints (15 endpoints) ─────────────────────────────
# Moved to routes/analytics_routes.py during Phase-2 monolith
# decomposition. Mounted via app.include_router() below.


@api.get("/")
async def root():
    return {"ok": True, "service": "PerkLocks AI", "date": _today_str()}


# ────────────────────── Historical Sports Intelligence Engine ──────────────────────
# Admin endpoints for Historical Engine (backfill / status / player-form
# lookup) now live in `routes/admin_routes.py`. The Lock Engine itself
# still reads from this engine via `historical.lookup.get_player_form`
# — no API call needed.


# ────────────────────── Parlay History (Save-on-Tap) ──────────────────────
# User taps "Save" on a generated parlay → we persist it and track the
# status of every leg until all settle. Endpoints now live in
# `routes/parlay_history_routes.py`. The data-layer module
# (`parlay_history.py`) is unchanged.


# ────────────────────── App wiring ──────────────────────

# Mount the isolated soccer module BEFORE the catch-all api router so its
# routes are registered on the same /api prefix. The soccer router is
# completely standalone — fully self-contained in /app/backend/soccer/.
try:
    from soccer.routes import router as soccer_router
    api.include_router(soccer_router)
    logger.info("Soccer module mounted at /api/sports/soccer")
except Exception as _soccer_mount_err:
    logger.warning("Soccer module failed to mount, continuing without it: %s", _soccer_mount_err)

# Mount the Survivability Engine — pure-insight conditional hit coverage
# for MLB hit props. Adds /api/picks/{id}/coverage. Isolated module so
# a failure here doesn't break pick loading.
try:
    from survival.routes import router as survival_router
    api.include_router(survival_router)
    logger.info("Survivability module mounted at /api/picks/{id}/coverage")
except Exception as _survival_mount_err:
    logger.warning("Survival module failed to mount, continuing without it: %s", _survival_mount_err)

# Mount the Lock Engine V2 — SHADOW MODE deep-thinking scoring layer.
# Adds /api/lock-v2/report and /api/picks/{id}/lock-breakdown. The v2
# engine writes hidden shadow fields (lock_score_v2, counter_score, ...)
# alongside every new pick when ENABLE_COUNTER_ENGINE=true. Production
# lock_score is NEVER modified by this layer.
try:
    from lock_v2.routes import router as lock_v2_router
    api.include_router(lock_v2_router)
    logger.info("Lock Engine V2 (shadow) mounted at /api/lock-v2/* and /api/picks/{id}/lock-breakdown")
except Exception as _lock_v2_mount_err:
    logger.warning("Lock V2 module failed to mount, continuing without it: %s", _lock_v2_mount_err)

# Mount the Market Competition Engine — ranks parallel markets per event so
# the UI can surface "Best Pick · Alternative · Alternative" for any match.
# Endpoints: /api/picks/{id}/market-rank and /api/market-rank/feed.
try:
    from market_competition.routes import router as market_rank_router
    api.include_router(market_rank_router)
    logger.info("Market Competition Engine mounted at /api/picks/{id}/market-rank")
except Exception as _mc_mount_err:
    logger.warning("Market Competition module failed to mount, continuing without it: %s", _mc_mount_err)

# Mount the Soccer Lab — dynamic league discovery + ranked global feed.
# Endpoints: /api/soccer-lab/leagues (cached active soccer_* leagues) and
# /api/soccer-lab/feed (confidence-sorted soccer picks across all leagues).
try:
    from soccer_lab import router as soccer_lab_router
    api.include_router(soccer_lab_router)
    logger.info("Soccer Lab mounted at /api/soccer-lab/*")
except Exception as _slab_mount_err:
    logger.warning("Soccer Lab failed to mount, continuing without it: %s", _slab_mount_err)

# Mount client telemetry — Milestone 1.1 stability layer. Captures
# uncaught frontend errors into `client_errors` collection for debug.
try:
    from routes.telemetry_routes import router as telemetry_router
    app.include_router(telemetry_router)   # note: app, not api — has /api prefix baked in
    logger.info("Client telemetry mounted at /api/telemetry/error")
except Exception as _tel_err:
    logger.warning("Client telemetry failed to mount: %s", _tel_err)

# Mount Auto-Elite Discovery — exposes data-driven scorer profiles + trends.
# Endpoints: /api/auto-elite, /api/player-profiles, /api/auto-elite/recompute.
try:
    from auto_elite import router as auto_elite_router
    api.include_router(auto_elite_router)
    logger.info("Auto-Elite module mounted at /api/auto-elite + /api/player-profiles")
except Exception as _ae_mount_err:
    logger.warning("Auto-Elite failed to mount, continuing without it: %s", _ae_mount_err)

# Mount Scorer Bundles — synthesizes 2+ goals / hat-trick / goal+assist SGP
# probabilities from anytime goal scorer odds (Poisson inversion). The Odds
# API doesn't expose those markets directly, so this gives users a fair-odds
# read for sizing their own SGPs.
try:
    from scorer_bundles import router as scorer_bundles_router
    api.include_router(scorer_bundles_router)
    logger.info("Scorer Bundles mounted at /api/picks/{id}/scorer-bundles")
except Exception as _sb_mount_err:
    logger.warning("Scorer Bundles failed to mount, continuing without it: %s", _sb_mount_err)

# ── Player Intelligence ──
# Canonical player resolver + archetype + volatility profiles, used to enrich
# every pick that mentions an athlete so downstream UI never deals with raw
# name strings. Routes: /api/player-intel/profile, /list, /refresh.
try:
    from player_intel import router as player_intel_router
    api.include_router(player_intel_router)
    logger.info("Player Intelligence mounted at /api/player-intel/*")
except Exception as _pi_mount_err:
    logger.warning("Player Intelligence failed to mount, continuing without it: %s", _pi_mount_err)

# ── Picks routes — mounted at the TOP of the file (right after
# `api = APIRouter()`). All Phase 1+2+3 extraction is complete; the
# deferred-mount workaround that lived here is no longer needed.

app.include_router(api)

# ── Extracted route modules (2026-06-24 monolith decomposition) ─────
# Each module owns its own APIRouter with prefix="/api" so the routes
# resolve to the same paths they had before extraction. Add new
# include_router() calls here as more endpoint families get pulled
# out of server.py.
try:
    from routes import parlay_history_routes, admin_routes, analytics_routes
    app.include_router(parlay_history_routes.router)
    logger.info("Parlay-History routes mounted at /api/parlay/*")
    app.include_router(admin_routes.router)
    logger.info("Admin routes mounted at /api/admin/*")
    # Admin user-management dashboard routes (added 2026-06-24).
    from routes import admin_users_routes
    app.include_router(admin_users_routes.router)
    logger.info("Admin Users dashboard routes mounted at /api/admin/users/*")
    app.include_router(analytics_routes.router)
    logger.info("Analytics routes mounted at /api/analytics/* (15 endpoints)")
    # ── User Bets + Personal Analytics (2026-07-21) ──────────────────
    # New endpoints for user-scoped bet tracking (see routes/user_bets_routes.py).
    # ALL admin analytics (/analytics/*) are locked to admin role — users
    # tap the "Track this Bet" button on the board, and everything they
    # see under /user/bets and /user/analytics/* is scoped to their own
    # user_id at the DB query level.
    from routes import user_bets_routes
    app.include_router(user_bets_routes.router)
    logger.info("User Bets + Personal Analytics routes mounted at /api/user/*")
    # NFL High-Hit-Rate + ATD engines (added 2026-06-26).
    from routes import nfl_routes
    app.include_router(nfl_routes.router)
    logger.info("NFL engines mounted at /api/nfl/safe-bets + /api/nfl/atd/*")
    # MLB Home-Run intelligence tab (added 2026-06-30).
    # Backs the new HR tab in the mobile app — Statcast park × pitcher
    # HR/9 × batter ISO/HR-PA × Open-Meteo wind/temp × H2H BvP.
    from routes import mlb_hr_routes
    app.include_router(mlb_hr_routes.router)
    logger.info("MLB HR intelligence mounted at /api/mlb/hr-slate")
    # ── User-facing performance / CLV dashboard (2026-07-27) ─────────
    # Public (any logged-in user) endpoints proving the picks board is
    # +EV. Backs the new CLV Dashboard screen in the mobile app.
    from routes import me_performance_routes
    app.include_router(me_performance_routes.router)
    logger.info("User Performance + CLV dashboard mounted at /api/me/*")
except Exception as _routes_mount_err:
    logger.exception("Extracted route modules failed to mount: %s", _routes_mount_err)

# SEC P3-B (2026-06-25): Replace `allow_origins=["*"]` + credentials with
# a regex allowlist that covers:
#   • Expo/Metro dev (localhost, 127.0.0.1 on any port)
#   • Emergent preview + deployed domains (*.emergentagent.com,
#     *.preview.emergentagent.com, *.emergent.host)
#   • An optional comma-separated EXTRA_CORS_ORIGINS env var for any
#     custom prod domain that gets added later
# Wildcard `*` was browser-safe because all auth is Bearer (not cookie),
# but the audit flagged it as a hardening miss — locking it down here.
_extra_origins = [o.strip() for o in (os.environ.get("EXTRA_CORS_ORIGINS") or "").split(",") if o.strip()]
_allow_origin_regex = (
    r"^(https?://(localhost|127\.0\.0\.1)(:\d+)?"
    r"|https://([a-z0-9-]+\.)?emergentagent\.com"
    r"|https://([a-z0-9-]+\.)?preview\.emergentagent\.com"
    r"|https://([a-z0-9-]+\.)?emergent\.host"
    r")$"
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_extra_origins,                 # explicit prod domains (env-controlled)
    allow_origin_regex=_allow_origin_regex,       # dev + Emergent preview/deploy
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ────────────────────── Reliability layer ──────────────────────
# Surgical hardening that does NOT change behavior — only catches errors,
# logs them, and returns friendly JSON instead of HTML 500 pages.
import time as _time_mod
import traceback as _traceback

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware


class _ReliabilityMiddleware(BaseHTTPMiddleware):
    """Catches every uncaught exception, logs with full traceback + request
    context, and returns a friendly JSON error. Also tracks per-route
    response times for monitoring.
    """
    async def dispatch(self, request: Request, call_next):
        rid = uuid.uuid4().hex[:12]
        request.state.request_id = rid
        start = _time_mod.perf_counter()
        try:
            response = await call_next(request)
            elapsed_ms = (_time_mod.perf_counter() - start) * 1000.0
            # Slow-request log (>1500 ms — likely a perf regression)
            if elapsed_ms > 1500:
                logger.warning(
                    "SLOW %s %s → %d in %.0fms (rid=%s)",
                    request.method, request.url.path, response.status_code,
                    elapsed_ms, rid,
                )
            # Always set a request-id header so the frontend can echo it
            # back in bug reports.
            response.headers["X-Request-ID"] = rid
            return response
        except Exception as exc:
            tb = _traceback.format_exc()
            logger.error(
                "UNHANDLED %s %s rid=%s\n%s\n%s",
                request.method, request.url.path, rid, exc, tb,
            )
            return JSONResponse(
                status_code=500,
                content={
                    "error": "Something went wrong — please retry.",
                    "request_id": rid,
                },
                headers={"X-Request-ID": rid},
            )


app.add_middleware(_ReliabilityMiddleware)


# ─── Gzip compression (2026-06-28) ──────────────────────────────────────
# JSON compresses ~85% — turns 518 KB lite payload into ~75 KB over the
# wire. Critical for mobile users on cellular networks where the heavy
# uncompressed payload was the most likely trigger for Cloudflare 520
# (origin slow → CF edge timeout). Min size 500 bytes so tiny responses
# (version, refresh-status) skip the gzip overhead.
from starlette.middleware.gzip import GZipMiddleware
app.add_middleware(GZipMiddleware, minimum_size=500, compresslevel=5)


# ─── Outermost resilience layer (2026-06-28) ────────────────────────────
# Adds JSON-coerce on /api/*, structured access logs, 85s wall-clock
# timeout (under CF's 100s edge timeout), and a FastAPI-level exception
# handler as a belt-and-suspenders fallback. This is the OUTERMOST
# middleware so it sees EVERY response (incl. those that bypass the
# inner _ReliabilityMiddleware via mounts). See backend/middleware/
# resilience.py for the full rationale.
try:
    from middleware.resilience import install as _install_resilience
    _install_resilience(app)
except Exception as _res_exc:
    logger.warning("Failed to install resilience middleware: %s", _res_exc)


@app.middleware("http")
async def _track_user_api_usage(request, call_next):
    """Bump a per-user counter on every authenticated /api request.

    Powers the admin dashboard's "top API users" ranking. Cheap
    (fire-and-forget update_one with upsert) — never blocks the response.
    Best-effort: any failure here is silently swallowed so a tracker bug
    can't break the API surface.
    """
    response = await call_next(request)
    try:
        path = str(request.url.path)
        if not path.startswith("/api/"):
            return response
        # Skip cheap probes / public endpoints — they'd dwarf real usage.
        if path in ("/api/version", "/api/auth/login", "/api/auth/register"):
            return response
        auth_h = request.headers.get("authorization") or ""
        if not auth_h.lower().startswith("bearer "):
            return response
        from auth import JWT_SECRET, JWT_ALGORITHM
        import jwt as _jwt
        try:
            payload = _jwt.decode(auth_h[7:], JWT_SECRET, algorithms=[JWT_ALGORITHM])
        except Exception:
            return response
        uid = payload.get("sub")
        if not uid:
            return response
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()
        await db.user_activity.update_one(
            {"user_id": uid},
            {
                "$inc": {"api_calls": 1, f"by_path.{path.replace('.', '_')}": 1},
                "$set": {"last_call_at": now_iso, "last_path": path},
            },
            upsert=True,
        )
        # Also bump user's `last_login_at` style "last_seen" field so the
        # overview can show active_24h accurately.
        await db.users.update_one(
            {"id": uid},
            {"$set": {"last_login_at": now_iso}},
        )
    except Exception:
        pass
    return response


@app.middleware("http")
async def _no_store_api_responses(request, call_next):
    """Force every /api response to bypass intermediate caches.

    The published Expo bundle was hitting our backend through the same
    edge proxy as the website, but the iOS native HTTP stack was serving
    cached responses (especially `/picks/today`) for hours. Setting
    Cache-Control: no-store + Pragma: no-cache + Expires: 0 stops every
    layer (CDN, proxy, iOS NSURLCache, RN fetch cache) from holding
    onto pick data.
    """
    response = await call_next(request)
    if str(request.url.path).startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


async def _daily_refresh_loop():
    """Refresh picks on startup and every 1 hour thereafter.

    The /picks/refresh endpoint has its own rate-limit guard so we never
    burn The Odds API credits unexpectedly — but a 1-hour scheduler keeps
    the feed fresh as new lines drop, players get scratched, and previous
    games complete. Game-time sorting feels live instead of frozen.

    Midnight-rollover safety: tracks the UTC date that was last refreshed
    so when the day flips mid-loop, we trigger a fresh refresh
    immediately for the new day instead of waiting up to 59 minutes for
    the next hourly tick. Fixes the "where did all the picks go" bug
    reported 2026-06-23 at 00:48 UTC.
    """
    try:
        await _ensure_today_picks()
    except Exception as e:
        logger.warning("Startup picks seed failed: %s", e)

    last_refresh_date = _today_str()
    while True:
        try:
            # Short tick so we detect UTC day rollover within ~5 minutes
            # instead of the old hourly cadence. The per-refresh code
            # path is no-op when the slate is already full + fresh.
            await asyncio.sleep(300)  # 5 min

            current_date = _today_str()
            if current_date != last_refresh_date:
                # Day rolled over — force a refresh NOW so new-day picks
                # show up within 5 minutes instead of waiting an hour.
                logger.info(
                    "Daily loop: UTC day rolled %s → %s, forcing refresh",
                    last_refresh_date, current_date,
                )
                await _refresh_picks(current_date)
                last_refresh_date = current_date
                continue

            # Hourly cadence for same-day freshness. We check
            # `_should_refresh_by_clock` against the previous tick — if we've
            # just crossed an hour boundary since last refresh, run.
            # Simpler: just count ticks (12 ticks × 5 min = 60 min).
            _daily_refresh_loop.tick_count = getattr(_daily_refresh_loop, "tick_count", 0) + 1
            if _daily_refresh_loop.tick_count >= 12:
                _daily_refresh_loop.tick_count = 0
                await _refresh_picks(current_date)
                last_refresh_date = current_date
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("Periodic refresh failed: %s", e)
            await asyncio.sleep(300)


# ─── MLB Pregame Quick-Refresh Loop ─────────────────────────────────────
# User-reported bug (verified in DB): "MLB games come on at 3:11 and get on
# board around 3:08 — barely have time to put bets in." Cause: the global
# refresh runs hourly, and books typically post MLB player props ~90 min
# before first pitch (when the manager publishes the lineup). With a 1-hour
# cadence, picks land 5–55 min pre-game depending on when the slate happened
# to align with the refresh tick.
#
# Fix: poll MLB-only every 5 min during the US daytime window (15:00–23:00
# UTC = 11 AM – 7 PM ET). Cheap because we filter to MLB only — non-MLB
# fetchers are skipped (saves Odds credits) and other sports' picks are not
# wiped from the DB.
_MLB_QUICK_REFRESH_INTERVAL = 5 * 60   # 5 minutes
_MLB_WINDOW_START_UTC_HOUR = 15        # 11 AM ET
_MLB_WINDOW_END_UTC_HOUR = 3           # 11 PM ET / 8 PM PT — covers late West Coast first pitches (wraps past midnight UTC)


async def _mlb_pregame_loop():
    """Refresh MLB picks every 5 min during US daytime so player props
    surface 60–90 min pre-game instead of 5–10 min pre-game.

    Look-ahead behaviour (2026-07-15 fix): the loop was only fetching
    `today` which meant during MLB All-Star break (~July 14–16) or any
    other empty day the slate went bone-dry until the following day
    rolled over. We now ALSO fetch tomorrow's slate on every cycle so
    Friday's post-All-Star games are on the board Thursday afternoon.
    """
    # Let startup settle so the initial seed completes first.
    await asyncio.sleep(120)
    while True:
        try:
            now = datetime.now(timezone.utc)
            hour = now.hour
            if hour >= _MLB_WINDOW_START_UTC_HOUR or hour < _MLB_WINDOW_END_UTC_HOUR:
                # Today's slate (real-time refresh — pregame line moves,
                # scratch news, weather, etc.)
                await _refresh_picks(_today_str(), sport_filter="MLB")
                # Tomorrow's slate — surfaces post-break / next-day games
                # 24h in advance. Same idempotent upsert path, cost is
                # a single extra Odds API call per cycle.
                try:
                    tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
                    await _refresh_picks(tomorrow_str, sport_filter="MLB")
                except Exception as e:
                    logger.debug("MLB tomorrow lookahead failed: %s", e)
                await asyncio.sleep(_MLB_QUICK_REFRESH_INTERVAL)
            else:
                # Outside MLB hours — sleep until the window reopens.
                # Compute seconds until 15:00 UTC today (or tomorrow if already past 23:00).
                target = now.replace(
                    hour=_MLB_WINDOW_START_UTC_HOUR, minute=0, second=0, microsecond=0,
                )
                if now >= target:
                    target = target + timedelta(days=1)
                wait_seconds = max(60, int((target - now).total_seconds()))
                logger.info(
                    "MLB pregame loop: outside window (UTC %02d:00–%02d:00), "
                    "sleeping %d min until next opener.",
                    _MLB_WINDOW_START_UTC_HOUR, _MLB_WINDOW_END_UTC_HOUR,
                    wait_seconds // 60,
                )
                await asyncio.sleep(min(wait_seconds, 3600))  # cap at 1h, re-check
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("MLB pregame refresh failed: %s", e)
            await asyncio.sleep(_MLB_QUICK_REFRESH_INTERVAL)


async def _settlement_loop():
    """Per-sport settlement cadence — fast where it's free, careful where it costs.

      • MLB → every **60 seconds** (MLB Stats API is FREE, no Odds credits).
        Picks settle within ~1 min of `completed: true` flipping on the feed.
        User spec 2026-06-22: "I want bets to settle win loss so we can
        learn from picks" — fast grading + 14-day fetch window means the
        learning engine sees real W/L within minutes, not days.
      • All other sports (Soccer/Tennis/UFC/NBA/NFL) → every **15 minutes**
        (cost-aware — Odds API bills per call).
      • Stale auto-void only after **14 days** (was 5) — gives picks the
        max chance to actually grade W/L before being voided as
        unresolvable. Voids only happen when score feeds truly don't expose
        the data anymore.
    """
    await asyncio.sleep(60)  # let startup settle
    FULL_INTERVAL_TICKS = 15   # full settlement every 15th tick = 15 min
    tick = 0
    while True:
        try:
            tick += 1
            is_full = (tick % FULL_INTERVAL_TICKS == 0)
            if is_full:
                # Full settlement — all sports, runs Learning v2 afterward.
                await settle_due_picks(db)
            else:
                # Cheap MLB-only tick — free upstream feed (MLB Stats API),
                # no Odds credits burned.
                await settle_due_picks(db, sport_filter=["MLB"])
            # Recompute Learning v2 immediately so new settlements feed
            # forward into the next pick generation cycle.
            try:
                from learning_system_v2 import recompute_and_persist
                v2_res = await recompute_and_persist(db)
                if not v2_res.get("gated"):
                    logger.info("Learning v2 recomputed: %d rows, %d weight overrides, %d log entries",
                                v2_res.get("rows", 0),
                                len(v2_res.get("market_weights") or {}),
                                v2_res.get("changes_log_count", 0))
            except Exception as e:
                logger.warning("Learning v2 recompute error: %s", e)
            # ── Parlay History Resolver ─────────────────────────────
            # Walk all `live` saved parlays and update leg statuses /
            # mark won/lost based on the just-settled picks.
            try:
                from parlay_history import resolve_saved_parlays
                await resolve_saved_parlays(db)
            except Exception as e:
                logger.warning("Parlay history resolver error: %s", e)
            # ── Auto-Recalibrate Lock Score Curve ───────────────────
            # Every RECALIBRATE_EVERY (100) newly-settled picks the
            # isotonic-regression curve is refit so the displayed
            # confidence stays calibrated as the model evolves.
            try:
                from lock_calibration import maybe_recalibrate
                summary = await maybe_recalibrate(db)
                if summary:
                    logger.info("Calibration auto-refit: %s", summary)
            except Exception as e:
                logger.warning("Calibration auto-refit error: %s", e)
            # Auto-Elite scorer discovery — promote players with 5+ picks
            # and 55%+ hit rate to auto_elite (protected slot in goalscorer cap).
            try:
                from auto_elite import recompute_auto_elite_scorers
                ae_res = await recompute_auto_elite_scorers(db)
                if ae_res.get("promoted"):
                    logger.info("Auto-elite scorers updated: %d promoted (e.g. %s)",
                                ae_res["promoted"],
                                ", ".join(ae_res.get("promoted_names", [])[:3]))
            except Exception as e:
                logger.warning("Auto-elite recompute error: %s", e)
            # Player Intelligence — refresh canonical profiles (seeds +
            # learned from settled picks). Powers archetype/usage/volatility
            # tags attached to every pick during generation.
            try:
                from player_intel import refresh_player_profiles
                pi_res = await refresh_player_profiles(db)
                logger.info(
                    "Player Intelligence: %d total profiles (%d seeded new, %d learned)",
                    pi_res.get("total_profiles", 0),
                    pi_res.get("seeded_new", 0),
                    pi_res.get("learned_updates", 0),
                )
            except Exception as e:
                logger.warning("Player Intelligence refresh error: %s", e)
            # ── Parlay Learning ──
            # Settle any pending parlays (all-legs-resolved → won/lost/push)
            # and rebuild the (sport, market_family) synergy map. The map
            # feeds back into the optimizer's leg scoring the very next
            # request, so the generator literally gets smarter every cycle.
            try:
                from parlay_learning import settle_parlays, compute_synergy_map
                pl_settled = await settle_parlays(db)
                pl_syn = await compute_synergy_map(db)
                logger.info(
                    "Parlay Learning: settled=%d (won=%d lost=%d push=%d) | %d synergy rows",
                    pl_settled.get("settled", 0),
                    pl_settled.get("won", 0),
                    pl_settled.get("lost", 0),
                    pl_settled.get("push", 0),
                    len(pl_syn),
                )
            except Exception as e:
                logger.warning("Parlay Learning settle/aggregate failed: %s", e)
            # Brain memory cache-bust so the next pick refresh picks up
            # the freshly-settled samples (calibration / ROI / market perf).
            try:
                from brain import process_brain  # noqa: F401 — for module import side effect
                from brain.pipeline import on_settlement
                await on_settlement(db)
            except Exception as e:
                logger.warning("Brain cache-bust error: %s", e)
            # ── Fusion Grading (2026-07-29) ─────────────────────────
            # Back-solve `fusion_predictions` for every pick that just
            # settled: writes `actual_value`, `outcome`, `correct`, and
            # `winning_component`. Feeds the Adaptive Learning stack
            # (calibration curves, per-component weights, drift metrics).
            #
            # Runs on the FULL tick (every 15 min) — not every 60s — to
            # amortise the lookback over MLB's fast tick without burning
            # DB cycles when no non-MLB games have settled.
            if is_full:
                try:
                    from services.pick_fusion_decorator import (
                        grade_settled_fusion_predictions,
                    )
                    fg = await grade_settled_fusion_predictions(
                        db, hours_lookback=48, limit=500,
                    )
                    if fg.get("graded", 0) or fg.get("scanned", 0):
                        logger.info(
                            "Fusion Grading: scanned=%d graded=%d "
                            "no_actual=%d errors=%d",
                            fg["scanned"], fg["graded"],
                            fg["no_actual"], fg["errors"],
                        )
                except Exception as e:
                    logger.warning("Fusion grading error: %s", e)
            # ── Daily Learning Snapshot (2026-07-29) ────────────────
            # Runs at most once per calendar day (UTC). Aggregates the
            # real settled-pick + fusion-prediction history into a
            # single dated snapshot: lock-tier ROI, WP calibration,
            # engine performance, sport perf, market perf, learned
            # fusion weights.
            #
            # Uses only REAL data — never synthesises training rows.
            # Errors NEVER halt the loop.
            try:
                if is_full:
                    from datetime import datetime as _dt2, timezone as _tz2
                    from services.adaptive_learning import (
                        run_daily_learning_job,
                    )
                    today_utc = _dt2.now(_tz2.utc).strftime("%Y-%m-%d")
                    already = await db["learning_snapshots"].find_one(
                        {"snapshot_date": today_utc},
                        {"_id": 0, "id": 1},
                    )
                    if not already:
                        lj = await run_daily_learning_job(
                            db, days=60, persist=True,
                        )
                        n_tiers = len((lj.get("lock_tier_performance")
                                        or {}).get("buckets") or [])
                        n_engines = len((lj.get("engine_performance")
                                          or {}).get("engines") or [])
                        n_sports = len(lj.get("sport_performance") or [])
                        logger.info(
                            "Daily learning snapshot [%s]: tiers=%d "
                            "engines=%d sports=%d errors=%d",
                            lj.get("snapshot_id"), n_tiers, n_engines,
                            n_sports, len(lj.get("errors") or []),
                        )
            except Exception as e:
                logger.warning("Daily learning job error: %s", e)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("Settlement loop error: %s", e)
        await asyncio.sleep(60)  # 60 seconds — MLB grades within 1 min of game-end


async def _weekly_model_tuning_loop():
    """Once a week, recompute learned weights from the FULL settled-pick
    dataset and reseed the model. This is the user-requested weekly model
    adjustment — the per-settlement recompute already updates incrementally,
    but the weekly run also re-applies fresh weights to today's open picks.
    """
    await asyncio.sleep(180)  # let startup settle
    SEVEN_DAYS = 7 * 24 * 3600
    while True:
        try:
            from learning_engine import recompute_learned_weights, apply_learning
            weights = await recompute_learned_weights(db)
            # Re-apply to all open picks for the next 7 days.
            cursor = db.picks.find(
                {"status": {"$in": [None, "pending"]}},
                {"_id": 0},
            )
            adjusted = 0
            async for p in cursor:
                before = p.get("win_probability")
                await apply_learning(db, p)
                if p.get("learning") and p.get("win_probability") != before:
                    adjusted += 1
                    # Re-apply the bet-quality floor + recompute grade /
                    # confidence so the persisted record stays coherent.
                    # Without this, `apply_learning` mutated `lock_score`
                    # while `grade` stayed at its pre-tuning value — which
                    # caused Lock-90+ picks to display the legacy/stale
                    # "Pass" badge until the next 30-min validator cycle.
                    try:
                        from sports_engine import _grade, _confidence
                        wp_v = float(p.get("win_probability") or 0)
                        ed_v = float(p.get("edge_percent") or 0)
                        cur_lock = float(p.get("lock_score") or 0)
                        # Step-function floor (matches sports_engine spec)
                        floor = 0.0
                        if wp_v >= 80.0 and ed_v >= 15.0: floor = 98.0
                        elif wp_v >= 75.0 and ed_v >= 10.0: floor = 95.0
                        elif wp_v >= 70.0 and ed_v >= 5.0:  floor = 90.0
                        elif wp_v >= 65.0 and ed_v >= 3.0:  floor = 85.0
                        new_lock = min(99.0, max(cur_lock, floor))
                        p["lock_score"] = round(new_lock, 1)
                        p["grade"] = _grade(new_lock)
                        p["confidence"] = _confidence(new_lock)
                    except Exception:
                        pass
                    await db.picks.update_one(
                        {"id": p["id"]},
                        {"$set": {"win_probability": p["win_probability"],
                                   "lock_score": p.get("lock_score"),
                                   "edge_percent": p.get("edge_percent"),
                                   "implied_probability": p.get("implied_probability"),
                                   "grade": p.get("grade"),
                                   "confidence": p.get("confidence"),
                                   "learning": p.get("learning")}},
                    )
            active = sum(1 for b in weights.get("buckets", []) if b.get("active"))
            logger.info("Weekly model tuning: %d active buckets, %d picks re-weighted",
                        active, adjusted)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("Weekly tuning failed: %s", e)
        await asyncio.sleep(SEVEN_DAYS)


async def _historical_props_loop():
    """Nightly recompute of player-prop hit-rates (L5/L10/L20/season)
    derived from `player_game_logs`. Cheap pure-DB pipeline — safe to run
    daily even at full DB size.

    Sleeps ~5 minutes after startup to let the boot dust settle, then
    rebuilds the entire `props_history` snapshot once every 12h. We don't
    chase wall-clock 4am here because the work is sport-agnostic and the
    cost is bounded.
    """
    await asyncio.sleep(300)  # let startup settle
    TWELVE_HOURS = 12 * 3600
    while True:
        try:
            from historical.props_engine import recompute_all_props
            summary = await recompute_all_props(db)
            logger.info("Player props recompute: %s", summary)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("Player props recompute failed: %s", e)
        await asyncio.sleep(TWELVE_HOURS)


@app.on_event("startup")
async def on_startup():
    # ── DEFERRED STARTUP (2026-06-28) ─────────────────────────────────
    # In production (emergent.host), all 20+ background loops fired at
    # T+0 and ran concurrently in a single worker, blocking HTTP
    # requests for 8+ seconds and triggering Cloudflare 520s. We now
    # stagger the heavy loops with `_defer(n)` helper — each sleeps n
    # seconds before doing its first heavy fetch, spreading CPU load
    # over the first ~2 minutes of boot. The HTTP server can serve
    # /api/version, /api/auth/login, /api/picks/today from second 1.
    # User report (2026-06-28): "Seems like server keeps going down
    # on expo go app" → preview returns /api/version in 0.29s while
    # the deployed origin took 8.4s.
    # `STARTUP_DEFER_SECONDS` env var lets you tune the base delay
    # without redeploying (defaults to 8 — safe across all envs).
    DEFER_BASE = float(os.environ.get("STARTUP_DEFER_SECONDS", "8"))

    def _deferred_task(coro_factory, delay: float):
        """Schedule `coro_factory()` to run after a `delay` second sleep.
        `coro_factory` is a callable returning a fresh coroutine (we
        accept a callable rather than the coroutine itself so it isn't
        instantiated until we're ready to execute it — avoids the
        'coroutine was never awaited' warning on shutdown)."""
        async def _runner():
            try:
                await asyncio.sleep(delay)
                await coro_factory()
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning("Deferred startup task failed (delay=%.1fs): %s", delay, e)
        return asyncio.create_task(_runner())

    await db.users.create_index("email", unique=True)
    await db.picks.create_index([("pick_date", 1), ("sport", 1)])
    await db.picks.create_index([("pick_date", 1), ("lock_score", -1)])
    await db.picks.create_index([("status", 1), ("settled_at", -1)])
    await db.picks.create_index("id", unique=True)
    # ── Signal-rank index (2026-07-18) ────────────────────────────
    # `signal_score` is queried in the `min_signal` filter on every
    # /picks/today request. Without a supporting index it fell back
    # to a full-collection scan on days with >500 picks, contributing
    # to the 12+s p99 that Expo Go users experienced as "connection
    # hiccup". Compound with `pick_date` because the filter is
    # always scoped to today's slate.
    await db.picks.create_index([("pick_date", 1), ("signal_score", -1)])

    # ── 2026-07-29 Fusion Predictions indexes ────────────────────────
    # Grading loop scans by (actual_value=None, pick_id set, created_at)
    # every 15 min. UI lazy-fetch reads by prediction_id.
    try:
        await db.fusion_predictions.create_index(
            [("actual_value", 1), ("created_at", -1)],
            name="fusion_grading_idx",
        )
        await db.fusion_predictions.create_index(
            "prediction_id", unique=True, name="fusion_pid_idx",
        )
        await db.fusion_predictions.create_index(
            [("pick_id", 1), ("created_at", -1)],
            name="fusion_pick_idx",
        )
        # Audit follow-up (2026-07-29): standalone `pick_id` +
        # `pick_date` indexes for the analytics + lazy-enrichment paths.
        # Compound `pick_id`+`created_at` above already covers pick_id
        # equality, but a plain single-key index keeps EXPLAIN plans
        # simpler for read-mostly aggregators and is cheap.
        await db.fusion_predictions.create_index(
            "pick_date", name="fusion_pick_date_idx",
        )
    except Exception as _fpi_err:
        logger.warning("fusion_predictions index skipped: %s", _fpi_err)

    # ── 2026-07-29 Learning log index (audit follow-up) ──────────────
    # Analytics dashboard reads with .sort("ts", -1).limit(30) on a
    # 35k-row collection every request. Add a descending index so the
    # sort is a single index scan instead of an in-memory sort.
    try:
        await db.learning_log.create_index(
            [("ts", -1)], name="learning_log_ts_idx",
        )
    except Exception as _lli_err:
        logger.warning("learning_log index skipped: %s", _lli_err)

    # ── 2026-07-29 Learning snapshots index ──────────────────────────
    # The daily job upserts one row per UTC day; the analytics endpoint
    # fetches the latest by `generated_at` DESC. Also index by
    # `snapshot_date` for the once-per-day gate in `_settlement_loop`.
    try:
        await db.learning_snapshots.create_index(
            [("generated_at", -1)], name="learning_generated_idx",
        )
        await db.learning_snapshots.create_index(
            "snapshot_date", unique=True, name="learning_date_idx",
        )
    except Exception as _lsi_err:
        logger.warning("learning_snapshots index skipped: %s", _lsi_err)

    # ── 2026-07-28 DEFECT #5 — no_bet schema invariant at startup ────
    # Sweep any legacy rows where `no_bet_reason` is set but `no_bet`
    # is False (crash-corruption or pre-helper writes), and install a
    # best-effort MongoDB $jsonSchema validator so future writes that
    # break the invariant get logged. Non-blocking — errors don't
    # halt boot.
    try:
        await _enforce_no_bet_schema_invariant()
    except Exception as _inv_err:
        logger.warning(
            "no_bet schema invariant enforcement failed at startup: %s",
            _inv_err,
        )

    # ── Warm the signal-rank cache at boot (2026-07-18) ───────────
    # Every backend restart previously left `_LAST_RUN` empty, so the
    # very first /picks/today request paid a 3-5s sync ranking cost.
    # Kick off a background rank refresh here so by the time the
    # first user hits the endpoint, the ranks are already fresh and
    # the request path is fully non-blocking.
    _deferred_task(
        lambda: (
            __import__("services.signal_engine", fromlist=["refresh_slate_signal_rank"])
            .refresh_slate_signal_rank(db, _today_str())
        ),
        delay=2.0,
    )

    # PRODUCTION HANG FIX 2026-07-15: index the on-read Soccer form
    # enrichment's lookup path (`soccer_matches.home_team` /
    # `.away_team` with an anchored regex + status filter). Without
    # these, /api/picks/today does a full-collection scan per team
    # per pick which on a large production dataset takes >100s and
    # returns 504 Gateway Timeout. Idempotent — Motor will no-op if
    # the index already exists.
    try:
        await db.soccer_matches.create_index("home_team")
        await db.soccer_matches.create_index("away_team")
        await db.soccer_matches.create_index([("status", 1), ("date", -1)])
    except Exception as e:
        logger.debug("soccer_matches index creation skipped: %s", e)
    # ── Historical Sports Intelligence Engine — wire DB handles ───────────
    # Read side (lookup) is called from the Lock Engine on every pick gen, so
    # we MUST set the db handle before any pick generation kicks off. Write
    # side (orchestrator) is used by the /api/admin/historical/* endpoints.
    try:
        from historical.orchestrator import _set_db as _hist_set_db
        from historical.lookup import _set_db as _hist_lookup_set_db
        _hist_set_db(db)
        _hist_lookup_set_db(db)
        # Indices for the historical collections (created lazily on first
        # insert by Motor, but we add the hot-path ones explicitly).
        await db.players.create_index([("sport", 1), ("name", 1)])
        await db.players.create_index([("player_id", 1), ("sport", 1)], unique=True)
        await db.games.create_index([("game_id", 1), ("sport", 1)], unique=True)
        await db.player_game_logs.create_index([("player_id", 1), ("date", -1)])
        # Composite index — NFL stores multiple rows per (player, game) keyed
        # by stat_block (passing/rushing/receiving). Other sports have one
        # row per (player, game). Non-unique to keep all sports working.
        await db.player_game_logs.create_index([("player_id", 1), ("game_id", 1), ("stat_block", 1)])
        await db.season_totals.create_index([("player_id", 1), ("sport", 1), ("season", 1), ("competition", 1)])
        await db.team_form.create_index([("team_id", 1), ("sport", 1)])
        # ── Multi-Season Ingestion (Phase 1 of historical props pipeline) ──
        # Tracks per-(sport, season) ingest state so backfills are resumable
        # and the admin status endpoint can show progress at a glance.
        await db.historical_ingestion_state.create_index([("sport", 1), ("season", -1)])
        # Player props derivation history (recomputed nightly from logs).
        await db.props_history.create_index([("sport", 1), ("updated_at", -1)])
        await db.player_game_logs.create_index([("sport", 1), ("date", -1)])
        logger.info("Historical Sports Intelligence Engine wired to MongoDB")
    except Exception as e:
        logger.warning("Historical Engine not armed: %s", e)
    # ── Background cron: nightly multi-season props recompute ──
    # Re-derives prop hit-rates (L5/L10/L20/season) for every player with
    # logs. Cheap (pure DB aggregate, no HTTP) so we can run daily.
    # ── BACKGROUND LOOP SCHEDULING (DEFERRED) ────────────────────────
    # The first few seconds after boot are the most fragile in
    # production. We schedule each loop with a staggered delay so the
    # HTTP server can bind, the resilience middleware can warm up, and
    # the first /api/* requests can be served BEFORE the heavy
    # enrichment + ingest pipelines start hammering CPU.
    #
    #   T+ 0 — lightweight maintenance loops (historical props, refresh)
    #   T+ 8 — MLB pregame loop, settlement
    #   T+16 — weekly model tuning
    #   T+24 — soccer pipeline + backfill
    #   T+32 — soccer player form (Understat)
    #   T+40 — MLB lineup verifier, NRFI/YRFI
    #   T+48 — Player DB refreshers
    #   T+56 — Services loop, tennis player DB
    #   T+64 — Line observer / closing snapshotter (line shopping)
    asyncio.create_task(_historical_props_loop())
    asyncio.create_task(_daily_refresh_loop())
    # ── ESPN Soccer Fixture Fallback (iter-97) ─────────────────────
    # Pulls upcoming fixtures + moneyline picks for the 4 lower-tier
    # soccer leagues (CSL, Sweden, Norway, Finland) from ESPN's public
    # scoreboards while The Odds API subscription is unavailable
    # (currently returning 401). Try/except so a startup failure
    # doesn't break the rest of the backend.
    try:
        from services.espn_soccer_fixtures import refresh_loop as _espn_soccer_loop
        _deferred_task(_espn_soccer_loop, DEFER_BASE * 1)
        logger.info("ESPN soccer fixture fallback loop armed")
    except Exception as _e:
        logger.warning("Failed to arm ESPN soccer fixture fallback: %s", _e)
    _deferred_task(_mlb_pregame_loop,                       DEFER_BASE * 1)
    logger.info(
        "MLB pregame quick-refresh loop armed (%d-sec cadence during UTC %02d:00–%02d:00)",
        _MLB_QUICK_REFRESH_INTERVAL,
        _MLB_WINDOW_START_UTC_HOUR, _MLB_WINDOW_END_UTC_HOUR,
    )
    # ── 2026-07-28 late-night one-shot MLB refresh ────────────────────
    # If the server boots between 23:00–03:00 UTC (i.e. late West Coast
    # first-pitch window), fire an immediate MLB refresh so the current
    # slate populates without waiting for the 5-min loop cadence.
    try:
        _boot_hour = datetime.now(timezone.utc).hour
        if _boot_hour >= 23 or _boot_hour < _MLB_WINDOW_END_UTC_HOUR:
            async def _mlb_late_night_boot_refresh():
                try:
                    logger.info(
                        "MLB late-night boot refresh firing (UTC hour=%02d, "
                        "inside 23:00–%02d:00 window)",
                        _boot_hour, _MLB_WINDOW_END_UTC_HOUR,
                    )
                    await _refresh_picks(_today_str(), sport_filter="MLB")
                except Exception as _bn_err:
                    logger.warning(
                        "MLB late-night boot refresh failed: %s", _bn_err,
                    )
            _deferred_task(_mlb_late_night_boot_refresh, DEFER_BASE * 1)
            logger.info(
                "MLB late-night one-shot boot refresh armed (UTC hour=%02d)",
                _boot_hour,
            )
    except Exception as _lnb_err:
        logger.warning("MLB late-night boot check failed: %s", _lnb_err)
    _deferred_task(_settlement_loop,                        DEFER_BASE * 1)
    _deferred_task(_weekly_model_tuning_loop,               DEFER_BASE * 2)
    # Soccer module: pregame pipeline every 15 min (user choice 3A — no
    # live loop). Try/except so a soccer init failure doesn't break the
    # rest of the backend startup.
    try:
        from soccer.pipeline import soccer_pipeline_loop
        from soccer.backfill import soccer_backfill_loop
        # Useful indices for the new soccer collections.
        await db.soccer_predictions.create_index("id", unique=True)
        await db.soccer_predictions.create_index([("created_at", -1)])
        await db.soccer_predictions.create_index("fixture_id")
        await db.soccer_predictions.create_index("correct")
        await db.soccer_accuracy.create_index("_id")
        _deferred_task(lambda: soccer_pipeline_loop(db),     DEFER_BASE * 3)
        _deferred_task(lambda: soccer_backfill_loop(db),     DEFER_BASE * 3)
        logger.info("Soccer pipeline scheduler armed (15-min pregame loop + 24h backfill loop)")
    except Exception as e:
        logger.warning("Soccer pipeline scheduler not armed: %s", e)
    # ── UEFA ESPN fallback ingest (Champions/Europa/Conference) ─────
    # Our primary sources (The Odds API + football-data.org) don't carry
    # UEFA qualification rounds until close to kickoff. ESPN's public
    # scoreboard has full DraftKings pricing days out — we pull from
    # there so the app shows the same fixtures Linemate does.
    try:
        from uefa_espn_ingest import uefa_espn_loop
        _deferred_task(lambda: uefa_espn_loop(db),           DEFER_BASE * 3)
        logger.info("UEFA ESPN fallback ingest armed (30-min loop, 7-day window)")
    except Exception as e:
        logger.warning("UEFA ESPN ingest not armed: %s", e)
    # ── UFC ESPN fallback ingest ────────────────────────────────────
    # ESPN carries UFC/PFL/Bellator cards 3+ weeks out with fighter
    # records; picks emit as fair-odds until DK posts markets.
    try:
        from ufc_espn_ingest import ufc_espn_loop
        _deferred_task(lambda: ufc_espn_loop(db),            DEFER_BASE * 3)
        logger.info("UFC ESPN fallback ingest armed (60-min loop, 21-day window)")
    except Exception as e:
        logger.warning("UFC ESPN ingest not armed: %s", e)
    # ── Soccer Hot Scorers (Wikipedia-driven, bypasses book gaps) ───
    # Emits Anytime-Goal-Scorer picks for niche-league top scorers
    # (Allsvenskan, Eliteserien, etc.) that sportsbooks don't cover.
    try:
        from soccer_hot_scorers import hot_scorers_loop
        _deferred_task(lambda: hot_scorers_loop(db),         DEFER_BASE * 3)
        logger.info("Soccer Hot Scorers armed (4h loop, Wikipedia-driven)")
    except Exception as e:
        logger.warning("Soccer Hot Scorers not armed: %s", e)
    # ── ESPN team meta + injury notes (all sports) ──────────────────
    # Powers logo/color rendering on pick cards and the injury chip.
    # Runs once at boot then every 6 hours.
    try:
        from services.espn_team_meta import refresh_all_teams
        from services.espn_injury_notes import refresh_all_injuries
        await db.espn_team_meta.create_index([("norm_name", 1), ("sport", 1)], unique=True)
        await db.espn_team_meta.create_index("aliases")
        await db.espn_injury_notes.create_index([("sport", 1), ("team_norm", 1)])

        async def _espn_meta_loop():
            await asyncio.sleep(30)
            while True:
                try:
                    from services.espn_form_cache import refresh_all_forms
                    from services.wikipedia_team_record import bulk_refresh_soccer
                    await refresh_all_teams(db)
                    await refresh_all_injuries(db)
                    await refresh_all_forms(db)
                    # Wikipedia deep-history — throttled to daily
                    await bulk_refresh_soccer(db, limit_teams=250)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.warning("ESPN meta/injury/form/wiki refresh error: %s", e)
                await asyncio.sleep(6 * 60 * 60)  # 6h cadence

        _deferred_task(_espn_meta_loop, DEFER_BASE * 3)
        logger.info("ESPN team-meta + injury-notes refresh armed (6h loop)")
    except Exception as e:
        logger.warning("ESPN meta refresh not armed: %s", e)
    # ── Soccer Player Form (Understat) ──────────────────────────────
    # Refreshes per-player season stats (xG, npxG, goals/xG ratio) for
    # the Top 5 European leagues every 12h. Powers the HOT FORM /
    # COLD chip on goalscorer cards + the ±6% form lift in the
    # probability engine.
    try:
        from soccer_player_form import soccer_player_form_loop
        await db.soccer_player_form.create_index("name_canonical")
        await db.soccer_player_form.create_index([("league", 1), ("season", 1)])
        await db.soccer_player_form.create_index([("updated_at", -1)])
        _deferred_task(lambda: soccer_player_form_loop(db),  DEFER_BASE * 4)
        logger.info("Soccer Player Form (Understat) armed (12h loop, Top 5 leagues)")
    except Exception as e:
        logger.warning("Soccer Player Form scheduler not armed: %s", e)
    # ── MLB Lineup Verifier ─────────────────────────────────────────
    # Voids picks for scratched MLB players ~30 min before first pitch.
    try:
        from mlb_lineup import lineup_verifier_loop
        _deferred_task(lambda: lineup_verifier_loop(db, _today_str), DEFER_BASE * 5)
        logger.info("MLB lineup verifier armed (5-min loop, 30-min pre-game)")
    except Exception as e:
        logger.warning("MLB lineup verifier failed to start: %s", e)
    # ── NRFI / YRFI 1st-Inning Picks ────────────────────────────────
    # Poisson model on free MLB Stats API data — generates one
    # NRFI or YRFI pick per game when edge >= 4% over fair.
    try:
        from brain.nrfi_engine import nrfi_yrfi_loop
        _deferred_task(lambda: nrfi_yrfi_loop(db),           DEFER_BASE * 5)
        logger.info("NRFI/YRFI pick generator armed (30-min loop during pregame)")
    except Exception as e:
        logger.warning("NRFI/YRFI loop failed to start: %s", e)
    # ── MLB Free Player Database (replaces SportsDataIO for MLB) ────
    # Daily nightly refresh of the free MLB Stats API roster + season
    # stats + injuries. Replaces ~80% of the SportsDataIO surface for
    # MLB at zero quota cost. Runs at 09:00 UTC (well after the daily
    # slate finishes & before the morning pre-game window).
    try:
        from player_db.ingestors.mlb_stats_api import refresh_all as _mlb_player_db_refresh
        async def _mlb_player_db_loop() -> None:
            # Cold-start refresh ~30 sec after boot so it doesn't race
            # the rest of the startup work, then daily after that.
            await asyncio.sleep(30)
            while True:
                try:
                    summary = await _mlb_player_db_refresh(db)
                    logger.info("MLB player_db nightly refresh: %s", summary)
                except Exception as e:
                    logger.warning("MLB player_db refresh failed: %s", e)
                await asyncio.sleep(24 * 60 * 60)
        asyncio.create_task(_mlb_player_db_loop())
        logger.info("MLB player_db (free MLB Stats API) armed — daily roster + stats refresh")
    except Exception as e:
        logger.warning("MLB player_db loop failed to start: %s", e)
    # ── NBA + NFL + CFB Free Player Database (Phase 2/4, ESPN public) ─
    # Daily refresh from ESPN's public site.api endpoints.
    #   NBA = 30 teams × ~17 players ≈ 600 active
    #   NFL = 32 teams × ~90 players ≈ 2,900 active (incl. practice squads)
    #   CFB = 130+ FBS teams × ~95 players ≈ 12k+ players (heavier — runs
    #         daily but takes ~3 min vs <15s for NBA/NFL)
    try:
        from player_db.ingestors.espn_public import refresh_nba, refresh_nfl, refresh_cfb
        async def _espn_player_db_loop() -> None:
            await asyncio.sleep(60)
            while True:
                for sport_name, fn in (
                    ("NBA", refresh_nba),
                    ("NFL", refresh_nfl),
                    ("CFB", refresh_cfb),
                ):
                    try:
                        summary = await fn(db)
                        logger.info("%s player_db nightly refresh: %s", sport_name, summary)
                    except Exception as e:
                        logger.warning("%s player_db refresh failed: %s", sport_name, e)
                    # Stagger leagues by 5s so we don't double-tax ESPN.
                    await asyncio.sleep(5)
                await asyncio.sleep(24 * 60 * 60)
        asyncio.create_task(_espn_player_db_loop())
        logger.info("NBA + NFL + CFB player_db (free ESPN public) armed — daily roster + stats + injuries refresh")
    except Exception as e:
        logger.warning("NBA/NFL/CFB player_db loop failed to start: %s", e)

    # ── ESPN MLS scorer leaderboard ingest (2026-07-22) ──────────────
    # Scrapes ESPN's public MLS scoring + assists tables into
    # `espn_mls_stats` collection. Used by `mls_scorer_gate` to
    # hard-gate MLS Anytime Goal Scorer / Score-or-Assist / First Goal
    # Scorer picks so reserves (Malachi Jones, Chase Adams, Seymour
    # Reid) can't surface as Elite Locks over real starters (Messi,
    # Mercau, Rossi, Cuypers, etc.). Refresh every 12h.
    try:
        from services.espn_mls_stats import refresh_mls_leaders, load_gate_snapshot
        from services.mls_scorer_gate import apply_espn_snapshot
        async def _mls_stats_loop() -> None:
            await asyncio.sleep(15)   # let boot settle
            while True:
                try:
                    summary = await refresh_mls_leaders(season=2025)
                    logger.info("ESPN MLS stats refresh: %s", summary)
                    by, names = await load_gate_snapshot()
                    apply_espn_snapshot(by, names)
                    logger.info(
                        "MLS scorer gate hydrated: %d players from ESPN", len(names),
                    )
                except Exception as e:
                    logger.warning("ESPN MLS stats refresh failed: %s", e)
                await asyncio.sleep(12 * 60 * 60)   # 12h
        asyncio.create_task(_mls_stats_loop())
        logger.info("ESPN MLS scorer stats armed — 12h refresh loop")
    except Exception as e:
        logger.warning("ESPN MLS stats loop failed to start: %s", e)

    # ── MLS player-vs-opponent matchup history (2026-07-22) ─────────
    # User request: "Also should pick up per-opponent scoring history"
    # (screenshot: Messi 7G vs Nashville, Surridge brace vs Charlotte).
    # Ingests every top-scorer's game log from ESPN core API and
    # aggregates per-opponent goals/assists. Used as a factor boost in
    # the pick engine so we upweight bets where the player has strong
    # scoring history vs the specific opponent tonight.
    try:
        from services.mls_player_matchup_history import refresh_all_top_scorers
        async def _mls_matchup_loop() -> None:
            # First run 90s after boot so espn_mls_stats has hydrated.
            await asyncio.sleep(90)
            while True:
                try:
                    summary = await refresh_all_top_scorers()
                    logger.info("MLS matchup history refresh: %s", summary)
                except Exception as e:
                    logger.warning("MLS matchup history refresh failed: %s", e)
                await asyncio.sleep(7 * 24 * 60 * 60)   # weekly
        asyncio.create_task(_mls_matchup_loop())
        logger.info("MLS matchup history armed — weekly refresh loop")
    except Exception as e:
        logger.warning("MLS matchup history loop failed to start: %s", e)

    # ── MLS Direct-Inject worker (2026-07-22) ──────────────────────
    # Bypasses the entire pick pipeline (chalk trap, longshot trap,
    # starter-gate, correlated dedupe, board validator) because those
    # layers keep killing MLS scorer picks (Surridge, Bouanga, Messi,
    # etc.). Writes picks straight to db.picks every 15 minutes.
    try:
        from services.mls_direct_inject import loop as _mls_direct_loop
        asyncio.create_task(_mls_direct_loop())
        logger.info("MLS Direct-Inject worker armed — 15min refresh loop")
    except Exception as e:
        logger.warning("MLS Direct-Inject worker failed to start: %s", e)

    # ── Soccer Prop Inject (Big-5 + UCL, 2026-07-22) ───────────────
    # Extends the Player Prop Intelligence System to EPL / La Liga /
    # Serie A / Bundesliga / Ligue 1 / UCL. Pulls candidates from
    # `soccer_player_form` (Understat) and runs the archetype + market
    # selector for each event on the board.
    try:
        from services.soccer_prop_inject import loop as _soccer_prop_loop
        asyncio.create_task(_soccer_prop_loop())
        logger.info("Soccer Prop Inject worker armed — Big-5+UCL, 15min loop")
    except Exception as e:
        logger.warning("Soccer Prop Inject worker failed to start: %s", e)

    # ── CSL ESPN Live (retired-player filter, user-requested 2026-06-27) ─
    # ESPN's free public soccer endpoints provide the authoritative ACTIVE
    # roster + current-season top scorers for the Chinese Super League.
    # We use this to BLOCK retired / transferred-out players (e.g. Guy
    # Mbenza) from ever landing on a goalscorer pick. Refreshes every 12h.
    try:
        import csl_espn_live
        # Hydrate from MongoDB FIRST so the synth scorer pipeline sees
        # last-known data before the first network refresh completes.
        await csl_espn_live.hydrate_from_db(db)
        # Kick off background refresh loop (idempotent).
        csl_espn_live.arm_scheduler(db)
        logger.info("CSL ESPN Live (free public ESPN) armed — 12h cadence, blocks retired players")
    except Exception as e:
        logger.warning("CSL ESPN Live loop failed to start: %s", e)

    # ── Unified `services/` ingestion layer (user-requested 2026-06-27) ─
    # Free-only multi-source player registry. ESPN public is the
    # always-on primary; Basketball-Reference + nfl.com layer on as
    # enrichment when reachable; nba.com/stats + PFR are best-effort
    # (datacenter-blocked today but light up behind residential proxy).
    # Every source funnels into `services.active_registry` which then
    # answers `is_active(sport, name)` for the picks pipeline.
    try:
        from services import active_registry as _registry, nba_ingest, nfl_ingest, soccer_ingest, cfb_ingest
        await _registry.hydrate_from_db(db)

        async def _services_loop():
            await asyncio.sleep(30)   # tiny grace period for startup
            await asyncio.gather(
                nba_ingest.loop(db),
                nfl_ingest.loop(db),
                soccer_ingest.loop(db),
                cfb_ingest.loop(db),
            )
        asyncio.create_task(_services_loop())
        logger.info(
            "services/ multi-source ingestion armed — NBA (ESPN+BBR+nba.com) + "
            "NFL (ESPN+nfl.com) + Soccer (Understat + ESPN 18 leagues) + "
            "CFB (CollegeFootballData) every 24h"
        )
    except Exception as e:
        logger.warning("services/ ingestion layer failed to start: %s", e)
    # ── Tennis ATP Free Player Database (Phase 3, Sackmann mirror) ─
    # Bulk-load 10y of ATP match data from the TML-Database mirror
    # (Sackmann format). One full refresh per week (data upstream
    # updates ~weekly after each tournament concludes). Cold-start
    # 180 sec post-boot so it doesn't compete with NBA/NFL loops.
    # Also includes WTA via ESPN rankings (Phase 3.5).
    try:
        from player_db.ingestors.tennis_sackmann import refresh_atp
        from player_db.ingestors.espn_public import refresh_wta
        async def _tennis_player_db_loop() -> None:
            await asyncio.sleep(180)
            while True:
                try:
                    summary = await refresh_atp(db, years=10)
                    logger.info("Tennis ATP player_db weekly refresh: %s", summary)
                except Exception as e:
                    logger.warning("Tennis ATP player_db refresh failed: %s", e)
                try:
                    wta_summary = await refresh_wta(db)
                    logger.info("Tennis WTA player_db weekly refresh: %s", wta_summary)
                except Exception as e:
                    logger.warning("Tennis WTA player_db refresh failed: %s", e)
                await asyncio.sleep(7 * 24 * 60 * 60)
        asyncio.create_task(_tennis_player_db_loop())
        logger.info("Tennis ATP + WTA player_db armed — weekly refresh (Sackmann + ESPN)")
    except Exception as e:
        logger.warning("Tennis player_db loop failed to start: %s", e)
    # ── Tennis Extra Settler ────────────────────────────────────────
    # Settles Mallorca/Eastbourne/Challenger picks from TennisExplorer
    # results page. Runs every 30 min, walks back 3 days.
    try:
        from tennis_extra.settle import tennis_extra_settler_loop
        _deferred_task(lambda: tennis_extra_settler_loop(db), DEFER_BASE * 6)
        logger.info("Tennis Extra settler armed (30-min loop)")
    except Exception as e:
        logger.warning("Tennis Extra settler failed to start: %s", e)

    # ── Grading Validator (permanent history-accuracy guardrail 2026-07-13) ──
    # Cross-checks recently-graded soccer goalscorer picks against
    # FotMob (independent source). On disagreement, reopens the pick
    # for re-settlement with the fixed logic. This is the self-healing
    # loop that catches grading regressions the moment they happen
    # instead of days later when a user notices.
    try:
        from grading_validator import grading_validator_loop
        _deferred_task(lambda: grading_validator_loop(db), DEFER_BASE * 7)
        logger.info("Grading Validator armed (1-hour loop, FotMob cross-check)")
    except Exception as e:
        logger.warning("Grading Validator failed to start: %s", e)

    # ── Stuck-Pick Reaper (permanent history guardrail 2026-07-13) ──
    # Voids any pick left as `pending` (or with a missing status field)
    # >48h after its event_time. Prevents any settler bug / name-match
    # gap / silent failure from ever leaving picks stuck in limbo where
    # they neither appear on the board (event has passed) nor in
    # History (settlement never completed). Runs every 30 min.
    try:
        from stuck_pick_reaper import stuck_pick_reaper_loop
        _deferred_task(lambda: stuck_pick_reaper_loop(db), DEFER_BASE * 6)
        logger.info("Stuck-Pick Reaper armed (30-min loop, 48h stale threshold)")
    except Exception as e:
        logger.warning("Stuck-Pick Reaper failed to start: %s", e)
    # ── Tennis Fair-Odds Engine ─────────────────────────────────────
    # Elo + surface + form + fatigue for matches without book odds.
    try:
        from tennis_extra.odds_engine import set_db as _te_odds_set_db
        _te_odds_set_db(db)
        await db.tennis_players.create_index("name_norm", unique=True)
        logger.info("Tennis fair-odds engine wired to MongoDB")
    except Exception as e:
        logger.warning("Tennis fair-odds engine not armed: %s", e)
    # ── Parlay History (Save-on-Tap) ────────────────────────────────
    try:
        await db.parlay_history.create_index("id", unique=True)
        await db.parlay_history.create_index([("user_id", 1), ("created_at", -1)])
        await db.parlay_history.create_index([("user_id", 1), ("status", 1)])
        logger.info("Parlay History indices ready")
    except Exception as e:
        logger.warning("Parlay History setup failed: %s", e)
    # ── Lock-Score Calibration Engine ───────────────────────────────
    # Loads the persisted isotonic-regression curve (or fits one from
    # the historical settled-pick set if no curve exists yet). The
    # curve maps the raw model lock_score → calibrated probability so
    # the displayed Elite/Premium/Strong bands actually match the
    # observed hit rates.
    try:
        from lock_calibration import load_curve as _calib_load
        await _calib_load(db)
    except Exception as e:
        logger.warning("Calibration curve load failed: %s", e)

    # Seed/promote the platform owner as admin.
    # ── SEC-001 (2026-06-26): hardened against email-squat takeover ──
    #
    # Previously the owner email was HARD-CODED and promoted on every
    # boot regardless of whether the actual owner had registered yet.
    # On a fresh DB / re-deploy / fork an attacker who registered the
    # owner email first would be auto-granted full admin. Two-layer fix:
    #
    #   1. Source the owner email from `ADMIN_OWNER_EMAIL` env var
    #      (falls back to the legacy hardcoded value for back-compat
    #      with existing deployments — but operators can rotate it).
    #
    #   2. Require the existing account to have `email_verified=True`
    #      OR `created_at` older than 24h before granting admin. This
    #      blocks the "register-the-owner-email-on-a-fresh-DB" attack:
    #      a fresh account can't auto-promote until it's been live for
    #      a full day, giving the real owner time to claim the email.
    #      A `created_at` check is used because email-verification
    #      isn't yet implemented end-to-end — once it is, swap to
    #      the stricter `email_verified` check.
    try:
        from datetime import timedelta
        OWNER_EMAIL = os.environ.get(
            "ADMIN_OWNER_EMAIL", "bossmanperkins@yahoo.com"
        ).strip().lower()
        if OWNER_EMAIL:
            existing = await db.users.find_one({"email": OWNER_EMAIL})
            if existing:
                # Parse created_at (stored as ISO string or datetime)
                ca = existing.get("created_at")
                if isinstance(ca, str):
                    try:
                        ca = datetime.fromisoformat(ca.replace("Z", "+00:00"))
                    except Exception:
                        ca = None
                now = datetime.now(timezone.utc)
                aged_24h = bool(
                    ca and (now - ca) > timedelta(hours=24)
                )
                verified = bool(existing.get("email_verified"))
                if verified or aged_24h:
                    await db.users.update_one(
                        {"email": OWNER_EMAIL},
                        {"$set": {"role": "admin", "status": "active"}},
                    )
                    logger.info(
                        "Owner admin promotion: %s (verified=%s, aged_24h=%s)",
                        OWNER_EMAIL, verified, aged_24h,
                    )
                else:
                    logger.warning(
                        "Owner admin promotion SKIPPED for %s — account is <24h old "
                        "and email not verified. Blocks fresh-DB email-squat takeover. "
                        "Account will auto-promote in 24h or once verified.",
                        OWNER_EMAIL,
                    )
    except Exception as e:
        logger.warning("Owner admin promotion failed: %s", e)

    # Index for the API-usage tracker — fast top-N lookups.
    try:
        await db.user_activity.create_index([("api_calls", -1)])
        await db.user_activity.create_index("user_id", unique=True)
    except Exception:
        pass
    # Closing-line snapshotter + line-history observer (CLV fix 2026-06-25).
    # Two independent background loops that finally populate `closing_odds`
    # with REAL closing prices so the analytics page's CLV column stops
    # reading zero.
    try:
        from closing_line_snapshotter import (
            line_observer_loop, closing_snapshotter_loop,
        )
        _deferred_task(lambda: line_observer_loop(db),       DEFER_BASE * 7)
        _deferred_task(lambda: closing_snapshotter_loop(db), DEFER_BASE * 7)
        logger.info("Closing-line snapshotter started (CLV tracking enabled)")
    except Exception as e:
        logger.warning("CLV snapshotter failed to start: %s", e)

    # Phase 1.1 — Baseball Savant Statcast daily refresh loop. Cheap
    # (~50KB × 3 endpoints = 150KB per refresh) and infrequent (1×/24h
    # is enough — Baseball Savant updates the leaderboard once daily
    # after all box scores are finalised). Populates `mlb_statcast_players`
    # with per-player xwOBA / xBA / barrel% / EV; consumed by
    # signal_engine.mlb_deep_signal at scoring time.
    try:
        from services.mlb_statcast import refresh_all as _statcast_refresh_all

        async def _mlb_statcast_loop():
            while True:
                try:
                    r = await _statcast_refresh_all(db)
                    logger.info(
                        "Statcast refresh: %d batters, %d pitchers",
                        (r or {}).get("batters", {}).get("upserted", 0),
                        (r or {}).get("pitchers", {}).get("upserted", 0),
                    )
                except Exception as e:
                    logger.warning("Statcast refresh cycle failed: %s", e)
                # 24h refresh interval (Baseball Savant updates once/day).
                await asyncio.sleep(24 * 60 * 60)

        _deferred_task(_mlb_statcast_loop, DEFER_BASE * 8)
        logger.info("Statcast daily refresh loop scheduled")
    except Exception as e:
        logger.warning("Statcast refresh loop failed to start: %s", e)

    # Phase 1.2 — Baseball Savant pitch-arsenal (Stuff+/Location+/Pitching+
    # analog) daily refresh loop. Uses the same Savant CSV pipeline as
    # Statcast, so it's cheap and reliable. Populates
    # `mlb_stuff_plus_players` with usage-weighted composite grades
    # consumed by signal_engine.mlb_deep_signal for pitcher K/Outs props.
    try:
        from services.mlb_stuff_plus import refresh_stuff_plus as _stuffplus_refresh

        async def _mlb_stuff_plus_loop():
            while True:
                try:
                    r = await _stuffplus_refresh(db)
                    logger.info(
                        "Stuff+ refresh: %d pitchers (year %s)",
                        (r or {}).get("upserted", 0),
                        (r or {}).get("year"),
                    )
                except Exception as e:
                    logger.warning("Stuff+ refresh cycle failed: %s", e)
                # 24h — Baseball Savant leaderboards update daily post-slate.
                await asyncio.sleep(24 * 60 * 60)

        _deferred_task(_mlb_stuff_plus_loop, DEFER_BASE * 8)
        logger.info("Stuff+ daily refresh loop scheduled")
    except Exception as e:
        logger.warning("Stuff+ refresh loop failed to start: %s", e)

    # Phase 2 — Soccer multi-source fallback ingest (football-data.co.uk
    # + Football-Data.org + TheSportsDB + OpenLigaDB). Runs once daily.
    # See services/soccer/README (also docstring in services/soccer/__init__.py)
    # for the architecture. Every ingested doc carries a `source` field so
    # we can audit which provider gave us which data point.
    try:
        from services.soccer import refresh_all_leagues as _soccer_refresh

        async def _soccer_refresh_loop():
            while True:
                try:
                    r = await _soccer_refresh(db, seasons=("2024-25", "2023-24"))
                    logger.info("Soccer multi-source refresh: %s", r)
                except Exception as e:
                    logger.warning("Soccer refresh cycle failed: %s", e)
                # 24h refresh; historical data doesn't change frequently.
                await asyncio.sleep(24 * 60 * 60)

        _deferred_task(_soccer_refresh_loop, DEFER_BASE * 9)
        logger.info("Soccer multi-source refresh loop scheduled")
    except Exception as e:
        logger.warning("Soccer refresh loop failed to start: %s", e)

    # Phase 3 — Tennis Sackmann/TML historical ingest (weekly refresh;
    # match histories are appended weekly by TML-Database). Populates
    # tennis_matches_history + tennis_player_stats.
    try:
        from services.tennis import refresh_tennis_history as _tennis_refresh

        async def _tennis_refresh_loop():
            while True:
                try:
                    r = await _tennis_refresh(db)
                    logger.info("Tennis Sackmann refresh: %s", r)
                except Exception as e:
                    logger.warning("Tennis refresh cycle failed: %s", e)
                await asyncio.sleep(7 * 24 * 60 * 60)   # weekly

        _deferred_task(_tennis_refresh_loop, DEFER_BASE * 10)
        logger.info("Tennis Sackmann weekly refresh loop scheduled")
    except Exception as e:
        logger.warning("Tennis refresh loop failed to start: %s", e)

    # Phase 3c — Tennis league-average calibration. Recomputes surface-
    # specific league means / stddevs for hold%, 1st-serve-won%, break-
    # saved%, and win% from `tennis_player_stats`. These normalize the
    # per-player z-scores so a top-10 pro scores 85+ and an ITF Futures
    # player scores 20-35 (was: everyone scored 99).
    try:
        from services.tennis_calibration import refresh_league_averages as _cal_refresh

        async def _tennis_cal_loop():
            # Wait 60s for the Sackmann refresh to populate tennis_player_stats
            # on cold start, then refresh averages daily.
            await asyncio.sleep(60)
            while True:
                try:
                    r = await _cal_refresh(db)
                    logger.info("Tennis calibration averages refreshed for %d surfaces",
                                len(r or {}))
                except Exception as e:
                    logger.warning("Tennis calibration refresh failed: %s", e)
                await asyncio.sleep(24 * 60 * 60)

        _deferred_task(_tennis_cal_loop, DEFER_BASE * 10)
        logger.info("Tennis calibration daily refresh loop scheduled")
    except Exception as e:
        logger.warning("Tennis calibration loop failed to start: %s", e)

    # Phase 4 — NFL nflverse (snap counts + player_stats_season)
    # preseason prep. Season-agg parquets are ~2 MB each and rarely
    # change once a game is final, so weekly refresh is plenty. Seeded
    # with 2024 + 2025 so the signal is populated at kickoff.
    try:
        from services.nfl_nflfastr import refresh_nfl_seasons as _nfl_refresh

        async def _nfl_nflverse_loop():
            while True:
                try:
                    # Include current year — becomes non-empty as soon
                    # as Week 1 games settle.
                    yrs = (datetime.now(timezone.utc).year, 2025, 2024)
                    r = await _nfl_refresh(db, seasons=yrs)
                    logger.info("NFL nflverse refresh: %s", r)
                except Exception as e:
                    logger.warning("NFL nflverse cycle failed: %s", e)
                await asyncio.sleep(7 * 24 * 60 * 60)   # weekly

        _deferred_task(_nfl_nflverse_loop, DEFER_BASE * 11)
        logger.info("NFL nflverse weekly refresh loop scheduled (pre-season prep)")
    except Exception as e:
        logger.warning("NFL nflverse loop failed to start: %s", e)

    # Phase 5c — Steam detector. Watches pick_line_history for rapid
    # implied-probability moves and tags picks with a `steam` block.
    # Runs every 60s (loop-internal), independent of line-observer
    # cadence — so it's timely as soon as the observer writes a new
    # observation.
    try:
        from steam_detector import steam_detector_loop
        _deferred_task(lambda: steam_detector_loop(db), DEFER_BASE * 11)
        logger.info("Steam detector loop scheduled")
    except Exception as e:
        logger.warning("Steam detector loop failed to start: %s", e)

    # Live alt-line feed (2026-06-30 user mandate — no synthetic lines).
    # Pulls real DK + FanDuel alt-line markets from The Odds API every
    # 10 min and stores them in `live_alt_lines` with TTL. The quality
    # gate validates every alt-line pick against this collection — any
    # pick whose (sportsbook, market_key, selection, line) isn't in the
    # live feed within the last 15 min is REJECTED with one of:
    #   line_not_found / market_removed / stale_odds / invalid_alt_mapping.
    try:
        from alt_lines_feed import ensure_indices, refresh_alt_lines

        async def _alt_lines_loop():
            try:
                await ensure_indices(db)
            except Exception as ie:
                logger.warning("alt_lines indices failed: %s", ie)
            while True:
                try:
                    await refresh_alt_lines(db)
                except Exception as re_:
                    logger.warning("alt_lines refresh error: %s", re_)
                await asyncio.sleep(600)  # 10 min

        _deferred_task(_alt_lines_loop, DEFER_BASE * 8)
        logger.info("Live alt-line feed armed (DK + FanDuel via Odds API, 10-min cadence)")
    except Exception as e:
        logger.warning("alt_lines_feed failed to start: %s", e)

    # Second alt-line source: prop-line.com (free Player Props API).
    # Covers MLB / NBA / NFL / NHL / NCAAF / NCAAB / Tennis / Golf /
    # 30+ soccer leagues with deep alt-line ladders (batter_total_bases,
    # batter_2plus_rbis, batter_2plus_home_runs, NFL alt yards, tennis
    # total_games, etc.) the Odds API doesn't carry for free. Stored in
    # `propline_alt_lines`; the quality-gate validator unions both
    # collections at query time and takes best-of-book.
    try:
        from propline_feed import (
            ensure_propline_indices, refresh_propline_alt_lines,
        )

        async def _propline_loop():
            try:
                await ensure_propline_indices(db)
            except Exception as ie:
                logger.warning("propline indices failed: %s", ie)
            while True:
                try:
                    await refresh_propline_alt_lines(db)
                except Exception as re_:
                    logger.warning("propline refresh error: %s", re_)
                await asyncio.sleep(480)  # 8 min — free API, slightly tighter than Odds API

        _deferred_task(_propline_loop, DEFER_BASE * 10)
        logger.info("Propline alt-line feed armed (DK+FD+BetMGM+BetRivers+Bovada, 8-min cadence)")
    except Exception as e:
        logger.warning("propline_feed failed to start: %s", e)

    logger.info("PerkLocks AI started")


@app.on_event("shutdown")
async def on_shutdown():
    client.close()
