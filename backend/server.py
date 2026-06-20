"""LockScore AI — Sports Betting Intelligence backend."""
import os
import logging
import uuid
import asyncio
from datetime import datetime, timezone, timedelta, time as dtime
from pathlib import Path
from typing import Annotated, Optional

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
from ai_engine import explain_pick, bet_killer_warning, analyze_loss  # noqa: E402
from settlement_engine import settle_due_picks  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("lockscore")

# Production-safe env loading with sane fallbacks so deployment doesn't crash
# if env vars aren't set on the production environment.
mongo_url = os.environ.get("MONGO_URL") or "mongodb://localhost:27017"
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ.get("DB_NAME") or "perkslocks_production"]

app = FastAPI(title="PerksLocks AI")
api = APIRouter(prefix="/api")


# ────────────────────── Auth ──────────────────────

async def current_user(
    token: Annotated[Optional[str], Depends(oauth2_scheme)],
) -> UserPublic:
    return await get_current_user_from_db(db, token)


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
async def login(payload: UserLogin):
    doc = await db.users.find_one({"email": payload.email.lower()})
    if not doc or not verify_password(payload.password, doc["hashed_password"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    public = UserPublic(id=doc["id"], email=doc["email"], name=doc.get("name"),
                        created_at=doc.get("created_at"))
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


def _filter_in_play_window(picks: list[dict]) -> list[dict]:
    """Drop picks whose game has already started.

    User spec: "I do want pregame picks I don't want live picks." So
    once an event's `event_time` is in the past (beyond a tiny clock-skew
    grace) we drop the pick from the visible slate — even player props,
    even MLB Hits/Strikeouts. Reused across /picks/today,
    /picks/bet-killer, /picks/under-of-the-day, and /picks/rollover.
    """
    now_utc = datetime.now(timezone.utc)
    out: list[dict] = []
    for p in picks:
        et = p.get("event_time") or ""
        try:
            dt = datetime.strptime(et, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except Exception:
            # Unknown time → keep the pick (safe default).
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


def _dedupe_goalscorer_per_event(picks: list[dict], top_n: int = 2) -> list[dict]:
    """Per-team-in-match cap on goalscorer / score-or-assist picks.

    The pick engine generates THREE variants per qualifying player
    (Anytime / First / To Score-or-Assist). For a 22-player World Cup
    match, that's 60+ near-identical picks dominating the feed.

    Rules:
      1. Group goalscorer picks by (sport, event, team) — TEAM is the
         critical addition. Without it, the "top 2 by win-probability"
         pick favors whichever side has more elite strikers (e.g.
         Netherlands' Depay+Gakpo+Malen will crowd out Sweden's
         Gyökeres on every Sweden @ Netherlands card).
      2. Within each (event, team) bucket, collapse to one row per
         player — keep highest win% market. Anytime > Score-or-Assist
         > First Goal Scorer on tie.
      3. Keep TOP N players per (event, team) — both sides of the
         match get their own quota.
      4. ELITE players ALWAYS survive — passed through unconditionally
         regardless of position in their team's win% ranking.
      5. Non-goalscorer picks pass through untouched.
    """
    GOALSCORER_KEYWORDS = (
        "anytime goal scorer", "first goal scorer", "last goal scorer",
        "to score or assist", "to score",
    )

    def _is_scorer(p: dict) -> bool:
        m = (p.get("market") or "").lower()
        return any(k in m for k in GOALSCORER_KEYWORDS)

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

    by_event_team: dict = {}
    passthrough: list[dict] = []
    for p in picks:
        if not _is_scorer(p):
            passthrough.append(p)
            continue
        player = _player_from_market(p.get("market") or "")
        team = _player_team_for_event(player, p.get("event") or "", p.get("sport") or "")
        # Fall back to a generic "?" team bucket when we can't ID the
        # player's side — keeps these picks visible, just not balanced.
        key = (p.get("sport"), p.get("event"), team or "?")
        by_event_team.setdefault(key, []).append(p)

    kept: list[dict] = []
    for key, group in by_event_team.items():
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
        # Step 3: top N by win_probability per team.
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
    return passthrough + kept


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



async def _refresh_picks(date_str: str) -> int:
    """Generate today's picks, replace any existing rows for that date.

    Critical: only delete existing picks AFTER we've successfully generated
    new ones. Otherwise, if the upstream API is down/rate-limited, we'd
    end up with an empty board instead of last-known-good picks.

    Pick IDs are deterministic (UUID5 derived from external_id) so cached
    references in user slips and the frontend remain valid across refreshes
    instead of pointing to a brand-new UUID that 404s.
    """
    logger.info("Refreshing picks for %s", date_str)
    picks = await generate_all_picks(date_str)
    if not picks:
        logger.warning(
            "Refresh produced 0 picks for %s — keeping existing rows intact "
            "instead of wiping the board.", date_str,
        )
        return 0
    namespace = uuid.UUID("00000000-0000-0000-0000-000000000001")
    for p in picks:
        ext = str(p.get("external_id") or "")
        p["id"] = str(uuid.uuid5(namespace, ext)) if ext else str(uuid.uuid4())

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
    await db.picks.delete_many({"pick_date": date_str})
    await db.picks.delete_many({"id": {"$in": list(seen_ids)}})
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

    if safe_picks:
        await db.picks.insert_many(safe_picks, ordered=False)
    logger.info("Stored %d picks for %s", len(safe_picks), date_str)
    return len(safe_picks)


async def _ensure_today_picks() -> None:
    today = _today_str()
    count = await db.picks.count_documents({"pick_date": today})
    if count == 0:
        await _refresh_picks(today)


# ── Market filter taxonomy ────────────────────────────────────────────────
# Tokens are the SAME across sports where it makes sense (moneyline, totals,
# spread). Sport-specific tokens (btts, goalscorer, player_points, etc.) only
# match their own sport. The regex is matched (case-insensitive) against the
# pick's stored `market` string.
_MARKET_REGEX = {
    # ── Soccer-specific families ──────────────────────────────────────────
    "1x2":           r"\bmoneyline\b|\bwin or draw\b",
    "btts":          r"both teams to score|\bbtts\b",
    "goalscorer":    r"anytime goal scorer|first goal scorer|last goal scorer|to score or assist",

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
    "batter_hits":          r"\bhits\b(?!\s*allowed)",
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
    "games_total":   r"\bgames over\b|\bgames under\b|\btotal games\b",

    # ── Broad catch-all (still used by analytics market-label grouping) ──
    "player_props":  r"hits|outs recorded|points|rebounds|assists|passing yards|rushing yards|receiving yards|touchdowns|goal scorer",
}


def _market_regex(token: str) -> str | None:
    return _MARKET_REGEX.get(token.lower().strip())


# Sport → available market filter tokens. Drives the UI MarketSelector pills.
SPORT_MARKETS = {
    "Soccer": [
        {"token": "1x2",         "label": "1X2"},
        {"token": "totals",      "label": "Over/Under"},
        {"token": "btts",        "label": "BTTS"},
        {"token": "goalscorer",  "label": "Goalscorer"},
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
        {"token": "batter_hits",        "label": "Hits"},
        {"token": "pitcher_strikeouts", "label": "Strikeouts"},
        {"token": "pitcher_outs",       "label": "Outs Recorded"},
    ],
    "Tennis": [
        {"token": "match_winner", "label": "Match Winner"},
        {"token": "sets",         "label": "Sets"},
        {"token": "games_total",  "label": "Games O/U"},
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
    standard_q = {
        "lock_score": {"$gte": floor},
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
        "lock_score": {"$gte": 80.0},
    }
    q: dict = {
        "pick_date": _today_str(),
        "$or": [standard_q, elite_q],
    }
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
        # League names come from The Odds API e.g. "Premier League", "MLS",
        # "MLB". Loose substring match keeps the UI simple.
        q["league"] = {"$regex": str(league).replace("\\", ""), "$options": "i"}
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
            # Pure lock_score sort — highest at top (or lowest at top if
            # asc=true). NO bucket pre-sort so tomorrow's 95-lock outranks
            # today's 75-lock — fixes the "have to scroll to find best
            # lock" UX bug. Elite anchor only applied to default desc.
            if asc:
                picks.sort(key=lambda p: p.get("lock_score", 0))
            else:
                picks.sort(key=lambda p: (_elite_rank(p), -p.get("lock_score", 0)))
    return {"picks": picks}


@api.get("/picks/all")
async def picks_all(user: Annotated[UserPublic, Depends(current_user)],
                    sport: Optional[str] = None):
    await _ensure_today_picks()
    q: dict = {"pick_date": _today_str()}
    if sport and sport.lower() != "all":
        q["sport"] = sport
    cursor = db.picks.find(q, {"_id": 0}).sort("lock_score", -1).limit(200)
    return {"picks": await cursor.to_list(length=200)}


@api.get("/picks/bet-killer")
async def picks_bet_killer(user: Annotated[UserPublic, Depends(current_user)],
                           sport: Optional[str] = None):
    """Legacy bet-killer endpoint (deprecated) — kept for backwards compat."""
    await _ensure_today_picks()
    q: dict = {"pick_date": _today_str(), "lock_score": {"$lt": 85}}
    if sport and sport.lower() != "all":
        q["sport"] = sport
    cursor = db.picks.find(q, {"_id": 0}).sort("lock_score", 1).limit(50)
    return {"picks": await cursor.to_list(length=50)}


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
        q["league"] = {"$regex": str(league).replace("\\", ""), "$options": "i"}
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
        "pick": pool[0],
        "alternates": pool[1:6],  # 5 backup alt-Under locks
        "total_evaluated": len(pool),
        "scoped_to_today": bool(today_picks),
    }


@api.get("/picks/rollover")
async def pick_rollover(user: Annotated[UserPublic, Depends(current_user)],
                        line_type: Optional[str] = None,
                        sport: Optional[str] = None,
                        market: Optional[str] = None,
                        league: Optional[str] = None):
    """Top 3 safest bets of the day — the user picks which one to roll.

    Rules:
      - Today's slate only (kickoff within 24h)
      - Lock score >= 90
      - NO Soccer by default (small leagues, high variance) — but if the user
        explicitly picks `sport=Soccer` we honour their choice.
      - Prefers player props over team moneylines (lower variance)
      - Ranks by win_probability first, then lock_score — "most likely to hit"
      - Diversifies: at most one pick per game so the trio isn't 3 of the same matchup
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

    # Progressive widening — Rollover needs at least 3 options. We try each
    # safety floor in order until we have ≥3 distinct candidate games, but we
    # NEVER widen the user-chosen sport/market/league filters.
    floors = [90, 85, 80, 75, 70]
    chalk_cap = -400
    picks: list = []
    floor_used: int = 90
    for f in floors:
        q = {**base_q, "lock_score": {"$gte": f}}
        cursor = db.picks.find(q, {"_id": 0})
        picks = await cursor.to_list(length=500)
        picks = [p for p in picks if (p.get("book_odds") or -9999) >= chalk_cap]
        if len({p.get("event") for p in picks}) >= 3:
            floor_used = f
            break
        floor_used = f
    # Final safety net — if still nothing matched, broaden the chalk cap.
    if len(picks) < 3:
        q = {**base_q, "lock_score": {"$gte": 70}}
        picks = await db.picks.find(q, {"_id": 0}).to_list(length=500)
        picks = [p for p in picks if (p.get("book_odds") or -9999) >= -700]
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
    ranked = sorted(
        pool,
        key=lambda p: (
            p.get("win_probability", 0) or 0,
            p.get("lock_score", 0) or 0,
            p.get("edge_percent", 0) or 0,
        ),
        reverse=True,
    )
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
    for p in ranked:
        ev = p.get("event")
        sp = p.get("sport") or ""
        if ev in seen_events:
            continue
        if sp in seen_sports:
            secondary.append(p)
            continue
        seen_events.add(ev)
        seen_sports.add(sp)
        primary.append({**p, "composite_rank": round(p.get("win_probability", 0) or 0, 1)})
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
        "picks": top,
        "pick": top[0] if top else None,  # back-compat for older clients
        "composite_rank": top[0]["composite_rank"] if top else None,
        "total_evaluated": len(pool),
        "scoped_to_today": bool(today_picks),
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
                     refresh_nonce: int = 0):
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
        league_filter = {"league": {"$regex": str(league).replace("\\", ""), "$options": "i"}}

    target_legs = max(10, min(20, max(1, int(legs or 10)))) if is_high_risk else max(2, min(8, max(1, int(legs or 3))))
    rank = max(1, min(20, int(rank or 1)))  # clamp refresh cursor to 1-20

    # ─── Time window filter ───
    # `commence_time` is stored as ISO-8601 string (UTC, e.g.
    # "2026-06-19T19:00:00Z"). Build a window cap and filter the DB query.
    window_hours = max(1, min(720, int(window_hours)))  # 1h .. 30d
    now_utc = datetime.now(timezone.utc)
    window_cap_iso = (now_utc + timedelta(hours=window_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    window_floor_iso = (now_utc - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    time_filter = {"event_time": {"$gte": window_floor_iso, "$lte": window_cap_iso}}

    # ─── Fetch candidate pool ───
    base_q = {
        "pick_date": _today_str(),
        "no_bet": {"$ne": True},
        "is_under_lock": {"$ne": True},
        **sport_filter, **line_filter, **market_filter, **league_filter,
        **time_filter,
    }
    base_q["lock_score"] = {"$gte": 70 if is_high_risk else 85}
    pool = await db.picks.find(base_q, {"_id": 0}).sort("lock_score", -1).limit(400).to_list(length=400)

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
    top = build_top_parlays(
        pool, target_legs=target_legs, high_risk=is_high_risk,
        bucket_map=bucket_map, rank=max(1, rank),
        locked_picks=locked_picks if locked_picks else None,
        single_sport_mode=is_single_sport,
        refresh_nonce=int(refresh_nonce or 0),
        synergy_map=synergy_map,
    )

    # ─── HIGH-RISK SAFETY NET: auto-expand window if empty ───
    # User feedback: "when I go 72hrs out I see soccer legs on board
    # that meet that criteria" — so if the requested window is too tight
    # and yields no parlays, automatically widen up to 168h (a week) so
    # the high-risk mode is never broken just because TODAY's slate is
    # thin. Standard mode does NOT auto-expand (user wants tight 24h
    # parlays for sharp action).
    auto_expanded_to: int | None = None
    if not top and is_high_risk and window_hours < 168:
        for fallback_window in (72, 168):
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
                    "High-risk parlay auto-expanded window %dh → %dh (%d candidate picks)",
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
    q: dict = {"settled_at": {"$gte": cutoff}}
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
    if not pick.get("explanation"):
        from ai_engine import _fallback_explanation, _fallback_killer
        if pick.get("lock_score", 0) >= 85:
            pick["explanation"] = _fallback_explanation(pick)
        else:
            pick["explanation"] = _fallback_killer(pick)
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
    # If we already cached a real AI explanation, return it.
    cached = pick.get("explanation_ai")
    if cached:
        return {"explanation": cached, "source": "cached"}
    # All picks reaching the UI are recommended picks (NO_BET filter removed
    # the bad ones). Always generate the "why to BET" explanation, never the
    # legacy bet-killer warning.
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
        if date_part:
            out[f"{ev_key}|{date_part}"] = entry
        # Backward-compat key — but ONLY for live or today's-pre-game lookups.
        # Avoid stamping a finished game over a future game with same teams.
        # Strategy: only set the plain key if there isn't already one with
        # better signal (live > pre > final).
        existing = out.get(ev_key)
        def _signal(e: dict) -> int:
            if e.get("is_live"): return 3
            if not e.get("is_final"): return 2  # upcoming/preview
            return 1                              # final
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


@api.get("/analytics/model-performance")
async def model_performance(
    user: Annotated[UserPublic, Depends(current_user)],
    days: int = 30,
    backfill: bool = True,
):
    """Auto-tracked model performance: ROI, CLV, Edge, calibration. Does NOT
    require the user to log any bets — every generated pick is simulated as
    a 1u flat stake."""
    from analytics import backfill_metrics, compute_model_performance
    if backfill:
        await backfill_metrics(db)
    return await compute_model_performance(db, days=days)


@api.get("/analytics/learned-weights")
async def learned_weights(user: Annotated[UserPublic, Depends(current_user)]):
    """What the self-tuning engine has learned from past picks. Used by the
    Analytics screen to surface every active bucket weight + calibration
    correction the engine is currently applying to new picks."""
    doc = await db.learned_weights.find_one({"_id": "current"}, {"_id": 0})
    if not doc:
        return {"buckets": [], "calibration": [], "updated_at": None, "sample_size": 0}
    return doc


@api.get("/analytics/v2")
async def analytics_v2(user: Annotated[UserPublic, Depends(current_user)]):
    """Learning System v2 dashboard payload.

    Returns: market performance rows, band calibration, market weights,
    learning changes log (last 30), and high-level totals. Used by the new
    Analytics dashboard sections."""
    state = await db.learning_state.find_one({"_id": "learning_v2_state"},
                                             {"_id": 0}) or {}
    # Last 30 audit log entries (most recent first).
    log = await db.learning_log.find({}, {"_id": 0}).sort(
        "ts", -1
    ).to_list(length=30)
    state["changes_log"] = log
    # Sport-level profit summary.
    sport_rows: dict[str, dict] = {}
    async for p in db.picks.aggregate([
        {"$match": {"status": {"$in": ["won", "lost", "push"]}}},
        {"$group": {
            "_id": "$sport",
            "n": {"$sum": 1},
            "won": {"$sum": {"$cond": [{"$eq": ["$status", "won"]}, 1, 0]}},
            "lost": {"$sum": {"$cond": [{"$eq": ["$status", "lost"]}, 1, 0]}},
            "units_risked": {"$sum": {"$ifNull": ["$units_risked", 0]}},
            "units_profit": {"$sum": {"$ifNull": ["$units_profit", 0]}},
            "clv_avg": {"$avg": "$clv_value"},
        }},
    ]):
        s = p["_id"]
        risked = p.get("units_risked") or 0
        profit = p.get("units_profit") or 0
        sport_rows[s] = {
            "sport": s,
            "n": p["n"], "won": p["won"], "lost": p["lost"],
            "units_risked": round(risked, 2),
            "units_profit": round(profit, 2),
            "roi_pct": round((profit / risked * 100.0) if risked else 0, 2),
            "hit_rate_pct": round((p["won"] / (p["won"] + p["lost"]) * 100.0)
                                  if (p["won"] + p["lost"]) else 0, 2),
            "clv_avg": round(p.get("clv_avg") or 0, 2),
        }
    state["profit_by_sport"] = list(sport_rows.values())

    # Bet-Type breakdown — STRAIGHT (1.0u) / REDUCED (0.5u) / PARLAY (0.25u)
    # ROI is now weighted per spec so heavy chalk doesn't distort the metric.
    bt_rows: dict[str, dict] = {}
    async for p in db.picks.aggregate([
        {"$match": {"status": {"$in": ["won", "lost", "push"]}}},
        {"$group": {
            "_id": {"$ifNull": ["$bet_type", "STRAIGHT"]},
            "n": {"$sum": 1},
            "won": {"$sum": {"$cond": [{"$eq": ["$status", "won"]}, 1, 0]}},
            "lost": {"$sum": {"$cond": [{"$eq": ["$status", "lost"]}, 1, 0]}},
            "units_risked": {"$sum": {"$ifNull": ["$units_risked", 0]}},
            "units_profit": {"$sum": {"$ifNull": ["$units_profit", 0]}},
        }},
    ]):
        bt = p["_id"]
        risked = p.get("units_risked") or 0
        profit = p.get("units_profit") or 0
        bt_rows[bt] = {
            "bet_type": bt,
            "n": p["n"], "won": p["won"], "lost": p["lost"],
            "units_risked": round(risked, 2),
            "units_profit": round(profit, 2),
            "roi_pct": round((profit / risked * 100.0) if risked else 0, 2),
            "hit_rate_pct": round((p["won"] / (p["won"] + p["lost"]) * 100.0)
                                  if (p["won"] + p["lost"]) else 0, 2),
        }
    state["profit_by_bet_type"] = list(bt_rows.values())
    return state


@api.post("/analytics/v2/recompute")
async def analytics_v2_recompute(user: Annotated[UserPublic, Depends(current_user)]):
    """Force re-run of the v2 learning aggregation (market perf, calibration,
    band gates, market weights, audit log). Returns the new state summary."""
    from learning_system_v2 import recompute_and_persist
    return await recompute_and_persist(db)


# ──────────────────────────────────────────────────────────────────────────
# Isolated learning buckets (sport × market_type × prop_type)
# ANALYTICS-ONLY: see /app/backend/learning_buckets.py — does NOT influence
# predictions, lock scores, or confidence outputs. Pure dashboard.
# ──────────────────────────────────────────────────────────────────────────

@api.get("/analytics/buckets")
async def analytics_buckets(user: Annotated[UserPublic, Depends(current_user)]):
    """Return per-sport, per-market-type, per-prop-type bucket performance.

    NEVER influences live predictions. Pure analytics for monitoring how
    each isolated learning group is performing.
    """
    from learning_buckets import get_buckets
    return await get_buckets(db)


@api.post("/analytics/buckets/recompute")
async def analytics_buckets_recompute(user: Annotated[UserPublic, Depends(current_user)]):
    """Force re-scan settled picks and rebuild all isolated learning buckets.

    Snapshots the previous state for rollback (keeps last 5). Analytics-only.
    """
    from learning_buckets import recompute_buckets
    return await recompute_buckets(db)


@api.post("/analytics/buckets/rollback")
async def analytics_buckets_rollback(
    user: Annotated[UserPublic, Depends(current_user)],
    snapshot_index: int = 1,
):
    """Restore the Nth-most-recent bucket snapshot. snapshot_index=1 = the
    previous version, =2 = two versions ago. Analytics-only."""
    from learning_buckets import rollback_buckets
    return await rollback_buckets(db, snapshot_index=max(1, min(5, int(snapshot_index or 1))))


@api.post("/analytics/learn")
async def learn_now(user: Annotated[UserPublic, Depends(current_user)]):
    """Force a recompute of learned weights and re-apply to today's picks."""
    from learning_engine import recompute_learned_weights, apply_learning
    weights = await recompute_learned_weights(db)
    # Apply to all picks generated for today that haven't been settled.
    cursor = db.picks.find(
        {"pick_date": _today_str(), "status": {"$in": [None, "pending"]}},
        {"_id": 0},
    )
    adjusted = 0
    async for p in cursor:
        before = p.get("win_probability")
        await apply_learning(db, p)
        if p.get("learning") and p.get("win_probability") != before:
            adjusted += 1
            await db.picks.update_one(
                {"id": p["id"]},
                {"$set": {
                    "win_probability": p["win_probability"],
                    "lock_score": p.get("lock_score"),
                    "edge_percent": p.get("edge_percent"),
                    "implied_probability": p.get("implied_probability"),
                    "learning": p.get("learning"),
                }},
            )
    return {"active_buckets": sum(1 for b in weights.get("buckets", []) if b.get("active")),
            "picks_adjusted": adjusted,
            "sample_size": weights.get("sample_size", 0)}


@api.get("/")
async def root():
    return {"ok": True, "service": "PerksLocks AI", "date": _today_str()}


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
    """
    try:
        await _ensure_today_picks()
    except Exception as e:
        logger.warning("Startup picks seed failed: %s", e)
    while True:
        try:
            # 1-hour cadence — balances slate freshness against Odds API
            # credit usage. The per-call rate limiter still owns the hard cap.
            await asyncio.sleep(3600)
            await _refresh_picks(_today_str())
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("Periodic refresh failed: %s", e)
            await asyncio.sleep(3600)


async def _settlement_loop():
    """Run settlement every 30 minutes.

    Previously 2 h — chosen to conserve Odds API credits. With MLB scores
    now sourced from the free MLB Stats API (see mlb_live.py), the bulk
    of settlement traffic costs ZERO Odds credits, so we can settle
    aggressively (within ~30 min of game-end) without budget impact.
    Non-MLB sports still use Odds API but they're a small minority of
    pending picks at any given time.

    After each settlement run, also recompute Learning v2 state so the next
    pick refresh picks up updated market weights + band-gate raises.
    """
    await asyncio.sleep(60)  # let startup settle
    while True:
        try:
            await settle_due_picks(db)
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
        await asyncio.sleep(1800)  # 30 minutes — MLB scores are free now


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
    asyncio.create_task(_daily_refresh_loop())
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
    logger.info("PerksLocks AI started")


@app.on_event("shutdown")
async def on_shutdown():
    client.close()
