"""Canonical board source-of-truth gate (P0-1, 2026-08-08).

Purpose
───────
Enforce the publication contract on the main Locks board without
forcing a full rewrite of `/picks/today`.

Per PUBLICATION_CONTRACT.md and ARCHITECTURE.md:
    "A prediction becomes user-board eligible only AFTER canonical
     publication."

Current reality (Phase 1a dual-write, incomplete):

* `PredictionPublicationService` writes an immutable row to
  `prediction_snapshots` AND dual-writes `published_*` fields (plus
  `publication_source`, `snapshot_version`, `published_at`,
  `payload_hash`, `idempotency_key`) onto the matching `picks`
  document by `id`.
* `/picks/today` reads `db.picks` and has historically applied no
  publication filter, so ingest paths that bypass the publication
  service (e.g. `soccer_hot_scorers`, `ufc_espn_ingest`,
  `espn_soccer_fixtures`, legacy pre-2026-08-06 rows) leak onto the
  main board even though they never went through canonical
  publication.

Design decision (Step 4 of the P0 audit):

    Direct cutover to `prediction_snapshots` is UNSAFE today because
    the snapshot doc lacks all presentation / enrichment / timing
    fields required to render a board card (sport, market, event,
    factors, insights, lineup, etc.).  Those live on `db.picks`.

    Therefore we implement the smallest safe compatibility layer:

        canonical eligibility  →  `publication_source` exists on picks
        stable identity        →  `picks.id`  ≡  `snapshots.prediction_id`
        canonical values       →  `published_*` fields (dual-written)
        presentation join      →  same `db.picks` row (already carries
                                   sport/market/event/factors/etc.)

    This makes the canonical publication the authoritative gate for
    board eligibility while leaving presentation joins in place.

Behaviour
─────────
* When `LOCKSCORE_REQUIRE_CANONICAL_PUBLICATION` (env) is truthy
  (default: TRUE), `canonical_publication_filter()` returns a Mongo
  filter fragment that requires `publication_source` to exist on the
  pick document.
* When the env is explicitly set to `"false"` / `"0"` / `"no"`, the
  helper returns an empty dict (no-op) — this is an emergency
  bypass reserved for hot-fix scenarios where legacy ingest paths
  haven't been migrated yet.

This module DOES NOT:
* mutate any canonical prediction field
* re-rank picks
* alter lock_score / Magic Tier / simulator behaviour
* touch Rollover, parlays, or Under-of-the-Day
* modify sport-specific ingestion
* deploy anything

It ONLY changes the base population source of `/picks/today`.
"""
from __future__ import annotations

import os
from typing import Any

# Env var name — canonical source of the on/off switch.
ENV_VAR = "LOCKSCORE_REQUIRE_CANONICAL_PUBLICATION"

# Default: ON.  The publication contract has been in effect since
# 2026-08-06; the flag exists purely as an emergency bypass.
_DEFAULT_ENABLED = True


def is_canonical_publication_required() -> bool:
    """Return True when the eligibility filter should be enforced.

    Reads the env var each call so operators can flip it live via
    supervisor restart without touching code.
    """
    raw = os.environ.get(ENV_VAR)
    if raw is None:
        return _DEFAULT_ENABLED
    return raw.strip().lower() not in {"false", "0", "no", "off", ""}


def canonical_publication_filter() -> dict[str, Any]:
    """Return a Mongo filter fragment enforcing canonical publication.

    A pick is board-eligible iff it carries a `publication_source`
    value on the `picks` document.  This field is written **only** by
    `services.prediction_publication_service.PredictionPublicationService`
    during its dual-write step, so its presence is equivalent to
    "a canonical publication row was created for this prediction".

    When the guard is disabled via env, returns an empty dict which
    is a no-op inside a Mongo `$and` clause.
    """
    if not is_canonical_publication_required():
        return {}
    # `publication_source` is a string when set (never null, never
    # empty by contract), so "exists" is the tightest safe check.
    return {"publication_source": {"$exists": True, "$ne": None}}


__all__ = [
    "ENV_VAR",
    "is_canonical_publication_required",
    "canonical_publication_filter",
]
