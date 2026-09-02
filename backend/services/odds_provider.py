"""Odds provider fallback layer (iter-93, temporary until The Odds API renews).

Provides a single facade `odds_health.status()` + `odds_health.decorate_pick(p)`
that the picks endpoint uses to tag every pick with:

    odds_source       : "odds_api" | "api_sports" | "espn" | "unavailable"
    odds_status       : "live" | "backup" | "missing"
    confidence_penalty: 0 for live, -10 for backup / missing

Behavior contract with the rest of the app
------------------------------------------
- The prediction engines still run (stats, injuries, matchup, lineups).
- When the primary Odds API is unreachable / quota-exhausted, `edge_percent`
  is set to `None` on new picks so nothing computes a false betting edge.
- lock_score is docked by 10 pts (soft-cap 0..99) so backup picks visually
  land in the Lean tier instead of the Elite Lock tier.
- No synthetic odds are generated — ever.

Health detection
----------------
Simple in-process circuit breaker keyed off two signals:

  1. Explicit runtime failures posted by callers via `odds_health.report_failure()`
     when The Odds API returns 401 / 403 / 429 / 5xx.
  2. A cheap 10-minute background probe (`_probe_primary`) — one GET to the
     `sports` metadata endpoint. If it succeeds, `_state = "live"`.

Env-driven config (Monday's revert = just flip `ODDS_PRIMARY_PROVIDER`)
    ODDS_PRIMARY_PROVIDER  = "odds_api" | "api_sports" | "espn"
    API_SPORTS_KEY_1/2/3   = rotated pool (429 → advance to next key)
    THE_ODDS_API_KEY       = untouched
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

import httpx

logger = logging.getLogger("lockscore.odds_provider")

_ODDS_API_KEY = os.getenv("THE_ODDS_API_KEY") or ""
# 2026-08-23 Pass 1B — unify PRIMARY_ODDS_PROVIDER + ODDS_PRIMARY_PROVIDER
# into ONE effective config truth so downstream code can read either
# name and get the same value.  Precedence: ODDS_PRIMARY_PROVIDER wins
# (existing production key), fallback to PRIMARY_ODDS_PROVIDER, then
# default 'odds_api'.
_PRIMARY = (
    os.getenv("ODDS_PRIMARY_PROVIDER")
    or os.getenv("PRIMARY_ODDS_PROVIDER")
    or "odds_api"
).strip().lower()
# Accept BOTH the rotated-pool names AND the single `APISPORTS_KEY` that is
# what actually ships in this app's secrets. Previously only the _1/_2/_3
# names were read, so on a real deployment `_API_SPORTS_KEYS` was always
# empty and the breaker could never fall back to api_sports — it went
# straight to "espn"/"unavailable", stripping edge_percent and docking
# lock_score on every pick instead of serving backup odds.
_API_SPORTS_KEYS = list(dict.fromkeys(
    k.strip() for k in (
        os.getenv("API_SPORTS_KEY_1"),
        os.getenv("API_SPORTS_KEY_2"),
        os.getenv("API_SPORTS_KEY_3"),
        os.getenv("APISPORTS_KEY"),
    ) if k and k.strip()
))

_HEALTH_LOCK = asyncio.Lock()
_FAIL_WINDOW_SECONDS = 300         # 5-minute rolling window
_FAIL_THRESHOLD = 3                # 3 failures in the window → degrade
_RECHECK_INTERVAL = 30 * 60        # retry primary every 30 min while degraded
_PROBE_URL = f"https://api.the-odds-api.com/v4/sports/?apiKey={_ODDS_API_KEY}"

# Runtime state.
_state: str = "live"                # "live" | "degraded"
_failures: list[float] = []         # UNIX timestamps of recent 401/403/429/5xx
_last_probe_ts: float = 0.0
_active_source: str = "odds_api"    # tracks which provider served the last odds

# Why the primary is unhealthy, so /api/admin/odds-health and the client can
# show a real reason instead of a silent empty board. "key_deactivated" is
# terminal until someone renews the subscription — no amount of retrying
# fixes it, and it must NOT be reported as a rate limit.
_last_failure_reason: Optional[str] = None
_KEY_DEAD_REASONS = frozenset({"key_deactivated", "key_invalid", "no_odds_api_key"})


def get_active_source() -> str:
    """Which provider is currently serving live odds."""
    return _active_source


def get_state() -> str:
    return _state


def report_failure(status_code: Optional[int], detail: str = "") -> None:
    """Callers hitting The Odds API should invoke this on any hard failure
    (401 / 403 / 429 / 5xx / timeout). Enough failures in the window and we
    flip the circuit breaker to `degraded` so the pick decorator switches.

    An auth failure (401/403) trips the breaker IMMEDIATELY — a deactivated
    or revoked key is not a transient condition, so waiting for
    `_FAIL_THRESHOLD` failures just burns 5 more minutes of empty boards.
    """
    global _state, _active_source, _last_failure_reason
    now = time.time()
    if detail:
        _last_failure_reason = detail
    _failures.append(now)
    # Drop entries outside the rolling window.
    cutoff = now - _FAIL_WINDOW_SECONDS
    while _failures and _failures[0] < cutoff:
        _failures.pop(0)
    # Auth failures are terminal, not transient — degrade on the first one.
    _auth_dead = status_code in (401, 403) or detail in _KEY_DEAD_REASONS
    if _auth_dead and _state != "degraded":
        _state = "degraded"
        _active_source = "api_sports" if _API_SPORTS_KEYS else "espn"
        logger.error(
            "The Odds API key REJECTED (status=%s reason=%s) — this is an "
            "account/subscription problem, NOT a rate limit. Renew the key "
            "or set THE_ODDS_API_KEY. Falling back to %s; picks will carry "
            "edge_percent=None until the primary recovers.",
            status_code, detail or "auth_rejected", _active_source,
        )
        return
    if len(_failures) >= _FAIL_THRESHOLD and _state != "degraded":
        _state = "degraded"
        _active_source = "api_sports" if _API_SPORTS_KEYS else "espn"
        logger.warning(
            "Odds primary degraded — %d failures in last %ds (%s). "
            "Falling back to %s.",
            len(_failures), _FAIL_WINDOW_SECONDS, detail or status_code,
            _active_source,
        )


def report_success() -> None:
    """Successful primary call — clear degraded state if we were degraded."""
    global _state, _active_source, _last_failure_reason
    if _state == "degraded":
        logger.info("Odds primary recovered — clearing degraded flag.")
    _state = "live"
    _active_source = "odds_api"
    _last_failure_reason = None
    _failures.clear()


async def _probe_primary() -> bool:
    """Cheap health check against The Odds API `/sports` endpoint.

    Returns True on 200, False otherwise. Cached-throttled so we don't
    hammer the API — one probe per _RECHECK_INTERVAL.
    """
    global _last_probe_ts
    now = time.time()
    if now - _last_probe_ts < _RECHECK_INTERVAL:
        return _state == "live"
    _last_probe_ts = now
    if not _ODDS_API_KEY:
        report_failure(None, "no_odds_api_key")
        return False
    try:
        async with httpx.AsyncClient(timeout=8.0) as cx:
            r = await cx.get(_PROBE_URL)
            if r.status_code == 200:
                report_success()
                return True
            # Surface the provider's own error_code. The Odds API answers a
            # lapsed/cancelled subscription with 401 + DEACTIVATED_KEY; that
            # must be reported as an account problem, not "probe_bad_status",
            # or the real cause stays invisible in the logs.
            reason = "probe_bad_status"
            try:
                body = r.json()
                code = str(body.get("error_code") or "").upper()
                if code == "DEACTIVATED_KEY":
                    reason = "key_deactivated"
                elif code in ("INVALID_KEY", "MISSING_KEY"):
                    reason = "key_invalid"
                elif code:
                    reason = f"probe:{code.lower()}"
            except Exception:
                pass
            report_failure(r.status_code, reason)
            return False
    except Exception as e:
        report_failure(None, f"probe_exc:{e.__class__.__name__}")
        return False


async def status() -> dict:
    """Public status snapshot — used by /api/admin/odds-health and by the
    picks endpoint to decide the tag on outbound picks.

    Runs the cheap probe if we're due; returns the current active source.
    """
    async with _HEALTH_LOCK:
        # Force a probe if we're degraded (attempt recovery).
        if _state == "degraded":
            await _probe_primary()
        # For live state: probe at the interval too, so we detect a fresh
        # 401 quickly without waiting for the first pick-write path to fail.
        elif time.time() - _last_probe_ts > _RECHECK_INTERVAL:
            await _probe_primary()

    return {
        "state": _state,
        "active_source": _active_source,
        "primary_provider": _PRIMARY,
        "api_sports_keys_configured": len(_API_SPORTS_KEYS),
        "failures_in_window": len(_failures),
        "last_probe_age_seconds": int(time.time() - _last_probe_ts) if _last_probe_ts else None,
        # Actionable reason + operator-facing message, so a dead subscription
        # reads as a dead subscription rather than an empty props board.
        "failure_reason": _last_failure_reason,
        "key_status": ("dead" if _last_failure_reason in _KEY_DEAD_REASONS
                       else "ok" if _state == "live" else "unknown"),
        "operator_message": (
            "The Odds API key is deactivated or invalid — renew the "
            "subscription and update THE_ODDS_API_KEY. Player-prop markets "
            "cannot be fetched until then."
            if _last_failure_reason in _KEY_DEAD_REASONS else None
        ),
    }


def decorate_pick(pick: dict) -> dict:
    """Add the standard odds-source / odds-status / confidence-penalty
    fields to a pick. Called by picks_routes.py at the last step before
    the response goes out.

    Rules:
      • Primary live → odds_source=odds_api, odds_status=live, penalty=0.
      • Degraded + API-Sports available → odds_source=api_sports,
        odds_status=backup, penalty=-10. edge_percent set to None
        (we don't have parity odds to compute a true edge).
      • Degraded + no API-Sports → odds_source=espn, odds_status=missing,
        penalty=-10. edge_percent set to None; lock_score docked 10.
      • Explicit ODDS_PRIMARY_PROVIDER override wins over the auto state.
    """
    active = _PRIMARY if _PRIMARY in ("api_sports", "espn") else _active_source
    if active == "odds_api":
        pick["odds_source"] = "odds_api"
        pick["odds_status"] = "live"
        pick["confidence_penalty"] = 0
        return pick
    if active == "api_sports":
        pick["odds_source"] = "api_sports"
        pick["odds_status"] = "backup"
        pick["confidence_penalty"] = -10
        # NO edge without real Odds API parity — user spec: "Do NOT
        # calculate true betting edge without real sportsbook odds".
        pick["edge_percent"] = None
        # ── Phase 2 (Lock Score Authority) — PROVIDER-FALLBACK LOCK
        # SCORE DOCK RETIRED.  Docking canonical Lock Score by 10
        # because the odds provider is on a backup source is an
        # unjustified provider-fallback penalty (the pick's underlying
        # evidence and scoring components are unchanged).  Backup
        # provenance is preserved via ``odds_source`` / ``odds_status``
        # / ``confidence_penalty`` — those remain the signals downstream
        # UI can consume (presentation-only).  The canonical Lock
        # Score is authoritative and no longer mutated at read time.
        return pick
    # ESPN or unavailable
    pick["odds_source"] = "espn" if active == "espn" else "unavailable"
    pick["odds_status"] = "missing"
    pick["confidence_penalty"] = -10
    pick["edge_percent"] = None
    # ── Phase 2 (Lock Score Authority) — provider-fallback dock
    # RETIRED here too (same rationale as above).  ``odds_status``
    # remains the presentation signal.
    return pick


__all__ = [
    "status", "decorate_pick",
    "report_failure", "report_success",
    "get_active_source", "get_state",
]
