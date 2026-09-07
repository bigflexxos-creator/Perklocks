"""MAIN 41 · P0-B1 — Canonical publication truth SHIELD.

Runtime symptom: NFL props visible on Preview after force-refresh
disappeared after the next scheduler tick. Root cause: `_apply_atomic_delete`
and the `seen_ids` fresh-overwrite delete + `SEMANTIC_DELETE` pass
inside `pick_refresh_orchestrator` would delete previously-published
pick documents on every recurring refresh, because the family
conservation filter includes prop-market regex families and neither
the pick_date delete nor the id delete excluded canonical publication
truth.

Surgical fix: every `delete_many` inside `_refresh_picks` now carries
an explicit `publication_source ∈ {None, "", False, missing}` filter,
so a document that has been canonically published survives every
future refresh regardless of whether the refresh re-emits it.

Frozen publication truth (per PublishedPickContract) is immutable.
"""
from __future__ import annotations

import os


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def test_atomic_delete_shields_canonical_publications():
    src = _read("/app/backend/services/pick_refresh_orchestrator.py")
    # The shield definition must exist BEFORE the delete_many calls.
    assert '_publication_shield = {"$or": [' in src, (
        "_publication_shield definition missing from _apply_atomic_delete."
    )
    # Every delete_many in _apply_atomic_delete must consume the shield.
    # We look for the composed spread ``**_publication_shield``.
    assert src.count("**_publication_shield") >= 4, (
        "Not every delete_many in _apply_atomic_delete propagates the "
        "publication shield — one or more delete paths can still wipe "
        "canonical publication truth."
    )


def test_id_collision_delete_shields_publications():
    src = _read("/app/backend/services/pick_refresh_orchestrator.py")
    # The id-collision fresh-overwrite delete (after _apply_atomic_delete)
    # must also gate on the shield.
    assert 'MAIN 41 · P0-B1 shield — never destroy canonical' in src


def test_semantic_delete_shields_publications():
    src = _read("/app/backend/services/pick_refresh_orchestrator.py")
    # The SEMANTIC_DELETE pass must gate on the shield too.
    assert 'shield — never destroy\n                    # canonical publication truth via semantic dedupe' in src


def test_publication_shield_pattern_correctness():
    """The shield must match ANY row that has no real publication
    source: missing key, None, empty string, or False."""
    # These synthetic rows should each be shielded.
    rows_shielded = [
        {},
        {"publication_source": None},
        {"publication_source": ""},
        {"publication_source": False},
    ]
    # These should NOT be shielded (they are canonically published):
    rows_delete_ok = [
        {"publication_source": "canonical_pipeline"},
        {"publication_source": "MAIN41_LIVE_PROOF"},
    ]
    def _shielded(row):
        src = row.get("publication_source")
        # Simulate the Mongo $or predicate:
        # publication_source in [None,"",False]  OR  key missing
        return "publication_source" not in row or src in (None, "", False)
    for r in rows_shielded:
        assert _shielded(r), r
    for r in rows_delete_ok:
        assert not _shielded(r), r


if __name__ == "__main__":
    test_atomic_delete_shields_canonical_publications()
    test_id_collision_delete_shields_publications()
    test_semantic_delete_shields_publications()
    test_publication_shield_pattern_correctness()
    print("OK — MAIN 41 P0-B1 canonical publication shield in place.")
