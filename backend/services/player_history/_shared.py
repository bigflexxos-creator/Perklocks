"""Shared helpers used by every Stage-2 sport adapter.

Loading, windowing, quality classification, and window-dict shaping
are identical across NFL / NBA / Soccer / Tennis / UFC — the only
sport-specific piece is the ``_extract_actual`` function each
adapter provides.

Phase 5.3 Stage 2 (2026-06) — pure functions, no I/O beyond the
supplied Motor collection cursor.
"""
from __future__ import annotations

from typing import Callable, Optional

from .models import DataQuality, PlayerHistoryEvidence
from .threshold_engine import (
    evaluate_threshold,
    evaluate_milestone,
    QUANTILE_MIN_SAMPLE,
)


# ═══════════════════════════════════════════════════════════════════
# Loader
# ═══════════════════════════════════════════════════════════════════
async def load_actuals_rows(
    db,
    *,
    sport: str,
    player_id: Optional[str],
    canonical_player_id: Optional[str],
    history_as_of: str,
    limit: int = 60,
    fallback_legacy: bool = True,
) -> tuple[list[dict], str]:
    """Load per-game rows for one player, newest-first, strictly
    before ``history_as_of``.

    Reads the normalized ``db.player_game_actuals`` first, then falls
    back to ``db.player_game_logs`` (which the NFL / NBA legacy
    ingesters populate).  Returns (rows, source_label).

    ``source_label`` is one of:

        NORMALIZED         — normalized collection served the query
        LEGACY_GAMELOGS    — fallback to legacy per-sport gamelogs
        UNAVAILABLE        — neither collection returned any rows

    NEVER fabricates rows.  When both collections are empty for the
    player, returns ``([], "UNAVAILABLE")``.
    """
    sport_l = (sport or "").lower()

    # ── Primary: normalized player_game_actuals ──────────────────
    try:
        coll = db.player_game_actuals
        q: dict = {"sport": sport_l, "event_time": {"$lt": history_as_of}}
        if canonical_player_id:
            q["canonical_player_id"] = canonical_player_id
        elif player_id:
            q["player_id"] = player_id
        else:
            return [], "UNAVAILABLE"
        cursor = coll.find(q, {"_id": 0}).sort("event_time", -1).limit(limit)
        rows = [d async for d in cursor]
        if rows:
            return rows, "NORMALIZED"
    except Exception:
        pass

    # ── Fallback: legacy player_game_logs ───────────────────────
    if fallback_legacy:
        try:
            legacy = db.player_game_logs
            q2: dict = {"sport": sport_l}
            if player_id:
                q2["player_id"] = player_id
            # Legacy uses "date" not "event_time".
            q2["date"] = {"$lt": history_as_of[:10]}
            cursor = legacy.find(q2, {"_id": 0}).sort("date", -1).limit(limit)
            rows = [d async for d in cursor]
            if rows:
                return rows, "LEGACY_GAMELOGS"
        except Exception:
            pass

    return [], "UNAVAILABLE"


# ═══════════════════════════════════════════════════════════════════
# Window shaping
# ═══════════════════════════════════════════════════════════════════
def window_dict(
    actuals: list[Optional[float]],
    label: str,
    threshold: float,
    direction: str,
    *,
    milestone: bool = False,
    milestone_semantics: str = "gte",
    requested: Optional[int] = None,
) -> dict:
    """Assemble the standard window dict consumed by
    ``PlayerHistoryEvidence.*`` slots.  Never fabricates missing
    fields; when ``actuals`` contains only ``None`` values every
    downstream number stays ``None`` per the threshold engine.
    """
    used = sum(1 for a in actuals if a is not None)
    if milestone:
        r = evaluate_milestone(actuals, threshold,
                                 semantics=milestone_semantics)
    else:
        r = evaluate_threshold(actuals, threshold, direction=direction)
    return {
        "label":            label,
        "result":           r.to_dict(),
        "games_used":       used,
        "games_requested":  requested if requested is not None else len(actuals),
    }


def sublist(actuals: list[Optional[float]], n: int) -> list[Optional[float]]:
    return actuals[:n]


# ═══════════════════════════════════════════════════════════════════
# Data-quality tiering (shared across sports)
# ═══════════════════════════════════════════════════════════════════
def classify_quality(sample: int) -> str:
    if sample == 0:
        return DataQuality.UNAVAILABLE.value
    if sample < 3:
        return DataQuality.INSUFFICIENT.value
    if sample < 8:
        return DataQuality.LOW.value
    if sample < 15:
        return DataQuality.MEDIUM.value
    return DataQuality.HIGH.value


# ═══════════════════════════════════════════════════════════════════
# Standard populate contract used by every sport adapter
# ═══════════════════════════════════════════════════════════════════
async def populate_standard_evidence(
    db,
    ev: PlayerHistoryEvidence,
    *,
    sport: str,
    player_id: Optional[str],
    canonical_player_id: Optional[str],
    market_extractor: Callable[[str, dict], Optional[float]],
    opponent: Optional[str],
    home_away: Optional[str],
    milestone_market: bool = False,
    milestone_semantics: str = "gte",
    row_context_fn: Optional[Callable[[list[dict]], dict]] = None,
) -> PlayerHistoryEvidence:
    """The generic evidence populator.

    Each sport adapter passes:

    * ``market_extractor(market, row) -> Optional[float]`` — the
      sport-specific mapping from a raw row to a numeric actual.
      Missing components MUST return ``None`` (never zero).
    * ``row_context_fn(rows)`` — optional function that returns a
      sport-specific ``extras`` payload derived from the rows (e.g.
      surface breakdown for Tennis, competition breakdown for
      Soccer, fight-specific metadata for UFC).  This is stored on
      ``ev.extras`` — never surfaced into Lock Score.

    Returns the same ``ev`` object (mutated in place).
    """
    from datetime import datetime, timezone

    history_as_of = ev.history_as_of or datetime.now(timezone.utc).isoformat()
    rows, source = await load_actuals_rows(
        db,
        sport=sport,
        player_id=player_id,
        canonical_player_id=canonical_player_id,
        history_as_of=history_as_of,
    )
    ev.source = source
    ev.source_timestamp = datetime.now(timezone.utc).isoformat()
    ev.games_requested = 60
    ev.games_available = len(rows)
    if not rows:
        ev.data_quality = DataQuality.UNAVAILABLE.value
        ev.games_used = 0
        return ev

    # Historical team AT event time — from the most recent row.
    ev.historical_team = rows[0].get("team") or ev.historical_team
    ev.player_name = ev.player_name or rows[0].get("player_name") \
        or rows[0].get("name")

    market = ev.market or ""
    # ``threshold`` = None means "just return raw distribution".
    threshold = float(ev.threshold) if ev.threshold is not None else 0.5
    direction = ev.direction or "over"
    all_actuals: list[Optional[float]] = [
        market_extractor(market, r) for r in rows
    ]
    valid_count = sum(1 for a in all_actuals if a is not None)
    ev.games_used = valid_count
    ev.missing_games = ev.games_available - valid_count

    def _win(actuals, label, requested=None):
        return window_dict(actuals, label, threshold, direction,
                             milestone=milestone_market,
                             milestone_semantics=milestone_semantics,
                             requested=requested)

    ev.last_5  = _win(sublist(all_actuals, 5),  "last_5",  5)
    ev.last_10 = _win(sublist(all_actuals, 10), "last_10", 10)
    ev.last_20 = _win(sublist(all_actuals, 20), "last_20", 20)

    # Season split.
    seasons = [r.get("season") for r in rows if r.get("season") is not None]
    if seasons:
        cur_season = max(seasons)
        season_actuals = [a for r, a in zip(rows, all_actuals)
                           if r.get("season") == cur_season]
        prev_actuals = [a for r, a in zip(rows, all_actuals)
                          if r.get("season") == cur_season - 1]
        ev.season = _win(season_actuals, "season", len(season_actuals))
        if prev_actuals:
            ev.previous_season = _win(
                prev_actuals, "previous_season", len(prev_actuals))

    # Home / away splits.
    home_actuals = [a for r, a in zip(rows, all_actuals)
                     if (r.get("home_away") or "").lower() == "home"]
    away_actuals = [a for r, a in zip(rows, all_actuals)
                     if (r.get("home_away") or "").lower() == "away"]
    if home_actuals:
        ev.home = _win(home_actuals, "home", len(home_actuals))
    if away_actuals:
        ev.away = _win(away_actuals, "away", len(away_actuals))

    # vs opponent (case-insensitive match against team/opponent).
    if opponent:
        opp_norm = opponent.upper()
        opp_actuals = [a for r, a in zip(rows, all_actuals)
                        if (r.get("opponent") or r.get("opp_team_id") or "")
                        and str(r.get("opponent")
                                 or r.get("opp_team_id")).upper() == opp_norm]
        if opp_actuals:
            ev.vs_opponent = _win(opp_actuals, "vs_opponent",
                                    len(opp_actuals))

    # Exact-threshold at requested line — reuse season by default,
    # but consumers can query any threshold via evaluate_threshold.
    ev.exact_threshold = ev.season

    # Recent + season averages from RAW valid actuals only.
    valid = [a for a in all_actuals if a is not None]
    if valid:
        ev.recent_average = round(
            sum(valid[:5]) / min(5, len(valid)), 3)
        ev.season_average = round(sum(valid) / len(valid), 3)

    ev.data_quality = classify_quality(valid_count)
    ev.identity_confidence = "HIGH" if (
        canonical_player_id or player_id) else "LOW"

    if row_context_fn is not None:
        try:
            extras = row_context_fn(rows) or {}
            if extras:
                ev.extras.update(extras)
        except Exception:
            # Sport-specific extras must never break the shared populate.
            pass
    return ev


__all__ = [
    "load_actuals_rows",
    "window_dict",
    "sublist",
    "classify_quality",
    "populate_standard_evidence",
]
