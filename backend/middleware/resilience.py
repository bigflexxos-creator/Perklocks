"""
Resilience middleware — hardens the FastAPI surface against the Cloudflare
520 "origin web server sent a response that Cloudflare could not parse"
class of failures.

User report (2026-06-28): PerkLocks login and picks are intermittently
failing with Cloudflare 520. We can't change the supervisor command
(it runs `uvicorn --reload --workers 1` and is marked READONLY), so
every backend file edit briefly drops in-flight requests. This module
ensures:

  1. Every /api/* response is well-formed JSON with explicit
     Content-Type: application/json (no HTML, no empty body).
  2. Every uncaught exception is logged with full traceback + request
     context, then converted to a clean JSON 500 (instead of letting
     uvicorn fall back to a half-formed response that CF can't parse).
  3. Every request gets a generated X-Request-ID echoed back so the
     mobile client can correlate logs.
  4. Structured access log per request (status, body size, elapsed_ms,
     request_id, route, exception flag) — emitted at INFO so they
     surface in `supervisorctl tail backend stdout`.
  5. Wall-clock timeout (default 85s, just under Cloudflare's 100s
     edge timeout) — long-running picks aggregations get a clean 504
     JSON response instead of a CF 504/520.
  6. Duplicate-header dedupe (last value wins) — protects against
     middleware chains that accidentally set Cache-Control twice.

Wiring: import + `install(app)` from `server.py` after the FastAPI
instance is constructed. Must be the OUTERMOST middleware so it sees
ALL exceptions.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import traceback
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger("lockscore.resilience")

# CF Free/Pro edge timeout is 100s. Stay safely under it so we emit a
# clean JSON 504 ourselves instead of CF returning 524/520.
REQUEST_TIMEOUT_SECONDS = 85.0

# ─────────────────────────────────────────────────────────────────────
# Route-specific timeout overrides.
#
# Context (2026-10-06 Emergent Support finding):
#   Legitimate ``POST /api/admin/canonical-import`` batch writes against
#   Production routinely take 100–181 s (large upsert batches with
#   ``w:majority`` under event-loop-starvation-free conditions).  The
#   global 85 s shield originally sized for Cloudflare's 100 s edge
#   budget is below this envelope and was returning spurious 504s to
#   the R3 push driver, forcing retries that compounded load.
#
#   Fix: raise the budget to 300 s ONLY for this exact route.  Every
#   other /api/* route keeps the 85 s CF-safe default so the Cloudflare
#   520 shield stays intact for user-facing endpoints.
#
# Keying: (upper-case method, exact FastAPI path).  Lookup is O(1); on
# miss we fall through to ``REQUEST_TIMEOUT_SECONDS``.
# ─────────────────────────────────────────────────────────────────────
_ROUTE_TIMEOUT_OVERRIDES: dict[tuple[str, str], float] = {
    ("POST", "/api/admin/canonical-import"):              300.0,
    # Scoped duplicate-key check — large canonical collections require
    # a full $group aggregation when the target unique index is not yet
    # present.  85 s is insufficient (observed 504 at ~85.2 s on
    # Perklocks R3 Production 2026-10-06).  300 s matches the write-side
    # override and is still well under any ingress / Cloudflare upper
    # bound for admin-authenticated maintenance endpoints.
    ("GET",  "/api/admin/canonical-cutover/dup-check"):   300.0,
}


def _timeout_for(method: str, path: str) -> float:
    """Return the per-request wall-clock timeout in seconds.

    Exact-match lookup against ``_ROUTE_TIMEOUT_OVERRIDES`` first;
    otherwise returns the global default.  Keeping it a plain function
    (not a decorator) means the override map can be audited / unit-
    tested in isolation and no route needs to opt in explicitly.
    """
    return _ROUTE_TIMEOUT_OVERRIDES.get((method.upper(), path),
                                          REQUEST_TIMEOUT_SECONDS)


class ResilienceMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        rid = uuid.uuid4().hex[:12]
        request.state.request_id = rid
        path = str(request.url.path)
        method = request.method
        start = time.perf_counter()
        is_api = path.startswith("/api/")

        route_timeout = _timeout_for(method, path)

        try:
            response: Response = await asyncio.wait_for(
                call_next(request),
                timeout=route_timeout,
            )
        except asyncio.TimeoutError:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            logger.error(
                "REQUEST_TIMEOUT method=%s path=%s rid=%s "
                "elapsed_ms=%.0f budget_s=%.1f",
                method, path, rid, elapsed_ms, route_timeout,
            )
            return JSONResponse(
                status_code=504,
                content={
                    "success": False,
                    "error": "gateway_timeout",
                    "request_id": rid,
                },
                headers={"X-Request-ID": rid, "Cache-Control": "no-store"},
            )
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            tb = traceback.format_exc()
            logger.error(
                "UNHANDLED method=%s path=%s rid=%s elapsed_ms=%.0f exc=%s\n%s",
                method, path, rid, elapsed_ms, exc, tb,
            )
            return JSONResponse(
                status_code=500,
                content={
                    "success": False,
                    "error": "internal_server_error",
                    "request_id": rid,
                },
                headers={"X-Request-ID": rid, "Cache-Control": "no-store"},
            )

        elapsed_ms = (time.perf_counter() - start) * 1000.0

        # Always tag the response with the request id so mobile clients
        # can include it in bug reports.
        response.headers["X-Request-ID"] = rid

        # Best-effort body-size accounting. Streaming responses don't
        # expose .body; we fall back to a hint via Content-Length.
        try:
            body_bytes = getattr(response, "body", None)
            body_size = len(body_bytes) if isinstance(body_bytes, (bytes, bytearray)) else None
            if body_size is None:
                cl = response.headers.get("content-length")
                body_size = int(cl) if cl and cl.isdigit() else -1
        except Exception:
            body_size = -1

        # Structured access log
        log_fn = logger.info
        if response.status_code >= 500:
            log_fn = logger.error
        elif response.status_code >= 400:
            log_fn = logger.warning
        log_fn(
            "ACCESS method=%s path=%s status=%d size=%d ms=%.0f rid=%s",
            method, path, response.status_code, body_size, elapsed_ms, rid,
        )

        # CRITICAL: every /api response MUST be application/json with a
        # non-empty body. If a handler accidentally returned a bare
        # `Response()` or HTML (e.g. an exception page from a
        # dependency), CF sees that as malformed and serves 520. Coerce
        # to a safe JSON envelope.
        #
        # Exception: the temporary ``/api/_migration_download/{filename}``
        # route intentionally streams ``application/gzip`` for one-time
        # SHA-verified checkpoint transfer during the R3 cutover.  The
        # bypass is removed when the download route is removed.
        if is_api and not path.startswith("/api/_migration_download/"):
            ct = (response.headers.get("content-type") or "").lower()
            needs_coerce = False
            # Streaming/file responses are fine — only check normal Responses.
            if isinstance(body_size, int) and body_size == 0:
                needs_coerce = True
            elif "application/json" not in ct and "text/event-stream" not in ct:
                # Non-JSON, non-SSE response on /api/* → coerce
                needs_coerce = True

            if needs_coerce:
                logger.warning(
                    "COERCE_TO_JSON method=%s path=%s status=%d ct=%r size=%s rid=%s",
                    method, path, response.status_code, ct, body_size, rid,
                )
                return JSONResponse(
                    status_code=response.status_code or 500,
                    content={
                        "success": False,
                        "error": "malformed_response",
                        "request_id": rid,
                    },
                    headers={"X-Request-ID": rid, "Cache-Control": "no-store"},
                )

        return response


def install_exception_handlers(app: FastAPI) -> None:
    """Register FastAPI-level handlers as a belt-and-suspenders fallback
    in case Starlette routes around the middleware (unusual but possible
    for sub-app mounts).
    """
    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception):
        rid = getattr(request.state, "request_id", uuid.uuid4().hex[:12])
        logger.error(
            "FASTAPI_UNHANDLED method=%s path=%s rid=%s exc=%s\n%s",
            request.method, request.url.path, rid, exc, traceback.format_exc(),
        )
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "internal_server_error",
                "request_id": rid,
            },
            headers={"X-Request-ID": rid, "Cache-Control": "no-store"},
        )


def install(app: FastAPI) -> None:
    """One-call wiring used by server.py."""
    app.add_middleware(ResilienceMiddleware)
    install_exception_handlers(app)
    overrides_summary = ", ".join(
        f"{m} {p}={s:.0f}s"
        for (m, p), s in sorted(_ROUTE_TIMEOUT_OVERRIDES.items())
    ) or "(none)"
    logger.info(
        "Resilience middleware installed (default_timeout=%.0fs, "
        "route_overrides=[%s], JSON-coerce on /api/*)",
        REQUEST_TIMEOUT_SECONDS, overrides_summary,
    )
