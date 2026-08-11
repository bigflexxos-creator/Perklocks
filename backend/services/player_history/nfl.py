"""NFL Player History adapter (Phase 5.3 Stage 2).

Reads normalized per-game actuals from ``db.player_game_actuals``
(sport=``nfl``) with fallback to legacy ``db.player_game_logs``.

Market → extractor mapping — every extractor returns ``None`` when
any required component is missing.  Missing data NEVER becomes 0
(§4).  Historical team is preserved from the event-time row and is
kept SEPARATE from ``current_team`` (§6).
"""
from __future__ import annotations

from typing import Optional

from .models import PlayerHistoryEvidence
from ._shared import populate_standard_evidence


# ── Market → raw actual mapping ────────────────────────────────────
def _extract_nfl_actual(market: str, row: dict) -> Optional[float]:
    m = (market or "").lower()
    actuals = row.get("actuals") or {}

    def _get(*keys) -> Optional[float]:
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

    # Passing markets ──────────────────────────────────────────────
    if m in ("player_pass_yds", "player_pass_yds_alternate",
              "passing_yards", "pass_yards"):
        return _get("pass_yds", "passing_yards", "pass_yards")
    if m in ("player_pass_tds", "passing_tds", "pass_tds"):
        return _get("pass_tds", "passing_tds")
    if m in ("player_pass_completions", "completions"):
        return _get("completions", "pass_completions")
    if m in ("player_pass_attempts", "pass_attempts"):
        return _get("attempts", "pass_attempts")
    if m in ("player_pass_interceptions", "interceptions"):
        return _get("interceptions", "pass_interceptions", "ints")

    # Rushing markets ──────────────────────────────────────────────
    if m in ("player_rush_yds", "player_rush_yds_alternate",
              "rushing_yards", "rush_yards"):
        return _get("rush_yds", "rushing_yards", "rush_yards")
    if m in ("player_rush_attempts", "rushing_attempts",
              "rush_attempts", "carries"):
        return _get("rush_attempts", "rushing_attempts", "carries")
    if m in ("player_rush_tds", "rushing_tds", "rush_tds"):
        return _get("rush_tds", "rushing_tds")

    # Receiving markets ────────────────────────────────────────────
    if m in ("player_reception_yds", "player_reception_yds_alternate",
              "receiving_yards", "reception_yards"):
        return _get("rec_yds", "receiving_yards", "reception_yards")
    if m in ("player_receptions", "player_receptions_alternate",
              "receptions"):
        return _get("receptions", "rec")
    if m in ("player_reception_tds", "receiving_tds", "reception_tds"):
        return _get("rec_tds", "receiving_tds")
    if m in ("player_receiving_targets", "targets"):
        return _get("targets")

    # Anytime TD / first TD (milestone markets) ────────────────────
    if m in ("player_anytime_td", "player_1st_td", "anytime_td"):
        rush = _get("rush_tds", "rushing_tds")
        rec = _get("rec_tds", "receiving_tds")
        # ATD is milestone (1+); require at least one component to
        # be present so we do not confuse missing data with a 0-TD
        # game.  If only one is present, use it.  If both are
        # present, sum them.
        if rush is None and rec is None:
            return None
        return (rush or 0.0) + (rec or 0.0)
    return None


def _nfl_context(rows: list[dict]) -> dict:
    """Sport-specific extras for NFL — position and week metadata."""
    if not rows:
        return {}
    latest = rows[0]
    return {
        "position":         latest.get("position"),
        "latest_week":      latest.get("week"),
        "latest_season":    latest.get("season"),
        "latest_opponent":  latest.get("opponent"),
    }


async def populate_nfl_evidence(
    db,
    ev: PlayerHistoryEvidence,
    *,
    player_id: Optional[str],
    canonical_player_id: Optional[str],
    player_name: Optional[str],
    opponent: Optional[str],
    home_away: Optional[str],
) -> PlayerHistoryEvidence:
    market = (ev.market or "").lower()
    milestone = market in {
        "player_anytime_td", "player_1st_td", "anytime_td",
    }
    return await populate_standard_evidence(
        db, ev,
        sport="nfl",
        player_id=player_id,
        canonical_player_id=canonical_player_id,
        market_extractor=_extract_nfl_actual,
        opponent=opponent,
        home_away=home_away,
        milestone_market=milestone,
        milestone_semantics="gte",
        row_context_fn=_nfl_context,
    )


__all__ = ["populate_nfl_evidence", "_extract_nfl_actual"]
