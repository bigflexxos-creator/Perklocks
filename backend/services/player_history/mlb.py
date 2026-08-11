"""MLB Player History adapter (Stage 1).

Reads normalized per-game actuals from ``db.player_game_actuals``
(new normalized collection — Phase 5.3 §23) FIRST, falling back to
the legacy ``db.player_game_logs`` when the normalized collection
has not been backfilled for a player.

DO NOT use ``db.picks`` as an athlete-stat source (Phase 5.3 §25).

Market → derived-actual mapping (Phase 5.3 §6, §7):

    Hits                → h
    Total Bases         → total_bases (or 1B+2·2B+3·3B+4·HR)
    Home Runs           → hr
    RBI                 → rbi
    Runs               → r
    Hits + Runs + RBI   → h + r + rbi         (ALL 3 required)
    Pitcher Ks          → strikeouts
    Pitcher Outs        → outs                (or ip*3)

If ANY required component is missing, the actual for that game is
``None`` and the game is EXCLUDED from decisions.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .models import (
    PlayerHistoryEvidence, DataQuality, HistoryDirection,
)
from .threshold_engine import evaluate_threshold, evaluate_milestone


# ── Market → actuals extractor ────────────────────────────────────
def _extract_actual(market: str, row: dict) -> Optional[float]:
    """Compute the actual for ONE game row for the given market.
    Returns None when a required component is missing."""
    m = (market or "").lower()
    actuals = row.get("actuals") or {}
    # Legacy player_game_logs shape uses flat fields with mlb_ prefix
    # or bare stat names.
    def _get(*keys):
        for k in keys:
            v = actuals.get(k) if actuals else None
            if v is None:
                v = row.get(k)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None
        return None

    if m in ("batter_hits", "hits"):
        return _get("h", "hits", "mlb_h")
    if m in ("batter_home_runs", "home_runs", "hr"):
        return _get("hr", "home_runs", "mlb_hr")
    if m in ("batter_rbis", "rbi", "rbis"):
        return _get("rbi", "rbis", "mlb_rbi")
    if m in ("batter_runs_scored", "runs"):
        return _get("r", "runs", "mlb_r")
    if m in ("batter_total_bases", "total_bases", "tb"):
        tb = _get("tb", "total_bases", "mlb_tb")
        if tb is not None:
            return tb
        # Derive from 1B/2B/3B/HR when TB not directly stored.
        b1 = _get("1b", "b1", "singles")
        b2 = _get("2b", "b2", "doubles")
        b3 = _get("3b", "b3", "triples")
        hr = _get("hr", "home_runs")
        h  = _get("h", "hits")
        if hr is not None and h is not None and b2 is not None and b3 is not None:
            singles = h - b2 - b3 - hr if b1 is None else b1
            return singles + 2 * b2 + 3 * b3 + 4 * hr
        return None
    if m in ("batter_hits_runs_rbis", "batter_hits_runs_rbis_alternate",
              "h+r+rbi", "hrr"):
        h   = _get("h", "hits")
        r   = _get("r", "runs")
        rbi = _get("rbi", "rbis")
        # ALL three required — no zero substitution.
        if h is None or r is None or rbi is None:
            return None
        return h + r + rbi
    if m in ("pitcher_strikeouts", "strikeouts", "ks"):
        return _get("k", "so", "strikeouts", "mlb_k")
    if m in ("pitcher_outs", "outs"):
        outs = _get("outs")
        if outs is not None:
            return outs
        ip = _get("ip", "innings_pitched")
        if ip is not None:
            return round(ip * 3)
        return None
    return None


def _is_milestone_market(market: str) -> bool:
    """Milestone (>=) markets — none of the currently-supported MLB
    hitter markets are inherently milestone; all are Over/Under."""
    return False


async def _load_games(
    db,
    *,
    player_id: Optional[str],
    canonical_player_id: Optional[str],
    history_as_of: str,
    limit: int = 40,
) -> tuple[list[dict], str]:
    """Load per-game rows for this player, newest-first, strictly
    before ``history_as_of``.  Returns (rows, source_label)."""
    # Primary: normalized player_game_actuals.
    try:
        actuals_coll = db.player_game_actuals
        q: dict = {"sport": "mlb", "event_time": {"$lt": history_as_of}}
        if canonical_player_id:
            q["canonical_player_id"] = canonical_player_id
        elif player_id:
            q["player_id"] = player_id
        else:
            return [], "UNAVAILABLE"
        cursor = actuals_coll.find(q, {"_id": 0}).sort("event_time", -1).limit(limit)
        rows = [d async for d in cursor]
        if rows:
            return rows, "MLB_STATSAPI"
    except Exception:
        pass
    # Fallback: legacy player_game_logs.
    try:
        legacy = db.player_game_logs
        q2: dict = {"sport": "mlb"}
        if player_id:
            q2["player_id"] = player_id
        # Legacy uses "date" not "event_time".
        q2["date"] = {"$lt": history_as_of[:10]}
        cursor = legacy.find(q2, {"_id": 0}).sort("date", -1).limit(limit)
        rows = [d async for d in cursor]
        if rows:
            return rows, "MLB_STATSAPI_LEGACY"
    except Exception:
        pass
    return [], "UNAVAILABLE"


def _window(actuals: list[Optional[float]], n: int) -> list[Optional[float]]:
    return actuals[:n]


def _quality(sample: int) -> str:
    if sample == 0:
        return DataQuality.UNAVAILABLE.value
    if sample < 3:
        return DataQuality.INSUFFICIENT.value
    if sample < 8:
        return DataQuality.LOW.value
    if sample < 15:
        return DataQuality.MEDIUM.value
    return DataQuality.HIGH.value


async def populate_mlb_evidence(
    db,
    ev: PlayerHistoryEvidence,
    *,
    player_id: Optional[str],
    canonical_player_id: Optional[str],
    player_name: Optional[str],
    opponent: Optional[str],
    home_away: Optional[str],
) -> PlayerHistoryEvidence:
    """Populate the evidence dataclass for MLB."""
    history_as_of = ev.history_as_of or datetime.now(timezone.utc).isoformat()
    rows, source = await _load_games(
        db,
        player_id=player_id,
        canonical_player_id=canonical_player_id,
        history_as_of=history_as_of,
    )
    ev.source = source
    ev.source_timestamp = datetime.now(timezone.utc).isoformat()
    ev.games_requested = 40
    ev.games_available = len(rows)
    if not rows:
        ev.data_quality = DataQuality.UNAVAILABLE.value
        ev.games_used = 0
        return ev

    # Historical team (from most-recent game) — do NOT overwrite
    # current_team (Phase 5.3 §19).
    ev.historical_team = rows[0].get("team") if rows else None
    ev.player_name = ev.player_name or rows[0].get("name") or rows[0].get("player_name")

    # Extract per-game actuals + windows.
    market = ev.market or ""
    threshold = float(ev.threshold) if ev.threshold is not None else 0.5
    direction = ev.direction or "over"
    all_actuals: list[Optional[float]] = [
        _extract_actual(market, r) for r in rows
    ]
    valid_count = sum(1 for a in all_actuals if a is not None)
    ev.games_used = valid_count
    ev.missing_games = ev.games_available - valid_count

    # Evaluator.
    def _eval(sub_actuals):
        if _is_milestone_market(market):
            return evaluate_milestone(sub_actuals, threshold, semantics="gte")
        return evaluate_threshold(sub_actuals, threshold, direction=direction)

    def _window_dict(actuals, label, requested):
        used = sum(1 for a in actuals if a is not None)
        r = _eval(actuals)
        return {
            "label":     label,
            "result":    r.to_dict(),
            "games_used": used,
            "games_requested": requested,
        }

    ev.last_5  = _window_dict(_window(all_actuals, 5),  "last_5",  5)
    ev.last_10 = _window_dict(_window(all_actuals, 10), "last_10", 10)
    ev.last_20 = _window_dict(_window(all_actuals, 20), "last_20", 20)
    # Season = current-season subset (rows with season == max season).
    seasons = [r.get("season") for r in rows if r.get("season") is not None]
    if seasons:
        cur_season = max(seasons)
        season_rows = [(r, a) for r, a in zip(rows, all_actuals)
                        if r.get("season") == cur_season]
        prev_rows = [(r, a) for r, a in zip(rows, all_actuals)
                      if r.get("season") == cur_season - 1]
        ev.season = _window_dict(
            [a for _, a in season_rows], "season", len(season_rows))
        if prev_rows:
            ev.previous_season = _window_dict(
                [a for _, a in prev_rows], "previous_season", len(prev_rows))

    # Home / away splits.
    home_rows = [a for r, a in zip(rows, all_actuals) if r.get("home_away") == "home"]
    away_rows = [a for r, a in zip(rows, all_actuals) if r.get("home_away") == "away"]
    if home_rows:
        ev.home = _window_dict(home_rows, "home", len(home_rows))
    if away_rows:
        ev.away = _window_dict(away_rows, "away", len(away_rows))

    # vs opponent (regardless of home/away).
    if opponent:
        opp_rows = [a for r, a in zip(rows, all_actuals)
                      if (r.get("opponent") or "").upper() == opponent.upper()]
        if opp_rows:
            ev.vs_opponent = _window_dict(
                opp_rows, "vs_opponent", len(opp_rows))

    # Exact-threshold season hit rate.
    ev.exact_threshold = ev.season

    # Recent + season averages.
    valid = [a for a in all_actuals if a is not None]
    if valid:
        ev.recent_average = round(sum(valid[:5]) / min(5, len(valid)), 3)
        ev.season_average = round(sum(valid) / len(valid), 3)

    ev.data_quality = _quality(valid_count)
    # Identity confidence: HIGH when we resolved on canonical_player_id
    # or exact provider player_id.
    ev.identity_confidence = "HIGH" if (
        canonical_player_id or player_id) else "LOW"
    return ev


# ── Index creation helper (Phase 5.3 §4) ──────────────────────────
async def ensure_player_game_actuals_indexes(db) -> list[str]:
    """Create Mongo indexes for the normalized player_game_actuals
    collection.  Idempotent — safe to call multiple times.

    Returns a list of index names created (or already present)."""
    coll = db.player_game_actuals
    created: list[str] = []
    for keys, name, opts in [
        ([("sport", 1), ("player_id", 1), ("event_time", -1)],
          "sport_player_event", {}),
        ([("sport", 1), ("canonical_player_id", 1), ("event_time", -1)],
          "sport_canon_event", {}),
        ([("game_id", 1)], "game_id", {}),
        ([("season", 1)], "season", {}),
        ([("source", 1)], "source", {}),
    ]:
        try:
            await coll.create_index(keys, name=name, **opts)
            created.append(name)
        except Exception:
            pass
    return created


__all__ = [
    "populate_mlb_evidence",
    "ensure_player_game_actuals_indexes",
    "_extract_actual",
]
