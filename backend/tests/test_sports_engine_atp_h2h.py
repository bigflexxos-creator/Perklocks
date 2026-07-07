"""
Regression test for the "ATP h2h moneylines silently disappear" bug.

BUG (fixed 2026-07-07)
----------------------
`_fetch_odds_for` was calling the Odds API bulk `/odds` endpoint with
`markets=h2h,spreads,totals` for every sport. Tennis + UFC have no
native spreads/totals on the bulk endpoint, so the API either 422'd or
returned games with empty `bookmakers[]` — silently dropping the
h2h moneyline picks we needed. The downstream
`_backfill_tennis_moneylines` was a compensating hack; it should now
be a no-op except in rare Odds-API-outage cases.

These tests monkey-patch `_get` to observe the params `_fetch_odds_for`
sends and confirm the per-sport market whitelist is being honoured.

Run: `python -m pytest tests/test_sports_engine_atp_h2h.py -v`
     `python tests/test_sports_engine_atp_h2h.py`
"""
import asyncio
from unittest.mock import patch, AsyncMock

from sports_engine import _fetch_odds_for


def _run(coro):
    return asyncio.run(coro)


def test_tennis_bulk_fetch_requests_h2h_only():
    """Tennis must request markets=h2h only — no spreads/totals."""
    async def _inner():
        with patch("sports_engine._get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = [
                {"id": "atp-1", "home_team": "Alcaraz",
                 "away_team": "Sinner", "bookmakers": []}
            ]
            await _fetch_odds_for("tennis_atp_wimbledon", regions="us",
                                  sport="Tennis")
            assert mock_get.called, "_get should have been called"
            _url, params = mock_get.call_args[0]
            assert params["markets"] == "h2h", (
                f"Tennis should request h2h-only, got: {params['markets']}"
            )
            assert params["regions"] == "us"
    _run(_inner())


def test_ufc_bulk_fetch_requests_h2h_only():
    """UFC (also a 1v1 sport) must request markets=h2h only."""
    async def _inner():
        with patch("sports_engine._get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = []
            await _fetch_odds_for("mma_mixed_martial_arts", regions="us",
                                  sport="UFC")
            assert mock_get.called
            _url, params = mock_get.call_args[0]
            assert params["markets"] == "h2h", (
                f"UFC should request h2h-only, got: {params['markets']}"
            )
    _run(_inner())


def test_team_sports_still_request_all_markets():
    """Non-1v1 team sports keep the full h2h,spreads,totals request so
    NBA totals + NFL spreads still surface in the bulk pull."""
    async def _inner():
        with patch("sports_engine._get", new_callable=AsyncMock) as mock_get:
            for sport in ("MLB", "NBA", "NFL", "Soccer", "CFB", "WNBA"):
                # Return a non-empty result so the defensive retry doesn't fire.
                mock_get.reset_mock()
                mock_get.return_value = [{"id": f"{sport}-1"}]
                await _fetch_odds_for("dummy_key", regions="us", sport=sport)
                _url, params = mock_get.call_args[0]
                assert params["markets"] == "h2h,spreads,totals", (
                    f"{sport} should keep h2h,spreads,totals — got "
                    f"{params['markets']}"
                )
    _run(_inner())


def test_defensive_422_retry_falls_back_to_h2h_only():
    """If a multi-market request returns nothing for a team sport, we
    should retry with h2h-only so the moneyline picks still surface."""
    async def _inner():
        call_log: list[str] = []

        async def fake_get(url, params):
            call_log.append(params["markets"])
            if params["markets"] == "h2h,spreads,totals":
                return None
            return [{"id": "game-1", "home_team": "A", "away_team": "B"}]

        with patch("sports_engine._get", side_effect=fake_get):
            result = await _fetch_odds_for("nba_h2h_test", regions="us", sport="NBA")
            assert call_log == ["h2h,spreads,totals", "h2h"], (
                f"Expected multi→h2h retry, got: {call_log}"
            )
            assert len(result) == 1
    _run(_inner())


def test_tennis_does_not_double_call_on_empty():
    """Tennis already requests h2h-only, so an empty result should NOT
    trigger a redundant second fetch."""
    async def _inner():
        call_count = 0

        async def fake_get(url, params):
            nonlocal call_count
            call_count += 1
            return None

        with patch("sports_engine._get", side_effect=fake_get):
            result = await _fetch_odds_for("tennis_atp_x", regions="us", sport="Tennis")
            assert call_count == 1, (
                f"Tennis empty result should not retry — got {call_count} calls"
            )
            assert result == []
    _run(_inner())


if __name__ == "__main__":
    test_tennis_bulk_fetch_requests_h2h_only()
    test_ufc_bulk_fetch_requests_h2h_only()
    test_team_sports_still_request_all_markets()
    test_defensive_422_retry_falls_back_to_h2h_only()
    test_tennis_does_not_double_call_on_empty()
    print("all sports_engine ATP h2h routing tests passed")
