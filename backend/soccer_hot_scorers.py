"""Soccer Hot Scorers — stats-driven goalscorer picks.

Analogous to `hot_hitters.py` for MLB. This module bypasses sportsbook
line coverage gaps by generating Anytime-Goal-Scorer picks directly
from Wikipedia's top-scorer tables (via `services/wiki_top_scorers`).

Why: The Odds API and Propline both leave massive gaps for niche
leagues (Allsvenskan, Eliteserien, Veikkausliiga, etc.). Linemate
shows their top scorers because they use league-official stats. So
do we, now — no sportsbook line required.

For each upcoming soccer fixture in a covered league:
  1. Look up top scorers on BOTH participating clubs.
  2. Compute anytime-goal probability from goals/games rate:
        p = 1 - (1 - g/estimated_games_played)^1
     with a floor of 20% (top scorers still hit that in tough matchups)
     and cap of 65% (Haaland-tier).
  3. Emit a fair-odds pick when p >= 30% and no equivalent sportsbook
     pick already exists for the same match+player.

Picks are tagged `source='soccer_hot_scorers_v1'` and `is_extra=True`
so they don't compete with real book-lined picks in the main feed but
do surface in the Lab's Hot Scorers panel.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import unicodedata
from datetime import datetime, timedelta, timezone

from services.espn_common import american_from_prob, grade_from_conf

logger = logging.getLogger("lockscore.soccer_hot_scorers")

_SOURCE_TAG = "soccer_hot_scorers_v1"
_MIN_PROB = 0.30
_MAX_PROB = 0.65

# Rough games-played estimate per league at various times of season.
# For most Nordic leagues (26-30 game season) mid-season is ~12-15
# games; we use a conservative 14 to slightly overweight raw goal
# counts (better to overclaim on top-scorer than to shrink them).
_LEAGUE_GAMES_ESTIMATE = {
    "Allsvenskan":                12,   # 30-game season, mid July → ~12 played
    "Eliteserien":                13,   # 30-game season
    "Veikkausliiga":              12,
    "League of Ireland":          20,
    "MLS":                        20,
    "Brasileirão Série A":        16,
    "Brasileirão Série B":        16,
    "China Super League":         14,
    "K League 1":                 18,
    "J1 League":                  18,
    "Argentine Primera División": 15,
    # Default fallback = 20
}


def _norm(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _pick_id(sport_key: str, event_id: str, player: str) -> str:
    raw = f"{_SOURCE_TAG}|{sport_key}|{event_id}|{player}".lower()
    return f"hot-{hashlib.sha256(raw.encode()).hexdigest()[:24]}"


def _match_player_to_club(player_club: str, home: str, away: str) -> str | None:
    """Return 'home' | 'away' | None depending on which club the
    scorer plays for."""
    pn = _norm(player_club)
    hn = _norm(home)
    an = _norm(away)
    if not pn:
        return None
    if pn == hn or (pn and hn and (pn in hn or hn in pn)):
        return "home"
    if pn == an or (pn and an and (pn in an or an in pn)):
        return "away"
    return None


async def sync_hot_scorers(db, days_ahead: int = 4) -> dict:
    """Walk today+N-day soccer picks, hydrate top-scorer lists per
    league, emit Anytime-Goal-Scorer picks for each top scorer whose
    club is in an upcoming fixture."""
    from services.wiki_top_scorers import get_top_scorers

    started = datetime.now(timezone.utc)
    now = datetime.now(timezone.utc)
    cutoff = (now + timedelta(days=days_ahead)).isoformat()

    # Group existing picks by (event_time, event) to derive fixtures
    docs = await db.picks.find(
        {"sport": "Soccer",
         "event_time": {"$gte": now.isoformat(), "$lte": cutoff}},
        {"league": 1, "event": 1, "event_time": 1, "sport_key": 1,
         "external_id": 1},
    ).to_list(length=None)

    fixtures: dict[str, dict] = {}
    for p in docs:
        key = f"{p.get('event')}|{p.get('event_time')}"
        if key not in fixtures:
            fixtures[key] = {
                "league":    p.get("league"),
                "event":     p.get("event") or "",
                "event_time": p.get("event_time"),
                "sport_key": p.get("sport_key") or "soccer",
                "external":  p.get("external_id"),
            }

    # Hydrate top-scorers per league (dedup)
    leagues = sorted({f["league"] for f in fixtures.values() if f["league"]})
    scorers_by_league: dict[str, list[dict]] = {}
    for lg in leagues:
        try:
            scorers_by_league[lg] = await get_top_scorers(db, lg)
        except Exception as e:
            logger.warning("get_top_scorers(%s) failed: %s", lg, e)
            scorers_by_league[lg] = []

    generated = 0
    dedup_skipped = 0
    upserted_picks: list[dict] = []
    for fx in fixtures.values():
        lg = fx["league"]
        scorers = scorers_by_league.get(lg) or []
        if not scorers:
            continue
        event = fx["event"] or ""
        if " @ " not in event:
            continue
        away_team, home_team = event.split(" @ ", 1)
        away_team = away_team.strip()
        home_team = home_team.strip()

        games_est = _LEAGUE_GAMES_ESTIMATE.get(lg, 20)

        for scorer in scorers:
            side = _match_player_to_club(scorer.get("club", ""),
                                          home_team, away_team)
            if not side:
                continue
            goals = int(scorer.get("goals") or 0)
            if goals < 1:
                continue

            # Goals-per-game rate → per-game anytime-goal probability.
            # Simple: p ≈ min(0.65, goals / games_est). Home advantage
            # bumps home scorers +3pp.
            base_p = min(_MAX_PROB, goals / max(1, games_est))
            if side == "home":
                base_p = min(_MAX_PROB, base_p + 0.03)
            if base_p < _MIN_PROB:
                continue

            player = scorer.get("name") or ""
            if not player:
                continue

            # Dedup — skip when a sportsbook pick already exists for
            # this player on this event.
            existing = await db.picks.find_one({
                "sport":      "Soccer",
                "event_time": fx["event_time"],
                "selection":  {"$regex": re.escape(player), "$options": "i"},
                "source":     {"$ne": _SOURCE_TAG},
            }, {"_id": 1})
            if existing:
                dedup_skipped += 1
                continue

            conf = round(base_p * 100, 1)
            fair_odds = american_from_prob(conf)
            event_id = fx.get("external") or hashlib.md5(event.encode()).hexdigest()[:12]

            # ── Lock score ≠ win probability (user mandate 2026-07-12) ──
            # A 65% per-game scoring rate is ELITE for an Anytime market —
            # it should read as a 95+ lock, not "lock 65". Reuse the same
            # tier-relative mapping the SportDB synth scorer engine uses
            # (career goals + weighted rate + probability-driven floors)
            # so Nordic-league hot scorers grade consistently with
            # CSL/MLS model-only picks.
            try:
                from sportdb_player_scorer import _prob_to_lock
                lock = _prob_to_lock(base_p, {
                    "goals":          goals,
                    "rate_per_match": base_p,
                    "matches":        games_est,
                })
            except Exception:
                lock = conf  # legacy fallback
            is_elite = goals >= 8

            doc = {
                "id":              _pick_id(fx["sport_key"], event_id, player),
                "external_id":     f"{fx['sport_key']}-{event_id}-hotscorer-{_norm(player)}",
                "sport":           "Soccer",
                "league":          lg,
                "event":           event,
                "event_time":      fx["event_time"],
                "market":          f"{player} - Anytime Goal Scorer",
                "selection":       f"{player} to Score",
                "win_probability": conf,
                "implied_probability": conf,
                "book_odds":       fair_odds,
                "edge_percent":    0.0,
                "lock_score":      lock,
                "lock_score_v2":   lock,
                # Set peak equal to lock so the 95+ sticky-pin filter in
                # the main refresh recognises high-lock hot-scorer picks
                # (safety net — the primary protection is the source-based
                # exclusion in server.py `_OUT_OF_BAND_SOURCES`).
                "lock_score_peak": lock,
                "grade":           grade_from_conf(lock),
                "pick_date":       now.strftime("%Y-%m-%d"),
                "is_under_lock":   False,
                "no_bet":          conf < 40.0,
                "elite_player":    is_elite,
                # League-leading scorers bypass the goalscorer-matchup
                # drop guard (same pattern as curated CSL seeds) — the
                # Wikipedia top-scorer table IS the form evidence.
                "elite_protect":   is_elite,
                # Model-only: no real bookmaker line exists for these
                # players. Matches the board's `model_only_q` carve-out
                # (lock ≥ 75) and the evidence-governor skip list.
                "is_model_only":   True,
                "deep_dive":       False,
                "source":          _SOURCE_TAG,
                "model_version":   "soccer.hot_scorers.v1",
                "bookmaker":       "Fair Odds (Model)",
                "created_at":      now.isoformat(),
                "is_extra":        True,
                "fair_odds_model": True,
                # ── Settlement lifecycle fields (2026-07-13 permanent fix) ──
                # Without these, hot-scorer picks landed in the DB with
                # NO `status` field at all — invisible to every settler
                # (they all filter by `status ∈ [None, "pending"]`, which
                # matches None too, BUT the picks also lacked `event`
                # normalisation for Nordic names so they silently piled
                # up. Explicit "pending" + auto_settle=True makes them
                # first-class settlement citizens same as any other pick.
                "status":          "pending",
                "auto_settle":     True,
                "sport_key":       fx["sport_key"],
                # Real scorer-form evidence — consumed by the quality
                # gate's AGS Rule 4 ("no form evidence" block) and the
                # card's "Why This Pick?" panel.
                "pick_rationale": {
                    "summary": (
                        f"{player} is a top scorer in the {lg} — "
                        f"{goals} goals in ~{games_est} league games "
                        f"({goals / games_est:.0%} per match)."
                    ),
                    "evidence": [
                        f"📈 {player} has scored {goals} goals in ~{games_est} "
                        f"{lg} games this season ({goals / games_est:.0%} per game).",
                        f"📊 Wikipedia league top-scorer table ranks {player} "
                        f"among the {lg}'s leading scorers.",
                    ] + ([f"🏠 Home fixture — home scorers get a +3pp scoring bump."]
                         if side == "home" else []),
                },
                "factors": {
                    "Coverage Source": (
                        f"Wikipedia top-scorer table for {lg}. Book lines "
                        f"don't cover this player — pick emitted from "
                        f"league stats bypass (like MLB Hot Hitters)."
                    ),
                    "Season Form": (
                        f"{player} has scored {goals} goals in ~{games_est} "
                        f"league games this season ({goals/games_est:.1%} "
                        f"per game). Home advantage: "
                        f"{'yes' if side == 'home' else 'no'}."
                    ),
                },
            }
            await db.picks.update_one(
                {"id": doc["id"]},
                {"$set": doc, "$setOnInsert": {"first_seen": doc["created_at"]}},
                upsert=True,
            )
            generated += 1
            upserted_picks.append(doc)

    # ── P0-2 canonical publication ─────────────────────────────────
    # Route every hot-scorer pick that was just upserted through the
    # publication service so an immutable snapshot exists BEFORE the
    # canonical board eligibility gate runs.  Idempotent per contract.
    if upserted_picks:
        try:
            from services.publication_helpers import publish_upserted_picks
            await publish_upserted_picks(
                db, upserted_picks,
                publication_source=_SOURCE_TAG,
                caller_label="Soccer hot scorers",
            )
        except Exception as _pub_err:
            logger.warning(
                "Soccer hot scorers publication step failed: %s", _pub_err,
            )

    finished = datetime.now(timezone.utc)
    return {
        "started_at":     started.isoformat(),
        "finished_at":    finished.isoformat(),
        "elapsed_ms":     int((finished - started).total_seconds() * 1000),
        "fixtures":       len(fixtures),
        "leagues":        len(leagues),
        "generated":      generated,
        "dedup_skipped":  dedup_skipped,
    }


async def hot_scorers_loop(db) -> None:
    """1h refresh cadence — top-scorer tables update slowly but we run
    every hour so the picks are re-seeded quickly after any external
    delete / DB restart / etc. (2026-07-12 user report: "Sweden and
    Norway goalscorers appeared then they disappeared, please
    permanently fix"). Combined with the source-based exclusion in
    server.py `_OUT_OF_BAND_SOURCES`, this guarantees the picks stay
    on the board."""
    await asyncio.sleep(75)
    while True:
        try:
            await sync_hot_scorers(db, days_ahead=4)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("hot_scorers_loop error: %s", e)
        await asyncio.sleep(60 * 60)
