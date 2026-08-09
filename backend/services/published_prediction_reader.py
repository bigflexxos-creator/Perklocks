"""PublishedPredictionReader — the single reader every endpoint uses.

Phase 1b — 2026-08.  Reads the immutable published contract off of
`prediction_snapshots` (or the dual-write projection on `picks`) and
merges those values into the pick dictionary that endpoints return.

Design goals
────────────
1. Every endpoint calls exactly one function — `hydrate(pick)` or
   `hydrate_many(picks)` — and gets back a pick dict whose scored
   fields (`lock_score`, `win_probability`, `edge_percent`, `grade`,
   `confidence`, `book_odds`, `line`, and `reasoning`) are the
   published values.
2. Backwards compatibility with the current frontend is preserved by
   aliasing `published_*` → the legacy field names the UI already
   uses.  No frontend change is required.
3. If a pick predates Phase 1c backfill (`published_*` fields absent),
   the reader passes the legacy values through unchanged and stamps
   `_prediction_source="legacy_unpublished"` on the pick so admin
   tooling can flag it.
4. This module is READ-ONLY.  It never writes to `picks` or
   `prediction_snapshots`.

Contract fields exposed on every hydrated pick
──────────────────────────────────────────────
    lock_score          ← published_lock_score
    win_probability     ← published_probability (normalized to [0, 1])
    edge_percent        ← published_edge
    grade               ← published_grade
    confidence          ← published_confidence
    book_odds / odds    ← published_odds
    line                ← published_line
    reasoning           ← published_reasoning
    _prediction_source  ← "snapshot" | "legacy_unpublished"
    _snapshot_version   ← int | None
    _model_version      ← str | None
    _published_at       ← str | None
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

logger = logging.getLogger("lockscore.published_reader")

# ─────────────────────────────────────────────────────────────────
# Legacy alias map — what fields the frontend already expects.
# Aliasing "published_X" back to the legacy name keeps the response
# schema unchanged so no UI work is required.
# ─────────────────────────────────────────────────────────────────
PUBLISHED_TO_LEGACY: dict[str, str] = {
    "published_lock_score":   "lock_score",
    "published_probability":  "win_probability",
    "published_edge":         "edge_percent",
    "published_grade":        "grade",
    "published_confidence":   "confidence",
    "published_odds":         "book_odds",
    "published_line":         "line",
    "published_reasoning":    "reasoning",
}

# Read-side aliases: legacy odds is exposed as both `book_odds` AND
# `odds` on some endpoints; keep both in sync when we hydrate.
ODDS_ALIASES: tuple[str, ...] = ("book_odds", "odds", "american_odds")


def normalize_probability(value: Any) -> float:
    """Coerce a stored probability to the canonical `[0, 1]` fraction.

    Handles the fraction/percentage inconsistency surfaced in the
    Phase 1a dual-write mismatch report:
      • None / non-numeric / NaN / inf → 0.0
      • [0, 1] fraction     → returned as-is
      • (1, 100] percentage → divided by 100
      • >100 or <0          → clamped to [0, 1] + WARN
    """
    import math
    if value is None:
        return 0.0
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(v):
        return 0.0
    if v < 0:
        logger.debug("normalize_probability: clamping negative %r → 0.0", v)
        return 0.0
    if v <= 1.0:
        return v
    if v <= 100.0:
        return v / 100.0
    logger.warning(
        "normalize_probability: value %r > 100 — clamping to 1.0", v)
    return 1.0


def hydrate(pick: dict, *, sport_defaults: bool = True) -> dict:
    """Return a copy of `pick` with published values aliased onto the
    legacy field names.  Never mutates the input.

    If the pick lacks `published_*` fields (legacy row pre-backfill),
    passes the legacy values through and tags `_prediction_source =
    'legacy_unpublished'`.

    ── P0-1 (2026-08-11) canonical → legacy unit conversion ────────
    The snapshot stores probability as a 0-1 fraction; the legacy
    ``win_probability`` alias is a 0-100 percentage (frontend renders
    ``${wp}%``).  We convert at this boundary and clamp defensively.
    ``published_edge`` may be None (no book line) — that state MUST
    survive as None on the legacy ``edge_percent`` alias.
    ``published_confidence`` is a label string ("Very High", …) —
    passthrough as-is.
    """
    if not isinstance(pick, dict):
        return pick
    p = dict(pick)   # shallow copy
    has_snapshot = "published_lock_score" in p
    if has_snapshot:
        # Alias each published_* back onto the field name the frontend
        # (and existing tests) expect.
        for pub_key, legacy_key in PUBLISHED_TO_LEGACY.items():
            if pub_key in p:
                v = p[pub_key]
                if legacy_key == "win_probability":
                    # Canonical fraction ⇒ legacy percentage.
                    if v is None:
                        v = None
                    else:
                        pf = normalize_probability(v)   # returns [0, 1]
                        v = round(pf * 100.0, 2)
                elif legacy_key == "edge_percent":
                    # Preserve None ⇒ "no line".  Any other value
                    # passes through as a percentage-point delta.
                    if v is None:
                        v = None
                # `confidence` label passes through verbatim.
                p[legacy_key] = v
        # Keep odds aliases in sync.
        odds = p.get("book_odds")
        if odds is not None:
            for a in ODDS_ALIASES:
                p.setdefault(a, odds)
                p[a] = odds
        # Provenance markers.
        p["_prediction_source"] = "snapshot"
        p["_snapshot_version"] = p.get("snapshot_version")
        p["_model_version"] = p.get("model_version")
        p["_published_at"] = p.get("published_at")
    else:
        # Legacy row — pass through, normalize probability if present.
        # `win_probability` on a legacy pick doc is already in the
        # frontend-visible unit (0-100 percentage) so we do NOT
        # convert here — only guard against unexpected fractional
        # values written by older writers by promoting fractions
        # (≤ 1.0) up to their percentage form.
        if sport_defaults and "win_probability" in p:
            wp = p.get("win_probability")
            if wp is not None:
                try:
                    wp_f = float(wp)
                    # Legacy row promoting a fraction that leaked
                    # through pre-fix.  Convert defensively.
                    if 0.0 < wp_f <= 1.0:
                        wp_f = wp_f * 100.0
                    p["win_probability"] = round(max(0.0, min(100.0, wp_f)), 2)
                except (TypeError, ValueError):
                    pass
        p["_prediction_source"] = "legacy_unpublished"
        p["_snapshot_version"] = None
        p["_model_version"] = None
        p["_published_at"] = None
    return p


def hydrate_many(picks: Iterable[dict]) -> list[dict]:
    return [hydrate(p) for p in picks]


class PublishedPredictionReader:
    """Optional class wrapper for callers that want to fetch snapshots
    on demand (e.g. when the picks document is stale relative to the
    snapshot collection).  Endpoints that receive picks from a query
    should use the free-function `hydrate()` — it's O(1) per pick.
    """

    def __init__(self, db) -> None:
        self.db = db

    async def get_active_snapshot(self, prediction_id: str) -> Optional[dict]:
        from services.prediction_publication_service import (
            SNAPSHOT_COLLECTION,
        )
        return await self.db[SNAPSHOT_COLLECTION].find_one(
            {"prediction_id": prediction_id, "is_active": True},
            {"_id": 0},
        )

    async def hydrate_from_snapshot(self, pick: dict) -> dict:
        """Same as `hydrate()` but ALWAYS fetches the latest snapshot
        from the collection.  Use when the caller is not confident
        that the dual-write projection on `picks` is up to date."""
        pid = pick.get("id") or pick.get("prediction_id")
        if not pid:
            return hydrate(pick)
        snap = await self.get_active_snapshot(pid)
        if not snap:
            return hydrate(pick)
        merged = dict(pick)
        for pub_key in (
            "published_lock_score", "published_probability",
            "published_edge", "published_grade", "published_confidence",
            "published_odds", "published_line", "published_reasoning",
            "snapshot_version", "model_version", "published_at",
        ):
            if pub_key in snap:
                merged[pub_key] = snap[pub_key]
        return hydrate(merged)


__all__ = [
    "PublishedPredictionReader",
    "hydrate", "hydrate_many",
    "normalize_probability",
    "PUBLISHED_TO_LEGACY",
]
