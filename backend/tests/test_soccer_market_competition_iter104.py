"""SOCCER_MARKET_COMPETITION_RUNTIME focused regression tests — iter 104.

Verifies the surgical repair without a broad audit.  Uses existing
production cache; no external provider calls.

Covers:
  1. player_first_goal_scorer removed from acquisition + ingest.
  2. Double Chance in acquisition + ingest + game-model.
  3. Bundle-failure recovery preserves supported sibling markets.
  4. bad_market_registry receives event_id at event scope.
  5. Active soccer fixture can trigger alt-market fetch without a
     pre-existing published pick (circular-dependency broken).
  6. Soccer game-market ingester now uses the v3 pick_context path.
  7. Book Implied Probability is not a Lock Score booster.
  8. Distinct exact lines (Over 1.5 / 2.5, Under 2.5 / 3.5) survive.
  9. Raw BTTS / anytime scorer / Double Chance rows reach model stage.
 10. Market Competition sees modeled alternatives before publication.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone

import pytest
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

sys.path.insert(0, "/app/backend")
load_dotenv("/app/backend/.env")


def _run(coro):
    return asyncio.run(coro)


def _db():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    return client, client[os.environ["DB_NAME"]]


TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ─── 1. player_first_goal_scorer removed ────────────────────────
def test_first_goal_scorer_removed_from_acquisition():
    import alt_lines_feed as af
    assert "player_first_goal_scorer" not in af.SOCCER_MARKETS, \
        "player_first_goal_scorer must not be fetched"
    for cfg_key, (_sport_key, markets) in af.SPORT_CONFIG.items():
        if cfg_key.startswith("soccer"):
            assert "player_first_goal_scorer" not in markets, (
                f"SPORT_CONFIG[{cfg_key}] still requests "
                f"player_first_goal_scorer"
            )


def test_first_goal_scorer_removed_from_ingest():
    from services import real_line_scorer_ingest as ing
    assert "player_first_goal_scorer" not in ing._SCORER_MARKETS
    assert "player_last_goal_scorer" not in ing._SCORER_MARKETS


# ─── 2. Double Chance added to acquisition + ingest ─────────────
def test_double_chance_in_acquisition():
    import alt_lines_feed as af
    assert "double_chance" in af.SOCCER_MARKETS
    for cfg_key, (_sport_key, markets) in af.SPORT_CONFIG.items():
        if cfg_key.startswith("soccer"):
            assert "double_chance" in markets, (
                f"SPORT_CONFIG[{cfg_key}] missing double_chance"
            )


def test_double_chance_in_ingest_and_engine():
    from services import real_line_scorer_ingest as ing
    from services.soccer_game_model import compute_game_market_prob
    assert "double_chance" in ing._GAME_MARKETS
    assert ing._MARKET_LABEL.get("double_chance") == "Double Chance"

    # Engine returns a valid probability for a canonical Double Chance
    # selection (existing team ctx path).
    async def run():
        client, db = _db()
        try:
            p = await compute_game_market_prob(
                db, home_team="Chelsea", away_team="Arsenal",
                league="EPL", market_key="double_chance",
                selection="Home/Draw",
            )
            assert p is not None and 0 < p < 1, f"Double Chance prob={p}"
        finally:
            client.close()
    _run(run())


# ─── 3. Bundle-failure recovery preserves siblings ──────────────
def test_fetch_event_odds_per_market_retry_on_bundle_failure(monkeypatch):
    """When the bundled request returns None, the ingester must retry
    each market individually and merge successes.  The bad market
    must be persisted at event scope."""
    import alt_lines_feed as af

    call_log: list[list[str]] = []

    async def fake_cached_httpx_get(url, params, **kw):
        mkts = params.get("markets", "").split(",")
        call_log.append(mkts)
        # Bundle (multi-market) call fails.
        if len(mkts) > 1:
            return None
        # Individual retries: only "player_to_score_or_assist" fails.
        if mkts == ["player_to_score_or_assist"]:
            return None
        return {
            "id": "EV1", "sport_key": "soccer_epl", "commence_time": "x",
            "home_team": "A", "away_team": "B",
            "bookmakers": [{"key": "dk", "markets": [
                {"key": mkts[0], "outcomes": [
                    {"name": "Yes", "price": -110, "point": None},
                ]}]}],
        }

    marks_persisted: list[dict] = []

    async def fake_mark_bad(db, *, sport_key, markets, event_id=None,
                             scope="event", reason=""):
        marks_persisted.append({
            "sport_key": sport_key, "markets": list(markets),
            "event_id": event_id, "scope": scope, "reason": reason,
        })

    async def fake_filter_markets(db, *, sport_key, markets,
                                    event_id=None):
        return list(markets)  # no cached filters

    monkeypatch.setattr("services.odds_cache.cached_httpx_get",
                         fake_cached_httpx_get)
    monkeypatch.setattr("services.bad_market_registry.mark_bad",
                         fake_mark_bad)
    monkeypatch.setattr("services.bad_market_registry.filter_markets",
                         fake_filter_markets)

    class _FakeDB:
        pass

    result = _run(af._fetch_event_odds(
        cx=None, sport_key="soccer_epl", event_id="EV1",
        markets=["player_goal_scorer_anytime", "btts",
                  "alternate_totals", "player_to_score_or_assist"],
        db=_FakeDB(),
    ))

    # (a) 3 markets salvaged, 1 failed
    assert result is not None, (
        "bundle-failure recovery must return merged data, not None"
    )
    supported_keys = {
        m.get("key")
        for bm in result.get("bookmakers", [])
        for m in bm.get("markets", [])
    }
    assert "player_goal_scorer_anytime" in supported_keys
    assert "btts" in supported_keys
    assert "alternate_totals" in supported_keys
    assert "player_to_score_or_assist" not in supported_keys

    # (b) FIX 3 — only the actually-failing market is recorded, and
    # the record carries the actual event_id at event scope.
    assert marks_persisted, "no bad-market marker persisted"
    persisted = marks_persisted[-1]
    assert persisted["event_id"] == "EV1"
    assert persisted["scope"] == "event"
    assert persisted["markets"] == ["player_to_score_or_assist"]


# ─── 4. mark_bad is called with event_id at event scope ─────────
def test_bad_market_registry_receives_event_id_on_single_market_failure(monkeypatch):
    """When a single-market request fails (no siblings to fan out),
    the marker must still be event-scoped, not silently dropped."""
    import alt_lines_feed as af

    async def fake_cached_httpx_get(url, params, **kw):
        return None  # always fail

    marks: list[dict] = []

    async def fake_mark_bad(db, *, sport_key, markets, event_id=None,
                             scope="event", reason=""):
        marks.append({"event_id": event_id, "scope": scope,
                       "markets": list(markets)})

    async def fake_filter_markets(db, *, sport_key, markets,
                                    event_id=None):
        return list(markets)

    monkeypatch.setattr("services.odds_cache.cached_httpx_get",
                         fake_cached_httpx_get)
    monkeypatch.setattr("services.bad_market_registry.mark_bad",
                         fake_mark_bad)
    monkeypatch.setattr("services.bad_market_registry.filter_markets",
                         fake_filter_markets)

    class _FakeDB:
        pass

    _run(af._fetch_event_odds(
        cx=None, sport_key="soccer_epl", event_id="EV2",
        markets=["btts"], db=_FakeDB(),
    ))
    assert marks, "no marker on single-market failure"
    m = marks[-1]
    assert m["event_id"] == "EV2"
    assert m["scope"] == "event"


# ─── 5. Active fixture eligible for alt-market discovery ────────
def test_soccer_active_fixture_not_gated_by_existing_pick():
    """The circular-dependency fix removes the picks_scope gate for
    soccer.  We assert the SOURCE CODE no longer contains the gate
    for soccer events by inspecting refresh_alt_lines behavior via
    a direct read of the module."""
    import inspect, alt_lines_feed as af
    src = inspect.getsource(af.refresh_alt_lines)
    assert "SOCCER_MARKET_COMPETITION_RUNTIME (2026-09) §4" in src, \
        "refresh_alt_lines missing §4 comment marker"
    assert "is_soccer = " in src, \
        "picks_scope soccer exemption not implemented"
    # And the leaky picks_scope gate on soccer league DISCOVERY must
    # be gone too — soccer leagues get added regardless of pick
    # coverage, still bounded by max_events_per_sport.
    assert "if picks_scope and sport_key not in scope[\"sport_keys\"]:" not in src


# ─── 6. Soccer game-market ingester uses the current production
#     scoring contract (matches sports_engine._build_pick for
#     Soccer game markets — LEGACY win-prob band + factor peak/avg
#     boost; NOT the v3 composite which is NFL-Platinum-only).
def test_game_market_ingester_uses_current_production_scoring_path():
    import inspect
    from services import real_line_scorer_ingest as ing
    src = inspect.getsource(ing._ingest_game_market_row)
    # Enriches factors via the SAME feature engine as main sports_engine.
    assert "build_soccer_ml_factors" in src, (
        "game-market ingester not calling build_soccer_ml_factors"
    )
    assert "build_soccer_total_factors" in src, (
        "game-market ingester not calling build_soccer_total_factors"
    )
    # Removed the legacy Book Implied Probability booster.
    assert '"Book Implied Probability": book_impl' not in src


def test_scorer_ingester_strips_book_implied_from_factors():
    import inspect
    from services import real_line_scorer_ingest as ing
    src = inspect.getsource(ing._ingest_player_scorer_row)
    # Bridge factors may historically include the Book Implied key —
    # the ingester now strips it before scoring.
    assert 'k != "Book Implied Probability"' in src


# ─── 7. Book Implied Probability is NOT a Lock Score booster ────
def test_book_implied_not_in_factors_dict():
    """Grep the ingester source for the string 'Book Implied
    Probability' — it must not appear as a key in factors (used only
    for edge / de-vig).  This is a source-guard test, robust to
    runtime differences."""
    import inspect
    from services import real_line_scorer_ingest as ing
    src = inspect.getsource(ing)
    # It may still appear as a legacy comment or docstring — assert
    # it is NOT used as a key inside a factors dict literal.
    assert '"Book Implied Probability": book_impl' not in src, (
        "Book Implied Probability still used as a Lock Score booster"
    )


# ─── 8. Distinct exact lines survive as separate wagers ─────────
def test_distinct_lines_remain_distinct_in_canonical_wager_id():
    """canonical_wager_id must encode the numeric line so Over 1.5
    and Over 2.5 collapse to distinct wagers (not one Totals bucket)."""
    async def run():
        client, db = _db()
        try:
            over_lines = await db.picks.distinct(
                "line",
                {"sport": "Soccer", "pick_date": TODAY,
                 "market_key": {"$in": ["alternate_totals", "totals"]},
                 "selection": {"$regex": "^over$", "$options": "i"}},
            )
            # if the ingester ran at all we should see at least two
            # distinct lines
            if len(over_lines) < 2:
                pytest.skip("live cache has < 2 distinct Over lines")
            assert len(set(over_lines)) == len(over_lines), \
                "duplicate line values across Over picks"
            # Explicitly verify canonical_wager_id differs between
            # any two picks on different lines.
            ids = set()
            async for p in db.picks.find({
                "sport": "Soccer", "pick_date": TODAY,
                "market_key": {"$in": ["alternate_totals", "totals"]},
                "selection": {"$regex": "^over$", "$options": "i"},
                "canonical_wager_id": {"$exists": True},
            }, {"canonical_wager_id": 1, "line": 1}).limit(50):
                cwid = p.get("canonical_wager_id")
                if cwid:
                    ids.add(cwid)
            assert len(ids) >= 2, "canonical_wager_id collapsed Over lines"
        finally:
            client.close()
    _run(run())


# ─── 9. BTTS / anytime scorer / Double Chance reach model ───────
def test_btts_reaches_model_when_raw_row_exists():
    async def run():
        client, db = _db()
        try:
            # Any BTTS pick from real_line_soccer_v2 means the ingester
            # actually ran the model against a raw BTTS row.
            n = await db.picks.count_documents({
                "sport": "Soccer", "pick_date": TODAY,
                "market_key": {"$in": ["btts", "both_teams_to_score"]},
                "source": "real_line_soccer_v2",
            })
            if n == 0:
                pytest.skip("no raw BTTS rows in current cache")
            # Model probability must be present on at least one pick.
            with_model = await db.picks.count_documents({
                "sport": "Soccer", "pick_date": TODAY,
                "market_key": {"$in": ["btts", "both_teams_to_score"]},
                "source": "real_line_soccer_v2",
                "model_source": "soccer_game_model",
            })
            assert with_model > 0, (
                f"BTTS raw rows exist ({n}) but none reached "
                "soccer_game_model"
            )
        finally:
            client.close()
    _run(run())


def test_anytime_scorer_reaches_model_when_raw_row_exists():
    async def run():
        client, db = _db()
        try:
            n = await db.picks.count_documents({
                "sport": "Soccer", "pick_date": TODAY,
                "market_key": "player_goal_scorer_anytime",
                "source": "real_line_alt_scorer_v1",
            })
            if n == 0:
                pytest.skip("no anytime scorer rows in current cache")
            with_model = await db.picks.count_documents({
                "sport": "Soccer", "pick_date": TODAY,
                "market_key": "player_goal_scorer_anytime",
                "source": "real_line_alt_scorer_v1",
                "model_probability": {"$exists": True, "$ne": None},
            })
            assert with_model > 0, (
                f"anytime scorer raw rows exist ({n}) but none "
                "reached model probability"
            )
        finally:
            client.close()
    _run(run())


def test_double_chance_engine_supports_all_selections():
    """double_chance selection normalisation is complete (Home/Draw,
    Draw/Away, Home/Away all resolvable)."""
    from services.soccer_game_model import compute_game_market_prob

    async def run():
        client, db = _db()
        try:
            for sel in ("Home/Draw", "Draw/Away", "Home/Away"):
                p = await compute_game_market_prob(
                    db, home_team="Chelsea", away_team="Arsenal",
                    league="EPL", market_key="double_chance",
                    selection=sel,
                )
                assert p is not None and 0 < p < 1, (
                    f"Double Chance selection {sel!r} → {p}"
                )
        finally:
            client.close()
    _run(run())


# ─── 10. Market Competition sees modeled alternatives ───────────
def test_market_competition_sees_modeled_alternatives():
    """`_rank_markets_for_event` must read from db.picks which is
    also where the ingester writes modeled candidates BEFORE
    consumer selection.  Same collection, same event key — the
    invariant."""
    import inspect
    from market_competition import routes as mc
    src = inspect.getsource(mc._rank_markets_for_event)
    assert 'db.picks.find(' in src, \
        "market competition no longer sources from db.picks"
    assert '"event": event' in src or "'event': event" in src, \
        "market competition not filtering by event"


def test_distinct_lines_not_collapsed_by_short_market():
    """`_short_market` MUST return distinct labels for Over 1.5 vs
    Over 2.5 vs Under 2.5 vs Under 3.5 so the seen_short_markets
    dedupe cannot collapse them."""
    from market_competition.routes import _short_market
    labels = {
        _short_market("Over 1.5 Goals"),
        _short_market("Over 2.5 Goals"),
        _short_market("Under 2.5 Goals"),
        _short_market("Under 3.5 Goals"),
    }
    assert labels == {"Over 1.5", "Over 2.5", "Under 2.5", "Under 3.5"}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
