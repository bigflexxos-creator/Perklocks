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
DATA_VERSION = "2026.08.08-canonical-board-cache-v46"
SERVER_STARTED_AT = datetime.now(timezone.utc)

# ── Block 2C-cont Issue-6 (2026-08): real deploy-identifier surfacing ─
# server_started_at is PROCESS-START time and advances on any restart
# (crash, pod restart, supervisor restart, config reload) — not just
# real deployments.  If the runtime environment provides an actual
# deployment identifier (deploy id / build id / git sha / explicit
# deploy timestamp), we expose it verbatim so the client can reason
# about real drift.  When no such identifier exists we DO NOT invent
# one — the frontend banner must not claim "deploy is X days behind"
# in that case (Block 2C-cont directive).
def _deploy_metadata_from_env() -> dict:
    keys = (
        "DEPLOYMENT_ID", "BUILD_ID",
        "GIT_COMMIT_SHA", "GIT_SHA", "COMMIT_SHA",
        "BACKEND_RELEASE_ID", "FRONTEND_RELEASE_ID",
        "DEPLOY_TIMESTAMP", "DEPLOY_TIME",
        "RENDER_GIT_COMMIT", "VERCEL_GIT_COMMIT_SHA",
    )
    md: dict = {}
    for k in keys:
        v = os.environ.get(k)
        if v:
            md[k.lower()] = v
    # Canonicalize the most useful identifiers.
    canonical = {
        "deploy_id":
            md.get("deployment_id") or md.get("build_id"),
        "git_commit_sha":
            md.get("git_commit_sha") or md.get("git_sha")
            or md.get("commit_sha") or md.get("render_git_commit")
            or md.get("vercel_git_commit_sha"),
        "backend_release_id":  md.get("backend_release_id"),
        "frontend_release_id": md.get("frontend_release_id"),
        "deploy_timestamp":
            md.get("deploy_timestamp") or md.get("deploy_time"),
    }
    canonical = {k: v for k, v in canonical.items() if v}
    return canonical


@api.get("/version")
async def get_version():
    """Public endpoint — no auth required. Phones poll this on launch and on
    tab focus to detect when the server has shipped new data they should
    rehydrate.

    Field semantics (Block 2C-cont Issue-6):

      data_version         SOURCE-CODE constant bumped on real backend
                           releases → RELIABLE deploy signal for the
                           StaleBuildBanner mismatch check.

      server_started_at    Process-start time.  RUNTIME marker only —
                           advances on any crash / pod / supervisor
                           restart, so MUST NOT be treated as deploy
                           age.  Retained for back-compat.

      runtime_started_at   Explicit alias of server_started_at with
                           truthful naming.  Prefer this on new
                           consumers.

      deploy_metadata      Present ONLY when the runtime exposes a
                           real deploy identifier
                           (deploy_id / git_commit_sha /
                           deploy_timestamp / release id).  Absent
                           when the environment provides nothing.
    """
    payload = {
        "data_version": DATA_VERSION,
        "server_time": datetime.now(timezone.utc).isoformat(),
        # Legacy field — advances on any process restart, NOT only
        # deploys.  Kept for backwards compatibility with older
        # frontend bundles; new consumers should use the fields
        # below.
        "server_started_at": SERVER_STARTED_AT.isoformat(),
        # Truthfully-named alias.
        "runtime_started_at": SERVER_STARTED_AT.isoformat(),
    }
    md = _deploy_metadata_from_env()
    if md:
        payload["deploy_metadata"] = md
    return payload


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
    """Phase 1b — hydrate from the published snapshot; fall back to the
    legacy repair path only for pre-Phase-1c legacy rows.

    Before Phase 1b this function was the single biggest read-time
    mutation in the codebase.  It performed a `max(v1, v2, raw, peak)`
    promotion + always-starter floor + coherence-cap clamp on every
    pick on every read.  The publication contract makes that repair
    unnecessary — the snapshot is now the single source of truth.

    Rules:
      • If the pick carries `published_lock_score` (i.e. the
        publication service already stamped it), we hydrate all
        contract fields from the snapshot and return.  Zero
        recomputation.
      • Otherwise (legacy row without a snapshot yet), we fall back
        to the historical repair logic for backwards compatibility.
        These rows will be v0-backfilled in Phase 1c, after which
        the legacy branch can be removed entirely.
    """
    # Phase 1b fast-path — snapshot exists ⇒ trust it.
    if pick.get("published_lock_score") is not None:
        from services.published_prediction_reader import hydrate
        return hydrate(pick)

    # ── Legacy path (Phase 1c will remove this) ────────────────────
    # Kept intact so any pick without a snapshot still renders
    # coherently until the v0 backfill lands.
    return _legacy_canonicalize_lock_score(pick)


def _legacy_canonicalize_lock_score(pick: dict) -> dict:
    """Phase 1c — REMOVED.

    Every pick now carries a `published_lock_score` field via the v0
    backfill or a v1+ canonical publication.  `_canonicalize_lock_score`
    (above) is the only public entrypoint and it delegates to the
    snapshot-first `hydrate()` for every pick.

    This function is retained ONLY as an anti-regression shim: any code
    path that still calls it (which should be none after Phase 1c) is
    detected here and logged.  The 306-line legacy repair logic
    (max-of-shadow-fields promotion, always-starter floor, coherence-cap
    clamp, read-time grade+confidence re-derive) is gone.
    """
    try:
        pid = pick.get("id") or pick.get("prediction_id") or "?"
        logger.warning(
            "_legacy_canonicalize_lock_score called after Phase 1c "
            "removal — pick=%s has_snapshot=%s. This should never "
            "happen; check the writer.",
            pid, "published_lock_score" in pick,
        )
    except Exception:
        pass
    # Last-resort behaviour — the fast-path above already handles
    # snapshot-backed picks, so if we're here the pick has no
    # snapshot AND no fast-path exit.  Return it untouched.
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




# ═════════════════════════════════════════════════════════════════════
# Phase 3F-1 (2026-08) — pick-refresh orchestration extracted to
# services/pick_refresh_orchestrator.py.  The compatibility wrappers
# below preserve the pre-extraction call signatures used across the
# codebase (admin routes, scheduler, tests):
#
#   _refresh_picks(date_str, sport_filter=None) -> int
#   _dedupe_and_limit_goalscorers, _cap_tennis_totals
#   _reconcile_player_prop_contradictions, _prop_family_key,
#   _atomic_mark_no_bet, _ensure_csl_elite_picks,
#   _shadow_capture_gs_v2
#
# All refresh business logic lives in the orchestrator; server.py holds
# only the thin coroutine below.
# ═════════════════════════════════════════════════════════════════════
from services.pick_refresh_orchestrator import (  # noqa: E402
    PickRefreshOrchestrator,
    PickRefreshRequest,
    PickRefreshResult,
    _dedupe_and_limit_goalscorers,
    _cap_tennis_totals,
    _prop_family_key,
    _atomic_mark_no_bet,
    _reconcile_player_prop_contradictions,
    _ensure_csl_elite_picks,
    _shadow_capture_gs_v2,
)


async def _refresh_picks(
    date_str: str, sport_filter: Optional[str] = None,
) -> int:
    """Phase 3F-1 compatibility wrapper.

    Every production caller that previously invoked
    ``server._refresh_picks(date_str, sport_filter)`` still works
    unchanged — the business logic now lives in
    :class:`services.pick_refresh_orchestrator.PickRefreshOrchestrator`.
    """
    orchestrator = PickRefreshOrchestrator()
    result = await orchestrator.refresh(PickRefreshRequest(
        slate_date=date_str,
        sport_filter=sport_filter,
        caller="server._refresh_picks_compat",
        reason="legacy signature",
    ))
    return int(result.published_count or 0)




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
    # Phase 3F-2: register even the internal admin-refresh task
    try:
        from services.runtime_task_registry import get_registry
        get_registry().register_and_start(
            f'background_refresh:{uuid.uuid4().hex[:8]}',
            _background_refresh,
            task_type='one_shot', critical=False,
        )
    except ValueError:
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
    # Total Bases + RBIs added 2026-08-08 Phase-1 market surfacing.
    # Anchored on the stat phrase; `batter_hits` above already
    # excludes "hits + …" via lookahead so no cross-token bleed.
    "batter_total_bases":   r"\btotal bases\b",
    "batter_rbis":          r"\brbis?\b(?!\s*allowed)",
    # Pitcher strikeouts — added 2026-06-18 with the pitcher Ks props
    "pitcher_strikeouts":   r"\bstrikeouts\b",
    # Pitcher outs recorded — added 2026-06-19. Main line only (no alt).
    "pitcher_outs":         r"\bouts recorded\b|\bpitcher outs\b",
    "player_points":        r"\bpoints\b(?!\s*total)(?!\s*\+)",
    "player_rebounds":      r"\brebounds\b(?!\s*\+)",
    "player_assists":       r"\bassists\b(?!\s*\+)",
    # NBA combo + shooters — added 2026-08-08 Phase-1 market surfacing.
    "player_points_rebounds_assists": r"points\s*\+\s*rebounds\s*\+\s*assists|\bpra\b|p\+r\+a",
    "player_threes":        r"\bthrees\b|3[- ]pointers made|three[- ]point(?:er)?s? made",

    # ── NFL props ─────────────────────────────────────────────────────────
    "passing_yards":   r"passing yards",
    "rushing_yards":   r"rushing yards",
    "receiving_yards": r"receiving yards",
    # Additional NFL/CFB categories added 2026-08-08 Phase-1 market
    # surfacing.  Backend already ingests these via propline_feed /
    # nfl_feature_engine / cfb_precompute; picks arrive with market
    # strings like "Player Passing TDs Over 1.5" etc.
    "player_1st_td":         r"\b1st td\b|\bfirst td\b|first touchdown scorer",
    "player_pass_tds":       r"passing tds?|pass tds?",
    "player_pass_attempts":  r"passing attempts?|pass attempts?",
    "player_pass_completions": r"passing completions?|pass completions?",
    "player_rush_attempts":  r"rushing attempts?|rush attempts?|\bcarries\b",
    "player_rush_tds":       r"rushing tds?|rush tds?",
    "player_receptions":     r"\breceptions?\b(?!\s*yards)(?!\s*tds?)",
    "player_reception_tds":  r"receiving tds?|reception tds?",

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
        {"token": "spread",            "label": "Handicap"},
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
        {"token": "player_points_rebounds_assists", "label": "PRA"},
        {"token": "player_threes",   "label": "3-Pointers"},
    ],
    "NFL": [
        {"token": "moneyline",   "label": "Moneyline"},
        {"token": "spread",      "label": "Spread"},
        {"token": "totals",      "label": "Totals"},
        {"token": "passing_yards",   "label": "Passing Yds"},
        {"token": "rushing_yards",   "label": "Rushing Yds"},
        {"token": "receiving_yards", "label": "Receiving Yds"},
        {"token": "player_1st_td",         "label": "1st TD"},
        {"token": "player_pass_tds",       "label": "Pass TDs"},
        {"token": "player_pass_attempts",  "label": "Pass Att"},
        {"token": "player_pass_completions","label": "Pass Comp"},
        {"token": "player_rush_attempts",  "label": "Rush Att"},
        {"token": "player_rush_tds",       "label": "Rush TDs"},
        {"token": "player_receptions",     "label": "Receptions"},
        {"token": "player_reception_tds",  "label": "Rec TDs"},
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
        {"token": "player_1st_td",         "label": "1st TD"},
        {"token": "player_pass_tds",       "label": "Pass TDs"},
        {"token": "player_pass_attempts",  "label": "Pass Att"},
        {"token": "player_pass_completions","label": "Pass Comp"},
        {"token": "player_rush_attempts",  "label": "Rush Att"},
        {"token": "player_rush_tds",       "label": "Rush TDs"},
        {"token": "player_receptions",     "label": "Receptions"},
        {"token": "player_reception_tds",  "label": "Rec TDs"},
    ],
    "MLB": [
        {"token": "moneyline",   "label": "Moneyline"},
        {"token": "run_line",    "label": "Run Line"},
        {"token": "totals",      "label": "Totals"},
        {"token": "team_total",  "label": "Team Total"},
        {"token": "batter_hits",            "label": "Hits"},
        {"token": "batter_total_bases",     "label": "Total Bases"},
        {"token": "batter_rbis",            "label": "RBIs"},
        # H+R+RBI chip restored 2026-07-21 per user — the market ban
        # in quality_gate.py has been lifted now that mlb_feature_engine
        # gates emission on ≥3 real factors (L10 hit rate, platoon,
        # BvP, matchup, home/away). No more variance-only 35% picks.
        {"token": "batter_hits_runs_rbis",  "label": "H+R+RBI"},
        {"token": "pitcher_strikeouts",     "label": "Strikeouts"},
        {"token": "pitcher_outs",           "label": "Outs Recorded"},
        # Home Run stays on its dedicated HR experience (not a chip).
        # NRFI/YRFI retired from active Locks per Phase 1 — no chip.
    ],
    "Tennis": [
        {"token": "match_winner",    "label": "Moneyline"},
        {"token": "spread",          "label": "Spread"},
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
    # ── Phase 2β observability (2026-08-15) ─────────────────────────
    # /api/admin/ops/* — JobCoordinator + ProviderBudget introspection.
    # Every route is admin-only; no secrets are ever exposed.
    from routes import ops_routes
    app.include_router(ops_routes.router)
    logger.info("Ops observability routes mounted at /api/admin/ops/*")
    # ── Phase 4C finalization (2026-08-06) ────────────────────────
    # /api/admin/mlb/rejections — structured MLB rejection counters.
    try:
        from routes import mlb_admin_diagnostics
        app.include_router(mlb_admin_diagnostics.router)
        logger.info("Phase 4C MLB diagnostics mounted at /api/admin/mlb/*")
    except Exception as _e_mlb_diag:
        logger.warning("Phase 4C MLB diagnostics failed to mount: %s", _e_mlb_diag)
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
    # ── Pre-Magic Certification (2026-06 P1) ─────────────────────────
    # Read-only certification harness proving Magic 2.0 evidence
    # foundation is real / canonical / reachable / as-of safe /
    # threshold-aware / provenance-aware.  NEVER wires Magic — the
    # matrix always reports magic_consumption=NOT_WIRED (§15).
    try:
        from routes import certification_routes
        app.include_router(certification_routes.router)
        logger.info(
            "Pre-Magic Certification mounted at /api/admin/certification/*")
    except Exception as _e_cert:
        logger.warning("Pre-Magic Certification failed to mount: %s", _e_cert)
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
                # ── Phase 2δ closeout: day-rollover refresh must go
                # through JobCoordinator + ProviderBudget + gateway.
                # Distributed lease guarantees only ONE instance runs
                # the refresh when multiple workers detect the rollover.
                logger.info(
                    "Daily loop: UTC day rolled %s → %s — attempting "
                    "coordinated rollover refresh",
                    last_refresh_date, current_date,
                )
                try:
                    from services.job_coordinator import JobCoordinator as _JCR
                    from services.provider_budget import ProviderBudget as _PBR
                    coord = _JCR(db)
                    budget = _PBR(db)
                    lease = await coord.acquire(
                        "picks_refresh_today",
                        lease_seconds=900,
                        min_interval_seconds=1800,
                        caller="daily_refresh_loop.day_rollover",
                        reason=f"day_rollover:{last_refresh_date}->{current_date}",
                    )
                    if lease:
                        token = lease.lease_token
                        r = await budget.reserve(
                            estimated_credits=800,
                            endpoint_type="picks_refresh",
                            caller="daily_refresh_loop.day_rollover",
                            job_name="picks_refresh_today",
                            reason="day_rollover",
                            request_key=f"day_rollover:{current_date}:{token}",
                            ttl_seconds=960,
                        )
                        if r.get("allowed"):
                            try:
                                await _refresh_picks(current_date)
                                await budget.commit(r["intent_id"])
                                await coord.complete(
                                    "picks_refresh_today", token,
                                    result_metadata={"day_rollover": current_date},
                                )
                            except Exception as _e:
                                await budget.release(r["intent_id"],
                                                       reason=f"rollover_err:{_e}")
                                await coord.fail(
                                    "picks_refresh_today", token,
                                    error=str(_e), retry_after_seconds=300,
                                )
                        else:
                            await coord.fail(
                                "picks_refresh_today", token,
                                error=f"budget_denied:{r.get('outcome')}",
                                retry_after_seconds=300,
                            )
                            logger.warning(
                                "Day rollover refresh budget denied: %s",
                                r.get("outcome"),
                            )
                    else:
                        logger.info(
                            "Day rollover refresh already owned by another "
                            "instance (%s) — skipping", lease.get("reason"),
                        )
                except Exception as _rov_err:
                    logger.warning("Day rollover coordinated refresh err: %s",
                                    _rov_err)
                last_refresh_date = current_date
                continue

            # Hourly cadence for same-day freshness. We check
            # `_should_refresh_by_clock` against the previous tick — if we've
            # just crossed an hour boundary since last refresh, run.
            # Simpler: just count ticks (12 ticks × 5 min = 60 min).
            _daily_refresh_loop.tick_count = getattr(_daily_refresh_loop, "tick_count", 0) + 1
            if _daily_refresh_loop.tick_count >= 12:
                _daily_refresh_loop.tick_count = 0
                # ── Phase 2γ Global Refresh Mode gate ─────────────
                # In `snapshot` mode (default), the hourly global
                # refresh is disabled — the 3×/day scheduled snapshots
                # (12/18/23 UTC) plus admin recovery cover freshness.
                # `legacy_hourly` mode preserves the old cadence for
                # emergency rollback and STILL goes through the
                # gateway + budget + coordinator.
                try:
                    from services.odds_api_gateway import _global_refresh_mode
                    _mode = _global_refresh_mode()
                except Exception:
                    _mode = "snapshot"
                if _mode == "legacy_hourly":
                    # Route through coordinator + budget so even the
                    # emergency-rollback path is single-flighted.
                    try:
                        from services.job_coordinator import JobCoordinator as _JC_g
                        from services.provider_budget import ProviderBudget as _PB_g
                        coord = _JC_g(db)
                        budget = _PB_g(db)
                        lease = await coord.acquire(
                            "picks_refresh_today",
                            lease_seconds=900,
                            min_interval_seconds=1800,
                            caller="daily_refresh_loop",
                            reason="legacy_hourly_refresh",
                        )
                        if lease:
                            token = lease.lease_token
                            r = await budget.reserve(
                                estimated_credits=800,
                                endpoint_type="picks_refresh",
                                caller="daily_refresh_loop",
                                job_name="picks_refresh_today",
                                reason="legacy_hourly_refresh",
                                request_key=f"legacy_hourly:{token}",
                                ttl_seconds=960,
                            )
                            if r.get("allowed"):
                                try:
                                    await _refresh_picks(current_date)
                                    await budget.commit(r["intent_id"])
                                    await coord.complete("picks_refresh_today", token)
                                except Exception as e:
                                    await budget.release(r["intent_id"], reason=f"err:{e}")
                                    await coord.fail("picks_refresh_today", token,
                                                      error=str(e), retry_after_seconds=300)
                            else:
                                await coord.fail("picks_refresh_today", token,
                                                  error=f"budget_denied:{r.get('outcome')}",
                                                  retry_after_seconds=300)
                    except Exception as _e:
                        logger.warning("legacy_hourly refresh err: %s", _e)
                else:
                    # snapshot mode — do nothing.  Scheduled snapshots
                    # (alt_lines_feed, mls_direct_inject, soccer_prop_inject,
                    # MLB pregame lease-gated loop) cover freshness.
                    logger.debug(
                        "daily_refresh_loop: snapshot mode — no hourly refresh"
                    )
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
_MLB_QUICK_REFRESH_INTERVAL = 5 * 60   # 5 minutes (today, near-start games)
_MLB_TOMORROW_REFRESH_INTERVAL = 30 * 60   # 30 minutes (tomorrow's board — Phase 2γ)
_MLB_WINDOW_START_UTC_HOUR = 15        # 11 AM ET
_MLB_WINDOW_END_UTC_HOUR = 3           # 11 PM ET / 8 PM PT — covers late West Coast first pitches (wraps past midnight UTC)


async def _mlb_pregame_loop():
    """Refresh MLB picks every 5 min during US daytime so player props
    surface 60–90 min pre-game instead of 5–10 min pre-game.

    Phase 2γ (2026-08-06): the today+tomorrow every-5-min fan-out was
    the biggest MLB paid burn.  We now:

      • Refresh **today** at 5-min cadence during the US window
        (unchanged — this is the user-visible value).
      • Refresh **tomorrow** at 30-min cadence (was 5-min) — books
        post next-day slates hours in advance, no need to poll them
        that hard.
      • Both paths acquire a JobCoordinator lease + ProviderBudget
        reservation so rolling deployments and multi-worker
        containers cannot fire duplicate paid work.
    """
    # Let startup settle so the initial seed completes first.
    await asyncio.sleep(120)
    from services.job_coordinator import JobCoordinator as _MJC
    from services.provider_budget import ProviderBudget as _MPB
    last_tomorrow_at: float = 0.0
    while True:
        try:
            now = datetime.now(timezone.utc)
            hour = now.hour
            if hour >= _MLB_WINDOW_START_UTC_HOUR or hour < _MLB_WINDOW_END_UTC_HOUR:
                # ── Today's slate ─────────────────────────────────
                coord = _MJC(db)
                budget = _MPB(db)
                lease = await coord.acquire(
                    "mlb_pregame_refresh_today",
                    lease_seconds=180,
                    min_interval_seconds=180,   # min 3-min spacing
                    caller="mlb_pregame_loop",
                    reason="pregame_5min",
                )
                if lease:
                    token = lease.lease_token
                    r = await budget.reserve(
                        estimated_credits=60,
                        endpoint_type="picks_refresh_mlb_today",
                        caller="mlb_pregame_loop",
                        job_name="mlb_pregame_refresh_today",
                        reason="pregame_5min",
                        request_key=f"mlb_today:{token}",
                        ttl_seconds=240,
                    )
                    if r.get("allowed"):
                        intent = r["intent_id"]
                        try:
                            await _refresh_picks(_today_str(), sport_filter="MLB")
                            await budget.commit(intent)
                            await coord.complete(
                                "mlb_pregame_refresh_today", token,
                            )
                        except Exception as e:
                            await budget.release(intent, reason=f"err:{e}")
                            await coord.fail(
                                "mlb_pregame_refresh_today", token,
                                error=str(e), retry_after_seconds=120,
                            )
                    else:
                        await coord.fail(
                            "mlb_pregame_refresh_today", token,
                            error=f"budget_denied:{r.get('outcome')}",
                            retry_after_seconds=300,
                        )
                # ── Tomorrow's slate — much slower cadence ────────
                t_now = asyncio.get_event_loop().time()
                if t_now - last_tomorrow_at >= _MLB_TOMORROW_REFRESH_INTERVAL:
                    last_tomorrow_at = t_now
                    coord = _MJC(db)
                    budget = _MPB(db)
                    lease = await coord.acquire(
                        "mlb_pregame_refresh_tomorrow",
                        lease_seconds=180,
                        min_interval_seconds=_MLB_TOMORROW_REFRESH_INTERVAL,
                        caller="mlb_pregame_loop",
                        reason="pregame_tomorrow_30min",
                    )
                    if lease:
                        token = lease.lease_token
                        r = await budget.reserve(
                            estimated_credits=40,
                            endpoint_type="picks_refresh_mlb_tomorrow",
                            caller="mlb_pregame_loop",
                            job_name="mlb_pregame_refresh_tomorrow",
                            reason="pregame_tomorrow_30min",
                            request_key=f"mlb_tomorrow:{token}",
                            ttl_seconds=240,
                        )
                        if r.get("allowed"):
                            intent = r["intent_id"]
                            try:
                                tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
                                await _refresh_picks(tomorrow_str, sport_filter="MLB")
                                await budget.commit(intent)
                                await coord.complete(
                                    "mlb_pregame_refresh_tomorrow", token,
                                )
                            except Exception as e:
                                await budget.release(intent, reason=f"err:{e}")
                                await coord.fail(
                                    "mlb_pregame_refresh_tomorrow", token,
                                    error=str(e), retry_after_seconds=120,
                                )
                        else:
                            await coord.fail(
                                "mlb_pregame_refresh_tomorrow", token,
                                error=f"budget_denied:{r.get('outcome')}",
                                retry_after_seconds=300,
                            )
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
    from services.job_coordinator import JobCoordinator
    coordinator = JobCoordinator(db)
    FULL_INTERVAL_TICKS = 15   # full settlement every 15th tick = 15 min
    tick = 0
    while True:
        try:
            # Guard against duplicate execution when the app runs multiple
            # replicas (standard for this deploy tier) — only one pod should
            # run a given 60s settlement tick. Same JobCoordinator pattern
            # already used by cold_start.py / background_lifecycle.py.
            lease = await coordinator.acquire(
                "settlement_loop",
                lease_seconds=180,
                min_interval_seconds=50,
                caller="settlement_loop",
                reason="60s settlement/grading tick",
            )
            if not lease:
                await asyncio.sleep(60)
                continue
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
            # ── Canonical Parlay Resolver (Phase 3G Step 7) ─────────
            # Walks canonical user_bets parlays whose picks have
            # settled since the last pass and rolls up their ticket
            # status.  Runs alongside the legacy resolver above so
            # both pre-Step-7 mirror rows (in parlay_history) and
            # post-Step-7 canonical-only rows are covered.
            try:
                from services.user_bet_ledger import resolve_pending_parlays_canonical
                await resolve_pending_parlays_canonical(db)
            except Exception as e:
                logger.warning("Canonical parlay resolver error: %s", e)
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
            await coordinator.complete("settlement_loop", lease.lease_token)
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
            # ── P0-3 IMMUTABILITY FIX (2026-08-08) ──────────────────
            # Weekly tuner previously re-applied learned weights to
            # EVERY open pick, including canonically-published ones,
            # rewriting `lock_score`, `win_probability`, `edge_percent`,
            # `grade`, `confidence` on already-published rows.  That
            # directly violated PUBLICATION_CONTRACT §3
            # (immutability of the legacy-alias projection of
            # published fields).
            #
            # Fix: constrain the reapply loop to picks WITHOUT
            # `publication_source` — legacy pre-2026-08-06 rows that
            # never received a snapshot are still eligible for
            # in-place adjustment (they'll be v0-backfilled in
            # Phase 1c).  Published picks are left alone; learning
            # continues to flow into `recompute_learned_weights`
            # (updates the shared `learning_weights` collection) so
            # the NEXT `_refresh_picks` cycle consumes the fresh
            # weights via `apply_learning` at pick-generation time
            # (see `services/pick_refresh_orchestrator.py:480`).
            cursor = db.picks.find(
                {"status": {"$in": [None, "pending"]},
                 "publication_source": {"$exists": False}},
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
                    # Safety net: even with the cursor filter above,
                    # double-check the pick isn't published before
                    # writing.  If a concurrent publication landed
                    # between our read and this write, skip the
                    # mutation.
                    if p.get("publication_source"):
                        continue
                    await db.picks.update_one(
                        {"id": p["id"],
                         "publication_source": {"$exists": False}},
                        {"$set": {"win_probability": p["win_probability"],
                                   "lock_score": p.get("lock_score"),
                                   "edge_percent": p.get("edge_percent"),
                                   "implied_probability": p.get("implied_probability"),
                                   "grade": p.get("grade"),
                                   "confidence": p.get("confidence"),
                                   "learning": p.get("learning")}},
                    )
            active = sum(1 for b in weights.get("buckets", []) if b.get("active"))
            logger.info(
                "Weekly model tuning: %d active buckets, %d "
                "unpublished picks re-weighted (published picks are "
                "immutable per PUBLICATION_CONTRACT §3 — future picks "
                "will consume the fresh weights on the next "
                "_refresh_picks cycle)",
                active, adjusted,
            )
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
    # Phase 3F-2 — every startup-created asyncio task goes
    # through the runtime task registry so shutdown can
    # signal + await each one.
    from services.runtime_task_registry import get_registry
    from services.application_lifecycle import get_lifecycle
    _TASK_REGISTRY = get_registry()
    _LIFECYCLE     = get_lifecycle()
    app.state.lifecycle = _LIFECYCLE
    app.state.task_registry = _TASK_REGISTRY

    # Run the lifecycle preflight (settings + DB + ping + indexes +
    # lease recovery).  All steps are idempotent — the existing
    # inline blocks below run again for observability but no work is
    # duplicated because each idempotent call short-circuits.
    try:
        _preflight_result = await _LIFECYCLE.preflight()
        logger.info(
            "Phase 3F-2 preflight: ok=%s db=%s idx=%s recov=%s duration_ms=%s",
            _preflight_result.success,
            _preflight_result.database_ready,
            _preflight_result.indexes_ready,
            _preflight_result.recovery_complete,
            _preflight_result.duration_ms,
        )
    except Exception as _e:
        logger.warning("Phase 3F-2 preflight raised: %s", _e)

    # ── Phase 2 Final (2026-08-11) — hydrate canonical player_identity
    # registry from Mongo so freshness timestamps survive restarts
    # and replicas.  Every startup snapshots the persisted registry
    # into memory before the first refresh loop touches it.
    #
    # P0-A (2026-08-11) — ensure the unique index required for
    # race-safe upserts is present BEFORE we hydrate or any ingest
    # loop attempts to write.
    #
    # P0-C (2026-08-11) — extend identity coverage to Big-5 European
    # leagues (from `soccer_player_form`) and seed the curated
    # national-team affiliation stream (independent freshness).  All
    # writes route through the P0-A race-safe persist_identity layer.
    try:
        from services.player_identity import (
            hydrate_registry_from_mongo, ensure_identity_indexes,
        )
        await ensure_identity_indexes(db)
        n = await hydrate_registry_from_mongo(db)
        logger.info(
            "Phase 2 Final: hydrated %d player identities from Mongo", n)
        # P0-C — refresh Big-5 + national-team affiliations.  Fire
        # and forget on startup so the HTTP server can serve
        # /api/version / /api/auth/login immediately; the identity
        # ingest is not on any critical request path.
        try:
            from services.soccer_identity_ingest import (
                refresh_soccer_identity_registry,
            )
            async def _p0c_seed():
                try:
                    summary = await refresh_soccer_identity_registry(db)
                    logger.info(
                        "P0-C/P0-D identity ingest: big5=%s national_teams=%s "
                        "live_teams=%s live_athletes=%s live_club_writes=%s "
                        "live_nt_writes=%s",
                        summary.get("big5", {}).get("upserts"),
                        summary.get("national_teams", {}).get("bootstrap_players"),
                        summary.get("live_rosters", {}).get("teams_scanned"),
                        summary.get("live_rosters", {}).get("athletes_scanned"),
                        summary.get("live_rosters", {}).get("club_writes"),
                        summary.get("live_rosters", {}).get("national_team_writes"),
                    )
                    # Re-hydrate so downstream loops see the new
                    # identities in-memory too.
                    m = await hydrate_registry_from_mongo(db)
                    logger.info(
                        "P0-C/P0-D: post-seed hydrate loaded %d identities", m)
                except Exception as _p0c_err:
                    logger.warning(
                        "P0-C/P0-D identity seed failed (non-fatal): %s", _p0c_err)
            asyncio.create_task(_p0c_seed())
        except Exception as _wire_err:
            logger.warning(
                "P0-C identity seed wiring failed (non-fatal): %s", _wire_err)
    except Exception as _ident_err:
        logger.warning(
            "player_identity hydrate skipped (non-fatal): %s", _ident_err)

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

    # ── Phase 3B — shared Mongo lifecycle (2026-08) ────────────────
    # Ensure the shared client owned by services/database.py is up
    # and reachable before we launch background loops.  This is
    # idempotent — deps.py's module-level import already called
    # initialize_database(); this call verifies the destination and
    # pings the cluster so a misconfigured deploy fails loudly at
    # startup instead of on the first user request.
    try:
        from services.database import (
            initialize_database as _init_db,
            ping_database as _ping_db,
            safe_database_diagnostics as _db_diag,
        )
        _init_db()  # idempotent
        _ok = await _ping_db(timeout_ms=5000)
        logger.info(
            "Phase 3B Mongo ready: ping=%s diagnostics=%s",
            _ok, _db_diag(),
        )
    except Exception as _e:
        logger.warning("Phase 3B Mongo readiness check raised: %s", _e)

    def _deferred_task(coro_factory, delay: float, name: str = None):
        """Schedule `coro_factory()` to run after a `delay` second sleep.
        `coro_factory` is a callable returning a fresh coroutine (we
        accept a callable rather than the coroutine itself so it isn't
        instantiated until we're ready to execute it — avoids the
        'coroutine was never awaited' warning on shutdown).

        Phase 3F-2: registered with runtime_task_registry so shutdown
        can signal + await this deferred task.  Name defaults to the
        coroutine factory's __name__ (falls back to a uuid suffix on
        collision)."""
        async def _runner():
            try:
                await asyncio.sleep(delay)
                await coro_factory()
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning("Deferred startup task failed (delay=%.1fs): %s", delay, e)
        tname = name or getattr(coro_factory, "__name__", None) or f"deferred_{uuid.uuid4().hex[:8]}"
        try:
            return _TASK_REGISTRY.register_and_start(
                tname, _runner,
                task_type="deferred_startup", critical=False,
                cadence=f"one-shot after {delay:.1f}s",
                startup_behavior="eager", restart_policy="none",
            )
        except ValueError:
            # Duplicate name — fall back to a uuid-suffixed registration
            # so shutdown still tracks it.
            uid = f"{tname}:{uuid.uuid4().hex[:6]}"
            return _TASK_REGISTRY.register_and_start(
                uid, _runner,
                task_type="deferred_startup", critical=False,
                cadence=f"one-shot after {delay:.1f}s",
            )

    # ── Phase 3C — Central Index Registry (2026-08) ───────────────────
    # One idempotent call replaces the fragmented `create_index` calls
    # that used to live directly under startup.  The registry declares
    # every critical index; missing ones are created here, matching
    # ones are re-used, and same-name conflicts are reported (never
    # dropped).  See services/index_registry.py.
    try:
        from services.index_registry import (
            ensure_all_indexes as _ensure_all_indexes,
            safe_index_diagnostics as _idx_diag,
        )
        _idx_summary = await _ensure_all_indexes(db)
        logger.info(
            "Phase 3C indexes ensured — diagnostics=%s summary=%s",
            _idx_diag(), _idx_summary,
        )
    except Exception as _idx_err:
        logger.warning("Phase 3C index registry ensure failed: %s", _idx_err)

    # Retain the legacy ad-hoc calls for AUXILIARY (non-critical)
    # collections that are not yet in the registry (soccer, players,
    # nflverse, ESPN meta, historical, etc.).  They will migrate in
    # subsequent Phase 3 sessions.
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
    # (Migrated to Phase 3C registry — this block is now a no-op kept
    # for reference.  learning_snapshots.learning_generated_idx and
    # learning_date_idx are declared in services/index_registry.py.)

    # ── Phase 2β — JobCoordinator + ProviderBudget bootstrap ─────────
    # (Also migrated to Phase 3C registry — the ensure_all_indexes()
    # call above covers scheduled_jobs, job_execution_log,
    # job_audit_log, provider_budget_state, provider_request_intents.
    # We keep the wrapper calls below only to emit the legacy log
    # line for observability parity.)
    try:
        from services.job_coordinator import JobCoordinator
        from services.provider_budget import ProviderBudget
        await JobCoordinator(db).ensure_indices()   # now delegates to registry
        await ProviderBudget(db).ensure_indices()   # now delegates to registry
        logger.info(
            "Phase 2β infra armed — JobCoordinator + ProviderBudget "
            "indices verified via Phase 3C registry."
        )
    except Exception as _p2_err:
        logger.warning("Phase 2β infra bootstrap failed: %s", _p2_err)

    # ── Phase 2δ — Background lifecycle bootstrap ───────────────────
    # Recovers stale leases and orphaned budget reservations from the
    # previous instance BEFORE any snapshot loop is armed.  Multiple
    # workers can boot simultaneously — the lease recovery is
    # atomic-single-document so no work is duplicated.
    try:
        from services.background_lifecycle import BackgroundLifecycle
        _lifecycle = BackgroundLifecycle(db)
        _startup_summary = await _lifecycle.on_startup()
        app.state.lifecycle = _lifecycle
        logger.info(
            "Phase 2δ lifecycle armed — startup recovery: %s",
            _startup_summary,
        )
    except Exception as _lc_err:
        logger.warning("Phase 2δ lifecycle bootstrap failed: %s", _lc_err)

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
    _TASK_REGISTRY.register_and_start(
        'historical_props_loop', lambda: _historical_props_loop(),
        task_type='recurring_loop', critical=False,
    )
    _TASK_REGISTRY.register_and_start(
        'daily_refresh_loop', lambda: _daily_refresh_loop(),
        task_type='recurring_loop', critical=True,
    )
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
                # Phase 2γ closeout: was an uncoordinated boot burst.
                # Now goes through cold_start freshness check + lease
                # + budget.  Multiple restarts within the window
                # produce at most ONE coordinated recovery job.
                try:
                    from services.cold_start import maybe_recover_on_cold_start
                    logger.info(
                        "MLB late-night boot: freshness check (UTC hour=%02d)",
                        _boot_hour,
                    )
                    async def _runner():
                        return await _refresh_picks(_today_str(), sport_filter="MLB")
                    await maybe_recover_on_cold_start(
                        db,
                        job_name="mlb_pregame_refresh_today",
                        runner=_runner,
                        caller="mlb_late_night_boot",
                    )
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
        _TASK_REGISTRY.register_and_start(
            'mlb_player_db_loop', lambda: _mlb_player_db_loop(),
            task_type='recurring_loop', critical=False,
        )
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
        _TASK_REGISTRY.register_and_start(
            'espn_player_db_loop', lambda: _espn_player_db_loop(),
            task_type='recurring_loop', critical=False,
        )
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
        from services.player_identity import persist_registry as _persist_ident
        async def _mls_stats_loop() -> None:
            await asyncio.sleep(15)   # let boot settle
            while True:
                try:
                    # Phase 2 (2026-08-11) dynamic season resolution —
                    # MLS runs Feb → Nov, calendar-year league.
                    _now = datetime.now(timezone.utc)
                    _mls_season = _now.year
                    summary = await refresh_mls_leaders(season=_mls_season)
                    logger.info("ESPN MLS stats refresh: %s", summary)
                    by, names = await load_gate_snapshot()
                    apply_espn_snapshot(by, names)
                    logger.info(
                        "MLS scorer gate hydrated: %d players from ESPN", len(names),
                    )
                    # P0-A (2026-08-11) — persist the canonical
                    # player_identity registry to Mongo so replicas
                    # and restarts see the freshest current-team
                    # observations.  Race-safe: older observations
                    # cannot overwrite fresher ones.
                    try:
                        n_written = await _persist_ident(db)
                        logger.info(
                            "player_identity persisted: %d writes to Mongo",
                            n_written,
                        )
                    except Exception as _pe:
                        logger.warning(
                            "player_identity persistence failed: %s", _pe)
                except Exception as e:
                    logger.warning("ESPN MLS stats refresh failed: %s", e)
                await asyncio.sleep(12 * 60 * 60)   # 12h
        _TASK_REGISTRY.register_and_start(
            'mls_stats_loop', lambda: _mls_stats_loop(),
            task_type='recurring_loop', critical=False,
        )
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
        _TASK_REGISTRY.register_and_start(
            'mls_matchup_loop', lambda: _mls_matchup_loop(),
            task_type='recurring_loop', critical=False,
        )
        logger.info("MLS matchup history armed — weekly refresh loop")
    except Exception as e:
        logger.warning("MLS matchup history loop failed to start: %s", e)

    # ── MLS Direct-Inject worker (2026-07-22) ──────────────────────
    # Bypasses the entire pick pipeline (chalk trap, longshot trap,
    # starter-gate, correlated dedupe, board validator) because those
    # layers keep killing MLS scorer picks (Surridge, Bouanga, Messi,
    # etc.). Writes picks straight to db.picks every 15 minutes.
    # ── MLS Direct Inject (2026-07-16, DB-first snapshot) ─────────
    # Extends Player Prop Intelligence to MLS.  Historically ran every
    # 15 min; converted 2026-08 to the 3×/day scheduled-snapshot
    # cadence (12:00 / 18:00 / 23:00 UTC) that matches the alt-lines
    # feed.  Writes picks straight to db.picks — the UI reads from DB
    # so there is no user-facing impact.
    try:
        from services.mls_direct_inject import run_once as _mls_direct_run
        from services.scheduled_snapshot import schedule_utc_hours
        from services.cold_start import maybe_recover_on_cold_start
        from services.job_coordinator import JobCoordinator
        from services.provider_budget import ProviderBudget
        from services.job_registry import get_job

        async def _run_under_lease_and_budget(job_name, runner, *, caller, reason):
            """Phase 2γ — every paid scheduled run acquires a
            coordinator lease + reserves budget before firing."""
            coord = JobCoordinator(db)
            budget = ProviderBudget(db)
            reg = get_job(job_name) or {}
            lease_s = int(reg.get("lease_seconds") or 600)
            min_iv  = int(reg.get("min_interval_seconds") or 1800)
            est     = int(reg.get("estimated_max_credits") or 100)
            lease = await coord.acquire(
                job_name,
                lease_seconds=lease_s,
                min_interval_seconds=min_iv,
                caller=caller,
                reason=reason,
                metadata={"scheduled": True},
            )
            if not lease:
                logger.info("[%s] scheduled skip: %s",
                             job_name, lease.get("reason"))
                return
            token = lease.lease_token
            reservation = await budget.reserve(
                estimated_credits=est, endpoint_type="snapshot",
                caller=caller, job_name=job_name,
                emergency_requested=False, reason=reason,
                request_key=f"scheduled:{job_name}:{token}",
                ttl_seconds=lease_s + 60,
            )
            if not reservation.get("allowed"):
                await coord.fail(job_name, token,
                                  error=f"budget_denied:{reservation.get('outcome')}",
                                  retry_after_seconds=300)
                logger.warning("[%s] scheduled budget denied: %s",
                                job_name, reservation.get("outcome"))
                return
            intent_id = reservation.get("intent_id")
            try:
                summary = await runner()
                await budget.commit(intent_id)
                await coord.complete(job_name, token,
                                      result_metadata={"summary": str(summary)[:400]})
                logger.info("[%s] scheduled complete: %s",
                             job_name, str(summary)[:200])
            except Exception as e:
                await budget.release(intent_id,
                                       reason=f"scheduled_error:{e}")
                await coord.fail(job_name, token, error=str(e),
                                  retry_after_seconds=300)
                logger.warning("[%s] scheduled err: %s", job_name, e)

        async def _mls_direct_snapshot_loop():
            # Phase 2γ: on cold start, read the last saved snapshot;
            # trigger recovery only if the board is missing or
            # critically stale.  No unconditional startup fan-out.
            try:
                await maybe_recover_on_cold_start(
                    db, job_name="mls_direct_inject",
                    runner=_mls_direct_run,
                    caller="startup_cold_check",
                )
            except Exception as _cs_err:
                logger.debug("mls cold_start err: %s", _cs_err)
            async for _ in schedule_utc_hours(
                name="mls_direct_inject",
                hours=[12, 18, 23],
                run_immediately=False,
            ):
                await _run_under_lease_and_budget(
                    "mls_direct_inject", _mls_direct_run,
                    caller="scheduler:mls_direct_inject",
                    reason="scheduled_snapshot",
                )

        _TASK_REGISTRY.register_and_start(
            'mls_direct_snapshot_loop', lambda: _mls_direct_snapshot_loop(),
            task_type='recurring_loop', critical=False,
        )
        logger.info("MLS Direct-Inject snapshot armed — 3×/day (12/18/23 UTC), lease-gated")
    except Exception as e:
        logger.warning("MLS Direct-Inject worker failed to start: %s", e)

    # ── Soccer Prop Inject (Big-5 + UCL, 2026-07-22) ───────────────
    # DB-first snapshot: was continuous 15-min loop; now runs on the
    # same 3×/day schedule as alt-lines to eliminate continuous Odds
    # API polling.  Frontend reads from db.picks so cadence change is
    # invisible to users.
    try:
        from services.soccer_prop_inject import run_once as _soccer_prop_run
        from services.scheduled_snapshot import schedule_utc_hours
        from services.cold_start import maybe_recover_on_cold_start

        async def _soccer_prop_snapshot_loop():
            try:
                await maybe_recover_on_cold_start(
                    db, job_name="soccer_prop_inject",
                    runner=_soccer_prop_run,
                    caller="startup_cold_check",
                )
            except Exception as _cs_err:
                logger.debug("soccer cold_start err: %s", _cs_err)
            async for _ in schedule_utc_hours(
                name="soccer_prop_inject",
                hours=[12, 18, 23],
                run_immediately=False,
            ):
                await _run_under_lease_and_budget(
                    "soccer_prop_inject", _soccer_prop_run,
                    caller="scheduler:soccer_prop_inject",
                    reason="scheduled_snapshot",
                )

        _TASK_REGISTRY.register_and_start(
            'soccer_prop_snapshot_loop', lambda: _soccer_prop_snapshot_loop(),
            task_type='recurring_loop', critical=False,
        )
        logger.info("Soccer Prop Inject snapshot armed — 3×/day (12/18/23 UTC), lease-gated")
    except Exception as e:
        logger.warning("Soccer Prop Inject worker failed to start: %s", e)

    # ── Circuit-Breaker Cross-Pod Sync (2026-08-09, ticket #222563) ───
    # Odds API circuit-breaker state (sports_engine._API_DISABLED etc.)
    # is per-process. With 2 replicas the pods diverge and an admin
    # reset only cleared whichever pod handled that request. This loop
    # pulls the shared Mongo doc every 20s so both pods converge.
    try:
        from sports_engine import sync_circuit_breaker_from_db

        async def _circuit_breaker_sync_loop():
            while True:
                try:
                    await sync_circuit_breaker_from_db()
                except Exception as e:
                    logger.warning("circuit breaker sync error: %s", e)
                await asyncio.sleep(20)

        _TASK_REGISTRY.register_and_start(
            'circuit_breaker_sync_loop', lambda: _circuit_breaker_sync_loop(),
            task_type='recurring_loop', critical=False,
        )
        logger.info("Circuit-breaker cross-pod sync armed — 20s poll")
    except Exception as e:
        logger.warning("Circuit-breaker sync loop failed to start: %s", e)

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
        _TASK_REGISTRY.register_and_start(
            'services_loop', lambda: _services_loop(),
            task_type='recurring_loop', critical=False,
        )
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
        _TASK_REGISTRY.register_and_start(
            'tennis_player_db_loop', lambda: _tennis_player_db_loop(),
            task_type='recurring_loop', critical=False,
        )
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
    # Live alt-line feed — DB-first, scheduled snapshots (2026-08).
    # Originally polled every 10 min (144 sweeps/day) which drove 77%
    # of the Odds API bill.  Now runs on the same 3×/day cadence as
    # the other snapshot loops (12:00 / 18:00 / 23:00 UTC) and only
    # fetches alt lines for events that already appear in today's
    # picks board (`picks_scope=True`).
    #
    # The quality gate still validates every alt-line pick against
    # `live_alt_lines`; picks referencing a market missing from the
    # last snapshot fail with `stale_odds` / `line_not_found` and are
    # hidden from users — same behavior as before, less credit burn.
    try:
        from alt_lines_feed import ensure_indices, refresh_alt_lines
        from services.scheduled_snapshot import schedule_utc_hours
        from services.cold_start import maybe_recover_on_cold_start
        from services.job_coordinator import JobCoordinator as _JC_alt
        from services.provider_budget import ProviderBudget as _PB_alt
        from services.job_registry import get_job as _get_job_alt

        async def _alt_lines_runner():
            return await refresh_alt_lines(
                db, picks_scope=True, event_window_hours=36,
            )

        async def _alt_lines_run_under_lease(caller: str, reason: str):
            coord = _JC_alt(db)
            budget = _PB_alt(db)
            reg = _get_job_alt("alt_lines_feed") or {}
            lease_s = int(reg.get("lease_seconds") or 600)
            min_iv  = int(reg.get("min_interval_seconds") or 1800)
            est     = int(reg.get("estimated_max_credits") or 400)
            lease = await coord.acquire(
                "alt_lines_feed",
                lease_seconds=lease_s,
                min_interval_seconds=min_iv,
                caller=caller, reason=reason,
                metadata={"scheduled": True},
            )
            if not lease:
                logger.info("[alt_lines_feed] scheduled skip: %s",
                             lease.get("reason"))
                return
            token = lease.lease_token
            reservation = await budget.reserve(
                estimated_credits=est, endpoint_type="alt_lines_snapshot",
                caller=caller, job_name="alt_lines_feed",
                emergency_requested=False, reason=reason,
                request_key=f"scheduled:alt_lines_feed:{token}",
                ttl_seconds=lease_s + 60,
            )
            if not reservation.get("allowed"):
                await coord.fail("alt_lines_feed", token,
                                  error=f"budget_denied:{reservation.get('outcome')}",
                                  retry_after_seconds=300)
                logger.warning("[alt_lines_feed] budget denied: %s",
                                reservation.get("outcome"))
                return
            intent_id = reservation.get("intent_id")
            try:
                summary = await _alt_lines_runner()
                await budget.commit(intent_id)
                await coord.complete("alt_lines_feed", token,
                                      result_metadata={"summary": str(summary)[:400]})
                logger.info("alt_lines snapshot: %s", summary)
            except Exception as e:
                await budget.release(intent_id, reason=f"error:{e}")
                await coord.fail("alt_lines_feed", token, error=str(e),
                                  retry_after_seconds=300)
                logger.warning("alt_lines snapshot err: %s", e)

        async def _alt_lines_loop():
            try:
                await ensure_indices(db)
            except Exception as ie:
                logger.warning("alt_lines indices failed: %s", ie)
            # Phase 2γ cold-start check — recover only if the last
            # snapshot is missing or critically stale, single-owner
            # across the fleet.
            try:
                await maybe_recover_on_cold_start(
                    db, job_name="alt_lines_feed",
                    runner=_alt_lines_runner,
                    caller="startup_cold_check",
                )
            except Exception as _cs_err:
                logger.debug("alt_lines cold_start err: %s", _cs_err)
            async for _ in schedule_utc_hours(
                name="alt_lines_feed",
                hours=[12, 18, 23],
                run_immediately=False,
            ):
                await _alt_lines_run_under_lease(
                    "scheduler:alt_lines_feed", "scheduled_snapshot",
                )

        _deferred_task(_alt_lines_loop, DEFER_BASE * 8)
        logger.info(
            "Live alt-line feed armed (DK+FanDuel via Odds API, "
            "3×/day snapshots at 12/18/23 UTC, picks-scope-only, "
            "coordinator+budget-gated)"
        )
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
    """Phase 3F-2 — full delegation to ApplicationLifecycle.shutdown().

    The lifecycle service owns:
      * task signalling + cancellation via runtime_task_registry
      * lease + reservation release
      * shared HTTP client close
      * MongoDB close (exactly once)
    """
    try:
        from services.application_lifecycle import get_lifecycle
        lc = get_lifecycle()
        result = await lc.shutdown(timeout=10.0)
        logger.info("Phase 3F-2 shutdown: %s", result.as_dict())
    except Exception as e:
        logger.warning("Phase 3F-2 shutdown error: %s", e)
        # Fallback close so we never leak a Mongo client.
        try:
            from services.database import close_database as _close_db
            await _close_db()
        except Exception as _e:
            logger.warning("fallback db close raised: %s", _e)
