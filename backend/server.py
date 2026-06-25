"""LockScore AI — Sports Betting Intelligence backend."""
import os
import logging
import re
import uuid
import asyncio
from datetime import datetime, timezone, timedelta, time as dtime
from pathlib import Path
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

app = FastAPI(title="PerksLocks AI")
api = APIRouter(prefix="/api")


# ────────────────────── Data version (cache-bust signal) ──────────────────────
# Bump `DATA_VERSION` whenever a backend change requires phones to wipe their
# AsyncStorage caches (changed pick schema, fabrication scrub, market filters,
# etc.). Phones poll /api/version on launch + tab focus and auto-wipe stale
# caches when their stored version differs from this one. See cachebust.ts
# on the frontend for the consumer logic.
#
# Format: YYYY.MM.DD-N
DATA_VERSION = "2026.06.24-admin-dashboard"
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


# ────────────────────── Auth ──────────────────────
# `current_user` lives in deps.py and is imported above. Keeping
# this section header as a navigational marker.


@api.post("/auth/register", response_model=Token, status_code=201)
async def register(payload: UserCreate):
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


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# Game-time cutoff for "today" feeds. We only want PREGAME picks — once
# a game starts (with a tiny 2-minute clock-skew grace for first-pitch
# timing) the pick is hidden. Player props get the same treatment as
# spreads/totals/moneyline: no live picks, period.
_PREGAME_GRACE_SECONDS = 2 * 60


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
    try:
        v1 = float(pick.get("lock_score") or 0)
    except Exception:
        v1 = 0.0
    try:
        v2 = float(pick.get("lock_score_v2") or 0)
    except Exception:
        v2 = 0.0
    if v2 > v1:
        try:
            from sports_engine import _grade, _confidence
            pick["lock_score"] = round(min(99.0, v2), 1)
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
    return pick


def _canonicalize_picks(picks: list[dict]) -> list[dict]:
    """Bulk variant of `_canonicalize_lock_score` — apply before returning
    any list of picks from an API endpoint."""
    return [_canonicalize_lock_score(p) for p in picks]


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


def _dedupe_goalscorer_per_event(picks: list[dict], top_n: int = 2) -> list[dict]:
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
      4. ELITE players ALWAYS survive — passed through unconditionally
         regardless of position in their team's win% ranking.
      5. Non-goalscorer picks pass through untouched.

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
        kept.extend(top_picks)
        kept.extend(protected_elite)
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
            # Elite override: win prob ≥ 70% AND positive edge
            if wp >= 70.0 and eg > 0:
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
    picks = await generate_all_picks(date_str, sport_filter=sport_filter)
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
        extra = await fetch_extra_tennis_picks(date_str=date_str)
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
        picks = apply_tennis_engine(picks)
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
    # ── STICKY 95+ PINS ──
    # Picks that ever crossed 95 lock_score_peak are PINNED — they survive
    # refresh wipes so a user who saw a 99-lock pick yesterday can still
    # find it on today's board (possibly with a "LINE MOVED" indicator if
    # the new generation produced a different version of the same pick).
    _pin_filter = {"$or": [
        {"lock_score_peak": {"$exists": False}},
        {"lock_score_peak": {"$lt": 95}},
    ]}
    if sport_filter:
        await db.picks.delete_many({"pick_date": date_str, "sport": sport_filter, **_pin_filter})
        await db.picks.delete_many({"id": {"$in": list(seen_ids)}, "sport": sport_filter, **_pin_filter})
    else:
        await db.picks.delete_many({"pick_date": date_str, **_pin_filter})
        await db.picks.delete_many({"id": {"$in": list(seen_ids)}, **_pin_filter})
    # Defensive write: drop malformed pick docs (missing required fields)
    # so a single broken doc never aborts the entire batch insert. Required
    # fields: id, sport, event_time, market, book_odds.
    REQUIRED = ("id", "sport", "event_time", "market", "book_odds")
    safe_picks = []
    dropped = 0
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
        safe_picks.append(p)
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

    if safe_picks:
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
    # ── GoalScorer Engine v2 shadow capture ──
    # Best-effort: log a v2 prediction for every soccer goalscorer pick
    # that just landed so calibration data starts accumulating. NEVER
    # raises — strictly shadow.
    try:
        await _shadow_capture_gs_v2(safe_picks)
    except Exception as e:
        logger.debug("gs_v2 shadow capture failed (non-fatal): %s", e)
    return len(safe_picks)


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
    """
    today = _today_str()
    count = await db.picks.count_documents({"pick_date": today})
    if count < 20:
        logger.info(
            "ensure_today_picks: only %d picks for %s — triggering refresh",
            count, today,
        )
        await _refresh_picks(today)


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
    "goalscorer":      r"anytime goal scorer|first goal scorer|last goal scorer|to score or assist",

    # ── Generic team markets ──────────────────────────────────────────────
    "moneyline":     r"\bmoneyline\b",
    "double_chance": r"\bwin or draw\b|double chance",
    "spread":        r"[+\-]\d+(\.\d+)?\s+spread\b|\bspread\b",
    "run_line":      r"\brun line\b|[+\-]\d+(\.\d+)?\s+spread\b",  # MLB run line ≡ spread

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
    "MLB": [
        {"token": "moneyline",   "label": "Moneyline"},
        {"token": "run_line",    "label": "Run Line"},
        {"token": "totals",      "label": "Totals"},
        {"token": "batter_hits",            "label": "Hits"},
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


@api.get("/picks/markets/{sport}")
async def markets_for_sport(
    user: Annotated[UserPublic, Depends(current_user)],
    sport: str,
):
    """Return the dynamic market list + active leagues for a given sport.
    Used by the Locks tab to populate the MarketSelector + League pills.

    Critically, the league `count` MUST be computed from the SAME pick
    universe that `/picks/today` serves — i.e. after `_filter_in_play_window`
    drops games that have already started. Otherwise the chip shows
    "MLB · Props · 28" but the filtered list only contains 21 picks, leaving
    the user staring at an empty state with a non-zero counter (the exact
    bug a user reported on the production app build).
    """
    markets = SPORT_MARKETS.get(sport, [])
    # Pull every pick for the sport today (raw — no qualification filter
    # here so chips remain stable across the same universe as /picks/today),
    # then apply the play-window filter and group in-Python.
    raw = await db.picks.find(
        {"sport": sport, "pick_date": _today_str()},
        {"_id": 0, "league": 1, "event_time": 1, "lock_score": 1,
         "is_under_lock": 1, "no_bet": 1, "edge_percent": 1,
         "elite_player": 1},
    ).to_list(length=1000)
    # Apply the same qualification logic /picks/today uses so the chip
    # count exactly matches the visible slate. NB: `is_under_lock` is
    # intentionally NOT excluded — under-style locks (e.g. "Total Games
    # Under 28.5") are still high-confidence picks that belong in the
    # sport tab, matching the relaxed filter in /picks/today.
    def _qualifies(p: dict) -> bool:
        if p.get("no_bet") is True:
            return False
        elite = bool(p.get("elite_player"))
        lock = float(p.get("lock_score") or 0)
        edge = float(p.get("edge_percent") or 0)
        # Same OR logic as /picks/today: base lock-score gate OR elite bypass.
        if elite:
            return True
        return lock >= 85 and edge >= 0
    raw = [p for p in raw if _qualifies(p)]
    raw = _filter_in_play_window(raw)
    counts: dict[str, int] = {}
    for p in raw:
        lg = p.get("league")
        if not lg:
            continue
        counts[lg] = counts.get(lg, 0) + 1
    leagues = [{"name": name, "count": c}
               for name, c in sorted(counts.items(), key=lambda kv: -kv[1])]
    return {"sport": sport, "markets": markets, "leagues": leagues}


@api.get("/picks/today")
async def picks_today(user: Annotated[UserPublic, Depends(current_user)],
                      sport: Optional[str] = None,
                      grade: Optional[str] = None,
                      day_offset: Optional[int] = None,
                      line_type: Optional[str] = None,
                      sort: Optional[str] = "time",
                      direction: Optional[str] = "desc",
                      min_lock: Optional[float] = None,
                      min_implied: Optional[float] = None,
                      max_implied: Optional[float] = None,
                      market: Optional[str] = None,
                      league: Optional[str] = None):
    """Top picks from today's 72-hour window (lock score >= 85).
    Filters:
      - `min_lock`: only show picks with lock_score >= this value.
      - `min_implied` / `max_implied`: only show picks whose implied
        probability falls in [min, max] (units: %, e.g. 60 → 90).
      - `market`: filter by market family token (see /picks/markets/{sport}).
      - `league`: substring-match against pick.league.
    Sort options:
      - "lock" / None (default): highest lock_score first
      - "time": soonest kickoff first
      - "edge": biggest model edge first
      - "win": highest model win-probability first
      - "implied": highest implied probability first (safest first)
    Direction:
      - "desc" (default): highest value at top — user's "best lock first" intent
      - "asc": lowest value at top (useful for finding longshots / weakest)
    """
    await _ensure_today_picks()
    # When the user explicitly filters by a single market, relax the default
    # 85+ lock floor — they're narrowing the pool themselves and want to see
    # everything that matches their selection.
    #
    # Also relax for the ALT line-type tab — alt lines like soccer
    # Over 1.5 / Under 3.5 are intentionally lower-confidence chalkier
    # OR longer-shot variations of the main consensus, so a strict 85
    # floor zeroes-out the tab entirely. User feedback: "soccer still
    # not showing alt on website or app" — drop floor to 55 for alt so
    # the synthesized lines surface.
    lt = (line_type or "").lower()
    default_floor = 75.0 if market else (55.0 if lt == "alt" else 85.0)
    floor = max(default_floor, float(min_lock)) if min_lock is not None else default_floor
    # Two-bucket query:
    #  • Standard picks: must pass lock floor + edge >= 0 + not no_bet
    #  • Elite-player anchors (Mbappé, Haaland, Messi, Kane, Ronaldo synth FGS
    #    etc.): bypass lock floor + edge filter — they're reputation-locked
    #    Elite tier even when raw math is borderline. Still must not be NO-BET.
    #
    # NOTE: We deliberately DO NOT exclude `is_under_lock` here anymore.
    # Under-style locks (e.g. "Total Games Under 28.5") are still high-
    # confidence picks the user expects to see when filtering by sport. The
    # Bet Killer / Under-of-the-Day tabs surface them separately too, but
    # users found their absence from the main sport tab confusing.
    # Floor check must consider EITHER lock_score (legacy/validator-drifted)
    # OR lock_score_v2 (canonical, refreshed every cycle). The pick_validator
    # can write a stale low value into lock_score while v2 still holds the
    # correct high score — without this OR, the home feed hides those picks
    # silently. The serializer (`_canonicalize_lock_score`) then promotes
    # whichever is higher before returning, so the user sees the right number.
    standard_q = {
        "$or": [
            {"lock_score": {"$gte": floor}},
            {"lock_score_v2": {"$gte": floor}},
        ],
        "no_bet": {"$ne": True},
        # Hide negative-edge picks from the main feed entirely.
        # Picks where model_WP < book_implied are by definition bad
        # bets (book is sharper than us). The Locks tab is for
        # actionable +EV picks only.
        "edge_percent": {"$gte": 0},
    }
    # Elite-player anchor query — marquee names (Mbappé, Haaland, Messi,
    # Kane, Ronaldo etc.) skip the strict edge ≥ 0 filter so a
    # reputation-locked superstar appears in the feed even when the
    # market doesn't quite price them as +EV. BUT we still apply a soft
    # lock-floor (≥ 80) so the user never sees the 58-67 lock garbage
    # they reported ("why is app showing 57 56 lock scores"). 80 means
    # "almost Playable" — close enough to the action band that the user
    # treats it as a marquee-name reference rather than a clearly
    # unactionable pick.
    elite_q = {
        "elite_player": True,
        "no_bet": {"$ne": True},
        "$or": [
            {"lock_score": {"$gte": 80.0}},
            {"lock_score_v2": {"$gte": 80.0}},
        ],
    }
    # ── Tennis Moneyline carve-out (bandit-hot exception) ──────────────
    # User report 2026-06-22: "Why I got so many tennis overs instead of
    # moneyline?" + "Still no money line tennis in app see spreads and
    # I see money lines on website". The bandit told us Tennis ML is our
    # HOTTEST arm (+13% ROI, Sharpe +1.11) but the edge ≥ 0 gate cuts
    # most of them because chalk tennis MLs (-200/-400) often produce
    # small negative edge vs the sharp market. Carve out: any Tennis ML
    # with a strong lock (≥ 80) gets through with edge ≥ -3, so the
    # bandit's actual winning market surfaces consistently with the book.
    #
    # Additional carve-out: `tennis_extra` picks are book-anchored
    # scrapes (TennisExplorer) with NO independent model — their reported
    # "edge_percent" comes from a self-heal validator pass that compares
    # win_prob vs book_implied, but those are intentionally equal upstream
    # so the validator's negative-edge number is meaningless. Surface
    # tennis_extra ML picks based purely on lock_score (≥ 80) so the
    # 48-hour scraped slate shows up in the feed.
    tennis_ml_q = {
        "sport": "Tennis",
        "market": {"$regex": "moneyline", "$options": "i"},
        "no_bet": {"$ne": True},
        "$or": [
            # Path 1: standard Odds-API tennis ML — must clear -3 edge floor + strong lock
            {
                "edge_percent": {"$gte": -3.0},
                "$or": [
                    {"lock_score": {"$gte": 80.0}},
                    {"lock_score_v2": {"$gte": 80.0}},
                    {"bandit_lift": {"$gt": 0}},
                ],
            },
            # Path 2: tennis_extra scraped picks — book-anchored, edge is meaningless
            {
                "source": {"$in": ["tennis_extra", "tennis_extra_model"]},
                "$or": [
                    {"lock_score": {"$gte": 80.0}},
                    {"lock_score_v2": {"$gte": 80.0}},
                ],
            },
        ],
    }
    # ── Tennis Alt-Line carve-out (chalk-ladder exception) ────────────
    # User report 2026-06-23: "you deleted all the alt spread tennis
    # after I told you to fix simulator add a tab under tennis (alt)
    # that all alt spread tennis picks". Alt-spread + alt-total tennis
    # picks (e.g. "Fritz -3.0 Spread", "Svitolina -3.5 Spread", "Under
    # 21.0 Games (Alt)") are chalk-ladder bets where the FAVORED side
    # is priced at -300 to -800. By construction these will register
    # tiny / slightly-negative edge against the sharp market, so the
    # default `edge_percent >= 0` gate erases them entirely. Surface
    # them under a relaxed gate (edge >= -8 + lock >= 70) so the user's
    # new "Alt" tab shows the chalkiest acceptable lines and the
    # synthesized chalk-totals the sports engine builds for them.
    tennis_alt_q = {
        "sport": "Tennis",
        "no_bet": {"$ne": True},
        "$or": [
            {"is_alt_prop": True},
            {"market": {"$regex": r"\(alt\)|[+\-]\d+(\.\d+)?\s+spread|\bspread\b|total games|games over|games under", "$options": "i"}},
        ],
        "edge_percent": {"$gte": -8.0},
        "$and": [{"$or": [
            {"lock_score": {"$gte": 70.0}},
            {"lock_score_v2": {"$gte": 70.0}},
        ]}],
    }
    # User report 2026-06-22: "I'm not seeing no strikeout bets" + Gerrit
    # Cole strikeout pick had lock=73.7 (strong) but edge=-6.87 (chalk-priced
    # against). Elite pitchers' K-line markets are often priced sharp, but
    # the lock score reflects the underlying probability accurately. Surface
    # these when lock >= 70 even with slight negative edge.
    #
    # 2026-06-25 update: Re-widened edge floor from -8 → -12 because chalk
    # K props like Seth Lugo Over 2.5 K's (lock 94.5, edge -8.4) and Bryce
    # Miller Over 3.5 K's (lock 94.8, edge -7.2) were appearing in the MLB
    # Lab but missing from the home board. User: "not putting lock bets on
    # board but they in the lab like strikeouts." Lab uses lock >= 78 with
    # no edge gate — board now matches that universe for the K market.
    mlb_k_q = {
        "sport": "MLB",
        "market": {"$regex": "strikeout", "$options": "i"},
        "no_bet": {"$ne": True},
        "edge_percent": {"$gte": -12.0},
        "$or": [
            {"lock_score": {"$gte": 70.0}},
            {"lock_score_v2": {"$gte": 70.0}},
        ],
    }
    # ── Soccer Goal Scorer / Score-or-Assist carve-out ──────────────────
    # User report 2026-06-23: "Goalscorers showing on soccer lab but not
    # on the board — shouldn't Harry Kane and Ronaldo be on board?"
    # Diagnosis: Ronaldo S-or-A lock=89.4 edge=-4.68%; Kane S-or-A
    # lock=89.0 edge=-2.0%. Both have strong locks (≥85) but slightly
    # negative edge because the book prices star strikers sharp. The
    # standard_q `edge ≥ 0` gate erases them entirely. Mirrors the
    # tennis ML / MLB strikeout chalk-pricing problem. Carve-out:
    # surface Anytime Goal Scorer + First Goal Scorer + Score-or-Assist
    # picks with strong locks (≥ 85) even at edge ≥ -6% so Kane,
    # Ronaldo, Watkins, etc. land on the board alongside the Soccer
    # Lab — same source of truth.
    soccer_scorer_q = {
        "sport": "Soccer",
        "market": {"$regex": "goal scorer|score or assist|score & assist", "$options": "i"},
        "no_bet": {"$ne": True},
        "edge_percent": {"$gte": -6.0},
        "$or": [
            {"lock_score": {"$gte": 85.0}},
            {"lock_score_v2": {"$gte": 85.0}},
        ],
    }
    # ── MLB Hitter alt-lock carve-out ──────────────────────────────
    # User report 2026-06-24: "Where are the hitters at for ATL/SD —
    # I don't see no 1H, HR, RBI". Diagnosis: Hits / Hits+Runs+RBIs /
    # HR / RBI / Total Bases ALT-LOCK picks (Over 0.5 lines) are
    # generated with implied prob ~94% (chalky -1500ish odds), but
    # the standard lock formula scores chalky alt-locks at 55-61 —
    # well below the board's 80 floor. Net effect: zero hitter alt-
    # locks ever surface. Same shape as the MLB pitcher-K and Soccer-
    # scorer carve-outs: surface alt-lock hitter props at lock ≥ 70
    # with edge tolerance to -3% (alt locks are chalky by design).
    #
    # 2026-06-25 update: Re-widened edge floor from -3 → -10 because
    # chalk Hits+Runs+RBIs alt-locks (e.g. Yandy Diaz Over 0.5 HRR at
    # lock 94.8, edge -7.7) were visible in the MLB Lab but missing from
    # the home board. Lab universe is the source of truth — board now
    # matches it for hitter props.
    mlb_hitter_q = {
        "sport": "MLB",
        "market": {
            "$regex": r"hits\s*\+\s*runs\s*\+\s*rbis?|\bhits?\b|home runs|\brbis?\b|total bases",
            "$options": "i",
        },
        # Exclude pitcher markets so the regex above doesn't accidentally
        # double-count strikeouts (already covered by mlb_k_q).
        "$nor": [{"market": {"$regex": "strikeout|outs recorded", "$options": "i"}}],
        "no_bet": {"$ne": True},
        "edge_percent": {"$gte": -10.0},
        "$or": [
            {"lock_score": {"$gte": 70.0}},
            {"lock_score_v2": {"$gte": 70.0}},
        ],
    }
    # ── Universal high-lock bypass ───────────────────────────────────
    # Any pick with lock_score ≥ 90 (or v2 ≥ 90) surfaces on the board
    # regardless of edge sign. Rationale: lock_score is the canonical
    # confidence signal — a Lock-band pick (90+) is by definition one
    # we're highly confident in, and chalk-priced props (negative edge
    # by construction) shouldn't be hidden just because the book agrees
    # with us. This mirrors the Soccer/MLB Lab universe (lock ≥ 78, no
    # edge gate) so the board stops disagreeing with the Lab.
    # User report 2026-06-25: "not putting lock bets on board but they
    # in the lab like strikeouts".
    high_lock_bypass_q = {
        "no_bet": {"$ne": True},
        "$or": [
            {"lock_score":    {"$gte": 90.0}},
            {"lock_score_v2": {"$gte": 90.0}},
        ],
    }
    q: dict = {
        "pick_date": _today_str(),
        "$or": [standard_q, elite_q, tennis_ml_q, tennis_alt_q, mlb_k_q, mlb_hitter_q, soccer_scorer_q, high_lock_bypass_q],
    }
    # ── User-supplied min_lock floor (global enforcement) ────────────
    # Each sub-query above uses its own lock floor (70 for tennis ML,
    # 85 for soccer scorers, 80 for elite anchors, etc.) tuned to its
    # carve-out's chalk-pricing reality. But when the user EXPLICITLY
    # slides the Min Lock filter to e.g. 95, those carve-out floors
    # would silently leak 70-94 picks back into the feed. To honour
    # the user's slider, we AND a global `lock_score >= min_lock`
    # condition over every sub-query. Check both `lock_score` and
    # `lock_score_v2` (same OR-of-both pattern used by every
    # sub-query) so picks where V2 has caught up but V1 hasn't yet
    # don't get filtered out wrongly. Default `floor` was already
    # applied per-sub-query, so this is purely about the user's
    # explicit override.
    if min_lock is not None and float(min_lock) > 0:
        user_floor = float(min_lock)
        q["$and"] = [{"$or": [
            {"lock_score":     {"$gte": user_floor}},
            {"lock_score_v2":  {"$gte": user_floor}},
        ]}]
    if sport and sport.lower() != "all":
        q["sport"] = sport
    if grade:
        q["grade"] = grade
    lt = (line_type or "").lower()
    if lt == "main":
        q["is_alt"] = {"$ne": True}
    elif lt == "alt":
        q["is_alt"] = True
    if min_implied is not None or max_implied is not None:
        imp_q: dict = {}
        if min_implied is not None:
            imp_q["$gte"] = float(min_implied)
        if max_implied is not None:
            imp_q["$lte"] = float(max_implied)
        q["implied_probability"] = imp_q
    # Market family filter — uses the same labelling we use in analytics so
    # the same token works on every sport (e.g. "moneyline", "spread",
    # "game_total", "btts", "1x2", "goalscorer", "player_points", etc.).
    if market:
        regex = _market_regex(market)
        if regex:
            q["market"] = {"$regex": regex, "$options": "i"}
    if league:
        # SEC-004: re.escape user input so metacharacters can't trigger
        # catastrophic regex backtracking (ReDoS) against MongoDB.
        q["league"] = {"$regex": re.escape(str(league)), "$options": "i"}
    cursor = db.picks.find(q, {"_id": 0}).sort("lock_score", -1).limit(200)
    picks = await cursor.to_list(length=200)
    # Hide picks for games that have already started (see _filter_in_play_window).
    picks = _filter_in_play_window(picks)
    # ── Cross-pipeline GAME OUTCOME dedupe ───────────────────────────────
    # The main pipeline (sports_engine.py) and the soccer pipeline
    # (soccer/predictor.py) BOTH write into `picks`. They can produce
    # picks on opposite sides of the same 3-way h2h market — e.g. main
    # pipeline writes "Sweden Moneyline" while soccer pipeline writes
    # "Netherlands Win or Draw" for the same game. Those bets are
    # MUTUALLY EXCLUSIVE (if Sweden wins, NL W-or-D loses) and showing
    # both makes the app look broken. Collapse to the single highest-
    # confidence side per game, preferring Win-or-Draw / Double Chance
    # over straight Moneyline (draw safety net = lower variance).
    picks = _dedupe_game_outcome_picks(picks)
    # Goalscorer pick cap — per match, surface at most the TOP 2 unique
    # players from the goalscorer family (Anytime / First / Last / To
    # Score or Assist). Without this dedupe a single player like Musiala
    # would clog the feed with 3 rows of identical lock score for the
    # same match (Anytime + First + Score-or-Assist all at lock 78.4).
    # Spec from user: "It should be the top 2".
    picks = _dedupe_goalscorer_per_event(picks, top_n=2)
    # ── Canonicalize lock_score (V2 → primary) BEFORE sorting ──────────
    # Without this, the sort uses the legacy V1 lock_score baked at pick
    # creation time. But `_canonicalize_lock_score` (called at the very
    # end) promotes lock_score_v2 to the displayed lock_score for ~25%
    # of picks — so by the time the user sees them, they're labelled
    # with HIGHER lock_scores than their position implies. The result:
    # an MLB pick at displayed lock 93.8 ends up below a Soccer pick at
    # displayed lock 92.5 — because the SORT keyed on the pick's stale
    # V1 score of e.g. 80, not its displayed-V2 of 93.8. 63/124 sort
    # inversions in the wild traced back to this exact ordering bug.
    picks = _canonicalize_picks(picks)

    # ── Lazy Evidence Governance ── (Phase 1, 2026-06-24)
    # Apply the Universal Evidence System to any pick missing an
    # `evidence_score` — typically picks generated before the engine
    # shipped. ONLY governs PENDING picks (we never re-write history
    # by adjusting a settled pick's lock score post-hoc).
    try:
        from evidence_engine import build_features_from_pick, govern_pick
        _gov_count = 0
        for _p in picks:
            if _p.get("evidence_score") is not None:
                continue
            if (_p.get("status") or "pending") != "pending":
                continue
            try:
                govern_pick(_p, build_features_from_pick(_p))
                _gov_count += 1
            except Exception:
                pass
        if _gov_count:
            logger.debug("Lazy evidence governance applied to %d picks", _gov_count)
    except Exception as _lazy_ev_err:
        logger.warning("Lazy evidence governance failed (continuing): %s", _lazy_ev_err)

    if day_offset is not None:
        target_day = (datetime.now(timezone.utc).date() + timedelta(days=day_offset)).isoformat()
        picks = [p for p in picks if (p.get("event_time") or "").startswith(target_day)]
    else:
        # Default ordering: today's games first (kickoff within 24h), then later
        # games — within each bucket, sorted by lock_score desc. Keeps the
        # "best bet for the day" front-and-center even if a 2-day-out game
        # has a higher base lock_score.
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=24)

        def _bucket(p: dict) -> int:
            et = p.get("event_time") or ""
            try:
                dt = datetime.strptime(et, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                return 0 if now <= dt <= cutoff else 1
            except Exception:
                return 1

        # Apply the user's sort preference. Default "lock": highest lock_score
        # first. "time": soonest kickoff first. "edge": biggest model edge
        # first. Direction (asc/desc) flips numerical sorts so the user can
        # find weakest picks too without having to scroll all the way down.
        s = (sort or "lock").lower()
        asc = (direction or "desc").lower() == "asc"
        # Multiplier for numerical sort fields: -1 = desc (highest first),
        # +1 = asc (lowest first). Time sort uses its own direction logic.
        m = 1 if asc else -1
        def _event_dt(p: dict) -> datetime:
            try:
                return datetime.strptime(
                    p.get("event_time") or "", "%Y-%m-%dT%H:%M:%SZ",
                ).replace(tzinfo=timezone.utc)
            except Exception:
                return datetime.max.replace(tzinfo=timezone.utc)
        # Elite-player anchor: float elite picks to the top within their
        # bucket — but ONLY for the default lock-desc view. When the user
        # has explicitly asked for asc / win / edge / time, respect their
        # chosen ordering without re-shuffling Mbappé/Haaland/Messi/Kane
        # to the top.
        def _elite_rank(p: dict) -> int:
            return 0 if p.get("elite_player") else 1
        if s == "time":
            # Pure chronological — earliest kickoff first by default;
            # latest first when asc=False reversed (we treat time asc as
            # earliest→latest, which is the natural meaning, so flip
            # signature only when direction explicitly says desc).
            # Default 'time' direction is "soonest first" which is asc by
            # natural time ordering — keep that as the default.
            if asc:
                picks.sort(key=lambda p: (_event_dt(p), -p.get("lock_score", 0)))
            else:
                picks.sort(key=lambda p: (_event_dt(p), -p.get("lock_score", 0)), reverse=True)
        elif s == "edge":
            # Pure edge sort — no today-first bucket so highest edges
            # always at top regardless of date.
            picks.sort(key=lambda p: (m * p.get("edge_percent", 0), -p.get("lock_score", 0)))
        elif s == "win":
            # Win % sort — model win_probability highest first by default.
            picks.sort(key=lambda p: (m * p.get("win_probability", 0), -p.get("lock_score", 0)))
        elif s == "implied":
            picks.sort(key=lambda p: (m * p.get("implied_probability", 0), -p.get("lock_score", 0)))
        else:  # "lock" (default)
            # Pure lock_score sort. The user's explicit ask: "It should
            # take highest score" — sorting by Lock Score should be a
            # strict ordering with no elite-player anchor, no league
            # round-robin, no bucket pre-sort. If a smaller-league pick
            # has a higher lock, it should win the top slot. Period.
            #
            # (League diversification still exists as a separate
            # affordance via the explicit league filter — surfacing
            # smaller leagues is the league pill's job, not the sort's.)
            if asc:
                picks.sort(key=lambda p: p.get("lock_score", 0))
            else:
                picks.sort(key=lambda p: -p.get("lock_score", 0))
    picks = await _decorate_with_player_form(picks)
    picks = await _decorate_with_understat_form(picks)
    return {"picks": _canonicalize_picks(picks)}


@api.get("/picks/all")
async def picks_all(user: Annotated[UserPublic, Depends(current_user)],
                    sport: Optional[str] = None):
    await _ensure_today_picks()
    q: dict = {"pick_date": _today_str()}
    if sport and sport.lower() != "all":
        q["sport"] = sport
    cursor = db.picks.find(q, {"_id": 0}).sort("lock_score", -1).limit(200)
    return {"picks": _canonicalize_picks(await cursor.to_list(length=200))}


@api.get("/picks/bet-killer", deprecated=True)
async def picks_bet_killer(user: Annotated[UserPublic, Depends(current_user)],
                           sport: Optional[str] = None):
    """DEPRECATED — Bet Killer was replaced by Under-of-the-Day.
    Returns an empty payload. Will be removed in a future release."""
    return {"picks": []}


@api.get("/picks/under-of-the-day")
async def under_of_the_day(user: Annotated[UserPublic, Depends(current_user)],
                           line_type: Optional[str] = None,
                           sort: Optional[str] = "time",
                           sport: Optional[str] = None,
                           market: Optional[str] = None,
                           league: Optional[str] = None):
    """The single safest Under lock across all sports.

    `line_type`:
      - "main": main-line totals only
      - "alt":  alt-prop Unders only
      - "both" / None: unrestricted (default)
    `sort`: "lock" (default), "time", or "edge"
    `sport` / `market` / `league`: same semantics as /picks/today.
    """
    await _ensure_today_picks()
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=24)
    q: dict = {"pick_date": _today_str(), "is_under_lock": True,
               "no_bet": {"$ne": True}}
    lt = (line_type or "").lower()
    if lt == "main":
        q["is_alt"] = {"$ne": True}
    elif lt == "alt":
        q["is_alt"] = True
    if sport and sport.lower() != "all":
        q["sport"] = sport
    if market:
        regex = _market_regex(market)
        if regex:
            q["market"] = {"$regex": regex, "$options": "i"}
    if league:
        q["league"] = {"$regex": re.escape(str(league)), "$options": "i"}  # SEC-004
    s = (sort or "lock").lower()
    if s == "time":
        cursor = db.picks.find(q, {"_id": 0}).sort("event_time", 1).limit(50)
    elif s == "edge":
        cursor = db.picks.find(q, {"_id": 0}).sort("edge_percent", -1).limit(50)
    else:
        cursor = db.picks.find(q, {"_id": 0}).sort("lock_score", -1).limit(50)
    picks = await cursor.to_list(length=50)
    # Drop picks for games that have already started — `_filter_in_play_window`
    # protects the fallback path on line 801 from leaking started games.
    picks = _filter_in_play_window(picks)

    def starts_today(p: dict) -> bool:
        et = p.get("event_time") or ""
        try:
            dt = datetime.strptime(et, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            return now <= dt <= cutoff
        except Exception:
            return False

    today_picks = [p for p in picks if starts_today(p)]
    pool = today_picks if today_picks else picks
    if not pool:
        return {"pick": None, "alternates": [], "total_evaluated": 0}

    # Rank by win probability (the higher, the safer the Under)
    pool.sort(key=lambda p: (p.get("win_probability", 0), p.get("lock_score", 0)), reverse=True)
    return {
        "pick": _canonicalize_lock_score(pool[0]),
        "alternates": _canonicalize_picks(pool[1:6]),  # 5 backup alt-Under locks
        "total_evaluated": len(pool),
        "scoped_to_today": bool(today_picks),
    }


@api.get("/picks/rollover")
async def pick_rollover(user: Annotated[UserPublic, Depends(current_user)],
                        line_type: Optional[str] = None,
                        sport: Optional[str] = None,
                        market: Optional[str] = None,
                        league: Optional[str] = None,
                        mode: str = "v2"):
    """Top 3 safest bets of the day — the user picks which one to roll.

    Rollover V2 (default) — "Survivability Mode":
      • HARD floors: odds ≥ -350, edge ≥ +5%, win_prob ≥ implied + 5pts
      • Risk-adjusted ranking (chalk penalty + edge multiplier + historical
        consistency bonus + alt-line penalty) replaces pure win_prob sort
      • At most ONE alt-line pick in the trio
      • Soccer goalscorer markets always blocked

    Modes:
      • `mode=v2` (default) — single best pick + 2 alternatives
      • `mode=split` — return 2 uncorrelated picks for split-stake bankroll
        management (lower variance than single -500 chalk)
      • `mode=v1` — legacy ranking (no floors), useful for comparison

    Rules:
      - Today's slate only (kickoff within 24h)
      - Lock score >= 90 (progressive floor)
      - NO Soccer by default (small leagues, high variance) — but if the user
        explicitly picks `sport=Soccer` we honour their choice.
      - Prefers player props over team moneylines (lower variance)
      - Diversifies: at most one pick per game / per sport
      - `line_type`: "main" / "alt" / "both" (default) — same semantics as /picks/today.
      - `sport` / `market` / `league`: optional narrowing filters.
    """
    await _ensure_today_picks()
    base_q: dict = {"pick_date": _today_str(), "no_bet": {"$ne": True}}
    lt = (line_type or "").lower()
    if lt == "main":
        base_q["is_alt"] = {"$ne": True}
    elif lt == "alt":
        base_q["is_alt"] = True
    sport_filter_active = bool(sport and sport.lower() != "all")
    if sport_filter_active:
        base_q["sport"] = sport
    if market:
        regex = _market_regex(market)
        if regex:
            base_q["market"] = {"$regex": regex, "$options": "i"}
    if league:
        base_q["league"] = {"$regex": str(league).replace("\\", ""), "$options": "i"}

    # Rollover = "most likely to hit" → require POSITIVE expected value. A
    # negative-edge pick (model WP < book implied) is a bad bet — must never
    # appear here regardless of lock_score.
    base_q["edge_percent"] = {"$gte": 0}

    # Always exclude Soccer goalscorer markets from Rollover — they're
    # high-variance lottery tickets (often 20-35% implied). Other Soccer
    # markets (Moneyline / Win-or-Draw / Totals) are now ALLOWED in rollover.
    existing_market_q = base_q.pop("market", None)
    goalscorer_block = {"market": {"$not": {"$regex": r"goal scorer|to score or assist", "$options": "i"}}}
    if existing_market_q:
        base_q["$and"] = [{"market": existing_market_q}, goalscorer_block]
    else:
        base_q["market"] = goalscorer_block["market"]

    # ── Rollover V2 — Survivability Mode ─────────────────────────────
    # Root cause of past losses (per user feedback "rollover did bad today"):
    #   • Alt-line picks at -400 to -750 odds carried lock_score=99 but
    #     edge was 0-2%. One loss wipes out 5+ wins → -EV in the long run.
    #   • Tennis Over 18.5 Games at -332 with 2% edge ("razor-thin") kept
    #     hitting the rollover and burning bankroll.
    #
    # Hard floors that EVERY rollover candidate must clear:
    #   1. book_odds ≥ -350           (no chalk bombs)
    #   2. edge_percent ≥ +5%         (no razor-thin edges)
    #   3. real EV gate: wp ≥ implied + 5  (kills lock=99 / odds=-500 traps)
    #
    # Diversification:
    #   • At most ONE alt-line pick in the trio.
    #   • At most one pick per game / per sport (existing rule).
    floors = [90, 85, 80, 75, 70]
    chalk_cap_strict = -350
    chalk_cap_relaxed = -400  # fallback if we genuinely have no safe pool

    def _implied_prob(odds: float) -> float:
        """American odds → implied probability (0..1)."""
        try:
            o = float(odds)
        except Exception:
            return 0.0
        if o == 0:
            return 0.0
        return (-o) / ((-o) + 100.0) if o < 0 else 100.0 / (o + 100.0)

    def _survivability_ok(p: dict, *, strict: bool = True) -> bool:
        """Filter that decides whether a pick deserves rollover placement."""
        odds = p.get("book_odds") or -9999
        edge = float(p.get("edge_percent") or 0)
        wp = float(p.get("win_probability") or 0)
        cap = chalk_cap_strict if strict else chalk_cap_relaxed
        if odds < cap:
            return False
        if strict and edge < 5.0:
            return False
        if not strict and edge < 3.0:
            return False
        # Real-EV gate — model must show ≥5pt edge over the book's implied
        # probability (strict) or ≥3pt (relaxed). This kills picks that look
        # locked-in only because the price is already pricing them as locks.
        implied_pct = _implied_prob(odds) * 100.0
        cushion = 5.0 if strict else 3.0
        if wp < (implied_pct + cushion):
            return False
        return True

    picks: list = []
    floor_used: int = 90
    strict_mode = True
    for f in floors:
        q = {**base_q, "lock_score": {"$gte": f}}
        cursor = db.picks.find(q, {"_id": 0})
        candidates = await cursor.to_list(length=500)
        picks = [p for p in candidates if _survivability_ok(p, strict=True)]
        if len({p.get("event") for p in picks}) >= 3:
            floor_used = f
            break
        floor_used = f
    # Relaxed fallback — only if strict mode couldn't muster 3 distinct
    # games. Still applies survivability gates, just slightly looser.
    if len({p.get("event") for p in picks}) < 3:
        strict_mode = False
        q = {**base_q, "lock_score": {"$gte": 75}}
        candidates = await db.picks.find(q, {"_id": 0}).to_list(length=500)
        picks = [p for p in candidates if _survivability_ok(p, strict=False)]
    # Last-ditch safety net — anything within reasonable bounds.
    if not picks:
        q = {**base_q, "lock_score": {"$gte": 70}}
        candidates = await db.picks.find(q, {"_id": 0}).to_list(length=500)
        picks = [p for p in candidates if (p.get("book_odds") or -9999) >= -500
                                          and float(p.get("edge_percent") or 0) >= 2.0]
    # Restrict Rollover to today's games only (start time within next 24h),
    # with graceful fallback to the broader pool if nothing starts today.
    # In ALL cases (including the fallback) we run picks through
    # _filter_in_play_window so games that have already started never leak
    # in — the "I see old MLB Hits in Rollover" bug.
    picks = _filter_in_play_window(picks)
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=24)
    def starts_today(p: dict) -> bool:
        et = p.get("event_time") or ""
        try:
            dt = datetime.strptime(et, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            return now <= dt <= cutoff
        except Exception:
            return False
    today_picks = [p for p in picks if starts_today(p)]
    pool = today_picks if today_picks else picks
    if not pool:
        return {"picks": [], "pick": None, "total_evaluated": 0}

    # User explicit ranking: win_probability first (highest chance to hit),
    # lock_score as tie-breaker, edge_percent as third tie-breaker. No
    # composite weighting — the strongest signal of "will it hit" should win.
    #
    # Rollover V2 — RISK-ADJUSTED ranking. Pure win_probability sort was
    # putting -500/-700 chalks at the top even when their edge was 0%. The
    # new composite penalizes chalk and rewards real edge + historical
    # consistency (Historical Engine signal).
    def _ev_score(p: dict) -> float:
        wp = float(p.get("win_probability") or 0)
        edge = float(p.get("edge_percent") or 0)
        odds = float(p.get("book_odds") or -100)
        # 1) Chalk penalty: 0% at -200, scaling to 30% by -350 and beyond.
        if odds <= -200:
            chalk_pen = min(0.30, (abs(odds) - 200) / 500.0)
        else:
            chalk_pen = 0.0
        # 2) Edge multiplier: +20% if edge ≥10%, baseline 1.0 at 5%.
        if edge >= 10:
            edge_mult = 1.20
        elif edge >= 7:
            edge_mult = 1.10
        else:
            edge_mult = 1.0
        # 3) Historical consistency bonus (Historical Engine).
        sig = p.get("historical_signal") or {}
        consistency = float(sig.get("consistency") or 0)
        if sig.get("label") == "hot" and consistency >= 0.7:
            hist_bonus = 1.05
        elif sig.get("label") == "cold":
            hist_bonus = 0.95
        else:
            hist_bonus = 1.0
        # 4) Alt-line penalty: alt lines have wider settlement variance.
        alt_pen = 0.92 if p.get("is_alt") else 1.0
        return wp * (1.0 - chalk_pen) * edge_mult * hist_bonus * alt_pen

    ranked = sorted(pool, key=_ev_score, reverse=True)
    # Diversify: at most one pick per game AND prefer at most one per
    # SPORT so the trio represents the day's slate broadly. Without the
    # sport-cap, soccer ML moneylines (which carry the highest single-bet
    # win probabilities) crowded out MLB / Tennis / UFC every day — user
    # spec: "where did baseball picks go".
    #
    # Algorithm:
    #   Pass 1 — pick the BEST candidate from each distinct (sport, event)
    #            cluster (no sport repeats unless we run out).
    #   Pass 2 — top up from leftover sport-repeats if we still need 3.
    seen_events: set = set()
    seen_sports: set = set()
    primary: list = []
    secondary: list = []
    alts_in_trio: int = 0
    MAX_ALTS = 1  # Rollover V2: at most ONE alt-line pick in the trio
    for p in ranked:
        ev = p.get("event")
        sp = p.get("sport") or ""
        if ev in seen_events:
            continue
        # Alt-line cap — once we have 1 alt, skip further alts.
        if p.get("is_alt") and alts_in_trio >= MAX_ALTS:
            secondary.append(p)
            continue
        if sp in seen_sports:
            secondary.append(p)
            continue
        seen_events.add(ev)
        seen_sports.add(sp)
        if p.get("is_alt"):
            alts_in_trio += 1
        primary.append({**p, "composite_rank": round(_ev_score(p), 2)})
        if len(primary) >= 3:
            break
    # Fall back to sport-repeats only if we couldn't fill the top 3 from
    # distinct sports (rare — happens when only 1 sport has eligible
    # picks today).
    top = primary
    if len(top) < 3:
        for p in secondary:
            ev = p.get("event")
            if ev in seen_events:
                continue
            seen_events.add(ev)
            top.append({**p, "composite_rank": round(p.get("win_probability", 0) or 0, 1)})
            if len(top) >= 3:
                break
    return {
        "picks": _canonicalize_picks(top),
        "pick": _canonicalize_lock_score(top[0]) if top else None,  # back-compat for older clients
        "composite_rank": top[0]["composite_rank"] if top else None,
        "total_evaluated": len(pool),
        "scoped_to_today": bool(today_picks),
        "rollover_version": "v2",
        "survivability": {
            "mode": "strict" if strict_mode else "relaxed",
            "odds_floor": chalk_cap_strict if strict_mode else chalk_cap_relaxed,
            "edge_floor": 5.0 if strict_mode else 3.0,
            "ev_cushion_pts": 5.0 if strict_mode else 3.0,
            "alt_cap": MAX_ALTS,
            "lock_floor_used": floor_used,
            "rejected_chalk": sum(
                1 for p in candidates
                if (p.get("book_odds") or -9999) < chalk_cap_strict
            ) if candidates else 0,
        },
    }


@api.get("/picks/parlay")
async def pick_parlay(user: Annotated[UserPublic, Depends(current_user)],
                     legs: int = 3,
                     mode: str = "standard",
                     sport: str | None = None,
                     line_type: str | None = None,
                     exclude_sports: str | None = None,
                     include_sports: str | None = None,
                     sport_mode: str = "auto",
                     window_hours: int = 24,
                     market: str | None = None,
                     league: str | None = None,
                     rank: int = 1,
                     locked_ids: str | None = None,
                     refresh_nonce: int = 0,
                     advanced_sub: str = "ev"):
    """Parlay Optimizer V1.1 — highest-probability parlay builder.

    Mode (`mode`):
      - standard: Lock≥88, Edge≥+3%, ROI non-negative. Target 2-5 legs.
      - high_risk: Lock≥75, Edge≥+1%. Target 10-20 legs.

    Sport selection (`sport_mode`):
      - auto: use everything (default).
      - custom: limit pool to `include_sports` (comma-separated list).
      - single: limit pool to one `sport` value AND bypass same-sport
        diversification (so 100% same sport is allowed).

    Time window (`window_hours`): only consider events with commence_time
    inside the next N hours. Defaults to 24h.

    Refresh: pass `rank=2,3,4…` to cycle through next-best candidates.
    Pin legs: pass `locked_ids` (comma-separated pick IDs).
    """
    from parlay_optimizer import (
        build_top_parlays, parlay_to_payload,
    )
    await _ensure_today_picks()
    is_high_risk = (mode or "").lower() == "high_risk"
    # "1-5H Today" is now a WINDOW overlay (not its own mode) — it works under
    # any active mode (Standard / Advanced / High Risk). Triggered whenever the
    # requested window is short (≤8h). Applies a 30-min start floor (so we
    # don't show games already starting) + auto-expand fallback if the tight
    # window is empty. The mode's lock floor / leg target rules still apply.
    is_today_window = (mode or "").lower() == "today_window" or int(window_hours or 24) <= 8
    is_advanced = (mode or "").lower() == "advanced"
    advanced_sub_norm = (advanced_sub or "ev").lower()
    if advanced_sub_norm not in ("safer", "ev"):
        advanced_sub_norm = "ev"
    mode_lower = (sport_mode or "auto").lower()
    is_single_sport = mode_lower == "single"

    # ─── Sport filter ───
    sport_filter: dict = {}
    sport_q = (sport or "").strip()
    if mode_lower == "single" and sport_q and sport_q.lower() not in ("mix", "all"):
        sport_filter = {"sport": sport_q}
    elif mode_lower == "custom" and include_sports:
        wanted = [s.strip() for s in include_sports.split(",") if s.strip()]
        if wanted:
            sport_filter = {"sport": {"$in": wanted}}
    else:
        # AUTO mode (or fallback): honour legacy exclude_sports if provided.
        if exclude_sports:
            excluded = [s.strip() for s in exclude_sports.split(",") if s.strip()]
            if excluded:
                sport_filter = {"sport": {"$nin": excluded}}

    lt = (line_type or "").lower()
    line_filter: dict = {}
    if lt == "main":
        line_filter = {"is_alt": {"$ne": True}}
    elif lt == "alt":
        line_filter = {"is_alt": True}
    market_filter: dict = {}
    if market:
        regex = _market_regex(market)
        if regex:
            market_filter = {"market": {"$regex": regex, "$options": "i"}}
    league_filter: dict = {}
    if league:
        league_filter = {"league": {"$regex": re.escape(str(league)), "$options": "i"}}  # SEC-004

    target_legs = (
        max(10, min(20, max(1, int(legs or 10)))) if is_high_risk else
        max(2, min(4, max(1, int(legs or 3)))) if is_today_window else
        # Advanced: SAFER caps at 4 legs (hit rate), EV up to 6 (more shots).
        max(2, min(4 if advanced_sub_norm == "safer" else 6, max(1, int(legs or 3))))
        if is_advanced else
        max(2, min(8, max(1, int(legs or 3))))
    )
    rank = max(1, min(20, int(rank or 1)))  # clamp refresh cursor to 1-20

    # ─── Time window filter ───
    # `commence_time` is stored as ISO-8601 string (UTC, e.g.
    # "2026-06-19T19:00:00Z"). Build a window cap and filter the DB query.
    if is_today_window:
        # "Today" mode = next 1-5 hours only. Lower bound 30 min from now
        # (give the user time to lock in) up to 5h cap.
        window_hours = 5
    else:
        window_hours = max(1, min(720, int(window_hours)))  # 1h .. 30d
    now_utc = datetime.now(timezone.utc)
    window_cap_iso = (now_utc + timedelta(hours=window_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # For today_window, push the floor 30 minutes out so we don't surface
    # games already starting / in play. Other modes use the legacy -30min
    # floor so a game starting in the next few minutes still shows.
    floor_delta = timedelta(minutes=30) if is_today_window else timedelta(minutes=-30)
    window_floor_iso = (now_utc + floor_delta).strftime("%Y-%m-%dT%H:%M:%SZ")
    time_filter = {"event_time": {"$gte": window_floor_iso, "$lte": window_cap_iso}}

    # ─── Fetch candidate pool ───
    base_q = {
        "pick_date": _today_str(),
        "no_bet": {"$ne": True},
        "is_under_lock": {"$ne": True},
        **sport_filter, **line_filter, **market_filter, **league_filter,
        **time_filter,
    }
    # Lock floor by mode: high_risk uses a looser 70 (more legs needed),
    # today_window uses a stricter 88 (same-day high-probability action),
    # standard uses 85 (balanced default).
    # Lock floor by mode. Advanced.safer is the strictest (92), high_risk the
    # loosest (70). We ALSO check `lock_score_v2` because the legacy `lock_score`
    # column can drift behind v2 between learning passes — exact same bug we
    # fixed in /picks/today. Without this `$or`, the parlay optimizer was
    # invisibly missing 30-60 picks from the board on every request, which
    # is the "on the board not populating into this system" symptom.
    if is_high_risk:
        lock_floor_val = 70
    elif is_advanced and advanced_sub_norm == "safer":
        lock_floor_val = 92
    elif is_advanced and advanced_sub_norm == "ev":
        lock_floor_val = 85
    elif is_today_window:
        lock_floor_val = 85
    else:
        lock_floor_val = 85
    # Drop any pre-existing lock_score filter and re-express as $or so v2-only
    # high scores aren't missed.
    base_q.pop("lock_score", None)
    base_q["$or"] = [
        {"lock_score": {"$gte": lock_floor_val}},
        {"lock_score_v2": {"$gte": lock_floor_val}},
    ]
    pool = await db.picks.find(base_q, {"_id": 0}).sort("lock_score", -1).limit(400).to_list(length=400)
    # Canonicalize so optimizer reads the v2-promoted lock_score.
    pool = _canonicalize_picks(pool)

    # ─── Bucket-map ROI ───
    raw_buckets = await _historical_winrates()
    bucket_map: dict = {}
    for k, v in raw_buckets.items():
        if k == "__global__":
            continue
        winrate = v.get("winrate", 0.0)
        n = v.get("n", 0)
        proxy_roi = (winrate - 0.524) / 0.524 if winrate > 0 else 0.0
        bucket_map[k] = {"roi": proxy_roi, "n": n}

    # ─── Locked picks ───
    locked_picks: list[dict] = []
    if locked_ids:
        wanted_ids = [s.strip() for s in locked_ids.split(",") if s.strip()]
        if wanted_ids:
            locked_picks = await db.picks.find(
                {"id": {"$in": wanted_ids}, "pick_date": _today_str()},
                {"_id": 0},
            ).to_list(length=len(wanted_ids))

    # ─── Load learned parlay synergy map ───
    # Per-(sport, market_family) hit rate FROM PRIOR SETTLED PARLAYS.
    # Feeds into the optimizer's leg scoring so families that have
    # historically cashed parlays get a small boost, and families that
    # tank parlays get a small penalty. Requires ≥3 settled parlays of
    # evidence per family before applying — until then synergy_bonus
    # returns 0 and the optimizer behaves identically to before.
    synergy_map: dict = {}
    try:
        from parlay_learning import load_synergy_map
        synergy_map = await load_synergy_map(db)
    except Exception as _sm_err:
        logger.warning("Parlay synergy map load failed: %s", _sm_err)

    # ─── Build ───
    # Pass through the actual mode flag. We tried passing high_risk=True for
    # today_window to relax the optimizer's MIN_EDGE check, but that also
    # bumps `min_legs` to 5 inside `build_one_parlay`, which kills 2-4 leg
    # parlays. high_risk=False (Standard rules) actually builds Today parlays
    # successfully on the same inventory — confirmed via direct optimizer
    # test (8/12 picks eligible, 3 parlays built).
    top = build_top_parlays(
        pool, target_legs=target_legs, high_risk=is_high_risk,
        bucket_map=bucket_map, rank=max(1, rank),
        locked_picks=locked_picks if locked_picks else None,
        single_sport_mode=is_single_sport,
        refresh_nonce=int(refresh_nonce or 0),
        synergy_map=synergy_map,
    )

    # ─── HIGH-RISK / TODAY SAFETY NET: auto-expand window if empty ───
    # Both high_risk and today_window need an inventory escape hatch — slates
    # can be thin (esp. early morning, between TV windows). If the initial
    # build returns nothing, widen the window step by step until something
    # qualifies or we hit 168h. Standard mode does NOT auto-expand because
    # the user explicitly asked for "today's 24h sharp action".
    auto_expanded_to: int | None = None
    expandable = is_high_risk or is_today_window
    if not top and expandable and window_hours < 168:
        # Today mode tries 8 → 12 → 24h first (still "same day-ish") before
        # going to a week, so we never blow past the user's intent.
        ladder = (8, 12, 24, 72, 168) if is_today_window else (72, 168)
        for fallback_window in ladder:
            if fallback_window <= window_hours:
                continue
            fb_cap = (now_utc + timedelta(hours=fallback_window)).strftime("%Y-%m-%dT%H:%M:%SZ")
            fb_q = {**base_q}
            fb_q["event_time"] = {"$gte": window_floor_iso, "$lte": fb_cap}
            fb_pool = await db.picks.find(fb_q, {"_id": 0}).sort("lock_score", -1).limit(400).to_list(length=400)
            if len(fb_pool) < 5:
                continue
            fb_top = build_top_parlays(
                fb_pool, target_legs=target_legs, high_risk=is_high_risk,
                bucket_map=bucket_map, rank=max(1, rank),
                locked_picks=locked_picks if locked_picks else None,
                single_sport_mode=is_single_sport,
                refresh_nonce=int(refresh_nonce or 0),
                synergy_map=synergy_map,
            )
            if fb_top:
                top = fb_top
                auto_expanded_to = fallback_window
                logger.info(
                    "%s parlay auto-expanded window %dh → %dh (%d candidate picks)",
                    "Today" if is_today_window else "High-risk",
                    window_hours, fallback_window, len(fb_pool),
                )
                break

    if not top:
        hints = []
        if mode_lower == "single" and sport_q:
            hints.append(f"in {sport_q}")
        elif mode_lower == "custom" and include_sports:
            hints.append(f"in {include_sports}")
        if window_hours != 24:
            hints.append(f"within {window_hours}h")
        hint_str = (" " + " ".join(hints)) if hints else ""
        return {
            "parlay": None,
            "parlays": [],
            "reason": (
                f"Not enough qualifying picks today{hint_str} to build a "
                f"{target_legs}-leg parlay (need Lock>=88, Edge>=+3%, "
                f"positive ROI)."
            ),
            "rank": rank,
            "locked_ids": [p.get("id") for p in locked_picks],
            "window_hours": window_hours,
            "sport_mode": mode_lower,
        }

    payloads = [parlay_to_payload(p, bucket_map) for p in top]
    # Canonicalize lock_score on every leg pick (parlay payload nests the
    # leg dicts under .legs). Single source of truth for displayed score.
    for _card in payloads:
        if isinstance(_card.get("legs"), list):
            _card["legs"] = _canonicalize_picks(_card["legs"])

    # ─── Substitute / Combination support ─────────────────────────────
    # Attach up to 5 alternate legs per parlay card — picks from the same
    # pool that didn't make the headline parlay but would be valid drop-in
    # swaps if a leg's odds move, line gets pulled, or the user wants to
    # rotate. The frontend can render these as "Swap" suggestions next to
    # each card. We pick alternates by:
    #   1. Excluding picks already used as legs in this card.
    #   2. Excluding picks from events already represented (anti-correlation).
    #   3. Sorting by lock_score desc (highest-confidence backups first).
    used_event_ids_per_card = [
        {l.get("event_id") for l in (c.get("legs") or []) if l.get("event_id")}
        for c in payloads
    ]
    used_pick_ids_per_card = [
        {l.get("id") for l in (c.get("legs") or [])}
        for c in payloads
    ]
    canonical_pool = _canonicalize_picks(pool)
    for idx, card in enumerate(payloads):
        used_events = used_event_ids_per_card[idx]
        used_ids = used_pick_ids_per_card[idx]
        alternates = [
            p for p in canonical_pool
            if p.get("id") not in used_ids
            and p.get("event_id") not in used_events
        ]
        # Top 5 alternates by lock_score for the swap UI.
        alternates.sort(key=lambda p: -(p.get("lock_score") or 0))
        card["alternates"] = alternates[:5]
        card["alternates_count"] = len(card["alternates"])
    # Persist this parlay slate into history so the learning loop has
    # data to settle and aggregate from. Cheap — dedupes by signature.
    try:
        from parlay_learning import record_parlay_shown
        for card in payloads:
            await record_parlay_shown(
                db, card, mode=mode or "standard", sport_mode=mode_lower,
            )
    except Exception as _rec_err:
        logger.warning("record_parlay_shown skipped: %s", _rec_err)
    legacy = payloads[1] if len(payloads) > 1 else payloads[0]
    return {
        "parlay": {
            "legs": legacy["legs"],
            "leg_count": legacy["leg_count"],
            "combined_decimal_odds": legacy["combined_decimal_odds"],
            "combined_american_odds": legacy["combined_american_odds"],
            "combined_win_probability": legacy["survival_pct"],
            "payout_on_100": legacy["payout_on_100"],
            "profit_on_100": legacy["profit_on_100"],
        },
        "parlays": payloads,
        "rank": rank,
        "locked_ids": [p.get("id") for p in locked_picks],
        "window_hours": window_hours,
        "auto_expanded_to": auto_expanded_to,
        "sport_mode": mode_lower,
    }

# Static routes MUST be declared BEFORE the parameterized /picks/{pick_id}
# route, otherwise FastAPI's routing would match them as a pick_id.

@api.post("/picks/settle")
async def trigger_settle(user: Annotated[UserPublic, Depends(current_user)]):
    """Manually trigger settlement (also runs every 30 min in background)."""
    result = await settle_due_picks(db)
    return result


@api.get("/picks/history")
async def picks_history(user: Annotated[UserPublic, Depends(current_user)],
                        days: int = 30,
                        rollover_only: bool = False):
    """Settled picks from the last N days, newest first.

    Applies the same correlated-pick dedup as the live picks endpoint so the
    History tab doesn't show "Player Over 0.5 Hits" AND "Player Over 0.5
    Total Bases" as two separate losses — they're one logical bet.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    q: dict = {
        "settled_at": {"$gte": cutoff},
        # Hide voided picks (e.g. the legacy soccer goalscorer payloads
        # generated before the new top-3-scorers logic shipped, which
        # wouldn't have made the cut under the new rules). Voided picks
        # are kept in the DB for the learning engine but never shown in
        # the user-facing History tab or counted toward W/L stats.
        "status": {"$nin": ["void"]},
        "excluded_from_history": {"$ne": True},
        # ── Board-floor gate (added 2026-06-23 — user complaint
        # "Why are picks like this being graded shouldn't be in
        # history wasn't on the board"). The pipeline generates
        # picks for many markets that the LIVE feed filters out for
        # low lock scores (Bosnia vs Switzerland "Score or Assist"
        # picks at lock 67-75 etc.). Those picks settle and then
        # leak into PICK HISTORY even though the user never saw
        # them. Result: a Lost record that pollutes the hit-rate.
        #
        # Fix: only show in history picks that ACTUALLY crossed the
        # surfacing floor (lock_score ≥ 80, matching the lowest
        # carve-out floor used by /picks/today). Use raw_lock_score
        # when present so the calibration overlay (which can lower
        # the display number for pending picks) doesn't accidentally
        # hide legitimate history rows. Settled picks were never
        # recalibrated by design so `lock_score` is still the
        # historical raw value for them.
        "$or": [
            {"lock_score": {"$gte": 80}},
            {"raw_lock_score": {"$gte": 80}},
            # Carve-out: elite-pitcher override picks were intentionally
            # surfaced even at lock<80 with strong edge — preserve them.
            {"elite_pitcher_override": True},
            {"is_alt": True, "lock_score": {"$gte": 75}},
        ],
    }
    cursor = db.picks.find(q, {"_id": 0}).sort("settled_at", -1).limit(2000)
    picks = await cursor.to_list(length=2000)

    # ─── Dedupe correlated historical picks ───
    # Same logic as sports_engine.generate_all_picks. Group by
    # (sport, event, selection, line_threshold) and keep the preferred one:
    #   1) Market family — Hits > anything > Total Bases
    #   2) Settled status outcome consistency (prefer won > lost > push > pending)
    #      so the user sees the strongest historical signal for that bet.
    #   3) Higher lock_score, then better odds.
    import re as _re
    def _key(p: dict) -> tuple:
        market = p.get("market") or ""
        m = _re.search(r"(-?\d+\.\d+)", market)
        return (
            p.get("sport"), p.get("event"), p.get("selection") or "",
            m.group(1) if m else "",
        )
    def _market_priority(market: str) -> int:
        m = (market or "").lower()
        if "hits" in m: return 0
        if "win or draw" in m or "double chance" in m: return 0
        if "moneyline" in m: return 2
        if "total bases" in m: return 2
        return 1
    _STATUS_RANK = {"won": 0, "lost": 1, "push": 2, "pending": 3}

    best: dict = {}
    for p in picks:
        k = _key(p)
        ex = best.get(k)
        if ex is None:
            best[k] = p
            continue
        new_pri = _market_priority(p.get("market"))
        old_pri = _market_priority(ex.get("market"))
        if new_pri != old_pri:
            if new_pri < old_pri:
                best[k] = p
            continue
        new_stat = _STATUS_RANK.get(p.get("status") or "pending", 4)
        old_stat = _STATUS_RANK.get(ex.get("status") or "pending", 4)
        if new_stat != old_stat:
            if new_stat < old_stat:
                best[k] = p
            continue
        if (p.get("lock_score") or 0) > (ex.get("lock_score") or 0):
            best[k] = p
        elif (p.get("lock_score") or 0) == (ex.get("lock_score") or 0):
            if (p.get("book_odds") or -9999) > (ex.get("book_odds") or -9999):
                best[k] = p
    picks = sorted(best.values(), key=lambda p: p.get("settled_at") or "", reverse=True)

    settled = [p for p in picks if p.get("status") in ("won", "lost", "push")]
    won = sum(1 for p in settled if p.get("status") == "won")
    lost = sum(1 for p in settled if p.get("status") == "lost")
    push = sum(1 for p in settled if p.get("status") == "push")
    decided = won + lost
    hit_rate = round(won / decided * 100, 1) if decided else 0.0
    rollover_picks = [p for p in settled if (p.get("lock_score") or 0) >= 90]
    ro_won = sum(1 for p in rollover_picks if p.get("status") == "won")
    ro_lost = sum(1 for p in rollover_picks if p.get("status") == "lost")
    ro_push = sum(1 for p in rollover_picks if p.get("status") == "push")
    ro_decided = ro_won + ro_lost
    ro_hit_rate = round(ro_won / ro_decided * 100, 1) if ro_decided else 0.0
    if rollover_only:
        # Stats must reflect the SAME scope as the returned picks list.
        # Previously we returned rollover_picks but kept the broader stats —
        # showed e.g. "6 picks · 77 won" which was nonsense.
        settled = rollover_picks
        return {
            "picks": settled,
            "stats": {
                "total": len(settled),
                "won": ro_won,
                "lost": ro_lost,
                "push": ro_push,
                "hit_rate": ro_hit_rate,
                "rollover_hit_rate": ro_hit_rate,
                "rollover_decided": ro_decided,
            },
        }
    return {
        "picks": settled,
        "stats": {
            "total": len(settled),
            "won": won,
            "lost": lost,
            "push": push,
            "hit_rate": hit_rate,
            "rollover_hit_rate": ro_hit_rate,
            "rollover_decided": ro_decided,
        },
    }


@api.get("/picks/refresh-status")
async def refresh_status_pre(user: Annotated[UserPublic, Depends(current_user)]):
    """Return the user's current refresh cooldown WITHOUT triggering a
    refresh (zero Odds API cost). Declared BEFORE /picks/{pick_id} so
    FastAPI's route matching doesn't capture the literal segment as an
    ID. Logic lives in _cooldown_payload() further down."""
    now = datetime.now(timezone.utc)
    user_doc = await db.users.find_one(
        {"id": user.id}, {"_id": 0, "last_refresh_at": 1},
    )
    last_iso = (user_doc or {}).get("last_refresh_at")
    return _cooldown_payload(last_iso, now)


@api.get("/picks/{pick_id}")
async def pick_detail(pick_id: str,
                      user: Annotated[UserPublic, Depends(current_user)]):
    pick = await db.picks.find_one({"id": pick_id}, {"_id": 0})
    if not pick:
        raise HTTPException(status_code=404, detail="Pick not found")
    # Canonicalize lock_score → max(v1, v2) so detail view matches the home
    # feed card. Single source of truth — see `_canonicalize_lock_score` doc.
    pick = _canonicalize_lock_score(pick)
    # Lazy evidence governance — see /api/picks/today for context.
    if pick.get("evidence_score") is None and (pick.get("status") or "pending") == "pending":
        try:
            from evidence_engine import build_features_from_pick, govern_pick
            govern_pick(pick, build_features_from_pick(pick))
        except Exception as _ev_err:
            logger.debug("Evidence governance failed in detail view: %s", _ev_err)
    if not pick.get("explanation"):
        from ai_engine import _fallback_explanation
        # Every pick reaching the UI is a recommended pick — always use the
        # "why to BET" fallback. Legacy bet-killer warning path retired.
        pick["explanation"] = _fallback_explanation(pick)
        pick["ai_pending"] = True
    else:
        pick["ai_pending"] = False
    return pick


@api.post("/picks/{pick_id}/ai-explain")
async def pick_ai_explain(pick_id: str,
                          user: Annotated[UserPublic, Depends(current_user)]):
    """Generate (or fetch cached) Claude Sonnet 4.5 explanation for a pick.
    Frontend calls this after the initial pick_detail render so the spinner
    stays scoped to the AI box only."""
    pick = await db.picks.find_one({"id": pick_id}, {"_id": 0})
    if not pick:
        raise HTTPException(status_code=404, detail="Pick not found")
    # If we already cached a real AI explanation, scrub any stale lock-score
    # / win-probability NUMERIC references from it before returning. The
    # numbers can shift via the Evidence Governor and read-time V2
    # canonicalization, so the live values come from the response payload
    # itself — never from cached narrative text (iter-50 finding #2).
    cached = pick.get("explanation_ai")
    if cached:
        import re as _re
        scrubbed = _re.sub(
            r"\b(Lock(?:\s*Score)?|Win(?:\s*Probability)?|Edge)\s*[:=]?\s*"
            r"[\-+]?\d+(?:\.\d+)?\s*%?",
            "",
            cached,
            flags=_re.IGNORECASE,
        )
        scrubbed = _re.sub(r"\s{2,}", " ", scrubbed).strip(" |·,;-")
        return {"explanation": scrubbed or cached, "source": "cached"}
    # All picks reaching the UI are recommended picks (NO_BET filter removed
    # the bad ones). Always generate the "why to BET" explanation.
    text, real = await explain_pick(pick)
    if real:
        await db.picks.update_one(
            {"id": pick_id},
            {"$set": {"explanation": text, "explanation_ai": text}},
        )
    return {"explanation": text, "source": "live" if real else "fallback"}


REFRESH_COOLDOWN_SECONDS = 3600  # 1 hour — matches scheduler cadence


def _cooldown_payload(last_iso: str | None, now: datetime) -> dict:
    """Compute cooldown state for the refresh rate-limiter.

    Returns dict with `can_refresh`, `cooldown_seconds` (remaining),
    `next_refresh_at` (ISO string, or None), `last_refresh_at`.
    Safe against missing/malformed timestamps.
    """
    if not last_iso:
        return {
            "can_refresh": True,
            "cooldown_seconds": 0,
            "next_refresh_at": None,
            "last_refresh_at": None,
        }
    try:
        last_dt = datetime.fromisoformat(last_iso)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
    except Exception:
        return {
            "can_refresh": True,
            "cooldown_seconds": 0,
            "next_refresh_at": None,
            "last_refresh_at": None,
        }
    elapsed = (now - last_dt).total_seconds()
    remaining = max(0, REFRESH_COOLDOWN_SECONDS - int(elapsed))
    next_dt = last_dt + timedelta(seconds=REFRESH_COOLDOWN_SECONDS)
    return {
        "can_refresh": remaining <= 0,
        "cooldown_seconds": remaining,
        "next_refresh_at": next_dt.isoformat() if remaining > 0 else None,
        "last_refresh_at": last_dt.isoformat(),
    }


@api.post("/picks/refresh")
async def force_refresh(user: Annotated[UserPublic, Depends(current_user)]):
    """Manually refresh today's picks. Rate-limited to 1× per hour per user
    to prevent button-mashing that burns The Odds API credits
    (each refresh costs ~250-400 credits)."""
    now = datetime.now(timezone.utc)
    # Check last refresh time for this user (stored in user doc).
    user_doc = await db.users.find_one({"id": user.id}, {"_id": 0, "last_refresh_at": 1})
    last_iso = (user_doc or {}).get("last_refresh_at")
    cd = _cooldown_payload(last_iso, now)
    if not cd["can_refresh"]:
        remaining_min = (cd["cooldown_seconds"] // 60) + (1 if cd["cooldown_seconds"] % 60 else 0)
        existing = await db.picks.count_documents({"pick_date": _today_str()})
        return {
            "refreshed": False,
            "rate_limited": True,
            "retry_after_minutes": remaining_min,
            "cooldown_seconds": cd["cooldown_seconds"],
            "next_refresh_at": cd["next_refresh_at"],
            "last_refresh_at": cd["last_refresh_at"],
            "count": existing,
            "date": _today_str(),
            "message": f"Picks were refreshed recently. Try again in {remaining_min} min — saves API credits.",
        }
    # Fire-and-forget: kick off the actual refresh in the background.
    # `_refresh_picks` takes ~45 s end-to-end (Odds API fetch +
    # generation + brain filter + validator) which exceeds mobile HTTP
    # timeouts, so the user's app would show "Refresh failed" even
    # when the refresh actually succeeded. We now mark cooldown
    # immediately, return instantly, and let the user's existing
    # focus-refetch (30 s) pull the new picks once they land.
    await db.users.update_one(
        {"id": user.id},
        {"$set": {"last_refresh_at": now.isoformat()}},
    )
    asyncio.create_task(_refresh_picks(_today_str()))
    existing = await db.picks.count_documents({"pick_date": _today_str()})
    next_dt = now + timedelta(seconds=REFRESH_COOLDOWN_SECONDS)
    return {
        "refreshed": True,
        "queued": True,
        "count": existing,                   # current count; new count lands soon
        "date": _today_str(),
        "cooldown_seconds": REFRESH_COOLDOWN_SECONDS,
        "next_refresh_at": next_dt.isoformat(),
        "last_refresh_at": now.isoformat(),
        "note": "Refresh started in background (~45 s). New picks will appear automatically on the next focus-refetch.",
    }

# ───────────────────────── Loss Analysis ─────────────────────────

@api.post("/picks/{pick_id}/loss-analysis")
async def pick_loss_analysis(pick_id: str,
                             user: Annotated[UserPublic, Depends(current_user)]):
    """AI 'Why It Lost' breakdown for a losing pick."""
    pick = await db.picks.find_one({"id": pick_id}, {"_id": 0})
    if not pick:
        raise HTTPException(status_code=404, detail="Pick not found")
    if pick.get("status") != "lost":
        return {"analysis": "This pick wasn't recorded as a loss. No analysis available.",
                "source": "skip"}
    if pick.get("loss_analysis"):
        return {"analysis": pick["loss_analysis"], "source": "cached"}
    text, real = await analyze_loss(pick)
    if real:
        await db.picks.update_one(
            {"id": pick_id},
            {"$set": {"loss_analysis": text, "loss_analysis_ai": text}},
        )
    return {"analysis": text, "source": "live" if real else "fallback"}


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



@api.get("/picks/{pick_id}/probability")
async def picks_probability(
    pick_id: str,
    user: Annotated[UserPublic, Depends(current_user)],
):
    """Unified Probability Engine breakdown for a single pick.

    Same source of truth as the inline `pick.probability` block
    attached to every pick by `_canonicalize_lock_score` — calling
    this endpoint is functionally identical to reading
    `/api/picks/today` and inspecting that pick's `probability` field.
    Provided as a standalone endpoint for clients that only want the
    breakdown without re-fetching the full pick payload.
    """
    pick = await db.picks.find_one({"id": pick_id}, {"_id": 0})
    if not pick:
        raise HTTPException(status_code=404, detail="Pick not found")
    from probability_engine import unified_probability_report
    return unified_probability_report(pick)


@api.get("/picks/{pick_id}/player-form")
async def pick_player_form(
    pick_id: str,
    user: Annotated[UserPublic, Depends(current_user)],
):
    """Soccer goalscorer-market player form panel.

    Surfaces the Understat-derived per-player season metrics (xG/90,
    npxG/90, goals over xG, shots/90 + form classification) for the
    player named in the goalscorer pick. Returns 404 cleanly if:
      - The pick isn't a soccer goalscorer market
      - The player isn't yet in the form DB (refresh hasn't run, or
        player isn't in the Top 5 European leagues)

    Form-based ±6% probability lift derived from this same row is
    already baked into the pick's lock score; this endpoint is purely
    for transparency / UI display in the deep-dive pane.
    """
    pick = await db.picks.find_one({"id": pick_id}, {"_id": 0})
    if not pick:
        raise HTTPException(status_code=404, detail="Pick not found")
    from soccer_player_form import (
        is_goalscorer_market, get_player_form, compute_form_lift,
    )
    if not is_goalscorer_market(pick):
        raise HTTPException(
            status_code=404,
            detail="Player form available for soccer goalscorer markets only",
        )
    # Extract player name from the market string (strips suffixes like
    # "Anytime Goal Scorer"). Goalscorer-pick `selection` is usually
    # "Yes" / "No" so we rely on the resolver as the primary source.
    try:
        from player_intel.resolver import extract_player_from_market
        player_name = extract_player_from_market(pick.get("market", "") or "") or ""
    except Exception:
        player_name = ""
    if not player_name:
        # Fallback chain — covers older picks where the player is in
        # `player`/`bet`/`selection` and the market string has no name.
        player_name = (
            pick.get("player") or pick.get("bet")
            or pick.get("selection") or ""
        ).strip()
    if not player_name or player_name.lower() in {"yes", "no", "over", "under"}:
        raise HTTPException(status_code=404, detail="Pick has no resolvable player name")
    form_doc = await get_player_form(db, player_name)
    if not form_doc:
        raise HTTPException(
            status_code=404,
            detail=f"No form data for {player_name} (not in Top 5 leagues, "
                   "or form refresh hasn't completed yet)",
        )
    # Strip Mongo ObjectId-equivalent fields and serialise datetimes
    updated_at = form_doc.get("updated_at")
    if hasattr(updated_at, "isoformat"):
        updated_at = updated_at.isoformat()
    return {
        "player_name":     form_doc.get("player_name"),
        "team":            form_doc.get("team"),
        "league":          form_doc.get("league"),
        "season":          form_doc.get("season"),
        "position":        form_doc.get("position"),
        "games":           form_doc.get("games"),
        "minutes":         form_doc.get("minutes"),
        "goals":           form_doc.get("goals"),
        "xg":              form_doc.get("xg"),
        "npxg":            form_doc.get("npxg"),
        "assists":         form_doc.get("assists"),
        "xa":              form_doc.get("xa"),
        "shots":           form_doc.get("shots"),
        "key_passes":      form_doc.get("key_passes"),
        "xg_per_90":       form_doc.get("xg_per_90"),
        "npxg_per_90":     form_doc.get("npxg_per_90"),
        "goals_per_90":    form_doc.get("goals_per_90"),
        "shots_per_90":    form_doc.get("shots_per_90"),
        "goals_over_xg":   form_doc.get("goals_over_xg"),
        "form_label":      form_doc.get("form_label"),
        "form_score":      form_doc.get("form_score"),
        "form_lift":       compute_form_lift(form_doc),
        "updated_at":      updated_at,
        "source":          form_doc.get("source") or "understat",
    }


# Soccer/Tennis admin ops endpoints
# (`/admin/refresh-soccer-player-form` + `/admin/backfill-tennis-elo`)
# now live in `routes/admin_routes.py`. Mounted in the app-wiring
# section near the bottom of this file.


@api.get("/picks/{pick_id}/pitcher-h2h")
async def pick_pitcher_h2h(
    pick_id: str,
    user: Annotated[UserPublic, Depends(current_user)],
):
    """MLB strikeout pick → pitcher's historical K performance vs opposing team.

    Returns: season K avg, vs-team K avg, last 5 starts vs the opposing team
    (date, opp, K count, IP). Only resolves for MLB strikeout markets.
    """
    pick = await db.picks.find_one({"id": pick_id}, {"_id": 0})
    if not pick:
        raise HTTPException(status_code=404, detail="Pick not found")
    if (pick.get("sport") or "") != "MLB" or "strikeout" not in (pick.get("market") or "").lower():
        raise HTTPException(status_code=404, detail="Pitcher H2H available for MLB strikeout picks only")
    # Extract pitcher name from "Gerrit Cole (NYY) Over 3.5 Strikeouts"
    import re
    market_str = pick.get("market") or ""
    m = re.match(r"^([A-Z][^()]+?)\s*\(", market_str)
    if not m:
        # Fallback to selection field which is just the pitcher's name
        sel = pick.get("selection") or ""
        if not sel:
            raise HTTPException(status_code=404, detail="Could not parse pitcher name")
        pitcher = sel.strip()
    else:
        pitcher = m.group(1).strip()
    # Opposing team — resolve via abbreviation in market parens
    event = pick.get("event") or ""
    pteam_m = re.search(r"\(([A-Z]{2,4})\)", market_str)
    pteam = pteam_m.group(1) if pteam_m else ""
    from mlb_pitcher_h2h import fetch_pitcher_h2h, resolve_opp_team_name
    opp_team = resolve_opp_team_name(event, pteam) if pteam else None
    if not opp_team:
        # Last-resort: just default to the 2nd team in the event string
        parts = re.split(r"\s+(?:@|vs)\s+", event)
        opp_team = (parts[1].strip() if len(parts) == 2 else "").strip()
    if not opp_team:
        raise HTTPException(status_code=404, detail="Could not parse opponent team")
    return await fetch_pitcher_h2h(pitcher, opp_team)


@api.get("/picks/{pick_id}/simulation")
async def pick_simulation(
    pick_id: str,
    user: Annotated[UserPublic, Depends(current_user)],
):
    """Run Monte Carlo on a single pick on demand. Returns sim output dict
    with sim_win_probability, 95% Wilson CI, runs, market category,
    disagreement vs blended model. Supports MLB, Soccer, NBA, Tennis."""
    pick = await db.picks.find_one({"id": pick_id}, {"_id": 0})
    if not pick:
        raise HTTPException(status_code=404, detail="Pick not found")
    sport = pick.get("sport") or ""
    if sport not in {"MLB", "Soccer", "NBA", "Tennis"}:
        raise HTTPException(status_code=404, detail=f"Simulation not yet available for {sport or 'this sport'}")
    from brain.sim_runner import simulate_pick
    sim = simulate_pick(pick)
    if not sim:
        raise HTTPException(status_code=404, detail="Simulator could not route this market")
    return sim


# ── Analytics endpoints (15 endpoints) ─────────────────────────────
# Moved to routes/analytics_routes.py during Phase-2 monolith
# decomposition. Mounted via app.include_router() below.


@api.get("/")
async def root():
    return {"ok": True, "service": "PerksLocks AI", "date": _today_str()}


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
except Exception as _routes_mount_err:
    logger.exception("Extracted route modules failed to mount: %s", _routes_mount_err)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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
_MLB_WINDOW_END_UTC_HOUR = 23          # 7 PM ET (catches late west-coast first pitches)


async def _mlb_pregame_loop():
    """Refresh MLB picks every 5 min during US daytime so player props
    surface 60–90 min pre-game instead of 5–10 min pre-game."""
    # Let startup settle so the initial seed completes first.
    await asyncio.sleep(120)
    while True:
        try:
            now = datetime.now(timezone.utc)
            hour = now.hour
            if _MLB_WINDOW_START_UTC_HOUR <= hour < _MLB_WINDOW_END_UTC_HOUR:
                await _refresh_picks(_today_str(), sport_filter="MLB")
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


@app.on_event("startup")
async def on_startup():
    await db.users.create_index("email", unique=True)
    await db.picks.create_index([("pick_date", 1), ("sport", 1)])
    await db.picks.create_index([("pick_date", 1), ("lock_score", -1)])
    await db.picks.create_index([("status", 1), ("settled_at", -1)])
    await db.picks.create_index("id", unique=True)
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
        logger.info("Historical Sports Intelligence Engine wired to MongoDB")
    except Exception as e:
        logger.warning("Historical Engine not armed: %s", e)
    asyncio.create_task(_daily_refresh_loop())
    asyncio.create_task(_mlb_pregame_loop())
    logger.info(
        "MLB pregame quick-refresh loop armed (%d-sec cadence during UTC %02d:00–%02d:00)",
        _MLB_QUICK_REFRESH_INTERVAL,
        _MLB_WINDOW_START_UTC_HOUR, _MLB_WINDOW_END_UTC_HOUR,
    )
    asyncio.create_task(_settlement_loop())
    asyncio.create_task(_weekly_model_tuning_loop())
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
        asyncio.create_task(soccer_pipeline_loop(db))
        asyncio.create_task(soccer_backfill_loop(db))
        logger.info("Soccer pipeline scheduler armed (15-min pregame loop + 24h backfill loop)")
    except Exception as e:
        logger.warning("Soccer pipeline scheduler not armed: %s", e)
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
        asyncio.create_task(soccer_player_form_loop(db))
        logger.info("Soccer Player Form (Understat) armed (12h loop, Top 5 leagues)")
    except Exception as e:
        logger.warning("Soccer Player Form scheduler not armed: %s", e)
    # ── MLB Lineup Verifier ─────────────────────────────────────────
    # Voids picks for scratched MLB players ~30 min before first pitch.
    try:
        from mlb_lineup import lineup_verifier_loop
        asyncio.create_task(lineup_verifier_loop(db, _today_str))
        logger.info("MLB lineup verifier armed (5-min loop, 30-min pre-game)")
    except Exception as e:
        logger.warning("MLB lineup verifier failed to start: %s", e)
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
    # ── Tennis Extra Settler ────────────────────────────────────────
    # Settles Mallorca/Eastbourne/Challenger picks from TennisExplorer
    # results page. Runs every 30 min, walks back 3 days.
    try:
        from tennis_extra.settle import tennis_extra_settler_loop
        asyncio.create_task(tennis_extra_settler_loop(db))
        logger.info("Tennis Extra settler armed (30-min loop)")
    except Exception as e:
        logger.warning("Tennis Extra settler failed to start: %s", e)
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

    # Seed/promote the platform owner as admin (user request 2026-06-24).
    # Idempotent: only promotes — never creates the account if it doesn't
    # already exist. The owner must register first via the normal flow,
    # then they auto-become admin on next backend boot.
    try:
        OWNER_EMAIL = "bossmanperkins@yahoo.com"
        await db.users.update_one(
            {"email": OWNER_EMAIL},
            {"$set": {"role": "admin", "status": "active"}},
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
        asyncio.create_task(line_observer_loop(db))
        asyncio.create_task(closing_snapshotter_loop(db))
        logger.info("Closing-line snapshotter started (CLV tracking enabled)")
    except Exception as e:
        logger.warning("CLV snapshotter failed to start: %s", e)

    logger.info("PerksLocks AI started")


@app.on_event("shutdown")
async def on_shutdown():
    client.close()
