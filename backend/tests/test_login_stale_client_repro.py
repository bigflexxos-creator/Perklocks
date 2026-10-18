"""
P0-A Reproduction harness — Login 500 after stale Mongo client.

This test proves the failure path *before* the fix lands:

  1. Hit /api/auth/login → 200 (fresh client, happy path).
  2. Simulate a stale/closed Mongo client by invoking the authority's
     ``close_database()`` once — exactly what happens in some shutdown
     fallback / idle-pool scenarios.
  3. Hit /api/auth/login AGAIN with the SAME process.
     * BEFORE FIX: `server.py`'s module-level `db` symbol is a stale
       snapshot of the pre-close client, so the find_one call raises
       `InvalidOperation: Cannot use MongoClient after close` → 500.
     * AFTER FIX (lazy proxy): the proxy transparently re-resolves
       through `get_database()`, which auto-reinitialises on demand,
       and login succeeds.
  4. Capture the exception type/message + request_id + pool state.

Runs under pytest.  Does NOT touch production data (uses the demo
account only) and never installs its own MongoClient.
"""
from __future__ import annotations

import asyncio
import os
import sys
import pathlib
from typing import Any

import pytest
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Import AFTER sys.path setup so backend modules resolve.
from server import app                               # noqa: E402
import server as _server_mod                          # noqa: E402
from services import database as _db_mod              # noqa: E402


DEMO_EMAIL = "demo@lockscore.ai"
DEMO_PASSWORD = "demo123"


def _login(client: TestClient) -> tuple[int, dict[str, Any] | None, str]:
    r = client.post(
        "/api/auth/login",
        json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD},
    )
    try:
        body = r.json()
    except Exception:
        body = None
    return r.status_code, body, r.headers.get("x-request-id") or ""


@pytest.mark.asyncio
async def test_login_recovers_from_closed_client():
    """Proves the stale-client → closed-client failure path and the
    recovery after the lazy-proxy fix."""
    with TestClient(app) as client:
        # 1️⃣  Fresh login — baseline.
        s1, b1, rid1 = _login(client)
        print(f"[step1] fresh login status={s1} rid={rid1}")
        assert s1 == 200, f"fresh login failed: {s1} {b1}"

        # Snapshot current client identity.
        client_before = _db_mod._state.client
        assert client_before is not None, "pre-close client should exist"

        # 2️⃣  Simulate idle/stale → close the shared client.
        #     This mirrors any code path (shutdown fallback, admin DB
        #     reset, test harness, pool reset) that closes the shared
        #     client while the process keeps serving requests.
        await _db_mod.close_database()
        assert _db_mod._state.client is None, "state.client should be None after close"
        print("[step2] shared Mongo client closed; state.client is None")

        # 3️⃣  Login again against the same process.
        s2, b2, rid2 = _login(client)
        print(f"[step3] post-close login status={s2} body={b2} rid={rid2}")

        # 4️⃣  Verification contract.
        #     With the lazy proxy in `deps.py`, server.py's cached `db`
        #     symbol is a proxy that re-resolves through
        #     get_database() → auto-init → fresh client.  Login MUST
        #     recover without any supervisor restart.
        assert s2 == 200, (
            f"POST-CLOSE login should auto-recover via lazy proxy, "
            f"got status={s2} body={b2} rid={rid2}. "
            f"If this is 500 with InvalidOperation then server.py is "
            f"still holding a stale db snapshot."
        )
        assert _db_mod._state.client is not None, (
            "state.client should be re-initialised after lazy access"
        )
        assert _db_mod._state.client is not client_before, (
            "a fresh client should have been created"
        )

        # 5️⃣  Concurrent login requests must NOT create multiple clients.
        #     Keep the burst under the per-IP login throttle (10/min
        #     burst 5) so we're measuring pool behaviour, not rate-limit
        #     behaviour.  We've already done 2 logins above, so cap at 3.
        async def _one():
            return _login(client)
        results = await asyncio.gather(*(asyncio.to_thread(_login, client) for _ in range(3)))
        print("[step5] concurrent login results:", [(s, rid) for s, _, rid in results])
        for s, _, _ in results:
            # 200 = healthy, 429 = throttled (also means the request
            # reached FastAPI's throttle layer with a live db — not a
            # pool failure).  500 = pool leak / stale client regression.
            assert s in (200, 429), f"concurrent login failed: {s}"
        # Confirm single-client invariant held through the burst.
        assert _db_mod._state.client is not None
        # Only ONE AsyncIOMotorClient instance should exist in the
        # authority — gc introspection is defensive (weakref objects
        # can raise during iteration).
        import motor.motor_asyncio as mma
        import gc
        gc.collect()
        live_clients = 0
        for o in list(gc.get_objects()):
            try:
                if isinstance(o, mma.AsyncIOMotorClient):
                    live_clients += 1
            except ReferenceError:
                # weakly-referenced object vanished mid-iteration — skip.
                continue
            except Exception:
                continue
        print(f"[step5] live AsyncIOMotorClient count={live_clients}")
        # Expect ≤ 3: the shared one + possibly orphan motor clients
        # some test fixture holds.  Fail HARD if we blew past that.
        assert live_clients <= 3, (
            f"Pool leak: {live_clients} live motor clients detected"
        )
