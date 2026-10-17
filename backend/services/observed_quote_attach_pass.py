"""Post-emission ObservedQuote attach pass (Blocks 1-2 of the final
cheap-surgical wiring directive).

Walks a batch of emitted picks ONCE per refresh cycle, groups them by
(sport, event_id), fetches the per-event Odds-API payload for the
event (cache-hot — same function the main producers already call),
runs ``observed_quote_adapter.extract_observed_quotes`` once per
event, and attaches ``observed_quotes`` to each matching pick dict.

The universal ``prepublication_lineage_pass`` (already wired into the
orchestrator) then upgrades those picks' ``book_odds`` + ``bookmaker``
+ ``odds_source`` from the exact observed sportsbook quote.

Design properties:
  * Zero per-emission-site changes — producers emit as-is, the attach
    pass runs ONCE per cycle AFTER they emit.
  * Cache-hot — ``_fetch_event_props_payload`` memoizes per event so
    no additional provider quota is spent.
  * Fail-closed — any attach exception leaves the pick unchanged (the
    universal pass is a no-op on picks without ``observed_quotes``).
  * Per-sport scoped — pass only wires MLB + NHL (+ Tennis ML) in
    this cheap pass; Soccer prop producer already attaches its own
    at its emission site; NFL alt lineage stays untouched.
"""
from __future__ import annotations

import logging
import unicodedata
from typing import Any, Iterable, Optional

logger = logging.getLogger("lockscore.observed_quote_attach")


# Market-key maps per sport.  Only sport/family combos listed here
# get an attach pass; everything else is skipped (fail-closed).
_MLB_MARKETS = (
    "batter_hits", "batter_hits_alternate",
    "batter_total_bases", "batter_total_bases_alternate",
    "batter_home_runs", "batter_home_runs_alternate",
    "batter_rbis", "batter_rbis_alternate",
    "batter_hits_runs_rbis", "batter_hits_runs_rbis_alternate",
    "batter_runs_scored", "batter_runs_scored_alternate",
    "batter_singles", "batter_doubles", "batter_triples",
    "batter_stolen_bases", "batter_walks",
    "pitcher_strikeouts", "pitcher_strikeouts_alternate",
    "pitcher_outs", "pitcher_outs_alternate",
    "pitcher_earned_runs", "pitcher_hits_allowed", "pitcher_walks",
)
_NHL_MARKETS = (
    "player_goals", "player_goals_alternate",
    "player_shots_on_goal", "player_shots_on_goal_alternate",
    "player_assists", "player_assists_alternate",
    "player_points", "player_points_alternate",
    "player_power_play_points",
)
_TENNIS_MARKETS = (
    "h2h",  # ML
    "spreads", "totals",
)


_SPORT_TO_PROVIDER_SPORT_KEY = {
    "MLB": "baseball_mlb",
    "NHL": "icehockey_nhl",
    "Tennis": None,   # tennis sport_key is per-tournament; filled per-pick
}


def _norm_name(name: str) -> str:
    if not name:
        return ""
    nk = unicodedata.normalize("NFKD", name)
    return "".join(c for c in nk if not unicodedata.combining(c)).lower().strip()


def _match_pick_side(pick: dict) -> str:
    side = (pick.get("pick_side") or pick.get("side") or "").strip().lower()
    if side in ("over", "under", "yes", "no"):
        return side
    market = (pick.get("market") or "").lower()
    if "over" in market and "under" not in market:
        return "over"
    if "under" in market and "over" not in market:
        return "under"
    return side or ""


async def _fetch_payload_safe(sport_label: str, sport_key: Optional[str],
                                event_id: str) -> Optional[dict]:
    try:
        from sports_engine import _fetch_event_props_payload
        return await _fetch_event_props_payload(
            sport_label, sport_key, event_id)
    except Exception as e:
        logger.debug("payload fetch skipped for %s/%s/%s: %s",
                     sport_label, sport_key, event_id, e)
        return None


async def attach_observed_quotes(picks: Iterable[dict],
                                   *, sport: str) -> dict:
    """Group ``picks`` by event_id, fetch the per-event payload once,
    extract observed quotes, attach to each matching pick.

    Returns a summary dict with attach counts.
    """
    sport_label = sport if sport in _SPORT_TO_PROVIDER_SPORT_KEY else sport
    sport_key = _SPORT_TO_PROVIDER_SPORT_KEY.get(sport)
    allowed_markets = {
        "MLB": _MLB_MARKETS, "NHL": _NHL_MARKETS, "Tennis": _TENNIS_MARKETS,
    }.get(sport, ())
    if not allowed_markets:
        return {"sport": sport, "attached": 0, "reason": "sport_not_wired"}

    # Group picks by event_id.
    by_event: dict[str, list[dict]] = {}
    for p in picks:
        if not isinstance(p, dict):
            continue
        if (p.get("sport") or "").upper() != sport.upper():
            continue
        # ── 2026-06 Commit B/1 (item 11) — external_id MUST NOT be
        # used as a provider_event_id fallback.  external_id is
        # Perklocks internal identity.  Provider refetch requires the
        # authentic provider event id.
        eid = (p.get("provider_event_id") or p.get("event_id"))
        if not eid:
            # Fail-closed telemetry — see return dict.
            continue
        by_event.setdefault(str(eid), []).append(p)

    attached_count = 0
    skipped_no_payload = 0
    skipped_no_match = 0
    # ── 2026-06 Commit B/1 (item 12) — DEFENSE-IN-DEPTH telemetry ──
    # The attach pass is now DEFENSE-IN-DEPTH only.  New rows should
    # already be publishable with producer-level lineage.  Count:
    #   producer_lineage_complete       — pick already has
    #                                     provider_event_id +
    #                                     provider_market_key +
    #                                     bookmaker + book_odds (NO
    #                                     repair needed).
    #   attach_pass_repaired            — pick was missing lineage
    #                                     but we found observed_quotes
    #                                     and attached them.
    #   attach_pass_missing_provider_event_id
    #                                   — fail-closed when no event
    #                                     identity is available.
    #   attach_pass_failed              — any other repair failure.
    producer_lineage_complete = 0
    attach_pass_repaired = 0
    attach_pass_missing_provider_event_id = 0
    attach_pass_failed = 0
    try:
        from services.observed_quote_adapter import (
            extract_observed_quotes, quotes_for,
        )
    except Exception as e:
        return {"sport": sport, "attached": 0,
                "reason": f"adapter_import_error:{e}"}

    for event_id, event_picks in by_event.items():
        # ── 2026-06 Commit B/1 (item 11, 12) — fail-closed on missing
        # provider_event_id.  external_id is PERKLOCKS internal and
        # MUST NEVER feed provider refetch.
        _pev = event_picks[0].get("provider_event_id") if event_picks else None
        if not _pev and not event_id:
            attach_pass_missing_provider_event_id += len(event_picks)
            continue
        # Each event_id may have a sport-specific provider sport_key.
        _sport_key = sport_key or (event_picks[0].get("sport_key") if event_picks else None)
        payload = await _fetch_payload_safe(sport_label, _sport_key, event_id)
        if not payload:
            skipped_no_payload += len(event_picks)
            continue
        extracted = extract_observed_quotes(
            payload, sport=sport, event_id=event_id,
            allowed_markets=list(allowed_markets),
        )
        if not extracted:
            skipped_no_match += len(event_picks)
            continue
        for p in event_picks:
            # Producer-level lineage check (defense-in-depth
            # telemetry — did the producer already stamp it?).
            if (p.get("provider_event_id")
                    and p.get("provider_market_key")
                    and p.get("bookmaker")
                    and p.get("book_odds") is not None):
                producer_lineage_complete += 1
            market_key = (p.get("provider_market_key")
                           or p.get("market_key")
                           or _derive_market_key(p, sport))
            if not market_key:
                attach_pass_failed += 1
                continue
            player_name = (p.get("player_name") or p.get("player") or "")
            side = _match_pick_side(p)
            quotes = quotes_for(extracted,
                                 market_key=market_key,
                                 player=player_name,
                                 side=side)
            if not quotes:
                # Try broader match without side.
                quotes = quotes_for(extracted,
                                     market_key=market_key,
                                     player=player_name,
                                     side=None)
            if quotes:
                p["observed_quotes"] = quotes
                attached_count += 1
                if not (p.get("provider_event_id")
                        and p.get("bookmaker")
                        and p.get("book_odds") is not None):
                    attach_pass_repaired += 1
            else:
                attach_pass_failed += 1

    return {
        "sport": sport,
        "events_seen": len(by_event),
        "attached": attached_count,
        "skipped_no_payload": skipped_no_payload,
        "skipped_no_match":  skipped_no_match,
        # 2026-06 Commit B/1 (item 12) — defense-in-depth telemetry.
        "producer_lineage_complete":            producer_lineage_complete,
        "attach_pass_repaired":                 attach_pass_repaired,
        "attach_pass_missing_provider_event_id": attach_pass_missing_provider_event_id,
        "attach_pass_failed":                   attach_pass_failed,
    }


def _derive_market_key(p: dict, sport: str) -> Optional[str]:
    """Fallback market-key derivation from the pick's market text.
    Only triggers when a pick doesn't carry an explicit
    provider_market_key — covers the legacy producers in
    sports_engine.py."""
    market = (p.get("market") or "").lower()
    if sport == "MLB":
        if "hits + runs + rbis" in market or "hits+runs+rbis" in market:
            return "batter_hits_runs_rbis"
        if "total bases" in market:
            return "batter_total_bases"
        if "home run" in market:
            return "batter_home_runs"
        if " rbi" in market or market.startswith("rbi"):
            return "batter_rbis"
        if "pitcher strikeout" in market or "pitching strikeout" in market:
            return "pitcher_strikeouts"
        if "pitcher out" in market or "pitching out" in market:
            return "pitcher_outs"
        if "earned run" in market:
            return "pitcher_earned_runs"
        if "singles" in market: return "batter_singles"
        if "doubles" in market: return "batter_doubles"
        if "triples" in market: return "batter_triples"
        if "stolen base" in market: return "batter_stolen_bases"
        if "runs scored" in market: return "batter_runs_scored"
        if " hits" in market or market.startswith("hits"):
            return "batter_hits"
    elif sport == "NHL":
        if "shots on goal" in market or " sog" in market:
            return "player_shots_on_goal"
        if "assist" in market:
            return "player_assists"
        if "point" in market:
            return "player_points"
        if "goal" in market:
            return "player_goals"
    elif sport == "Tennis":
        if ("moneyline" in market or "match winner" in market
                or market == "h2h" or market.strip() == "ml"):
            return "h2h"
        if "spread" in market or "handicap" in market:
            return "spreads"
        if "total" in market:
            return "totals"
    return None


__all__ = ["attach_observed_quotes", "_derive_market_key",
            "_MLB_MARKETS", "_NHL_MARKETS", "_TENNIS_MARKETS"]
