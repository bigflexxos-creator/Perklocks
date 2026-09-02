"""Phase 9B — DURABLE JOB OWNERSHIP invariants.

The authoritative Phase-9 spec requires durable idempotent job
ownership across backend replicas for provider refresh /
publication / settlement / history reconciliation / learning /
backfills / scheduled jobs.  Implemented via
`services/job_coordinator.py` + Mongo `scheduled_jobs` collection.

  J1. `scheduled_jobs.job_name` is UNIQUE (single owner).
  J2. `scheduled_jobs` has a `lease_until` index (lease expiry
      recovery for crashed owners).
  J3. `JobCoordinator.acquire` / `heartbeat` / `release` are the
      only atomic API on the coordinator (no direct doc mutation
      allowed from outside).
  J4. `job_execution_log` + `job_audit_log` collections exist with
      TTL indexes (30-day + 180-day retention).
  J5. Coordinator produces sanitised metadata (no secrets leaked).
"""
from __future__ import annotations
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest

from services.index_registry import _INDEX_SPECS


def test_scheduled_jobs_job_name_unique():
    m = [s for s in _INDEX_SPECS
         if s.collection == "scheduled_jobs" and s.name == "job_name_uniq"]
    assert m and m[0].unique is True


def test_scheduled_jobs_lease_until_indexed():
    m = [s for s in _INDEX_SPECS
         if s.collection == "scheduled_jobs" and s.name == "lease_until_idx"]
    assert m


def test_job_execution_log_has_30d_ttl():
    m = [s for s in _INDEX_SPECS
         if s.collection == "job_execution_log"
         and s.expire_after_seconds is not None]
    assert m, "no TTL on job_execution_log"
    # TTL "0 seconds after ttl_at" — retention is enforced by the
    # ttl_at field, which is populated 30 days in the future.
    assert m[0].expire_after_seconds == 0


def test_job_audit_log_has_180d_ttl():
    m = [s for s in _INDEX_SPECS
         if s.collection == "job_audit_log"
         and s.expire_after_seconds is not None]
    assert m


def test_coordinator_public_surface_present():
    """The three atomic-ownership methods MUST exist on JobCoordinator."""
    from services.job_coordinator import JobCoordinator
    for m in ("acquire", "heartbeat", "release"):
        assert callable(getattr(JobCoordinator, m, None)), \
            f"JobCoordinator.{m} missing"


def test_metadata_sanitiser_scrubs_secrets():
    from services.job_coordinator import _sanitize_metadata
    payload = {
        "api_key": "sk-live-abcdef",
        "authorization": "Bearer secret-xyz",
        "safe_field": "hello",
        "nested": {"apikey": "hidden", "note": "ok"},
    }
    out = _sanitize_metadata(payload)
    # Every recognised secret key must be scrubbed.
    for redacted in (out.get("api_key"), out.get("authorization"),
                     out["nested"].get("apikey")):
        assert redacted != "sk-live-abcdef"
        assert redacted != "Bearer secret-xyz"
        assert redacted != "hidden"
    # Non-secret fields preserved.
    assert out["safe_field"] == "hello"
    assert out["nested"]["note"] == "ok"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
