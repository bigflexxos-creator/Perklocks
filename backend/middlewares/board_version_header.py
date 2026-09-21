"""Universal canonical-freshness header middleware.

Every canonical GET response gets an ``X-Canonical-Version`` header
derived from ``services.board_generation.active()``.  This is the
SINGLE monotonic fingerprint the client uses to detect stale caches
across Preview (web) and Expo Go (native) — SWR + AsyncStorage entries
are tagged with the version they were written under and invalidated
whenever a newer version is observed on any subsequent response.

Cross-process visibility: the ``_active`` dict in ``board_generation``
is per-process.  A maintenance script (different interpreter) that
calls ``commit()`` writes the new fingerprint to Mongo but the web
server's in-memory ``_active`` stays stale until it re-reads.  We
therefore refresh from Mongo at MOST every ``ACTIVE_RESYNC_INTERVAL``
seconds — cheap eventual consistency that closes the cross-process gap
without adding a DB hit to every request.

Scope:
  * All GETs whose path starts with the canonical prefixes list.
  * Never fails the response — a missing/None version simply omits
    the header, matching the pre-fix default behaviour.
"""
from __future__ import annotations

import asyncio
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# Paths that participate in the universal freshness contract.  Any GET
# under these prefixes will carry X-Canonical-Version.
_CANONICAL_PREFIXES: tuple[str, ...] = (
    "/api/picks",
    "/api/rollover",
    "/api/parlay",
    "/api/my-bets",
    "/api/version",
    "/api/lab",
    "/api/analytics",
    "/api/me",
    "/api/pinned",
    "/api/picks/today",
    "/api/historical-intelligence",
)

# Paths under the canonical umbrella that should NOT participate (auth,
# webhooks, admin operations that mutate state).
_CANONICAL_DENY: tuple[str, ...] = (
    "/api/auth/",
    "/api/admin/",
)

# Re-load ``board_generation._active`` from Mongo at most this often.
# 8 s is well under the typical human-scale cache TTL and cheap enough
# that even 100 rps hits Mongo at most 12 times/second.
ACTIVE_RESYNC_INTERVAL = 8.0

_last_resync_at: float = 0.0
_resync_lock: asyncio.Lock | None = None


def _participates(path: str, method: str) -> bool:
    if method.upper() not in ("GET", "HEAD"):
        return False
    for deny in _CANONICAL_DENY:
        if path.startswith(deny):
            return False
    for allow in _CANONICAL_PREFIXES:
        if path.startswith(allow):
            return True
    return False


async def _maybe_resync_active() -> None:
    """Refresh the in-process ``_active`` dict from Mongo if the last
    resync is older than ``ACTIVE_RESYNC_INTERVAL``.  Guarded by a
    module-scope asyncio.Lock so concurrent requests only trigger ONE
    DB read."""
    global _last_resync_at, _resync_lock
    now = time.time()
    if now - _last_resync_at < ACTIVE_RESYNC_INTERVAL:
        return
    if _resync_lock is None:
        _resync_lock = asyncio.Lock()
    async with _resync_lock:
        if now - _last_resync_at < ACTIVE_RESYNC_INTERVAL:
            return  # another coroutine already refreshed
        _last_resync_at = now
        try:
            from services import board_generation
            await board_generation.load_active()
        except Exception:
            # Never break requests on a Mongo hiccup — the previously
            # cached ``_active`` remains valid.
            pass


class BoardVersionHeaderMiddleware(BaseHTTPMiddleware):
    """Stamp ``X-Canonical-Version`` on canonical GET responses.

    Uses a distinct header name from the endpoint-scoped
    ``X-Board-Version`` (which some routes already emit for cursor
    pinning and per-population snapshots).  ``X-Canonical-Version`` is
    the SINGLE globally monotonic fingerprint, sourced from
    ``services.board_generation.active()`` — it advances if-and-only-if
    canonical truth actually changes (rescore / republish).  Client
    freshness observers subscribe to this header exclusively; the
    per-route ``X-Board-Version`` continues to serve its existing
    cursor-pinning role.
    """

    async def dispatch(self, request: Request, call_next):
        # Refresh (best-effort, throttled) BEFORE dispatching so the
        # response header reflects the freshest committed truth even
        # when it was committed by a different interpreter.
        try:
            if _participates(request.url.path, request.method):
                await _maybe_resync_active()
        except Exception:
            pass
        response: Response = await call_next(request)
        try:
            if _participates(request.url.path, request.method):
                from services import board_generation
                active = board_generation.active() or {}
                ver = active.get("board_version") or None
                if ver:
                    response.headers["X-Canonical-Version"] = str(ver)
                gid = active.get("generation_id") or None
                if gid:
                    response.headers["X-Canonical-Generation-Id"] = str(gid)
                # ─── ORDERED integer revision (2026-06-21 v2) ──────
                # ``board_generation._active["revision"]`` is a monotone
                # non-decreasing integer that advances by +1 on every
                # real (non-no-op) commit.  This is the ONE authoritative
                # ordering signal the client uses to distinguish a
                # LATE stale response from a genuine advance — opaque
                # hashes cannot answer that question.
                rev = active.get("revision")
                if rev is not None:
                    response.headers["X-Canonical-Revision"] = str(int(rev))
        except Exception:
            pass
        return response


__all__ = ["BoardVersionHeaderMiddleware"]
