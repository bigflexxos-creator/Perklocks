"""NFL Ladder Monotonicity Guard — §A10 (Universal NFL Prop Closure).

Per user directive:
    "Threshold probabilities for one player/market must come from one
     coherent distribution.  Require:
       P(easier Over threshold) >= P(harder Over threshold).
     Fail closed if ladder integrity breaks."

Non-monotonic Over ladders are a symptom of the model treating each
line-rung as an independent event rather than a shared distribution.
The surgical containment: detect Over-ladder inversions inside a
refresh cycle, tag the offending pick with ``ladder_monotonicity_violated``,
and fail-closed by capping its Lock Score at 84.9 so it drops below the
85 board threshold.  The picks still surface off-board for
transparency (auditing / debugging), but never claim Lock authority.

Scope:
    * NFL sport only.
    * Over side only (Under is the complement — if Over ladder is
      monotone decreasing, the Under complement is monotone
      increasing by construction; enforcing on Over is sufficient).
    * Group by (canonical_player_id or selection, market_family)
      where market_family collapses `player_reception_yds_alternate`
      and `player_reception_yds` (etc.) into one family so ladders
      that span standard + alt lines are evaluated together.
"""

from __future__ import annotations

import logging
from typing import Iterable

logger = logging.getLogger(__name__)

_FAIL_LOCK = 84.9  # below the 85 Locks-board threshold


def _market_family(market_key: str | None, market: str | None) -> str | None:
    """Collapse alt + standard market keys into one family for grouping.

    Uses the provider market key when present (canonical); falls back
    to the human market string.  Returns the FAMILY key (with
    `_alternate` stripped) so a player's standard 250.5 line and alt
    150.5/175.5/200.5 rungs land in the same ladder.
    """
    mk = (market_key or "").strip().lower()
    if mk:
        return mk.replace("_alternate", "").replace("alternate_", "")
    m = (market or "").strip().lower()
    if not m:
        return None
    # Strip common decorations for stability.
    for tok in (" · alt lock", " · lock", " · playable"):
        m = m.replace(tok, "")
    return m


def _grouping_key(pick: dict) -> tuple[str, str] | None:
    pid = pick.get("canonical_player_id") or pick.get("selection")
    if not pid:
        return None
    fam = _market_family(pick.get("market_key"), pick.get("market"))
    if not fam:
        return None
    return (str(pid), fam)


def enforce_ladder_monotonicity(
    picks: Iterable[dict],
    *,
    sport: str = "NFL",
) -> dict:
    """Detect Over-ladder inversions and fail-close offenders.

    Mutates the input picks in place (sets ``ladder_monotonicity_violated=True``
    and caps ``lock_score``/``lock_score_v2``/``lock_score_peak`` at 84.9).
    Returns a summary dict for logging.

    An inversion is defined as:
        given ladder rungs sorted by ``line`` ascending, there exists a
        pair (i < j) such that ``win_probability[i] < win_probability[j]``.
    That is: a HARDER Over rung (higher line) has HIGHER model
    probability than an EASIER rung.  Both offending picks are flagged
    so the distribution defect is surfaced fully.
    """
    picks_list = [
        p for p in picks
        if (p.get("sport") or "").strip() == sport
        and (p.get("side") or "over").lower() == "over"
        and p.get("line") is not None
        and p.get("win_probability") is not None
    ]
    ladders: dict[tuple[str, str], list[dict]] = {}
    for p in picks_list:
        k = _grouping_key(p)
        if k is None:
            continue
        ladders.setdefault(k, []).append(p)

    violations = 0
    ladders_checked = 0
    ladders_ok = 0
    ladders_violated = 0
    for k, rungs in ladders.items():
        if len(rungs) < 2:
            continue
        ladders_checked += 1
        try:
            rungs.sort(key=lambda x: float(x.get("line") or 0.0))
        except (TypeError, ValueError):
            continue
        # A rung is a violator if any later (harder) rung has strictly
        # higher win_probability.  Mark ALL rungs involved in any
        # inversion pair (both the too-low easy rung AND the too-high
        # hard rung), because either could be the distribution defect.
        n = len(rungs)
        offenders: set[int] = set()
        for i in range(n):
            for j in range(i + 1, n):
                try:
                    wi = float(rungs[i].get("win_probability") or 0)
                    wj = float(rungs[j].get("win_probability") or 0)
                except (TypeError, ValueError):
                    continue
                if wj > wi + 0.5:  # allow tiny numeric noise
                    offenders.add(i)
                    offenders.add(j)
        if offenders:
            ladders_violated += 1
            for idx in offenders:
                p = rungs[idx]
                p["ladder_monotonicity_violated"] = True
                p["ladder_monotonicity_reason"] = (
                    f"non_monotonic_ladder:family={k[1]}:rungs={n}"
                )
                try:
                    cur_lock = float(p.get("lock_score") or 0)
                except (TypeError, ValueError):
                    cur_lock = 0
                if cur_lock >= 85.0:
                    p["lock_score"]      = _FAIL_LOCK
                    p["lock_score_v2"]   = min(float(p.get("lock_score_v2") or _FAIL_LOCK), _FAIL_LOCK)
                    p["lock_score_peak"] = min(float(p.get("lock_score_peak") or _FAIL_LOCK), _FAIL_LOCK)
                    p["apex_lock"]       = False
                    p["apex_score"]      = _FAIL_LOCK
                    p["apex_status"]     = "NOT_APEX"
                    p["apex_reason"]     = "ladder_monotonicity_broken"
                    try:
                        from sports_engine import _grade as _grade_fn
                        p["grade"] = _grade_fn(_FAIL_LOCK)
                    except Exception:
                        pass
                violations += 1
        else:
            ladders_ok += 1

    summary = {
        "sport": sport,
        "ladders_checked": ladders_checked,
        "ladders_ok": ladders_ok,
        "ladders_violated": ladders_violated,
        "picks_capped": violations,
    }
    if ladders_violated:
        logger.warning(
            "NFL ladder monotonicity guard: %d/%d ladders violated, "
            "%d picks capped below 85 (fail-closed per §A10).",
            ladders_violated, ladders_checked, violations,
        )
    else:
        logger.info(
            "NFL ladder monotonicity guard: %d/%d ladders OK.",
            ladders_ok, ladders_checked,
        )
    return summary


__all__ = ["enforce_ladder_monotonicity"]
