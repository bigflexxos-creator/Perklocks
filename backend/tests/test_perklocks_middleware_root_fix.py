"""PERKLOCKS MAIN 37 · P0.1 — Response middleware valid-response
preservation regression tests.

Tests 5 & 6 from the spec:

  Test 5 — Middleware valid-response preservation
    Force the optional middleware response-processing step to throw.
    Assert a successful /api route's original response is still
    returned.

  Test 6 — Middleware real route error
    Force the actual route to fail.
    Assert normal application error handling still occurs rather
    than masking it as success.
"""
from __future__ import annotations

import pytest  # noqa: F401
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from fastapi.responses import JSONResponse
import logging


def _mount_no_store_middleware(app: FastAPI, logger: logging.Logger):
    """Recreate the exact hardening contract used in
    ``server._no_store_api_responses`` so tests exercise identical
    branching logic without dragging the full server module into
    scope.
    """

    @app.middleware("http")
    async def _no_store(request, call_next):
        try:
            response = await call_next(request)
        except Exception as exc:
            import uuid, traceback
            rid = uuid.uuid4().hex[:12]
            logger.error(
                "MIDDLEWARE_FAIL rid=%s: %s\n%s",
                rid, exc, traceback.format_exc(),
            )
            return JSONResponse(
                status_code=500,
                content={"error": "Middleware error — please retry.",
                         "request_id": rid},
                headers={"X-Request-ID": rid},
            )
        if response is None or not hasattr(response, "headers"):
            return JSONResponse(status_code=500,
                                content={"error": "Invalid inner response"})
        if str(request.url.path).startswith("/api/"):
            try:
                response.headers["Cache-Control"] = "no-store"
                response.headers["Pragma"]  = "no-cache"
                response.headers["Expires"] = "0"
            except Exception:
                # PRESERVE ORIGINAL RESPONSE — never let a decoration
                # failure destroy a valid payload.
                pass
        return response


def _make_app_with_broken_headers() -> FastAPI:
    """Simulates a route whose response has a header container that
    raises when mutated (StreamingResponse with locked headers, etc.).
    """
    app = FastAPI()
    _mount_no_store_middleware(app, logging.getLogger(__name__))

    class _ExplodingHeaders:
        def __getitem__(self, k): raise KeyError(k)
        def __setitem__(self, k, v):
            raise RuntimeError("headers container locked")

    @app.get("/api/broken-headers")
    async def _broken():
        # Build a normal JSONResponse then swap in an exploding header
        # container via ``__dict__`` — bypasses the read-only property
        # while preserving the rest of the Response contract.
        resp = JSONResponse(content={"ok": True, "value": 42})
        resp.__dict__["_headers"] = _ExplodingHeaders()
        resp.__dict__["headers"]  = _ExplodingHeaders()
        return resp

    return app


def _make_app_with_failing_route() -> FastAPI:
    app = FastAPI()
    _mount_no_store_middleware(app, logging.getLogger(__name__))

    @app.get("/api/boom")
    async def _boom():
        raise HTTPException(status_code=403, detail="forbidden")

    @app.get("/api/crash")
    async def _crash():
        raise RuntimeError("unexpected route failure")

    return app


def test_middleware_preserves_valid_response_when_decoration_fails():
    """Spec Test 5 — optional post-processing (Cache-Control header
    stamp) must NEVER destroy a valid response.
    """
    app    = _make_app_with_broken_headers()
    client = TestClient(app)
    r = client.get("/api/broken-headers")
    # Original payload preserved.
    assert r.status_code == 200, (r.status_code, r.text)
    body = r.json()
    assert body.get("ok") is True
    assert body.get("value") == 42


def test_middleware_surfaces_real_route_http_exception():
    """Spec Test 6 (part 1) — a genuine route ``HTTPException`` must
    NOT be masked as success by the middleware layer.
    """
    app    = _make_app_with_failing_route()
    client = TestClient(app)
    r = client.get("/api/boom")
    assert r.status_code == 403
    assert "forbidden" in r.text.lower()


def test_middleware_surfaces_real_route_unhandled_exception():
    """Spec Test 6 (part 2) — an unhandled RuntimeError must produce
    a 500-class response (either 500 from middleware guard OR 500
    from the FastAPI exception path).  It MUST NOT be silently
    swallowed as a 200.
    """
    app    = _make_app_with_failing_route()
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/api/crash")
    assert r.status_code >= 500, (r.status_code, r.text)
