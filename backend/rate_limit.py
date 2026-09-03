"""Lightweight per-key in-memory rate limiter (SEC-005, 2026-06-25).

Token-bucket style — refills at `rate_per_min` per minute, capped at
`burst` tokens. Used as a FastAPI dependency on heavy endpoints
(parlay optimizer, analytics recompute, login) to keep any single
account/IP from burning paid third-party quota or exhausting CPU.

Process-local (asyncio.Lock + dict). Sufficient for a single-pod
deployment; swap for Redis if we horizontally scale.
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import Annotated, Callable, Optional

from fastapi import Depends, HTTPException, Request, status

from deps import current_user
from auth import UserPublic


class _Bucket:
    __slots__ = ("tokens", "last")

    def __init__(self, tokens: float, last: float) -> None:
        self.tokens = tokens
        self.last = last


_buckets: dict[str, _Bucket] = defaultdict(lambda: _Bucket(0.0, 0.0))
_lock = asyncio.Lock()


async def _consume(key: str, rate_per_min: float, burst: float, cost: float = 1.0) -> tuple[bool, float]:
    """Return (allowed, retry_after_seconds). Atomic refill+consume."""
    now = time.monotonic()
    refill_per_sec = rate_per_min / 60.0
    async with _lock:
        b = _buckets[key]
        if b.last == 0.0:
            b.tokens = burst
        else:
            b.tokens = min(burst, b.tokens + (now - b.last) * refill_per_sec)
        b.last = now
        if b.tokens >= cost:
            b.tokens -= cost
            return True, 0.0
        deficit = cost - b.tokens
        return False, deficit / refill_per_sec if refill_per_sec > 0 else 60.0


def rate_limit(rate_per_min: float, burst: float, scope: str = "user") -> Callable:
    """Build a FastAPI dependency that throttles by user-id (or IP if anon).

    scope: "user" (default) keys by authenticated user id; "ip" keys by
    client IP — use "ip" for /api/auth/login since the caller has no
    user id yet (brute-force protection).
    """
    if scope == "ip":
        async def _ip_dep(request: Request) -> None:
            ip = (request.client.host if request.client else "unknown") or "unknown"
            key = f"ip:{ip}:{rate_per_min}:{burst}"
            ok, retry = await _consume(key, rate_per_min, burst)
            if not ok:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"Too many requests. Try again in {int(retry) + 1}s.",
                    headers={"Retry-After": str(int(retry) + 1)},
                )
        return _ip_dep

    async def _user_dep(
        user: Annotated[UserPublic, Depends(current_user)],
    ) -> UserPublic:
        key = f"user:{user.id}:{rate_per_min}:{burst}"
        ok, retry = await _consume(key, rate_per_min, burst)
        if not ok:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Too many requests. Try again in {int(retry) + 1}s.",
                headers={"Retry-After": str(int(retry) + 1)},
            )
        return user

    return _user_dep


# ─────────────────────────────────────────────────────────────────────
# TEST HELPER — deterministic contract tests
# ─────────────────────────────────────────────────────────────────────
def _reset_for_tests(scope_prefix: str | None = None) -> int:
    """Clear the in-memory token buckets so contract tests do not
    depend on shared live 429 throttle state.

    PERKLOCKS-MAIN 35 · P1-7 — auth throttle cleanup.  Production
    behaviour is unchanged: this helper is a plain function that
    tests call directly.  It is NEVER wired into any FastAPI route
    or middleware and cannot be triggered from a live client.

    ``scope_prefix``:  when supplied, only buckets whose key starts
    with the given prefix are cleared (e.g. ``"ip:"`` to reset the
    login throttle without perturbing per-user compute throttles).
    Passing ``None`` clears every bucket.

    Returns the number of buckets cleared.
    """
    if scope_prefix is None:
        n = len(_buckets)
        _buckets.clear()
        return n
    to_delete = [k for k in _buckets if k.startswith(scope_prefix)]
    for k in to_delete:
        _buckets.pop(k, None)
    return len(to_delete)
