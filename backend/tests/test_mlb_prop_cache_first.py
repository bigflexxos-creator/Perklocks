"""MLB Prop Cache-First μ-closure — focused regressions.

Scope: verify the acquisition-cache boundary in
``sports_engine._fetch_event_props_payload`` for MLB.
  • Real provider payload → live_alt_lines rows written.
  • Fresh cache → ZERO external provider call, payload reconstructed.
  • Stale/missing cache → provider refresh permitted.
  • Provider-budget block + valid cache → local processing OK.
  • Real-line safety preserved.
  • Canonical publication path untouched.
  • Hitter lineup caps intact (88 / 92 / 99).
"""
import asyncio, os, sys
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, AsyncMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MARK = "mlb_prop_cachefirst"


def _fake_odds_payload(event_id: str,
                        commence: str,
                        market_key: str = "pitcher_strikeouts",
                        selection: str = "Gerrit Cole",
                        line: float = 7.5,
                        price: int = -110) -> dict:
    return {
        "id":            event_id,
        "sport_key":     "baseball_mlb",
        "home_team":     "New York Yankees",
        "away_team":     "Boston Red Sox",
        "commence_time": commence,
        "bookmakers": [{
            "key": "draftkings",
            "markets": [{
                "key": market_key,
                "outcomes": [{
                    "name":        "Over",
                    "description": selection,
                    "point":       line,
                    "price":       price,
                }],
            }],
        }],
    }


def test_1_2_provider_write_and_cache_first():
    """Real provider response writes normalized live_alt_lines rows.
    Second call with fresh cache returns synthesized payload without
    touching the provider."""
    async def _run():
        from motor.motor_asyncio import AsyncIOMotorClient
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
        cx = AsyncIOMotorClient(os.getenv("MONGO_URL"))
        db = cx[os.getenv("DB_NAME", "test_database")]
        # Bind db module-global for sports_engine.
        import server, sports_engine
        original_db = getattr(server, "db", None)
        server.db = db
        try:
            await db.live_alt_lines.delete_many(
                {"event_id": {"$regex": f"^{MARK}_"}})

            ev_k = f"{MARK}_evtK"
            ev_h = f"{MARK}_evtH"
            future = (datetime.now(timezone.utc)
                       + timedelta(hours=6)).isoformat().replace("+00:00","Z")

            payload_K = _fake_odds_payload(
                ev_k, future, "pitcher_strikeouts", "Gerrit Cole", 7.5, -110)
            payload_H = _fake_odds_payload(
                ev_h, future, "batter_hits",       "Aaron Judge",  1.5, +105)

            # Round 1: patch _get so we can observe the network call
            # count and inject the fake payload.
            call_count = {"n": 0}
            async def _mock_get(path, params):
                call_count["n"] += 1
                return payload_K if ev_k in path else payload_H
            with patch.object(sports_engine, "_get",
                              side_effect=_mock_get) as _:
                d1_K = await sports_engine._fetch_event_props_payload(
                    "MLB", "baseball_mlb", ev_k)
                d1_H = await sports_engine._fetch_event_props_payload(
                    "MLB", "baseball_mlb", ev_h)
            # (1) Provider payload written to live_alt_lines.
            n_K = await db.live_alt_lines.count_documents(
                {"event_id": ev_k, "market_key": "pitcher_strikeouts"})
            n_H = await db.live_alt_lines.count_documents(
                {"event_id": ev_h, "market_key": "batter_hits"})
            assert n_K >= 1, "K row not persisted to live_alt_lines"
            assert n_H >= 1, "Hits row not persisted to live_alt_lines"
            round1_calls = call_count["n"]
            assert round1_calls == 2, (
                f"round1 expected 2 provider calls, got {round1_calls}")

            # Round 2: same events, cache is fresh — expect ZERO calls.
            call_count["n"] = 0
            with patch.object(sports_engine, "_get",
                              side_effect=_mock_get):
                d2_K = await sports_engine._fetch_event_props_payload(
                    "MLB", "baseball_mlb", ev_k)
                d2_H = await sports_engine._fetch_event_props_payload(
                    "MLB", "baseball_mlb", ev_h)
            # (3) ZERO external calls on second pass.
            assert call_count["n"] == 0, (
                f"cache-first violated — {call_count['n']} provider calls "
                "on second pass with fresh cache")
            # (4) Cache-first payload contains the same market and outcome.
            assert d2_K.get("_cache_hit") is True
            assert d2_H.get("_cache_hit") is True
            _found_K = False
            for bm in (d2_K.get("bookmakers") or []):
                for mk in (bm.get("markets") or []):
                    if mk.get("key") == "pitcher_strikeouts":
                        for o in (mk.get("outcomes") or []):
                            if o.get("point") == 7.5:
                                _found_K = True
            assert _found_K, "cache-first K payload missing outcome"
        finally:
            await db.live_alt_lines.delete_many(
                {"event_id": {"$regex": f"^{MARK}_"}})
            server.db = original_db
            cx.close()
    asyncio.run(_run())
    print("test_1_2_provider_write_and_cache_first OK")


def test_5_stale_cache_allows_refresh():
    """Stale cached row triggers a provider refresh."""
    async def _run():
        from motor.motor_asyncio import AsyncIOMotorClient
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
        cx = AsyncIOMotorClient(os.getenv("MONGO_URL"))
        db = cx[os.getenv("DB_NAME", "test_database")]
        import server, sports_engine
        original_db = getattr(server, "db", None)
        server.db = db
        try:
            ev = f"{MARK}_stale_evt"
            await db.live_alt_lines.delete_many({"event_id": ev})
            # Seed a stale row (30 min old).
            stale = datetime.now(timezone.utc) - timedelta(minutes=30)
            await db.live_alt_lines.insert_one({
                "event_id":    ev,
                "sport":       "mlb",
                "sportsbook":  "draftkings",
                "market_key":  "pitcher_strikeouts",
                "selection":   "Old",
                "line":        6.5,
                "price":       -120,
                "market_id":   f"{ev}_stale",
                "last_seen":   stale,
                "fetched_at":  stale,
            })
            call_count = {"n": 0}
            payload = _fake_odds_payload(ev,
                (datetime.now(timezone.utc)
                  + timedelta(hours=3)).isoformat().replace("+00:00","Z"))
            async def _mock_get(path, params):
                call_count["n"] += 1
                return payload
            with patch.object(sports_engine, "_get",
                              side_effect=_mock_get):
                d = await sports_engine._fetch_event_props_payload(
                    "MLB", "baseball_mlb", ev)
            # Stale cache: provider must have been called.
            assert call_count["n"] == 1, (
                f"stale cache should allow refresh, got {call_count['n']} calls")
            assert not d.get("_cache_hit")
        finally:
            await db.live_alt_lines.delete_many({"event_id": ev})
            server.db = original_db
            cx.close()
    asyncio.run(_run())
    print("test_5_stale_cache_allows_refresh OK")


def test_6_budget_block_allows_cache_reprocessing():
    """When the provider is blocked (returns None), a valid cached
    line MUST still allow local processing."""
    async def _run():
        from motor.motor_asyncio import AsyncIOMotorClient
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
        cx = AsyncIOMotorClient(os.getenv("MONGO_URL"))
        db = cx[os.getenv("DB_NAME", "test_database")]
        import server, sports_engine
        original_db = getattr(server, "db", None)
        server.db = db
        try:
            ev = f"{MARK}_budget_evt"
            await db.live_alt_lines.delete_many({"event_id": ev})
            fresh = datetime.now(timezone.utc)
            await db.live_alt_lines.insert_one({
                "event_id":     ev,
                "sport":        "mlb",
                "sportsbook":   "draftkings",
                "market_key":   "pitcher_strikeouts",
                "selection":    "Gerrit Cole",
                "line":         7.5,
                "price":        -110,
                "market_id":    f"{ev}_row",
                "home_team":    "Y",
                "away_team":    "R",
                "commence_time": (datetime.now(timezone.utc)
                                   + timedelta(hours=4)).isoformat(),
                "last_seen":    fresh,
                "fetched_at":   fresh,
            })
            # Simulate provider budget block by making _get return None.
            call_count = {"n": 0}
            async def _mock_get(path, params):
                call_count["n"] += 1
                return None
            with patch.object(sports_engine, "_get",
                              side_effect=_mock_get):
                d = await sports_engine._fetch_event_props_payload(
                    "MLB", "baseball_mlb", ev)
            # ZERO provider calls: fresh cache satisfied the request.
            assert call_count["n"] == 0
            assert d.get("_cache_hit") is True
            # And the reconstructed payload carries the cached line.
            outcomes = []
            for bm in (d.get("bookmakers") or []):
                for mk in (bm.get("markets") or []):
                    outcomes.extend(mk.get("outcomes") or [])
            assert any(o.get("point") == 7.5 for o in outcomes)
        finally:
            await db.live_alt_lines.delete_many({"event_id": ev})
            server.db = original_db
            cx.close()
    asyncio.run(_run())
    print("test_6_budget_block_allows_cache_reprocessing OK")


def test_9_canonical_publication_unchanged():
    """Ensure the μ-closure did not introduce a new publication writer
    or bypass PredictionPublicationService."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "sports_engine.py")) as f:
        src = f.read()
    # New writes go ONLY to live_alt_lines (real-line store), never
    # directly to picks or a new collection.
    fn_start = src.find("async def _fetch_event_props_payload")
    fn_end   = src.find("async def _isolate_and_merge_event_props")
    fn = src[fn_start:fn_end]
    assert "live_alt_lines" in fn
    assert "db.picks" not in fn, "canonical writer bypass — picks written from fetcher"
    print("test_9_canonical_publication_unchanged OK")


def test_10_hitter_lineup_caps_intact():
    from services.mlb_gates import data_quality_cap_for_status
    assert data_quality_cap_for_status("unknown")           == 88.0
    assert data_quality_cap_for_status("projected_starter") == 92.0
    assert data_quality_cap_for_status("confirmed_starter") == 99.0
    assert data_quality_cap_for_status("bench")             is None
    assert data_quality_cap_for_status("scratched")         is None
    print("test_10_hitter_lineup_caps_intact OK")


if __name__ == "__main__":
    test_1_2_provider_write_and_cache_first()
    test_5_stale_cache_allows_refresh()
    test_6_budget_block_allows_cache_reprocessing()
    test_9_canonical_publication_unchanged()
    test_10_hitter_lineup_caps_intact()
    print("\nMLB_PROP_CACHE_FIRST_TESTS_ALL_PASSED")
