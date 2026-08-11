"""UFC / MMA Fighter History adapter (Phase 5.3 Stage 2).

Fight-appropriate schema (§10).  UFC has NO recurring season /
opponent-history / home-away structure — the shared windows still
work but the strongest signal for MMA is career + method mix.

Unavailable statistics REMAIN UNKNOWN — every numeric extractor
returns ``None`` when the corresponding stat is not present on the
row.  Missing data never becomes zero (§4).
"""
from __future__ import annotations

from typing import Optional

from .models import PlayerHistoryEvidence
from ._shared import populate_standard_evidence


def _get(row: dict, *keys) -> Optional[float]:
    actuals = row.get("actuals") or {}
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


def _extract_ufc_actual(market: str, row: dict) -> Optional[float]:
    m = (market or "").lower()

    if m in ("fighter_significant_strikes", "significant_strikes",
              "sig_strikes", "player_significant_strikes"):
        return _get(row, "significant_strikes", "sig_strikes")
    if m in ("fighter_total_strikes", "total_strikes"):
        return _get(row, "total_strikes")
    if m in ("fighter_takedowns", "takedowns"):
        return _get(row, "takedowns")
    if m in ("fighter_submission_attempts", "submission_attempts",
              "sub_attempts"):
        return _get(row, "submission_attempts", "sub_attempts")
    if m in ("fighter_control_time", "control_time"):
        return _get(row, "control_time")
    if m in ("fight_duration_seconds", "fight_duration"):
        return _get(row, "fight_duration_seconds", "fight_duration")
    if m in ("fight_round", "round_reached"):
        return _get(row, "round", "round_reached")
    if m in ("fighter_knockdowns", "knockdowns"):
        return _get(row, "knockdowns")
    return None


def _ufc_context(rows: list[dict]) -> dict:
    if not rows:
        return {}
    # Method / result distribution across the sample — never
    # fabricates a value.  Unknown methods are counted under
    # ``UNKNOWN``.
    method_mix: dict[str, int] = {}
    result_mix: dict[str, int] = {}
    for r in rows:
        m = (r.get("method") or "UNKNOWN").upper()
        method_mix[m] = method_mix.get(m, 0) + 1
        res = (r.get("result") or "UNKNOWN").upper()
        result_mix[res] = result_mix.get(res, 0) + 1
    latest = rows[0]
    return {
        "latest_event":     latest.get("event") or latest.get("event_id"),
        "latest_method":    latest.get("method"),
        "latest_result":    latest.get("result"),
        "latest_round":     latest.get("round"),
        "career_method_mix": method_mix,
        "career_result_mix": result_mix,
    }


async def populate_ufc_evidence(
    db,
    ev: PlayerHistoryEvidence,
    *,
    player_id: Optional[str],
    canonical_player_id: Optional[str],
    player_name: Optional[str],
    opponent: Optional[str],
    home_away: Optional[str],
) -> PlayerHistoryEvidence:
    return await populate_standard_evidence(
        db, ev,
        sport="ufc",
        player_id=player_id,
        canonical_player_id=canonical_player_id,
        market_extractor=_extract_ufc_actual,
        opponent=opponent,
        home_away=None,     # UFC has no true home/away context
        milestone_market=False,
        row_context_fn=_ufc_context,
    )


__all__ = ["populate_ufc_evidence", "_extract_ufc_actual"]
