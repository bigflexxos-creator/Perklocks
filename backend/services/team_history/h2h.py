"""Head-to-Head Foundation (§8, §9).

Canonical-identity-only H2H.  Text matching is deliberately NOT
supported at this layer — the caller must resolve identity first.
"""
from __future__ import annotations

from statistics import median as _stats_median
from typing import Optional

from .models import H2HResult
from ._shared import (
    _safe_num,
    _linear_quantile,
    _variance,
    QUANTILE_MIN_SAMPLE,
    load_team_rows,
)


def build_h2h_result(
    *,
    canonical_team_id: Optional[str],
    canonical_opponent_id: Optional[str],
    rows: list[dict],
) -> H2HResult:
    """Aggregate a list of already-filtered head-to-head rows.

    ``rows`` MUST already be from the perspective of
    ``canonical_team_id`` — i.e. ``team_score`` is this team's score
    and ``opponent_score`` is the opponent's score for every row.
    The perspective invariant is asserted by consumers, not this
    helper — the helper only aggregates.
    """
    wins = losses = draws = ot_losses = 0
    scored: list[float] = []
    conceded: list[float] = []
    seasons: set = set()
    competitions: set = set()
    home_ct = away_ct = 0
    events: list[dict] = []
    for r in rows:
        ts = _safe_num(r.get("team_score"))
        os = _safe_num(r.get("opponent_score"))
        if ts is not None and os is not None:
            scored.append(ts)
            conceded.append(os)
        result = (r.get("result") or "").upper()
        if not result and ts is not None and os is not None:
            result = "WIN" if ts > os else ("LOSS" if ts < os else "DRAW")
        if result == "WIN":       wins += 1
        elif result == "LOSS":    losses += 1
        elif result == "DRAW":    draws += 1
        elif result in ("OTL", "OT_LOSS", "SO_LOSS"):
            ot_losses += 1
            losses += 1
        s = r.get("season")
        if s is not None:
            try:
                seasons.add(int(s))
            except (TypeError, ValueError):
                pass
        c = r.get("competition") or r.get("league")
        if isinstance(c, str):
            competitions.add(c)
        ha = (r.get("home_away") or "").lower()
        if ha == "home":
            home_ct += 1
        elif ha == "away":
            away_ct += 1
        events.append({
            "event_id":       r.get("event_id"),
            "event_time":     r.get("event_time") or r.get("date"),
            "season":         r.get("season"),
            "competition":    r.get("competition") or r.get("league"),
            "home_away":      r.get("home_away"),
            "team_score":     ts,
            "opponent_score": os,
            "result":         result or None,
            "overtime":       r.get("overtime"),
        })
    result = H2HResult(
        canonical_team_id=canonical_team_id,
        canonical_opponent_id=canonical_opponent_id,
        sample_size=max(len(scored), len(conceded), len(rows)),
        wins=wins, losses=losses, draws=draws, ot_losses=ot_losses,
        events=events,
        seasons=sorted(seasons),
        competitions=sorted(competitions),
        home_events=home_ct,
        away_events=away_ct,
    )
    if scored:
        result.scored_avg = round(sum(scored) / len(scored), 4)
        if len(scored) >= QUANTILE_MIN_SAMPLE:
            sv = sorted(scored)
            result.scored_median = _stats_median(sv)
            result.scored_variance = _variance(sv)
    if conceded:
        result.conceded_avg = round(sum(conceded) / len(conceded), 4)
        if len(conceded) >= QUANTILE_MIN_SAMPLE:
            sv = sorted(conceded)
            result.conceded_median = _stats_median(sv)
            result.conceded_variance = _variance(sv)
    return result


async def get_h2h_history(
    db,
    *,
    sport: str,
    canonical_team_id: str,
    canonical_opponent_id: str,
    as_of: Optional[str] = None,
    limit: int = 40,
) -> H2HResult:
    """Fetch canonical H2H from the perspective of ``canonical_team_id``.

    Returns ``H2HResult`` — sample_size accurately reflects the true
    number of events (§9 — a 2-meeting sample is NEVER inflated).
    Missing scores stay UNKNOWN, they do NOT become 0.
    """
    from datetime import datetime, timezone
    as_of_iso = as_of or datetime.now(timezone.utc).isoformat()

    if not canonical_team_id or not canonical_opponent_id:
        return H2HResult(
            canonical_team_id=canonical_team_id,
            canonical_opponent_id=canonical_opponent_id,
        )

    rows, _source = await load_team_rows(
        db, sport=sport,
        canonical_team_id=canonical_team_id,
        team_name=None,
        as_of=as_of_iso,
        limit=limit * 4,     # over-fetch and filter
    )
    h2h_rows = [r for r in rows
                  if r.get("canonical_opponent_id") == canonical_opponent_id]
    h2h_rows = h2h_rows[:limit]
    return build_h2h_result(
        canonical_team_id=canonical_team_id,
        canonical_opponent_id=canonical_opponent_id,
        rows=h2h_rows,
    )


__all__ = ["build_h2h_result", "get_h2h_history"]
