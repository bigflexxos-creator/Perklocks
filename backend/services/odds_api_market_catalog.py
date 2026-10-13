"""Odds API market-key catalogue — pre-flight whitelist (Layer A).

Context
───────
2026-10-03 P0-B incident: a single invalid Odds API market key in a
bundled event-odds request returns HTTP 422 and causes the ENTIRE
bundle to fail.  The existing event-scoped 422 isolation
(``alt_lines_feed._fetch_event_odds`` + ``sports_engine
._isolate_and_merge_event_props``) does recover by fanning out, but at
~10× the quota cost per event — on a $30 monthly Odds API budget that
is enough to deplete the slate before all events are harvested.

This module is the PRIMARY defense: a static whitelist of valid Odds
API market keys per sport, verified against the 2026 provider
catalogue (the-odds-api.com/sports-odds-data/betting-markets.html).

Any market key NOT in the whitelist is dropped BEFORE the request is
built — the invalid key never reaches the API, no 422, no fan-out, no
quota burn.  The rejection is logged + funnel-telemetried so new typos
surface immediately.

Layered defense
───────────────
    Layer A (THIS FILE) — static whitelist, blocks typos pre-flight.
    Layer B — ``services.bad_market_registry`` global-scope seeding
              at app startup, so even if a key sneaks past Layer A it
              is suppressed before the first request.
    Layer C — event-scoped 422 isolation in ``alt_lines_feed._fetch
              _event_odds`` and ``sports_engine._isolate_and_merge
              _event_props`` (unchanged; last line of defense).

Scope
─────
* NFL and NHL only in this pass (per P0-B directive).  Other sports
  will be added once their catalogues are audited the same way.
* Odds API ONLY.  ``propline_feed.py`` (api.prop-line.com) uses a
  different vocabulary and MUST be audited against its own catalogue
  in a separate pass.

Immutability promise
────────────────────
* NO scoring / threshold / Lock Score / 85+ eligibility / alt-ladder
  construction references here.
* Pure data constants + one pure function (``validate_markets``) +
  one list of known-invalid keys for Layer B seeding.
"""
from __future__ import annotations

import logging
from typing import Iterable


logger = logging.getLogger("lockscore.odds_api_market_catalog")


# ─────────────────────── NFL — verified 2026 catalogue ────────────
# Verified against https://the-odds-api.com/sports-odds-data/betting-
# markets.html as of 2026-10-03.  Keep sorted-ish by family for ease
# of review.  To add a new market: verify it in the live provider
# catalogue first, then add here + add a comment with the verification
# date.
NFL_VALID_MARKETS: frozenset[str] = frozenset({
    # Game markets (returned by bulk `/sports/{sport}/odds` endpoint)
    "h2h", "spreads", "totals",
    # Alt game markets (event-level `/events/{id}/odds` endpoint)
    "alternate_spreads", "alternate_totals",
    # Player prop — passing
    "player_pass_yds", "player_pass_yds_alternate",
    "player_pass_tds", "player_pass_tds_alternate",
    "player_pass_attempts", "player_pass_attempts_alternate",
    "player_pass_completions", "player_pass_completions_alternate",
    "player_pass_interceptions", "player_pass_interceptions_alternate",
    "player_pass_longest_completion",
    # Player prop — rushing
    "player_rush_yds", "player_rush_yds_alternate",
    "player_rush_attempts", "player_rush_attempts_alternate",
    "player_rush_tds", "player_rush_longest",
    # Player prop — receiving
    "player_receptions", "player_receptions_alternate",
    "player_reception_yds", "player_reception_yds_alternate",
    "player_reception_tds", "player_reception_longest",
    # Player prop — touchdowns
    "player_anytime_td", "player_1st_td", "player_last_td",
    "player_tds_over",
    # Player prop — kicking / defense (acquisition-ready; low priority)
    "player_kicking_points", "player_field_goals",
    "player_tackles_assists",
})

# ─────────────────────── NHL — verified 2026 catalogue ────────────
# Verified against https://the-odds-api.com/sports/nhl-odds.html as
# of 2026-10-03.  NB: NHL alt puck lines are returned by Odds API
# under ``alternate_spreads`` (NOT ``alternate_puck_lines`` — that
# key was removed from the catalogue; the engine's spread→puck_line
# family remap at canonical_publication_boundary.py:112-117 handles
# the semantic translation downstream).
NHL_VALID_MARKETS: frozenset[str] = frozenset({
    # Game markets
    "h2h", "spreads", "totals",
    # Alt game markets
    "alternate_spreads", "alternate_totals",
    # Player prop — core families
    "player_shots_on_goal", "player_shots_on_goal_alternate",
    "player_goals", "player_goals_alternate",
    "player_assists", "player_assists_alternate",
    "player_points", "player_points_alternate",
    # Player prop — goalie / specials (acquisition-ready; low priority)
    "player_total_saves", "player_total_saves_alternate",
    "player_power_play_points", "player_power_play_points_alternate",
    "player_goal_scorer_anytime", "player_goal_scorer_first",
    "player_goal_scorer_last",
})

# ─────────────────────── Known-invalid keys (Layer B seeds) ───────
# Keys that were historically present in Perklocks acquisition
# bundles but are NOT in the current Odds API catalogue.  Seeded as
# ``scope="global"`` bad markers at app startup so they are
# suppressed before the first request even if a developer re-adds the
# typo somewhere Layer A missed.
#
# Each entry is a (sport_key, market_key) pair with a short reason
# so logs clearly explain WHY the key is blocked.
KNOWN_INVALID_MARKETS: tuple[tuple[str, str, str], ...] = (
    ("americanfootball_nfl",
     "player_reception_alternate",
     "typo — not in Odds API catalogue; correct is "
     "player_receptions_alternate OR player_reception_yds_alternate"),
    ("americanfootball_ncaaf",
     "player_reception_alternate",
     "same typo as NFL — not in Odds API catalogue"),
    ("icehockey_nhl",
     "alternate_puck_lines",
     "stale key — removed from Odds API; alt puck lines are now "
     "under alternate_spreads with downstream family remap"),
)


# ─────────────────────── Sport-key dispatcher ──────────────────────
# Maps Odds API ``sport_key`` strings to the per-sport whitelist.
# Only NFL + NHL are gated in this pass; other sports pass through
# unchanged (returning ``None`` means "no whitelist — accept all").
def _whitelist_for(sport_key: str) -> frozenset[str] | None:
    sk = (sport_key or "").lower()
    if sk.startswith("americanfootball_nfl"):
        return NFL_VALID_MARKETS
    # NFL/CFB share the same request builder (same bundle shape in
    # alt_lines_feed.py:111-118) but each sport has its own catalogue
    # on the provider side.  For now we treat CFB with the same
    # whitelist because every market key in the CFB bundle is also
    # a valid NFL key — the mismatch risk is zero and avoids drift.
    if sk.startswith("americanfootball_ncaaf"):
        return NFL_VALID_MARKETS
    if sk.startswith("icehockey_nhl"):
        return NHL_VALID_MARKETS
    return None


# ─────────────────────── Public API ────────────────────────────────
def validate_markets(
    sport_key: str,
    markets: Iterable[str],
    *,
    caller: str = "<unknown>",
) -> tuple[list[str], list[str]]:
    """Partition ``markets`` into ``(valid, rejected)``.

    Keys in ``rejected`` are logged at WARNING level and recorded in
    funnel-telemetry (best-effort — swallowed on error) so new typos
    surface in the normal operations dashboard without blocking the
    request path.

    Behaviour contract:
    * For sports with NO whitelist registered (every sport except NFL,
      CFB, NHL today) → returns ``(list(markets), [])``.
    * For whitelisted sports → every key not in the whitelist is
      rejected.  Deduplicated preserving original order.
    * Never raises — the request path must not fail because the
      validator itself crashed.
    """
    try:
        wl = _whitelist_for(sport_key)
        if wl is None:
            return list(dict.fromkeys(markets)), []
        valid: list[str] = []
        rejected: list[str] = []
        for m in markets:
            if not m:
                continue
            if m in wl:
                if m not in valid:
                    valid.append(m)
            else:
                if m not in rejected:
                    rejected.append(m)
        if rejected:
            logger.warning(
                "odds_api_market_catalog: dropping %d unknown market "
                "key(s) for sport=%s caller=%s dropped=%s",
                len(rejected), sport_key, caller, rejected,
            )
            try:
                from services import funnel_telemetry as _ft
                for bad in rejected:
                    _ft.record(
                        sport=sport_key, market=bad,
                        stage="acquisition",
                        reason="UNKNOWN_PROVIDER_MARKET_KEY",
                        detail=f"caller={caller}",
                    )
            except Exception:        # pragma: no cover
                pass
        return valid, rejected
    except Exception as e:           # pragma: no cover
        logger.exception(
            "validate_markets crashed; passing markets through: %s", e
        )
        return list(markets), []


async def seed_known_invalid_markers(db) -> int:
    """Layer B — seed ``bad_market_registry`` with global markers for
    every entry in ``KNOWN_INVALID_MARKETS``.

    Called once at app startup (`server.py` lifespan).  Idempotent —
    repeated calls refresh TTL only.  Returns the number of markers
    written for log reporting.

    Fail-safe: if the DB handle is unavailable or any insert errors,
    the function returns 0 without raising.  Layer A + Layer C still
    cover the gap.
    """
    if db is None:
        return 0
    try:
        from services.bad_market_registry import mark_bad
    except Exception as e:           # pragma: no cover
        logger.debug("seed_known_invalid_markers: import failed: %s", e)
        return 0
    written = 0
    for sport_key, market_key, reason in KNOWN_INVALID_MARKETS:
        try:
            await mark_bad(
                db, sport_key=sport_key, markets=[market_key],
                event_id=None,       # global scope
                scope="global",
                reason=f"KNOWN_INVALID:{reason}",
            )
            written += 1
        except Exception as e:       # pragma: no cover
            logger.debug(
                "seed_known_invalid_markers: write failed for "
                "sport=%s market=%s: %s", sport_key, market_key, e,
            )
    if written:
        logger.info(
            "odds_api_market_catalog: seeded %d global bad-market "
            "marker(s) at startup", written,
        )
    return written


__all__ = [
    "NFL_VALID_MARKETS",
    "NHL_VALID_MARKETS",
    "KNOWN_INVALID_MARKETS",
    "validate_markets",
    "seed_known_invalid_markers",
]
