"""Phase 2A.5 UNIVERSAL — cross-league, cross-market Soccer production
truth regression suite.

Contract invariants:

  * League-agnostic ingestion: every supported soccer sport_key
    (MLS, EPL, La Liga, Serie A, Bundesliga, Ligue 1, UCL, Europa,
    Conference, Liga MX, Leagues Cup, Norway, Sweden, etc.) traverses
    the same canonical pipeline — no league hard-codes.
  * League-agnostic market family coverage:
        - player-scorer markets (anytime / first / SoA / assist / shots)
        - game markets: h2h / 1X2 / draw / double_chance
        - game totals: alternate_totals with exact provider line
                       preserved (2.0 / 2.25 / 2.5 / 2.75 / 3.0 etc.)
        - BTTS Yes and BTTS No (both reachable, no hard-coded preference)
        - spreads / Asian handicap where provider-supported
  * Real book odds always preserved (`odds_source=real_book_line`,
    `no_real_book_line=False`).  Never synthetic / model_only.
  * Deterministic UUID5 pick id — restart-stable.
  * Off-board attribution routes through
    :mod:`services.soccer_rejection_taxonomy` (no generic labels).
  * Feature resolver is league-aware — MLS players do not need European
    sources and vice versa.
  * Universal source strings `real_line_alt_scorer_v1` /
    `real_line_soccer_v2` are marked out-of-band in the main refresh
    atomic delete so they survive general refresh cycles.
  * Startup healer + 15-min recurring task wired.
"""
from __future__ import annotations

import os, sys, asyncio, uuid, inspect, importlib
BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from services.soccer_rejection_taxonomy import (
    SoccerRejection, ALL_CODES, is_valid_code,
)


# ─────────────────────────────────────────────────────────────────────
# Minimal fake mongo double (async iterator + upsert capture).
# ─────────────────────────────────────────────────────────────────────
class _FakeCursor:
    def __init__(self, rows):
        self._rows = list(rows)
    def __aiter__(self):
        self._i = 0
        return self
    async def __anext__(self):
        if self._i >= len(self._rows):
            raise StopAsyncIteration
        r = self._rows[self._i]
        self._i += 1
        return r
    def sort(self, *_a, **_k):
        return self
    def limit(self, *_a, **_k):
        return self


class _FakeCollection:
    def __init__(self, rows=None):
        self._rows = list(rows or [])
        self.upserts = []
    def find(self, q=None, projection=None):
        def _match(row):
            if not q:
                return True
            for k, v in q.items():
                if isinstance(v, dict) and "$in" in v:
                    if row.get(k) not in v["$in"]:
                        return False
                elif isinstance(v, dict) and "$regex" in v:
                    import re
                    if not re.search(v["$regex"], str(row.get(k) or ""),
                                     re.I if "$options" in v and "i" in v["$options"] else 0):
                        return False
                elif isinstance(v, dict) and "$ne" in v:
                    if row.get(k) == v["$ne"]:
                        return False
                elif row.get(k) != v:
                    return False
            return True
        return _FakeCursor([r for r in self._rows if _match(r)])
    async def find_one(self, q=None, *_a, **_k):
        cur = self.find(q)
        async for r in cur:
            return r
        return None
    async def update_one(self, filt, update, upsert=False):
        self.upserts.append((filt, update))
        return type("R", (), {"upserted_id": None, "matched_count": 0})
    async def count_documents(self, *_a, **_k):
        return len(self._rows)
    def aggregate(self, pipeline):
        return _FakeCursor([])


class _FakeDB:
    def __init__(self, alt_rows=None, form_rows=None, team_form_rows=None):
        self.live_alt_lines = _FakeCollection(alt_rows or [])
        self.soccer_player_form = _FakeCollection(form_rows or [])
        self.soccer_player_game_logs = _FakeCollection([])
        self.soccer_team_form = _FakeCollection(team_form_rows or [])
        self.team_form = _FakeCollection([])
        self.soccer_team_xg_rolling = _FakeCollection([])
        self.picks = _FakeCollection([])


def _run(coro):
    return asyncio.run(coro)


def _alt_row(**kw):
    base = dict(
        sport="soccer",
        odds_api_sport="soccer_usa_mls",
        event_id="evt1",
        event_name="A @ B",
        home_team="B",
        away_team="A",
        commence_time="2026-08-15T22:00:00Z",
        sportsbook="draftkings",
        market_key="player_goal_scorer_anytime",
        selection="Some Player",
        selection_norm="some player",
        line=None,
        price=-110,
    )
    base.update(kw)
    return base


# ═════════════════════════════════════════════════════════════════════
# 1. Rejection taxonomy invariants
# ═════════════════════════════════════════════════════════════════════
def test_taxonomy_has_all_required_codes():
    required = {
        "NO_PROVIDER_MARKET", "EVENT_IDENTITY_FAILURE",
        "PLAYER_IDENTITY_FAILURE", "MARKET_NORMALIZATION_FAILURE",
        "REAL_LINE_NOT_PRESERVED", "CANDIDATE_NOT_CREATED",
        "MISSING_FEATURE_DATA", "MODEL_NOT_INVOKED",
        "NO_MODEL_PROBABILITY", "NO_IMPLIED_PROBABILITY",
        "NO_EDGE_VALUE", "LOW_LOCK_SCORE", "NO_POSITIVE_EDGE",
        "RELATED_MARKET_DOMINATED", "TEAMMATE_DOMINATED",
        "CANONICAL_PUBLICATION_REJECTED", "DUPLICATE_CANONICAL_MARKET",
    }
    for code in required:
        assert code in ALL_CODES, f"Taxonomy missing required code {code}"
    for code in ALL_CODES:
        assert is_valid_code(code)


# ═════════════════════════════════════════════════════════════════════
# 2. League-agnostic ingestion — sport_key inference
# ═════════════════════════════════════════════════════════════════════
def test_league_derivation_is_universal_not_mls_specific():
    from services.real_line_scorer_ingest import _league_from_sport_key
    # A representative subset of leagues currently supported by
    # sports_engine.SPORT_KEYS['Soccer'] — no hard-code check for a
    # specific league name; only that the resolver returns a stable
    # non-empty label for each.
    for sk in (
        "soccer_usa_mls", "soccer_epl", "soccer_spain_la_liga",
        "soccer_italy_serie_a", "soccer_germany_bundesliga",
        "soccer_france_ligue_one",
        "soccer_uefa_champs_league", "soccer_uefa_europa_league",
        "soccer_uefa_europa_conference_league",
        "soccer_mexico_ligamx", "soccer_concacaf_leagues_cup",
        "soccer_norway_eliteserien", "soccer_sweden_allsvenskan",
        "soccer_finland_veikkausliiga", "soccer_china_superleague",
        "soccer_japan_j_league",
    ):
        label = _league_from_sport_key(sk)
        assert isinstance(label, str) and label, sk
        assert "soccer" not in label.lower() or "world" in label.lower(), \
            f"league label for {sk} looks like a raw sport_key: {label}"


# ═════════════════════════════════════════════════════════════════════
# 3. Player-scorer ingest across multiple leagues
# ═════════════════════════════════════════════════════════════════════
def test_player_scorer_ingest_across_multiple_leagues():
    from services.real_line_scorer_ingest import (
        ingest_real_line_soccer_scorers,
    )
    rows = [
        _alt_row(odds_api_sport="soccer_usa_mls",       event_id="e_mls",
                  selection="Lionel Messi",   price=-110),
        _alt_row(odds_api_sport="soccer_spain_la_liga", event_id="e_lal",
                  selection="Vinicius Junior", price=+140),
        _alt_row(odds_api_sport="soccer_italy_serie_a", event_id="e_ser",
                  selection="Lautaro Martinez", price=+130),
        _alt_row(odds_api_sport="soccer_epl",           event_id="e_epl",
                  selection="Bukayo Saka",     price=+180),
    ]
    db = _FakeDB(alt_rows=rows)
    stats = _run(ingest_real_line_soccer_scorers(db, today="2026-08-15"))
    assert stats["scanned"] == len(rows)
    assert stats["written"] == len(rows)
    leagues_written = set(stats["by_league"].keys())
    assert {"MLS", "La Liga", "Serie A", "EPL"}.issubset(leagues_written), (
        f"leagues written: {leagues_written}"
    )
    # Every written doc must carry real book odds provenance.
    for _, upd in db.picks.upserts:
        d = upd["$set"]
        assert d["odds_source"] == "real_book_line"
        assert d["no_real_book_line"] is False
        assert d["book_odds"] != 0
        assert d["source"] == "real_line_alt_scorer_v1"


# ═════════════════════════════════════════════════════════════════════
# 4. BTTS reachability — Yes AND No, multiple leagues, no preference
# ═════════════════════════════════════════════════════════════════════
def test_btts_yes_and_no_both_traverse_pipeline():
    from services.real_line_scorer_ingest import (
        ingest_real_line_soccer_scorers,
    )
    rows = [
        _alt_row(odds_api_sport="soccer_usa_mls",       event_id="e_mls_btts",
                  market_key="btts", selection="Yes", price=-125),
        _alt_row(odds_api_sport="soccer_usa_mls",       event_id="e_mls_btts",
                  market_key="btts", selection="No",  price=+105),
        _alt_row(odds_api_sport="soccer_spain_la_liga", event_id="e_lal_btts",
                  market_key="btts", selection="Yes", price=-150),
        _alt_row(odds_api_sport="soccer_spain_la_liga", event_id="e_lal_btts",
                  market_key="btts", selection="No",  price=+130),
    ]
    db = _FakeDB(alt_rows=rows)
    stats = _run(ingest_real_line_soccer_scorers(db, today="2026-08-15"))
    assert stats["written"] == 4
    yes_docs = [u[1]["$set"] for u in db.picks.upserts
                if u[1]["$set"]["selection"] == "Yes"]
    no_docs  = [u[1]["$set"] for u in db.picks.upserts
                if u[1]["$set"]["selection"] == "No"]
    assert len(yes_docs) == 2, "BTTS Yes not reaching pipeline"
    assert len(no_docs)  == 2, "BTTS No not reaching pipeline"
    # No hard-coded preference: both directions marked as game_market
    # family with real book odds regardless of side.
    for d in (yes_docs + no_docs):
        assert d["market_family"] == "game_market"
        assert d["odds_source"] == "real_book_line"


# ═════════════════════════════════════════════════════════════════════
# 5. Over/Under reachability — exact line preservation, no auto-normalize
# ═════════════════════════════════════════════════════════════════════
def test_over_under_preserves_exact_provider_line():
    from services.real_line_scorer_ingest import (
        ingest_real_line_soccer_scorers,
    )
    rows = []
    # Exact provider lines: 2.0 / 2.25 / 2.5 / 2.75 / 3.0 — none should
    # be silently converted to 2.5.
    for line in (2.0, 2.25, 2.5, 2.75, 3.0):
        for side, price in (("Over", -110), ("Under", -110)):
            rows.append(_alt_row(
                odds_api_sport="soccer_epl",
                event_id=f"e_epl_tot_{line}",
                market_key="alternate_totals",
                selection=side, price=price, line=line,
            ))
    db = _FakeDB(alt_rows=rows)
    stats = _run(ingest_real_line_soccer_scorers(db, today="2026-08-15"))
    assert stats["written"] == len(rows)
    lines_seen = sorted({u[1]["$set"].get("line") for u in db.picks.upserts})
    assert lines_seen == [2.0, 2.25, 2.5, 2.75, 3.0], (
        f"Exact provider lines not preserved: {lines_seen}"
    )
    # Both sides reachable at every line.
    for line in lines_seen:
        docs = [u[1]["$set"] for u in db.picks.upserts
                if u[1]["$set"]["line"] == line]
        sides = {d["selection"] for d in docs}
        assert sides == {"Over", "Under"}, f"line {line} missing side: {sides}"


# ═════════════════════════════════════════════════════════════════════
# 6. 1X2 / h2h reachability — Home, Draw, Away
# ═════════════════════════════════════════════════════════════════════
def test_1x2_home_draw_away_all_reach_pipeline():
    from services.real_line_scorer_ingest import (
        ingest_real_line_soccer_scorers,
    )
    rows = [
        _alt_row(odds_api_sport="soccer_epl", event_id="e_h",
                  market_key="h2h", selection="Arsenal",  price=+140),
        _alt_row(odds_api_sport="soccer_epl", event_id="e_h",
                  market_key="h2h", selection="Draw",     price=+260),
        _alt_row(odds_api_sport="soccer_epl", event_id="e_h",
                  market_key="h2h", selection="Chelsea",  price=+180),
    ]
    db = _FakeDB(alt_rows=rows)
    stats = _run(ingest_real_line_soccer_scorers(db, today="2026-08-15"))
    sels = sorted({u[1]["$set"]["selection"] for u in db.picks.upserts})
    assert sels == ["Arsenal", "Chelsea", "Draw"]


# ═════════════════════════════════════════════════════════════════════
# 7. Deterministic pick id → restart stable
# ═════════════════════════════════════════════════════════════════════
def test_pick_ids_stable_across_restarts():
    from services.real_line_scorer_ingest import (
        ingest_real_line_soccer_scorers,
    )
    row = _alt_row()
    ids_seen = []
    for _ in range(3):
        db = _FakeDB(alt_rows=[row])
        _run(ingest_real_line_soccer_scorers(db, today="2026-08-15"))
        ids_seen.append(db.picks.upserts[0][1]["$set"]["id"])
    assert len(set(ids_seen)) == 1, f"Pick ids drift across restarts: {ids_seen}"


# ═════════════════════════════════════════════════════════════════════
# 8. Off-board attribution uses taxonomy codes only — no generic labels
# ═════════════════════════════════════════════════════════════════════
def test_off_board_reasons_are_taxonomy_codes():
    from services.real_line_scorer_ingest import (
        ingest_real_line_soccer_scorers,
    )
    row = _alt_row(market_key="btts", selection="Yes", price=-125)
    db = _FakeDB(alt_rows=[row])
    _run(ingest_real_line_soccer_scorers(db, today="2026-08-15"))
    doc = db.picks.upserts[0][1]["$set"]
    reasons = doc.get("off_board_reasons") or []
    if reasons:
        for r in reasons:
            assert is_valid_code(r), f"invalid taxonomy code: {r}"
            assert r not in ("filtered", "skipped", "unavailable",
                              "not selected"), (
                f"generic label leaked into off_board_reasons: {r}"
            )


# ═════════════════════════════════════════════════════════════════════
# 9. Game-model universal entry point handles every market family
# ═════════════════════════════════════════════════════════════════════
def test_game_model_universal_entry_point_covers_all_markets():
    from services.soccer_game_model import compute_game_market_prob
    # A pre-populated ctx would come from team_form; here we just
    # verify the function exists and handles each market via the
    # public API without raising.
    async def _run_all():
        db = _FakeDB()
        # Insufficient team ctx → None (attribution code path).
        for mk, sel, line in (
            ("h2h",              "Arsenal", None),
            ("h2h",              "Draw",    None),
            ("btts",             "Yes",     None),
            ("btts",             "No",      None),
            ("totals",           "Over",    2.5),
            ("totals",           "Under",   2.5),
            ("alternate_totals", "Over",    3.0),
            ("double_chance",    "1X",      None),
            ("spreads",          "Arsenal", -1.5),
        ):
            out = await compute_game_market_prob(
                db, home_team="Arsenal", away_team="Chelsea",
                league="EPL", market_key=mk, selection=sel, line=line,
            )
            # No team ctx → None (returns NO_MODEL_PROBABILITY code).
            # We only assert the function did not raise.
            assert out is None or (0.0 <= out <= 1.0), (mk, sel, out)
    _run(_run_all())


# ═════════════════════════════════════════════════════════════════════
# 10. Feature resolver is league-aware
# ═════════════════════════════════════════════════════════════════════
def test_feature_resolver_league_aware_falls_back_gracefully():
    from services.soccer_feature_resolver import (
        resolve_soccer_player_features,
    )
    async def _tests():
        db = _FakeDB()
        # No data anywhere → (None, "")
        row, src = await resolve_soccer_player_features(
            db, player_name="Random Player", league="MLS",
        )
        assert row is None and src == ""

        # Direct form row present → returned with its source label.
        db2 = _FakeDB(form_rows=[{
            "name_canonical": "vinicius junior",
            "minutes": 1500, "games": 20, "goals": 15,
            "xg": 12.0, "xa": 5.0, "team": "Real Madrid",
        }])
        row2, src2 = await resolve_soccer_player_features(
            db2, player_name="Vinicius Junior", league="La Liga",
        )
        assert row2 is not None
        assert src2 == "soccer_player_form"
    _run(_tests())


# ═════════════════════════════════════════════════════════════════════
# 11. Out-of-band source registration (main refresh cannot wipe picks)
# ═════════════════════════════════════════════════════════════════════
def test_universal_sources_marked_out_of_band():
    mod = importlib.import_module("services.pick_refresh_orchestrator")
    src = inspect.getsource(mod)
    assert "real_line_alt_scorer_v1" in src, (
        "player-scorer source not in _OUT_OF_BAND_SOURCES"
    )
    # Adding real_line_soccer_v2 as out-of-band is REQUIRED for the
    # universal game-market ingester so BTTS/Totals/1X2 real-line
    # picks survive main refresh cycles.
    assert "real_line_soccer_v2" in src, (
        "game-market source not in _OUT_OF_BAND_SOURCES — main refresh "
        "will wipe BTTS/Totals/1X2 picks between cycles"
    )


# ═════════════════════════════════════════════════════════════════════
# 12. Startup healer + recurring task wired
# ═════════════════════════════════════════════════════════════════════
def test_universal_ingest_wired_into_server_startup():
    import server
    src = inspect.getsource(server.on_startup)
    assert "ingest_real_line_soccer_scorers" in src
    assert "phase_2a5e_real_line_scorer_ingest" in src


# ═════════════════════════════════════════════════════════════════════
# 13. alt_lines_feed requests BTTS + first_goalscorer for every active
#     league (universal auto-discovery contract)
# ═════════════════════════════════════════════════════════════════════
def test_alt_lines_feed_requests_universal_soccer_market_set():
    import alt_lines_feed as alf
    assert "btts" in alf.SOCCER_MARKETS, (
        "alt_lines_feed.SOCCER_MARKETS must request BTTS so BTTS "
        "reaches live_alt_lines universally across every active league"
    )
    assert "player_first_goal_scorer" in alf.SOCCER_MARKETS
    assert "alternate_totals" in alf.SOCCER_MARKETS
    assert "player_goal_scorer_anytime" in alf.SOCCER_MARKETS


# ═════════════════════════════════════════════════════════════════════
# 14. Diagnostic — public API
# ═════════════════════════════════════════════════════════════════════
def test_universal_diagnostic_public_api_returns_matrix():
    from services.soccer_universal_diagnostic import run_diagnostic
    async def _t():
        db = _FakeDB()
        rep = await run_diagnostic(db, today="2026-08-15",
                                     sample_examples=False)
        assert isinstance(rep, dict)
        assert "matrix" in rep and isinstance(rep["matrix"], list)
        assert "rejection_totals" in rep
        assert "leagues_scanned" in rep
        assert "pick_date" in rep
    _run(_t())
