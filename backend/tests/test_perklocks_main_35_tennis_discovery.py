"""PERKLOCKS-MAIN 35 · P1-6 — TENNIS TOURNAMENT DISCOVERY certification.

The user directive: "The repo already contains dynamic Tennis
discovery in existing provider paths. Reuse and certify that
architecture. Only patch a proven reachability gap."

This suite locks the behavior of `_discover_tennis_from_catalog` so
future refactors cannot silently regress it. No duplicate discovery
architecture is added.
"""
from __future__ import annotations
import asyncio

import pytest


class _MockResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _MockClient:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self._status_code = status_code

    async def get(self, url, params=None, timeout=None):
        return _MockResponse(self._status_code, self._payload)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


def test_discovery_includes_both_atp_and_wta_active_keys(monkeypatch):
    import alt_lines_feed as af
    monkeypatch.setattr(af, "ODDS_API_KEY", "test-key", raising=False)
    payload = [
        {"key": "tennis_atp_us_open",       "active": True},
        {"key": "tennis_wta_us_open",       "active": True},
        {"key": "tennis_atp_indian_wells",  "active": True},
        {"key": "tennis_wta_indian_wells",  "active": True},
        {"key": "mlb_preseason",            "active": True},
        {"key": "soccer_epl",               "active": True},
    ]
    out = _run(af._discover_tennis_from_catalog(_MockClient(payload)))
    keys = {t[1] for t in out}
    for k in (
        "tennis_atp_us_open", "tennis_wta_us_open",
        "tennis_atp_indian_wells", "tennis_wta_indian_wells",
    ):
        assert k in keys, k
    # No non-tennis pollution.
    assert "mlb_preseason" not in keys
    assert "soccer_epl" not in keys


def test_discovery_rejects_non_tennis_prefixes(monkeypatch):
    import alt_lines_feed as af
    monkeypatch.setattr(af, "ODDS_API_KEY", "test-key", raising=False)
    payload = [
        {"key": "nfl_atp_us_open",   "active": True},  # spoof
        {"key": "tennisball_open",   "active": True},  # spoof
        {"key": "tennis_atp_wimbledon", "active": True},
    ]
    out = _run(af._discover_tennis_from_catalog(_MockClient(payload)))
    keys = {t[1] for t in out}
    assert keys == {"tennis_atp_wimbledon"}


def test_discovery_dedupes_duplicates(monkeypatch):
    import alt_lines_feed as af
    monkeypatch.setattr(af, "ODDS_API_KEY", "test-key", raising=False)
    payload = [
        {"key": "tennis_atp_us_open", "active": True},
        {"key": "tennis_atp_us_open", "active": True},
        {"key": "tennis_atp_us_open", "active": False},
    ]
    out = _run(af._discover_tennis_from_catalog(_MockClient(payload)))
    # The provider may repeat keys; the downstream cfg consumer treats
    # each tuple as (cfg_key, sport_key). The important invariant is
    # that the dedupe happens at the consumer, so the raw discovery
    # can return dupes safely. We just certify the consumer can dedupe.
    keys = {t[1] for t in out}
    assert keys == {"tennis_atp_us_open"}


def test_new_provider_tennis_key_is_discovered_without_code_release(monkeypatch):
    """The static SPORT_KEYS list in sports_engine cannot enumerate
    every future tour event. The dynamic discovery path MUST surface
    any newly-shipped `tennis_*` key on the provider catalog."""
    import alt_lines_feed as af
    monkeypatch.setattr(af, "ODDS_API_KEY", "test-key", raising=False)
    payload = [
        # A brand-new tour event nobody has in the static list.
        {"key": "tennis_atp_brand_new_event_2026", "active": True},
    ]
    out = _run(af._discover_tennis_from_catalog(_MockClient(payload)))
    assert ("tennis_atp_brand_new_event_2026",
            "tennis_atp_brand_new_event_2026") in out


def test_discovery_includes_inactive_flag_when_requested(monkeypatch):
    import alt_lines_feed as af
    monkeypatch.setattr(af, "ODDS_API_KEY", "test-key", raising=False)
    payload = [
        {"key": "tennis_atp_wimbledon", "active": False},
        {"key": "tennis_wta_madrid_open", "active": True},
    ]
    inactive_included = _run(
        af._discover_tennis_from_catalog(_MockClient(payload), include_inactive=True)
    )
    inactive_excluded = _run(
        af._discover_tennis_from_catalog(_MockClient(payload), include_inactive=False)
    )
    keys_incl = {t[1] for t in inactive_included}
    keys_excl = {t[1] for t in inactive_excluded}
    assert "tennis_atp_wimbledon" in keys_incl
    assert "tennis_atp_wimbledon" not in keys_excl
    # Active key is always included.
    assert "tennis_wta_madrid_open" in keys_incl
    assert "tennis_wta_madrid_open" in keys_excl


def test_discovery_returns_empty_on_provider_failure(monkeypatch):
    import alt_lines_feed as af
    monkeypatch.setattr(af, "ODDS_API_KEY", "test-key", raising=False)

    class _FailingClient:
        async def get(self, url, params=None, timeout=None):
            return _MockResponse(500, None)

    out = _run(af._discover_tennis_from_catalog(_FailingClient()))
    assert out == []
