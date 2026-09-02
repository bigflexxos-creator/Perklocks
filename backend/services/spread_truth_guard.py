"""Spread Truth Guard — Phase 7 defect A closure (2026-09-02).

Live evidence: same Preview refresh flipped the CFB
`Akron @ Wake Forest` spread between:
   Wake Forest -24.5  (Lock 92, WP 56.52, Edge +6.52, -110)
and
   Akron +24.5        (Lock 92, WP 54.70, Edge +3.60, -115)

Root cause: `totals_truth_guard` handled Total O/U conservation but
NO analogous guard existed for spreads.  Wake -24.5 and Akron +24.5
are the SAME canonical wager (opposite sides of one spread market)
yet each was published as its own ACTIVE row.  Provider ordering
across refreshes decided which side "won".

Fix: side-neutral CANONICAL SPREAD KEY on `abs(line)`, with a
deterministic supersession rule:

    key = (sport, event_id, "SPREAD", period, abs(line))

Wake -24.5 → abs=24.5.  Akron +24.5 → abs=24.5.  Same key.  In-run
dedup picks the higher canonical edge; tie-broken by higher
model_probability, then lock_score, then pick_id (lexicographic —
process-stable).  Both observed sides remain in `db.picks` (with
`revision_state=SUPERSEDED_IN_RUN` and `off_board=True` on the
losing side) for provenance / audit / joint devig — never deleted.
"""
from __future__ import annotations
import logging
from typing import Any


log = logging.getLogger("lockscore.spread_truth_guard")


def _canonical_spread_key(pick: dict) -> str | None:
    """Return the side-NEUTRAL canonical spread supersession key.

    ``abs(line)`` collapses Wake -24.5 and Akron +24.5 into the same
    canonical wager.  Different absolute thresholds (24.5 vs 27.5)
    are DISTINCT canonical wagers (real ladder rungs preserved).
    """
    sport = pick.get("sport") or ""
    event_id = pick.get("event_id") or pick.get("canonical_event_id") or ""
    market_lower = (pick.get("market") or "").lower()
    market_family = (pick.get("market_family") or "").lower()
    if not (sport and event_id):
        return None
    # Recognise spreads via label OR explicit family.
    is_spread = (
        market_family == "spread"
        or "spread" in market_lower
        or "point spread" in market_lower
        or "handicap" in market_lower
        or "run line" in market_lower       # MLB analogue
        or "puck line" in market_lower      # NHL analogue
    )
    if not is_spread:
        return None
    line = pick.get("line")
    if line is None:
        return None
    try:
        abs_line = round(abs(float(line)), 4)
    except (TypeError, ValueError):
        return None
    period = pick.get("period") or "FULL_GAME"
    return f"SPREAD|{sport}|{event_id}|{period}|{abs_line}"


def enforce_single_active_spread(picks: list[dict]) -> dict[str, int]:
    """Collapse both sides of the same event/|line| spread wager to
    ONE ACTIVE row.  Deterministic winner selection — no board
    flapping across refreshes when inputs are stable.

    Winner ordering (stable across process restarts):
        1. Higher canonical edge_percent (largest first)
        2. Higher model_probability / win_probability
        3. Higher lock_score
        4. Lexicographic pick_id (deterministic tiebreak)

    Both observed rows remain in-list — only `revision_state`,
    `off_board`, and audit fields are updated on the loser.
    """
    stats = {"spreads_seen": 0, "keys_stamped": 0, "superseded": 0}
    if not picks:
        return stats
    # 1) Stamp key on all spread picks.
    for p in picks:
        k = _canonical_spread_key(p)
        if not k:
            continue
        p["canonical_spread_key"] = k
        p["revision_state"] = p.get("revision_state") or "ACTIVE"
        stats["spreads_seen"] += 1
        stats["keys_stamped"] += 1

    # 2) In-run supersession — one ACTIVE per key.
    by_key: dict[str, list[dict]] = {}
    for p in picks:
        k = p.get("canonical_spread_key")
        if not k:
            continue
        by_key.setdefault(k, []).append(p)

    def _sort_key(p: dict) -> tuple:
        edge = p.get("edge_percent")
        try:
            e = float(edge) if edge is not None else -1e9
        except Exception:
            e = -1e9
        mp = p.get("model_probability") or p.get("win_probability") or 0
        try:
            m = float(mp)
            if m > 1: m = m / 100.0
        except Exception:
            m = 0.0
        ls = p.get("lock_score") or 0
        try:
            ls = float(ls)
        except Exception:
            ls = 0.0
        pid = str(p.get("id") or "")
        # Highest edge, mp, lock_score win; lexicographic id is the
        # final deterministic tiebreak (ASC — smallest string wins).
        return (-e, -m, -ls, pid)

    for k, rows in by_key.items():
        if len(rows) < 2:
            continue
        rows.sort(key=_sort_key)
        winner = rows[0]
        for loser in rows[1:]:
            if loser.get("revision_state") == "SUPERSEDED_IN_RUN":
                continue
            loser["revision_state"] = "SUPERSEDED_IN_RUN"
            loser["superseded_by_selection"] = winner.get("selection")
            loser["superseded_by_pick_id"] = winner.get("id")
            loser["superseded_reason"] = "SPREAD_SIDE_CONFLICT"
            loser["off_board"] = True
            loser["off_board_reasons"] = list(
                loser.get("off_board_reasons") or []
            ) + ["SPREAD_SIDE_CONFLICT"]
            stats["superseded"] += 1
            log.debug(
                "spread_supersede key=%s winner=%s loser=%s",
                k, winner.get("id"), loser.get("id"),
            )
    return stats


__all__ = [
    "enforce_single_active_spread",
    "_canonical_spread_key",
]
