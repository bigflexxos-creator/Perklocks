"""NFL Alt-Ladder Monotonicity + Same-Threshold Consistency Guard
================================================================

2026-06-22 · READ-TIME post-scoring guard applied inside the
``/api/picks/today`` projection.  ZERO database writes.

Purpose
-------
Prior diagnostic confirmed two defects on the live NFL alt board:

1. **Non-monotone probability ladders.**  Same ``(player, market
   family, side)`` produced win-probabilities that were NOT
   monotonic in the threshold, e.g.::

      Prescott Pass Yds  260.5 → wp=54.1%   270.5 → wp=57.1%   (BREAK)
      Burrow   Pass Yds  268.5 → wp=54.3%   273.5 → wp=55.1%   (BREAK)

   A harder OVER threshold cannot have a higher probability under
   the same predictive distribution.  Root cause: alt-rungs were
   scored by different sub-models / book-implied blends instead of
   a single canonical player-stat distribution.

2. **Same-threshold multi-probability.**  Burrow had three rows at
   ``line=249.5`` with win-probabilities 62.0 / 61.1 / 56.3 —
   different books producing different independent probabilities
   for the identical canonical threshold.  Books legitimately
   differ on ODDS, never on the INDEPENDENT football model.

Guard semantics
---------------
For every group ``(canonical_player_id, market_family, side)`` in
the outgoing pick list:

  * Sort OVER rows by ``line`` ascending, UNDER rows descending.
  * Consolidate rows sharing the same ``(family, side, line)``
    canonical threshold.  Consolidation picks the row with the
    SAFEST (most conservative) model probability — this avoids
    silently promoting a rogue outlier and keeps the surviving
    ``book`` + ``book_odds`` intact from that same row so no
    book/odds mismatch is ever emitted.
  * Enforce ``wp[i+1] <= wp[i]`` for OVER (``>=`` for UNDER).
    When the raw scored probability breaks monotonicity, the row's
    ``win_probability`` and ``model_probability`` are CLAMPED to
    the previous rung's value.  A ``monotonicity_clamp`` audit
    marker is stamped on the pick for observability.  The
    ``lock_score`` is NOT recomputed here — Lock Score is stamped
    by the writer path; the clamp is a truth-preserving display
    guard.  Downstream Lock Score refresh will pick up the clamped
    probability on the next scoring pass.

Contract
--------
* ZERO database writes (read-time projection only).
* Frozen ``db.picks`` rows are IMMUTABLE.
* Never alters ``line``, ``book_odds``, ``book`` on the surviving
  row (only which row survives when duplicates share a threshold).
* Never invents a threshold, never fabricates a probability, never
  inflates a Lock Score.
* Applies universally across NFL alt-ladder families — no
  per-player logic, no star-list.
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional
from collections import defaultdict

logger = logging.getLogger("lockscore.nfl_alt_ladder_monotonicity")


# Market-family detection (mirrors ``nfl_alt_label_projection`` +
# ``sports_engine._prop_market_label``).  Kept local so this module
# stays self-contained.
def _family_of(market: str) -> Optional[str]:
    m = (market or "").lower()
    if "pass yd" in m or "passing yard" in m:  return "pass_yds"
    if "rush yd" in m or "rushing yard" in m:  return "rush_yds"
    if "rec yd" in m or "receiving yard" in m or "reception yd" in m: return "rec_yds"
    if "receptions" in m and "yd" not in m:    return "receptions"
    if "pass td" in m or "passing td" in m:    return "pass_tds"
    if "pass att" in m or "passing att" in m:  return "pass_attempts"
    if "pass comp" in m or "passing comp" in m: return "pass_completions"
    if "rush att" in m:                         return "rush_attempts"
    return None


def _side_of(pick: dict) -> str:
    m = (pick.get("market") or "").lower()
    if "under" in m: return "under"
    return "over"  # milestone-projected "200+ …" reads as OVER too


def _player_key(pick: dict) -> Optional[str]:
    return (
        pick.get("canonical_player_id")
        or pick.get("player_id")
        or (pick.get("selection") or "").strip().lower()
        or None
    )


def _line_of(pick: dict) -> Optional[float]:
    v = pick.get("line") if pick.get("line") is not None else pick.get("point")
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _wp(pick: dict) -> Optional[float]:
    wp = pick.get("win_probability")
    if wp is None:
        wp = pick.get("model_probability")
        if wp is not None:
            try: wp = float(wp) * 100.0
            except (TypeError, ValueError): wp = None
    try:
        return float(wp) if wp is not None else None
    except (TypeError, ValueError):
        return None


def _set_wp(pick: dict, clamped_pct: float, source_wp: float) -> None:
    """Clamp win_probability + model_probability on ``pick`` in place."""
    pick["win_probability_raw"] = source_wp
    pick["win_probability"] = round(clamped_pct, 2)
    try:
        pick["model_probability"] = round(clamped_pct / 100.0, 4)
    except Exception:
        pass
    prior = list(pick.get("monotonicity_notes") or [])
    prior.append(
        f"clamped {round(source_wp, 2)}%→{round(clamped_pct, 2)}% "
        f"(monotone with easier rung)"
    )
    pick["monotonicity_notes"] = prior
    pick["monotonicity_clamp"] = True


def apply_nfl_alt_ladder_guard(picks: Iterable[dict]) -> dict:
    """Group NFL alt-ladder picks by (player, family, side), consolidate
    duplicate thresholds, enforce probability monotonicity.

    Idempotent.  Returns stats dict for logging.
    """
    if not picks:
        return {"scanned": 0, "consolidated": 0, "clamped": 0}
    # Convert to list to allow in-place mutation + removal.
    if not isinstance(picks, list):
        picks = list(picks)

    scanned = consolidated = clamped = 0
    to_remove: set[int] = set()  # indices to drop after consolidation

    # Bucket by (player, family, side).
    buckets: dict[tuple, list[tuple[int, dict]]] = defaultdict(list)
    for idx, p in enumerate(picks):
        if not isinstance(p, dict):
            continue
        if str(p.get("sport") or "").upper() != "NFL":
            continue
        family = _family_of(p.get("market") or "")
        if not family:
            continue
        pkey = _player_key(p)
        if not pkey:
            continue
        side = _side_of(p)
        line = _line_of(p)
        wp = _wp(p)
        if line is None or wp is None:
            continue
        scanned += 1
        buckets[(pkey, family, side)].append((idx, p))

    for key, entries in buckets.items():
        pkey, family, side = key
        # ── Same-threshold consolidation ─────────────────────────
        # Group by canonical line (rounded to 0.5).  When multiple
        # picks share a line, KEEP the row with the safest wp (min
        # for OVER, max for UNDER) so we never inflate.  Others get
        # marked for removal from the outgoing list.
        by_line: dict[float, list[tuple[int, dict]]] = defaultdict(list)
        for idx, p in entries:
            by_line[round(_line_of(p) or 0.0, 1)].append((idx, p))

        # Build the survivor list post-consolidation.
        survivors: list[tuple[float, int, dict]] = []
        for ln, group in by_line.items():
            if len(group) == 1:
                survivors.append((ln, group[0][0], group[0][1]))
                continue
            # Pick the safest (most conservative) probability.
            if side == "over":
                keep_idx, keep_p = min(group, key=lambda x: _wp(x[1]) or 1e9)
            else:
                keep_idx, keep_p = max(group, key=lambda x: _wp(x[1]) or -1e9)
            for idx, p in group:
                if idx != keep_idx:
                    to_remove.add(idx)
                    consolidated += 1
                    # Annotate the survivor with a duplicate audit note.
                    notes = list(keep_p.get("dedupe_notes") or [])
                    notes.append(
                        f"consolidated duplicate line={ln} "
                        f"wp={round(_wp(p) or 0.0, 2)}% "
                        f"(kept safest={round(_wp(keep_p) or 0.0, 2)}%)"
                    )
                    keep_p["dedupe_notes"] = notes
            survivors.append((ln, keep_idx, keep_p))

        # ── Monotonicity enforcement ────────────────────────────
        # Sort by line asc for OVER (harder = higher line, wp must
        # be non-increasing).  UNDER: sort desc (harder = lower
        # line, wp must be non-increasing).
        survivors.sort(key=lambda t: t[0], reverse=(side == "under"))
        prev_wp: Optional[float] = None
        for ln, idx, p in survivors:
            wp = _wp(p)
            if wp is None:
                continue
            if prev_wp is not None and wp > prev_wp + 1e-6:
                # Break — clamp DOWN to the easier rung's wp.
                _set_wp(p, prev_wp, wp)
                clamped += 1
                wp = prev_wp
            prev_wp = wp

    # Drop consolidated duplicates by rebuilding the list in-place.
    if to_remove:
        kept = [p for i, p in enumerate(picks) if i not in to_remove]
        # ``picks`` is the caller's outgoing list — mutate in place so
        # downstream reference stays valid.
        picks.clear()
        picks.extend(kept)

    return {
        "scanned": scanned,
        "consolidated": consolidated,
        "clamped": clamped,
    }


__all__ = ["apply_nfl_alt_ladder_guard"]
