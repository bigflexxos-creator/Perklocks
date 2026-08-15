"""Soccer teammate + related-market ranker — Phase 2A.5D FINAL DELTA.

Applied POST-canonicalization, BEFORE off_board tagging.

Contracts
---------
1. Teammate rule: normally exactly ONE main-board scorer/creator wager
   per team.  A second is allowed only when both are ELITE Lock (LS>=95)
   AND the two picks target uncorrelated pathways (Anytime Goalscorer +
   Anytime Assist for DIFFERENT players count as separate teams; same
   team scorer + same team scorer competes and loses).
2. Related-market rule: for the same player across
   {Anytime Goal Scorer, Anytime Assist, To Score or Assist,
    First Goal Scorer}, normally exactly ONE main-board wager. Winner
   maximises expected_value = model_prob * (american→decimal-1) subject
   to Lock Score >=85. Losers stay in DB but get off_board=True with
   reason RELATED_MARKET_DOMINATED.
3. Underlying rows are NEVER deleted — only `off_board` + `off_board_reasons`.
4. Universal ≥85 rule unchanged. No score manipulation.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

logger = logging.getLogger("lockscore.soccer_team_ranker")

_SCORER_MARKETS = {
    "Anytime Goal Scorer",
    "Anytime Assist",
    "To Score or Assist",
    "First Goal Scorer",
    "Last Goal Scorer",
}


def _canonical_ls(p: dict) -> float:
    for k in ("published_lock_score",):
        v = p.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    return max(float(p.get("lock_score") or 0), float(p.get("lock_score_v2") or 0))


def _decimal_from_american(odds) -> float:
    try:
        o = int(odds)
    except (TypeError, ValueError):
        return 2.0
    if o == 0:
        return 2.0
    return 1.0 + (o / 100.0 if o > 0 else 100.0 / abs(o))


def _expected_value(p: dict) -> float:
    """`model_prob * (decimal_odds - 1)` — proxy for scoring risk-adjusted EV."""
    mp = p.get("model_win_prob") or p.get("model_probability") or 0.0
    try:
        mp = float(mp)
    except Exception:
        mp = 0.0
    if mp > 1.0:
        mp /= 100.0
    return mp * (_decimal_from_american(p.get("book_odds") or 0) - 1.0)


def _is_soccer_scorer(p: dict) -> bool:
    if p.get("sport") != "Soccer":
        return False
    mk = str(p.get("market") or "")
    for name in _SCORER_MARKETS:
        if name in mk:
            return True
    return False


def apply_soccer_selection(picks: Iterable[dict]) -> dict[str, Any]:
    """Tag `off_board=True` + reason on losers of teammate + related-
    market competition.  Winners are left untouched.

    Returns stats dict {teammate_demoted, related_market_demoted, teams_touched}.
    """
    stats = {"teammate_demoted": 0, "related_market_demoted": 0,
             "teams_touched": 0, "players_touched": 0}
    picks = list(picks)

    # ── 1. Related-market: same-player, multiple qualifying markets ──
    by_player: dict[tuple[str, str], list[dict]] = {}
    for p in picks:
        if not _is_soccer_scorer(p) or p.get("off_board") is True:
            continue
        if _canonical_ls(p) < 85.0:
            continue
        player = (p.get("selection") or "").strip().lower()
        event = str(p.get("event") or "")
        if not player:
            continue
        by_player.setdefault((player, event), []).append(p)

    for (player, event), group in by_player.items():
        if len(group) <= 1:
            continue
        stats["players_touched"] += 1
        # Winner = highest expected_value * canonical LS as tiebreaker.
        winner = max(group, key=lambda p: (
            _expected_value(p), _canonical_ls(p)))
        for p in group:
            if p is winner:
                continue
            p["off_board"] = True
            p.setdefault("off_board_reasons", []).append(
                "RELATED_MARKET_DOMINATED")
            p["related_market_dominated"] = True
            stats["related_market_demoted"] += 1

    # ── 2. Teammate rule: per team, exactly one primary ──────────────
    by_team: dict[tuple[str, str], list[dict]] = {}
    for p in picks:
        if not _is_soccer_scorer(p) or p.get("off_board") is True:
            continue
        if _canonical_ls(p) < 85.0:
            continue
        team = (p.get("team") or p.get("player_team") or "").strip().lower()
        # Phase 2A.5D FINAL — teammate rule requires an authoritative
        # ``team`` field.  When absent (older synth writers), skip
        # teammate grouping — the related-market rule already handles
        # same-player competition.  Do NOT fall back to event-pooling;
        # that treated both sides of a fixture as one team and demoted
        # legitimate opposing-team candidates.
        if not team:
            continue
        event = str(p.get("event") or "")
        by_team.setdefault((team, event), []).append(p)

    for (team, event), group in by_team.items():
        if len(group) <= 1:
            continue
        stats["teams_touched"] += 1
        # Winner = highest expected_value.  Tie-break by LS.
        winners_sorted = sorted(group, key=lambda p: (
            _expected_value(p), _canonical_ls(p)), reverse=True)
        primary = winners_sorted[0]
        # Exceptional-second exception: both LS >= 95 AND different
        # market categories (goal vs assist) → keep the second.
        for p in winners_sorted[1:]:
            if (_canonical_ls(p) >= 95.0
                    and _canonical_ls(primary) >= 95.0
                    and str(p.get("market") or "") !=
                        str(primary.get("market") or "")):
                continue
            p["off_board"] = True
            p.setdefault("off_board_reasons", []).append("SCORER_TEAM_RANK")
            p["teammate_rank_demoted"] = True
            stats["teammate_demoted"] += 1

    return stats


__all__ = ["apply_soccer_selection"]
