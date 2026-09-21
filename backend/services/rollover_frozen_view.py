"""Rollover Frozen-View Reader — TRUE wager immutability
=============================================================

P0 Root Closure (2026-06-21) — Rollover displayed values must reflect
the EXACT wager the selector froze at slate freeze time, not whatever
the mutable ``db.picks`` row currently holds.

Prior to this module, the Rollover endpoint used ``canonical_pick_id``
from the frozen slate leg only for membership/order, then rehydrated
selection/market/line/odds/win_probability/lock_score from the live
``db.picks`` document.  If Locks moved a line, re-priced, or the model
adjusted the lock score, the "frozen" Rollover would silently mutate
underneath the user.

``build_frozen_view(leg, live_doc)`` returns a dict shaped like a pick
document, BUT with every wager field sourced from the frozen leg
snapshot when the freeze contract version is ≥ 2.  Non-wager runtime
information (status / actual / result / event_status) is layered from
the live document on top so the settlement engine can still attach
outcomes to the frozen wager.

Legacy v1 legs (frozen before this contract landed) carry only
identity + a small ranking subset — for those legs we transparently
fall back to the live document for wager fields the leg does not
carry, and stamp ``frozen_wager_provenance = "legacy_v1_partial"`` so
diagnostic UIs can flag the coverage gap without breaking display.
"""
from __future__ import annotations

from typing import Any, Optional


# Wager fields that MUST come from the frozen leg when present.  These
# are the values the user saw at selection time.
_FROZEN_WAGER_KEYS = (
    "market", "market_display", "selection", "line",
    "sportsbook", "odds",
    "win_probability", "lock_score", "grade",
    "player_name",
)

# Runtime informational fields the frozen view is happy to inherit from
# the live document — status/result/actual only.  Any drift in these
# is expected (settlement writes them) and does NOT alter the wager.
_RUNTIME_INFO_KEYS = (
    "status", "actual", "result", "event_status",
    "settled_at", "graded_at", "grading_state",
    "score", "final_score",
)


def _leg_v(leg: dict) -> int:
    try:
        return int(leg.get("frozen_wager_version") or 1)
    except (TypeError, ValueError):
        return 1


def build_frozen_view(leg: dict, live_doc: Optional[dict]) -> dict:
    """Return a pick-shaped dict with WAGER truth from ``leg`` and
    runtime status from ``live_doc``.

    * Wager fields (selection/market/line/odds/book/probability/lock
      score/grade/player) come STRICTLY from ``leg`` when v2 frozen and
      the field is present in the leg.
    * Identity (canonical_pick_id / canonical_event_id / publication
      version / board_version) always come from ``leg``.
    * Runtime informational fields (status/actual/result) come from
      ``live_doc`` — settlement is allowed to attach an outcome to the
      original wager.
    * Everything else (metadata the UI does not directly render as
      wager truth) is inherited from ``live_doc`` for display continuity
      and falls back to the leg's own copy when the live doc is absent.
    """
    if not isinstance(leg, dict):
        return dict(live_doc or {})
    live = dict(live_doc) if isinstance(live_doc, dict) else {}
    fv = _leg_v(leg)

    # Start from the live doc so we inherit every non-wager display
    # field (sport, league, event, event_time, home/away, provider ids).
    out: dict = dict(live)

    # Identity — always from the leg.
    if leg.get("canonical_pick_id") is not None:
        out["id"] = leg["canonical_pick_id"]
        out["canonical_pick_id"] = leg["canonical_pick_id"]
    if leg.get("canonical_event_id") is not None:
        out["canonical_event_id"] = leg["canonical_event_id"]
    if leg.get("publication_version") is not None:
        out["publication_revision"] = leg["publication_version"]
    if leg.get("board_version") is not None:
        out["board_version"] = leg["board_version"]

    # Display fields — leg wins when present (rank, event, times).
    for k in ("rank", "sport", "league", "event", "event_time",
              "home_team", "away_team"):
        v = leg.get(k)
        if v is not None:
            out[k] = v

    # Wager truth — leg wins for v2 legs when the leg carries the field.
    if fv >= 2:
        for k in _FROZEN_WAGER_KEYS:
            if k in leg and leg[k] is not None:
                out[k] = leg[k]
        # Preserve canonical aliases the downstream serializers may
        # inspect (published_* fields drive display in some renderers).
        if leg.get("odds") is not None:
            out["book_odds"] = leg["odds"]
            out["published_odds"] = leg["odds"]
        if leg.get("line") is not None:
            out["published_line"] = leg["line"]
        if leg.get("win_probability") is not None:
            out["published_probability"] = leg["win_probability"]
        if leg.get("lock_score") is not None:
            out["published_lock_score"] = leg["lock_score"]
        if leg.get("grade") is not None:
            out["published_grade"] = leg["grade"]
        if leg.get("sportsbook") is not None:
            out["book"] = leg["sportsbook"]
        out["frozen_wager_provenance"] = "leg_v2"
    else:
        # Legacy v1 leg — only overlay values the leg happens to carry.
        for k in _FROZEN_WAGER_KEYS:
            v = leg.get(k)
            if v is not None:
                out[k] = v
        out["frozen_wager_provenance"] = "legacy_v1_partial"
        out["legacy_frozen_leg"] = True

    # Runtime info — settlement / grading state ALWAYS from live doc.
    if live:
        for k in _RUNTIME_INFO_KEYS:
            if k in live:
                out[k] = live[k]

    # Invalidation flag from the reconcile pipeline (LEG_INVALIDATED).
    if leg.get("invalidated"):
        out["frozen_leg_invalidated"] = True
        out["frozen_leg_invalidated_reason"] = leg.get("invalidated_reason")

    # Absence of live doc — still a valid response: wager stands.
    if not live:
        out["live_pick_absent"] = True

    return out


def build_frozen_views(slate: dict, live_docs_by_id: dict) -> list[dict]:
    """Convenience: build views for every non-invalidated leg in ``slate``
    preserving rank order.  ``live_docs_by_id`` maps ``canonical_pick_id``
    → live ``db.picks`` document (or empty dict / missing when absent).
    """
    if not isinstance(slate, dict):
        return []
    out: list[dict] = []
    for leg in (slate.get("legs") or []):
        if leg.get("invalidated"):
            continue
        cid = leg.get("canonical_pick_id")
        live = live_docs_by_id.get(cid) if isinstance(live_docs_by_id, dict) else None
        out.append(build_frozen_view(leg, live))
    return out


__all__ = ["build_frozen_view", "build_frozen_views"]
