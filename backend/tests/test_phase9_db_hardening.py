"""Phase 9 — PUBLICATION / DATABASE HARDENING invariants.

  H1. `picks.id` is UNIQUE — a canonical pick_id has exactly ONE
      row.  No collision, no re-write.
  H2. `prediction_snapshots` has unique (prediction_id,
      snapshot_version) so re-publishing the same version is
      idempotent.
  H3. `prediction_snapshots` has unique (prediction_id,
      idempotency_key) so the payload_hash boundary blocks silent
      double-writes.
  H4. `users.email` is unique.
  H5. `publication_mismatch_report` has a 30-day TTL on
      `logged_at_dt` (retention).
  H6. Every index in the registry declares owner_service (no
      orphan indexes).
  H7. No two IndexSpecs share the same (collection, name).
"""
from __future__ import annotations
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest


def _get_specs():
    from services.index_registry import _INDEX_SPECS
    return list(_INDEX_SPECS)


def test_picks_id_is_unique():
    specs = _get_specs()
    m = [s for s in specs if s.collection == "picks" and s.name == "id_1"]
    assert m, "picks.id_1 spec missing"
    assert m[0].unique is True


def test_prediction_snapshots_unique_version_key():
    specs = _get_specs()
    m = [s for s in specs
         if s.collection == "prediction_snapshots"
         and s.name == "prediction_snapshot_version_uniq"]
    assert m, "prediction_snapshot_version_uniq missing"
    assert m[0].unique is True


def test_prediction_snapshots_unique_idempotency_key():
    specs = _get_specs()
    m = [s for s in specs
         if s.collection == "prediction_snapshots"
         and s.name == "prediction_idempotency_uniq"]
    assert m, "prediction_idempotency_uniq missing"
    assert m[0].unique is True


def test_users_email_unique():
    specs = _get_specs()
    m = [s for s in specs
         if s.collection == "users" and s.name == "email_1"]
    assert m and m[0].unique is True


def test_publication_mismatch_report_has_ttl():
    specs = _get_specs()
    ttl = [s for s in specs
           if s.collection == "publication_mismatch_report"
           and s.expire_after_seconds]
    assert ttl, "no TTL declared on publication_mismatch_report"
    assert ttl[0].expire_after_seconds == 2_592_000  # 30 days


def test_every_index_declares_owner_service():
    specs = _get_specs()
    orphans = [f"{s.collection}.{s.name}"
               for s in specs if not (getattr(s, 'owner_service', None))]
    assert not orphans, f"indexes with no owner_service: {orphans[:10]}"


def test_no_duplicate_index_names_per_collection():
    from collections import Counter
    specs = _get_specs()
    dupes = [k for k, v in Counter(
        (s.collection, s.name) for s in specs).items() if v > 1]
    assert not dupes, f"duplicate index specs: {dupes}"


def test_prediction_snapshots_has_board_version_idx():
    specs = _get_specs()
    m = [s for s in specs
         if s.collection == "prediction_snapshots"
         and s.name == "board_version_idx"]
    assert m, "board_version_idx missing (needed for board queries)"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
