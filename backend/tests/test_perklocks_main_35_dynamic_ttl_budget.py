"""PERKLOCKS-MAIN 35 — DYNAMIC TIME-TO-EVENT PROVIDER BUDGET.

Locks in the mandate that ``bad_market_registry.mark_bad`` uses an
adaptive TTL keyed to time-to-event, so late-appearing markets
(NFL alt props posted 2-6h before kickoff, MLB late-game hitter
markets that lag the early-slate publication) are re-probed
aggressively as kickoff approaches, without starving the provider
budget by hammering markets on events days out.

Bands (from the ``_adaptive_ttl_hours`` helper):

    < 6h to kickoff        →  1h  TTL   (aggressive re-probe)
    6h ≤ Δ < 24h           →  6h  TTL   (medium)
    ≥ 24h                  → 24h  TTL   (legacy / long)
    already-started (<0)   → 24h  TTL   (won't re-post; keep quiet)

If ``event_commence_time`` is not supplied, TTL falls back to the
legacy 24h so callers that don't yet plumb commence through remain
compatible.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.bad_market_registry import (
    DEFAULT_TTL_HOURS,
    _adaptive_ttl_hours,
)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def test_adaptive_ttl_near_event_short_cadence():
    now = _now_utc()
    # 30 minutes to kickoff → 1h probe cadence
    ttl = _adaptive_ttl_hours(now, now + timedelta(minutes=30))
    assert ttl == 1, (
        f"expected 1h TTL for a 30-min-out event, got {ttl}h. "
        "Late-appearing markets (NFL alt props posted 2-6h pre-game) "
        "would be missed at a slower cadence."
    )
    # 3 hours out → still 1h probe cadence
    assert _adaptive_ttl_hours(now, now + timedelta(hours=3)) == 1


def test_adaptive_ttl_medium_window():
    now = _now_utc()
    # 6h boundary — inclusive of the 6h bucket
    assert _adaptive_ttl_hours(now, now + timedelta(hours=6)) == 6
    # 12h out
    assert _adaptive_ttl_hours(now, now + timedelta(hours=12)) == 6
    # Just under the 24h boundary
    assert _adaptive_ttl_hours(now, now + timedelta(hours=23, minutes=30)) == 6


def test_adaptive_ttl_far_event_conserves_budget():
    now = _now_utc()
    # 3 days out — long TTL, do not spam the provider
    assert _adaptive_ttl_hours(now, now + timedelta(hours=72)) == DEFAULT_TTL_HOURS
    # 5 days out — same
    assert _adaptive_ttl_hours(now, now + timedelta(days=5)) == DEFAULT_TTL_HOURS
    # Boundary at exactly 24h
    assert _adaptive_ttl_hours(now, now + timedelta(hours=24)) == DEFAULT_TTL_HOURS


def test_adaptive_ttl_already_started_uses_default():
    now = _now_utc()
    # Event already started 10 minutes ago
    assert _adaptive_ttl_hours(
        now, now - timedelta(minutes=10)) == DEFAULT_TTL_HOURS
    # Started an hour ago
    assert _adaptive_ttl_hours(
        now, now - timedelta(hours=1)) == DEFAULT_TTL_HOURS


def test_adaptive_ttl_missing_commence_uses_default():
    """Legacy callers that don't pass ``event_commence_time`` still
    receive the historical 24h TTL — no surprise short cadence for
    call-sites that haven't been migrated yet.
    """
    now = _now_utc()
    assert _adaptive_ttl_hours(now, None) == DEFAULT_TTL_HOURS


def test_mark_bad_uses_adaptive_ttl(monkeypatch):
    """Integration-level check: ``mark_bad`` respects
    ``event_commence_time`` and writes the correct ttl_hours to the
    upserted document.  We fake the Mongo update_one to capture the
    payload without needing a live DB.
    """
    import asyncio
    from services import bad_market_registry as bmr

    captured: list[dict] = []

    class _FakeColl:
        async def update_one(self, key_filter, doc, upsert=False):
            captured.append(doc.get("$set", {}))

    class _FakeDb:
        def __getitem__(self, name):
            return _FakeColl()

    async def _drive():
        now = _now_utc()
        # Near event — should write ttl_hours=1
        await bmr.mark_bad(
            _FakeDb(), sport_key="americanfootball_nfl",
            markets=["player_pass_yds_alternate"],
            event_id="evt-near",
            event_commence_time=now + timedelta(minutes=45),
        )
        # Far event — should write ttl_hours=24
        await bmr.mark_bad(
            _FakeDb(), sport_key="americanfootball_nfl",
            markets=["player_pass_yds_alternate"],
            event_id="evt-far",
            event_commence_time=now + timedelta(days=4),
        )

    asyncio.run(_drive())
    assert len(captured) == 2
    assert captured[0].get("ttl_hours") == 1, (
        f"near-event row wrote ttl_hours={captured[0].get('ttl_hours')}, "
        "expected 1h for aggressive re-probe of late-appearing markets"
    )
    assert captured[1].get("ttl_hours") == DEFAULT_TTL_HOURS, (
        f"far-event row wrote ttl_hours={captured[1].get('ttl_hours')}, "
        "expected 24h to conserve provider budget"
    )


def test_alt_lines_feed_plumbs_commence_through():
    """The ``alt_lines_feed._fetch_event_odds`` helper must accept
    ``event_commence_time`` so callers in the refresh loop can
    propagate the parsed commence into the bad-market registry.
    Without this the adaptive TTL is dead code from the acquisition
    perspective.
    """
    import inspect
    import alt_lines_feed
    sig = inspect.signature(alt_lines_feed._fetch_event_odds)
    assert "event_commence_time" in sig.parameters, (
        "_fetch_event_odds missing event_commence_time param — "
        "bad-market registry cannot compute adaptive TTLs."
    )
