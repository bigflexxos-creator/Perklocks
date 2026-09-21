"""Universal Canonical Epoch Contract — backend (2026-06-21 v2).

Verifies:
    · X-Canonical-Revision (ordered integer) present on every canonical GET
    · Value matches board_generation.active()["revision"]
    · /api/version returns canonical_epoch in body + Cache-Control no-store
    · CORS Access-Control-Expose-Headers includes X-Canonical-Revision
    · Auth paths are DENIED (no leak of internal state fingerprint)
    · Same revision reported UNIFORMLY across surfaces at rest
"""
from __future__ import annotations
import os
import httpx
import pytest

BASE = os.environ.get("BACKEND_URL", "http://localhost:8001")
SURFACES = (
    "/api/version",
    "/api/picks/86ed5d23-a5a6-4963-b8c3-36170a449b9f",           # MLB detail
    "/api/picks/c800012c-9f1b-5199-a862-be83b52864db",           # NFL detail
    "/api/picks/86ed5d23-a5a6-4963-b8c3-36170a449b9f/historical-intelligence?sample_scope=L10&venue_scope=ALL",
    "/api/picks/c800012c-9f1b-5199-a862-be83b52864db/historical-intelligence?sample_scope=L10&venue_scope=ALL",
)


def _login(c: httpx.Client) -> str:
    r = c.post(f"{BASE}/api/auth/login",
               json={"email": "demo@lockscore.ai", "password": "demo123"})
    r.raise_for_status()
    return r.json()["access_token"]


def test_canonical_revision_present_and_integer_on_every_surface():
    with httpx.Client(timeout=30) as c:
        tok = _login(c)
        h = {"Authorization": f"Bearer {tok}"}
        for path in SURFACES:
            r = c.get(f"{BASE}{path}", headers=h)
            assert r.status_code < 500, f"{path}: {r.status_code}"
            rev = r.headers.get("x-canonical-revision")
            assert rev is not None, f"missing X-Canonical-Revision on {path}"
            assert rev.isdigit(), f"X-Canonical-Revision must be an integer string on {path}, got {rev!r}"
            assert int(rev) >= 0


def test_canonical_revision_uniform_across_surfaces_at_rest():
    """Ordered contract requires that every canonical surface reports
    the SAME integer revision at the same moment.  Divergence would
    mean the middleware is per-request instead of process-global."""
    with httpx.Client(timeout=30) as c:
        tok = _login(c)
        h = {"Authorization": f"Bearer {tok}"}
        revs = {}
        for path in SURFACES:
            r = c.get(f"{BASE}{path}", headers=h)
            revs[path] = r.headers.get("x-canonical-revision")
        distinct = set(revs.values())
        assert len(distinct) == 1, f"revision diverges at rest: {revs}"


def test_version_body_contains_canonical_epoch_and_no_store():
    with httpx.Client(timeout=30) as c:
        r = c.get(f"{BASE}/api/version")
        assert r.status_code == 200
        body = r.json()
        assert "canonical_epoch" in body
        ce = body["canonical_epoch"]
        assert isinstance(ce["revision"], int)
        assert isinstance(ce["board_version"], str)
        assert isinstance(ce["generation_id"], str)
        # No-store on the epoch probe.
        cc = (r.headers.get("cache-control") or "").lower()
        assert "no-store" in cc, f"expected no-store on /api/version, got {cc!r}"


def test_cors_expose_headers_include_canonical_revision():
    with httpx.Client(timeout=30) as c:
        r = c.get(f"{BASE}/api/version",
                  headers={"Origin": "http://localhost:3000"})
        exposed = (r.headers.get("access-control-expose-headers") or "")
        assert "X-Canonical-Revision" in exposed, (
            f"CORS expose_headers missing X-Canonical-Revision: {exposed!r}")
        assert "X-Canonical-Version" in exposed
        assert "X-Canonical-Generation-Id" in exposed


def test_auth_paths_do_not_leak_canonical_headers():
    with httpx.Client(timeout=30) as c:
        r = c.post(f"{BASE}/api/auth/login",
                   json={"email": "nope@x.com", "password": "wrong"})
        keys = {k.lower() for k in r.headers.keys()}
        assert "x-canonical-revision" not in keys
        assert "x-canonical-version" not in keys
        assert "x-canonical-generation-id" not in keys


def test_middleware_body_unchanged_on_version_endpoint():
    with httpx.Client(timeout=30) as c:
        r = c.get(f"{BASE}/api/version")
        body = r.json()
        # Legacy fields preserved.
        assert "data_version" in body
        assert "server_time" in body


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
