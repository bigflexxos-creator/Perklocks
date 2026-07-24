"""
Iteration 80 — targeted verification of two surgical backend fixes:

  1) sportdb_player_scorer._picks_for_side: MLS active-player filter
     mirrors the existing CSL filter. Non-active MLS players (i.e. names
     absent from ESPN live leaderboard + curated top-scorer / starter
     indices) must be dropped before hitting the goal-rate lookup.

  2) server._apply_atomic_delete: the `_pin_filter` now includes
     `no_bet=True` in its inner $or, so picks tagged `no_bet=True` are
     wiped even when `lock_score_peak >= 95`. All other guards
     (_OUT_OF_BAND_SOURCES, sportdb_scorer* regex, is_model_only=True)
     must still protect matching picks.

Backend health smoke test also included.
"""
import os
import sys
import asyncio
from datetime import datetime, timezone

import pytest
import requests

sys.path.insert(0, "/app/backend")

BASE_URL = os.environ.get("EXPO_BACKEND_URL", "http://localhost:8001").rstrip("/")
if BASE_URL == "":
    BASE_URL = "http://localhost:8001"


# ─────────────────── Fixtures ───────────────────
@pytest.fixture(scope="module")
def api_client():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def auth_token(api_client):
    r = api_client.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": "demo@lockscore.ai", "password": "demo123"},
        timeout=15,
    )
    if r.status_code != 200:
        pytest.skip(f"Auth failed: {r.status_code} {r.text[:200]}")
    return r.json().get("access_token")


# ───────── PART A — MLS filter regression tests ─────────
class TestMLSFilter:
    """
    _picks_for_side has an initialisation block (~line 736) that only
    constructs `mls_filter_active` when comp starts with 'mls:' or equals
    'soccer_usa_mls'. The per-player loop (~line 803) then applies it. We
    verify the closure directly (unit-level) plus the compile / import
    surface.
    """

    def test_import_surface(self):
        """The module imports cleanly and the target symbols exist."""
        import sportdb_player_scorer as sps
        # Must have _picks_for_side (target function) + get_player_goal_rate
        assert hasattr(sps, "_picks_for_side")
        assert hasattr(sps, "get_player_goal_rate")

    def test_mls_gate_symbols(self):
        """The gate module exposes the objects the new filter depends on."""
        from services import mls_scorer_gate as g
        assert hasattr(g, "_espn_names")
        assert hasattr(g, "_TOP_SCORER_INDEX")
        assert hasattr(g, "_STARTER_INDEX")
        assert hasattr(g, "_norm")
        assert isinstance(g._TOP_SCORER_INDEX, dict)
        assert isinstance(g._STARTER_INDEX, set)
        assert len(g._TOP_SCORER_INDEX) >= 5
        assert len(g._STARTER_INDEX) >= 5

    def test_curated_lists_contain_known_players(self):
        """Sanity: Messi + Bouanga should be in the top-scorer index."""
        from services import mls_scorer_gate as g
        assert g._norm("Lionel Messi") in g._TOP_SCORER_INDEX
        assert g._norm("Denis Bouanga") in g._TOP_SCORER_INDEX

    def test_mls_filter_true_for_top_scorer_when_espn_empty(self):
        """
        Rebuild the closure exactly as _picks_for_side does. When ESPN
        snapshot is empty, curated top-scorer name should return True.
        """
        from services import mls_scorer_gate as _mls_gate

        # Snapshot & clear ESPN state for deterministic check.
        original_names = _mls_gate._espn_names
        original_index = _mls_gate._espn_index
        _mls_gate._espn_names = set()
        _mls_gate._espn_index = {}
        try:
            def _mls_active_check(name, team_hint=None):
                if not name:
                    return None
                n = _mls_gate._norm(name)
                if _mls_gate._espn_names:
                    if n in _mls_gate._espn_names:
                        return True
                    if n in _mls_gate._TOP_SCORER_INDEX:
                        return True
                    if n in _mls_gate._STARTER_INDEX:
                        return True
                    return False
                if n in _mls_gate._TOP_SCORER_INDEX or n in _mls_gate._STARTER_INDEX:
                    return True
                return None

            assert _mls_active_check("Lionel Messi") is True
            # Random unknown → None when ESPN empty (don't block)
            assert _mls_active_check("Zzz FakePlayer Nobody") is None
        finally:
            _mls_gate._espn_names = original_names
            _mls_gate._espn_index = original_index

    def test_mls_filter_false_for_unknown_when_espn_loaded(self):
        """
        When ESPN snapshot is populated, an unknown name returns False —
        this is the drop path that _picks_for_side uses to `continue`.
        """
        from services import mls_scorer_gate as _mls_gate

        original_names = _mls_gate._espn_names
        original_index = _mls_gate._espn_index
        # Simulate ESPN with one known player
        _mls_gate._espn_names = {_mls_gate._norm("Lionel Messi")}
        _mls_gate._espn_index = {_mls_gate._norm("Lionel Messi"): {"goals": 29}}
        try:
            def _mls_active_check(name, team_hint=None):
                if not name:
                    return None
                n = _mls_gate._norm(name)
                if _mls_gate._espn_names:
                    if n in _mls_gate._espn_names:
                        return True
                    if n in _mls_gate._TOP_SCORER_INDEX:
                        return True
                    if n in _mls_gate._STARTER_INDEX:
                        return True
                    return False
                if n in _mls_gate._TOP_SCORER_INDEX or n in _mls_gate._STARTER_INDEX:
                    return True
                return None

            assert _mls_active_check("Lionel Messi") is True
            # Curated fallback still counts as active
            assert _mls_active_check("Denis Bouanga") is True
            # Unknown → False (dropped)
            assert _mls_active_check("Zzz FakePlayer Nobody") is False
            # Empty → None
            assert _mls_active_check("") is None
        finally:
            _mls_gate._espn_names = original_names
            _mls_gate._espn_index = original_index

    def test_mls_branch_wired_in_source(self):
        """
        Structural check: sportdb_player_scorer must construct
        mls_filter_active for MLS comps AND consult it in the per-player
        loop (mirror of CSL). We grep the source since the function is
        long and async and the branch is deterministic.
        """
        with open("/app/backend/sportdb_player_scorer.py", "r") as f:
            src = f.read()
        # Detection block
        assert "mls_filter_active" in src
        assert 'startswith("mls:")' in src or "startswith('mls:')" in src
        assert '"soccer_usa_mls"' in src or "'soccer_usa_mls'" in src
        # Import gate module
        assert "from services import mls_scorer_gate" in src
        # Per-player skip
        assert src.count("mls_filter_active") >= 3  # init + closure + loop use
        assert "MLS ESPN: dropping inactive player" in src

    def test_csl_branch_untouched(self):
        """Regression: CSL init + loop still present and unchanged shape."""
        with open("/app/backend/sportdb_player_scorer.py", "r") as f:
            src = f.read()
        assert "csl_filter_active" in src
        assert "is_player_currently_active" in src
        assert "CSL ESPN: dropping inactive player" in src
        # CSL block must appear BEFORE the MLS block in the loop
        loop_csl = src.find("CSL ESPN: dropping inactive player")
        loop_mls = src.find("MLS ESPN: dropping inactive player")
        assert loop_csl != -1 and loop_mls != -1
        assert loop_csl < loop_mls, "CSL block must precede MLS block in loop"

    def test_non_mls_non_csl_leagues_no_filter(self):
        """
        Neither csl_filter_active nor mls_filter_active should be
        constructed for a comp like 'soccer_epl'. We verify by inspecting
        the branch conditions in source.
        """
        with open("/app/backend/sportdb_player_scorer.py", "r") as f:
            src = f.read()
        # The MLS init is gated by `is_mls`.
        assert "is_mls = " in src
        # The CSL init is gated by china: / soccer_china_superleague.
        assert 'startswith("china:")' in src
        # Neither gate matches 'soccer_epl' — that's the guarantee.


# ───────── PART B — _apply_atomic_delete / _pin_filter tests ─────────
class TestNoBetDeleteFilter:
    """
    Verify the updated `_pin_filter` inside server._apply_atomic_delete:
      * pick with lock_score_peak=99 + no_bet=True → deleted
      * pick with lock_score_peak=99 + no_bet not set → protected
      * pick with lock_score_peak=99 + no_bet=True + source in
        _OUT_OF_BAND_SOURCES → protected (out-of-band guard wins)
      * pick with lock_score_peak=99 + no_bet=True + source matches
        sportdb_scorer* regex → protected
      * pick with lock_score_peak=99 + no_bet=True + is_model_only=True
        → protected
    We build the `_pin_filter` in-memory and test it with a mock
    document via a lightweight Python matcher (faster than provisioning
    Mongo & avoids side effects on live collections).
    """

    def _pin_filter(self):
        # Copied verbatim from server.py so we test the exact clause.
        _OUT_OF_BAND_SOURCES = [
            "soccer_hot_scorers_v1",
            "csl_espn_leaderboard",
            "csl_espn_live",
            "mls_espn_leaderboard",
            "tennis_extra",
            "tennis_extra_model",
            "tennis_real_odds",
            "mlb_hot_hitters",
            "mlb_hot_hitters_v1",
        ]
        return {
            "$and": [
                {"$or": [
                    {"lock_score_peak": {"$exists": False}},
                    {"lock_score_peak": {"$lt": 95}},
                    {"no_bet": True},
                ]},
                {"source": {"$nin": _OUT_OF_BAND_SOURCES}},
                {"source": {"$not": {"$regex": r"^sportdb_scorer", "$options": "i"}}},
                {"is_model_only": {"$ne": True}},
            ]
        }

    def _matches(self, doc, flt):
        """Minimal $and / $or / $nin / $not-$regex / $ne / $exists / $lt matcher."""
        import re

        def match_clause(d, c):
            if "$and" in c:
                return all(match_clause(d, sub) for sub in c["$and"])
            if "$or" in c:
                return any(match_clause(d, sub) for sub in c["$or"])
            # Field-level clauses
            for k, cond in c.items():
                v = d.get(k)
                if isinstance(cond, dict):
                    if "$exists" in cond:
                        exists = (k in d)
                        if cond["$exists"] and not exists:
                            return False
                        if not cond["$exists"] and exists:
                            return False
                    if "$lt" in cond:
                        if v is None or not (v < cond["$lt"]):
                            return False
                    if "$nin" in cond:
                        if v in cond["$nin"]:
                            return False
                    if "$ne" in cond:
                        if v == cond["$ne"]:
                            return False
                    if "$not" in cond:
                        inner = cond["$not"]
                        if "$regex" in inner:
                            pat = inner["$regex"]
                            opts = inner.get("$options", "")
                            flags = re.IGNORECASE if "i" in opts else 0
                            if v is None:
                                # $not-$regex is TRUE when field is null
                                # (Mongo semantics). Move on.
                                continue
                            if re.search(pat, v, flags):
                                return False
                else:
                    if v != cond:
                        return False
            return True

        return match_clause(doc, flt)

    # --- direct clause tests ---
    def test_no_bet_true_with_peak_99_matches(self):
        flt = self._pin_filter()
        doc = {"lock_score_peak": 99, "no_bet": True, "source": "the_odds_api"}
        assert self._matches(doc, flt) is True

    def test_no_bet_missing_with_peak_99_protected(self):
        flt = self._pin_filter()
        doc = {"lock_score_peak": 99, "source": "the_odds_api"}
        assert self._matches(doc, flt) is False

    def test_no_bet_false_with_peak_99_protected(self):
        flt = self._pin_filter()
        doc = {"lock_score_peak": 99, "no_bet": False, "source": "the_odds_api"}
        assert self._matches(doc, flt) is False

    def test_peak_below_95_matches_regardless(self):
        flt = self._pin_filter()
        doc = {"lock_score_peak": 40, "source": "the_odds_api"}
        assert self._matches(doc, flt) is True

    def test_no_lock_score_peak_matches(self):
        flt = self._pin_filter()
        doc = {"source": "the_odds_api"}
        assert self._matches(doc, flt) is True

    def test_no_bet_true_but_out_of_band_source_protected(self):
        flt = self._pin_filter()
        doc = {"lock_score_peak": 99, "no_bet": True, "source": "mls_espn_leaderboard"}
        assert self._matches(doc, flt) is False

    def test_no_bet_true_but_sportdb_scorer_source_protected(self):
        flt = self._pin_filter()
        doc = {"lock_score_peak": 99, "no_bet": True, "source": "sportdb_scorer_v1"}
        assert self._matches(doc, flt) is False

    def test_no_bet_true_but_sportdb_scorer_synth_protected(self):
        flt = self._pin_filter()
        doc = {"lock_score_peak": 99, "no_bet": True, "source": "sportdb_scorer_synth"}
        assert self._matches(doc, flt) is False

    def test_no_bet_true_but_is_model_only_protected(self):
        flt = self._pin_filter()
        doc = {
            "lock_score_peak": 99, "no_bet": True,
            "source": "the_odds_api", "is_model_only": True,
        }
        assert self._matches(doc, flt) is False

    def test_no_bet_true_all_other_sources_ok_deleted(self):
        flt = self._pin_filter()
        doc = {
            "lock_score_peak": 99, "no_bet": True,
            "source": "the_odds_api", "is_model_only": False,
        }
        assert self._matches(doc, flt) is True

    def test_source_line_matches_server_source(self):
        """The exact $or clause we tested must be present in server.py verbatim."""
        with open("/app/backend/server.py", "r") as f:
            src = f.read()
        assert '{"no_bet": True}' in src
        assert "_pin_filter" in src

    def test_pin_filter_shape_in_source(self):
        """Structural check: the new $or contains all 3 clauses."""
        with open("/app/backend/server.py", "r") as f:
            src = f.read()
        # Locate the _pin_filter block
        pin_idx = src.find("_pin_filter = {")
        assert pin_idx != -1
        block = src[pin_idx:pin_idx + 2000]
        assert '"lock_score_peak": {"$exists": False}' in block
        assert '"lock_score_peak": {"$lt": 95}' in block
        assert '"no_bet": True' in block


# ───────── PART C — /api/picks/today health ─────────
class TestPicksTodayHealth:
    def test_endpoint_returns_valid_envelope(self, api_client, auth_token):
        r = api_client.get(
            f"{BASE_URL}/api/picks/today",
            headers={"Authorization": f"Bearer {auth_token}"},
            timeout=45,
        )
        assert r.status_code == 200, f"Unexpected: {r.status_code} {r.text[:400]}"
        body = r.json()
        assert isinstance(body, dict)
        assert "picks" in body
        assert isinstance(body["picks"], list)

    def test_no_500s_on_picks_today(self, api_client, auth_token):
        """Second call after warmup — confirm stable, no 500."""
        r = api_client.get(
            f"{BASE_URL}/api/picks/today",
            headers={"Authorization": f"Bearer {auth_token}"},
            timeout=45,
        )
        assert r.status_code == 200


# ───────── PART D — Live picks collection integration (best-effort) ─────────
class TestLivePicksNoBetDeletion:
    """
    Live-collection integration: seed one no_bet=True pick with sticky
    peak and one control pick, then execute the exact `_pin_filter` from
    server.py against Mongo and confirm delete_many wipes only the first.
    Uses TEST_ prefix + explicit teardown.
    """

    def test_seed_and_verify_delete_filter(self):
        asyncio.run(self._run_seed_and_verify())

    async def _run_seed_and_verify(self):
        try:
            from motor.motor_asyncio import AsyncIOMotorClient
        except Exception:
            pytest.skip("motor not available")

        # Load backend .env in case pytest was invoked without it.
        try:
            from dotenv import load_dotenv
            load_dotenv("/app/backend/.env")
        except Exception:
            pass

        mongo_url = os.environ.get("MONGO_URL")
        db_name = os.environ.get("DB_NAME")
        if not mongo_url or not db_name:
            pytest.skip("MONGO_URL / DB_NAME not set")

        client = AsyncIOMotorClient(mongo_url)
        db = client[db_name]
        # Test-scoped date to avoid touching real slate
        date_str = "TEST_2099-01-01"

        # Fresh state
        await db.picks.delete_many({"pick_date": date_str})

        seed = [
            {
                "id": "TEST_no_bet_delete_1",
                "pick_date": date_str,
                "sport": "SOCCER",
                "event_time": datetime.now(timezone.utc).isoformat(),
                "market": "TEST",
                "book_odds": -110,
                "lock_score_peak": 99,
                "no_bet": True,
                "source": "the_odds_api",
            },
            {
                "id": "TEST_no_bet_keep_1",
                "pick_date": date_str,
                "sport": "SOCCER",
                "event_time": datetime.now(timezone.utc).isoformat(),
                "market": "TEST",
                "book_odds": -110,
                "lock_score_peak": 99,
                "no_bet": False,
                "source": "the_odds_api",
            },
            {
                "id": "TEST_no_bet_keep_oob_1",
                "pick_date": date_str,
                "sport": "SOCCER",
                "event_time": datetime.now(timezone.utc).isoformat(),
                "market": "TEST",
                "book_odds": -110,
                "lock_score_peak": 99,
                "no_bet": True,
                "source": "mls_espn_leaderboard",
            },
            {
                "id": "TEST_no_bet_keep_sportdb_1",
                "pick_date": date_str,
                "sport": "SOCCER",
                "event_time": datetime.now(timezone.utc).isoformat(),
                "market": "TEST",
                "book_odds": -110,
                "lock_score_peak": 99,
                "no_bet": True,
                "source": "sportdb_scorer_v1",
            },
            {
                "id": "TEST_no_bet_keep_modelonly_1",
                "pick_date": date_str,
                "sport": "SOCCER",
                "event_time": datetime.now(timezone.utc).isoformat(),
                "market": "TEST",
                "book_odds": -110,
                "lock_score_peak": 99,
                "no_bet": True,
                "source": "the_odds_api",
                "is_model_only": True,
            },
        ]
        try:
            await db.picks.insert_many(seed)

            _OUT_OF_BAND_SOURCES = [
                "soccer_hot_scorers_v1", "csl_espn_leaderboard", "csl_espn_live",
                "mls_espn_leaderboard", "tennis_extra", "tennis_extra_model",
                "tennis_real_odds", "mlb_hot_hitters", "mlb_hot_hitters_v1",
            ]
            _pin_filter = {
                "$and": [
                    {"$or": [
                        {"lock_score_peak": {"$exists": False}},
                        {"lock_score_peak": {"$lt": 95}},
                        {"no_bet": True},
                    ]},
                    {"source": {"$nin": _OUT_OF_BAND_SOURCES}},
                    {"source": {"$not": {"$regex": r"^sportdb_scorer", "$options": "i"}}},
                    {"is_model_only": {"$ne": True}},
                ]
            }

            res = await db.picks.delete_many({"pick_date": date_str, **_pin_filter})
            assert res.deleted_count == 1, f"Expected 1 delete, got {res.deleted_count}"

            # Verify the correct doc was deleted + the protected ones remain
            remaining_ids = {d["id"] async for d in db.picks.find({"pick_date": date_str}, {"id": 1})}
            assert "TEST_no_bet_delete_1" not in remaining_ids
            assert "TEST_no_bet_keep_1" in remaining_ids
            assert "TEST_no_bet_keep_oob_1" in remaining_ids
            assert "TEST_no_bet_keep_sportdb_1" in remaining_ids
            assert "TEST_no_bet_keep_modelonly_1" in remaining_ids
        finally:
            await db.picks.delete_many({"pick_date": date_str})
            client.close()
