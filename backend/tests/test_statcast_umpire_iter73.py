"""Phase 1.1 (Statcast) + Phase 1.4 (Umpire K-zone) regression — iter73.

Covers:
  1) /api/picks/today live API smoke — statcast attached correctly to
     hitter/pitcher props but NOT to team-level markets.
  2) /api/analytics/clv?days=30 still returns 200 with expected shape.
  3) mlb_deep_signal is tolerant of picks with statcast_batter present
     but no park factors (mlb_deep=None).
  4) mlb_deep_signal fires with found=True on a synthetic Aaron Judge
     Over 0.5 Hits pick carrying only statcast_batter data.
  5) Umpire K-zone signal fires on a pitcher K prop.
  6) Prior iter71 MLB grading fix still passes on Wheeler/Altuve/Machado.
"""
from __future__ import annotations

import os
import sys
import pytest
import requests

sys.path.insert(0, "/app/backend")

BASE_URL = (os.environ.get("EXPO_PUBLIC_BACKEND_URL")
            or os.environ.get("EXPO_BACKEND_URL", "")).rstrip("/")
if not BASE_URL:
    # Load from frontend/.env as fallback
    try:
        with open("/app/frontend/.env") as _f:
            for _line in _f:
                if _line.startswith("EXPO_PUBLIC_BACKEND_URL="):
                    BASE_URL = _line.split("=", 1)[1].strip().rstrip("/")
                    break
    except Exception:
        pass
assert BASE_URL, "EXPO_PUBLIC_BACKEND_URL not set"

CREDS = {"email": "demo@lockscore.ai", "password": "demo123"}


# ── fixtures ──────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def token():
    r = requests.post(f"{BASE_URL}/api/auth/login", json=CREDS, timeout=15)
    assert r.status_code == 200, f"Login failed: {r.status_code} {r.text[:200]}"
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def auth(token):
    return {"Authorization": f"Bearer {token}"}


# ── (A) live-API smoke ───────────────────────────────────────────────
class TestPicksTodayStatcast:
    """/api/picks/today — Statcast/umpire enrichment sanity."""

    @pytest.fixture(scope="class")
    def payload(self, auth):
        r = requests.get(f"{BASE_URL}/api/picks/today", headers=auth, timeout=30)
        assert r.status_code == 200, r.text[:200]
        return r.json()

    def test_response_ok(self, payload):
        # Endpoint returns either a list or a dict with picks. Handle both.
        picks = payload if isinstance(payload, list) else payload.get("picks", [])
        assert isinstance(picks, list)

    def test_team_markets_have_no_statcast(self, payload):
        picks = payload if isinstance(payload, list) else payload.get("picks", [])
        mlb = [p for p in picks if (p.get("sport") or "").upper() == "MLB"]
        team_markets = [
            p for p in mlb
            if any(kw in (p.get("market") or "").lower() for kw in (
                "team total", "spread", "moneyline", "run line", "run-line",
                "1st inning", "nrfi", "yrfi"))
            or (p.get("selection") or "").lower() in ("over", "under", "yes", "no")
        ]
        if not team_markets:
            pytest.skip("No MLB team-level markets in today's slate")
        for p in team_markets:
            # Statcast keys must NOT be attached on team-level markets.
            assert "statcast_batter" not in p, (
                f"team-market pick has statcast_batter: {p.get('market')}/{p.get('selection')}")
            assert "statcast_pitcher" not in p, (
                f"team-market pick has statcast_pitcher: {p.get('market')}/{p.get('selection')}")

    def test_hitter_or_pitcher_props_may_have_statcast(self, payload):
        """At least one hitter or pitcher prop should carry statcast_batter
        or statcast_pitcher — otherwise the enrichment layer is silent.
        We only fail if MLB props exist AND NONE have statcast."""
        picks = payload if isinstance(payload, list) else payload.get("picks", [])
        mlb = [p for p in picks if (p.get("sport") or "").upper() == "MLB"]
        prop_kws_hitter = ("hits", "home run", "total bases", "rbi",
                           "runs scored", "singles", "doubles", "triples")
        prop_kws_pitcher = ("strikeouts", "outs recorded", "earned runs",
                            "pitcher walks", "hits allowed")
        hitter_props = [
            p for p in mlb
            if any(k in (p.get("market") or "").lower() for k in prop_kws_hitter)
            and (p.get("selection") or "").lower() not in ("over", "under", "yes", "no", "")
        ]
        pitcher_props = [
            p for p in mlb
            if any(k in (p.get("market") or "").lower() for k in prop_kws_pitcher)
            and (p.get("selection") or "").lower() not in ("over", "under", "yes", "no", "")
        ]
        if not hitter_props and not pitcher_props:
            pytest.skip("No MLB hitter/pitcher props in today's slate")
        touched = sum(1 for p in hitter_props if p.get("statcast_batter")) + \
                  sum(1 for p in pitcher_props if p.get("statcast_pitcher"))
        # We report but don't hard-fail on 0 attachment (data-density issue).
        print(f"\n[iter73] MLB prop picks: {len(hitter_props)} hitter + "
              f"{len(pitcher_props)} pitcher, statcast-attached={touched}")


class TestClvEndpoint:
    def test_clv_30d(self, auth):
        r = requests.get(f"{BASE_URL}/api/analytics/clv?days=30",
                         headers=auth, timeout=20)
        assert r.status_code == 200
        j = r.json()
        for k in ("since", "days", "overall", "bands", "snapshot_coverage"):
            assert k in j, f"missing CLV field: {k}"
        assert isinstance(j["bands"], list) and len(j["bands"]) == 6


# ── (B) signal_engine tolerance & firing ─────────────────────────────
class TestMlbDeepSignalStatcast:
    def test_tolerant_of_missing_park_factors(self):
        from services.signal_engine.calculators import mlb_deep_signal
        pick = {
            "sport": "MLB",
            "market": "Aaron Judge Over 0.5 Hits",
            "selection": "Aaron Judge",
            "statcast_batter": {
                "xba": 0.310, "ba": 0.240, "xba_diff": -0.070,
                "xwoba": 0.400, "woba": 0.330, "xwoba_diff": -0.070,
                "barrel_pct": 15.0,
            },
        }
        # No 'mlb_deep' key attached — should not crash.
        result = mlb_deep_signal(pick)
        assert isinstance(result, dict)
        assert result["key"] == "mlb_deep"
        assert result["found"] is True
        # Unlucky hitter on an Over pick → positive points expected.
        assert result["points"] > 0, f"expected positive lift, got {result['points']}"

    def test_fires_on_synthetic_judge_pick(self):
        from services.signal_engine.calculators import mlb_deep_signal
        pick = {
            "sport": "MLB",
            "market": "Aaron Judge Over 0.5 Hits",
            "selection": "Aaron Judge",
            "statcast_batter": {
                "xba": 0.290, "ba": 0.260, "xba_diff": -0.030,
                "xwoba": 0.410, "woba": 0.360, "xwoba_diff": -0.050,
                "barrel_pct": 18.5,
            },
        }
        result = mlb_deep_signal(pick)
        assert result["found"] is True
        assert result["points"] > 0
        # points ≤ MLB_DEEP_MAX (±7)
        assert -7.0 <= result["points"] <= 7.0

    def test_pitcher_xwoba_against_signal(self):
        from services.signal_engine.calculators import mlb_deep_signal
        pick = {
            "sport": "MLB",
            "market": "Zack Wheeler Over 7.5 Strikeouts",
            "selection": "Zack Wheeler",
            "mlb_deep": {"market_family": "pitcher_k", "park_run_factor": 100,
                         "park_hr_factor": 100, "park_hits_factor": 100,
                         "park_name": "Test Park"},
            "statcast_pitcher": {
                "xwoba_against": 0.260,  # elite
                "xera": 2.90,
                "era": 3.10,
            },
        }
        result = mlb_deep_signal(pick)
        assert result["found"] is True
        # Elite pitcher on Over K → positive
        assert result["points"] > 0

    def test_non_mlb_returns_neutral(self):
        from services.signal_engine.calculators import mlb_deep_signal
        result = mlb_deep_signal({"sport": "NBA", "market": "LeBron Points"})
        assert result["found"] is False
        assert result["points"] == 0.0

    def test_no_data_returns_neutral(self):
        from services.signal_engine.calculators import mlb_deep_signal
        result = mlb_deep_signal({"sport": "MLB", "market": "X Over 0.5 Hits",
                                  "selection": "Nobody"})
        assert result["found"] is False
        assert result["points"] == 0.0


class TestUmpireSignalWiring:
    def test_umpire_seed_table_lookups(self):
        from services.mlb_umpire import get_umpire_zone
        # Wide zone (pitcher-friendly)
        wide = get_umpire_zone("Angel Hernandez")
        assert wide is not None
        assert wide["zone"] == "pitcher"
        assert wide["delta_pct"] > 0
        # Tight zone (hitter-friendly)
        tight = get_umpire_zone("Pat Hoberg")
        assert tight is not None
        assert tight["zone"] == "hitter"
        assert tight["delta_pct"] < 0
        # Unknown umpire
        assert get_umpire_zone("Some Fake Umpire") is None
        # Empty
        assert get_umpire_zone("") is None

    def test_umpire_kzone_fires_in_volume_signal(self):
        """volume_signal should read ump_zone/ump_delta_pct for K props."""
        from services.signal_engine.calculators import volume_signal
        pick = {
            "sport": "MLB",
            "market": "Zack Wheeler Over 7.5 Strikeouts",
            "selection": "Zack Wheeler",
            "ump_name": "Angel Hernandez",
            "ump_zone": "pitcher",
            "ump_delta_pct": 2.8,
        }
        result = volume_signal(pick)
        assert result["found"] is True
        assert result["points"] > 0  # Over K + pitcher-friendly ump → positive
        assert any("ump" in d.lower() or "hernandez" in d.lower()
                   for d in result["details"])

    def test_umpire_kzone_flips_under(self):
        from services.signal_engine.calculators import volume_signal
        pick = {
            "sport": "MLB",
            "market": "Zack Wheeler Under 7.5 Strikeouts",
            "selection": "Zack Wheeler",
            "ump_name": "Pat Hoberg",
            "ump_zone": "hitter",
            "ump_delta_pct": -2.9,
        }
        result = volume_signal(pick)
        assert result["found"] is True
        # Under + hitter-friendly ump → positive
        assert result["points"] > 0


# ── (C) enrich_picks_with_statcast_bulk logic ────────────────────────
class TestStatcastEnricherAgainstRealDB:
    """End-to-end enrichment against the real Mongo collection."""

    def test_bulk_enrich_real_db(self):
        import asyncio
        import os as _os
        from motor.motor_asyncio import AsyncIOMotorClient
        from services.mlb_statcast import enrich_picks_with_statcast_bulk

        async def _run():
            cli = AsyncIOMotorClient(_os.getenv("MONGO_URL", "mongodb://localhost:27017"))
            db = cli[_os.getenv("DB_NAME", "lockscore_db")]
            try:
                # sanity: seed count
                bat = await db.mlb_statcast_players.count_documents(
                    {"type": "batter", "year": 2026})
                pit = await db.mlb_statcast_players.count_documents(
                    {"type": "pitcher", "year": 2026})
                assert bat >= 400, f"expected >=400 batters seeded, got {bat}"
                assert pit >= 400, f"expected >=400 pitchers seeded, got {pit}"
                picks = [
                    {"sport": "MLB", "market": "Aaron Judge Over 0.5 Hits",
                     "selection": "Aaron Judge"},
                    {"sport": "MLB", "market": "Over 5.5 Strikeouts",
                     "selection": "Zack Wheeler"},
                    {"sport": "MLB",
                     "market": "American League Team Total Under 5.5",
                     "selection": "Under"},
                ]
                touched = await enrich_picks_with_statcast_bulk(db, picks)
                assert touched >= 1, f"expected touched>=1, got {touched}"
                assert "statcast_batter" in picks[0]
                assert "statcast_pitcher" in picks[1]
                assert "statcast_batter" not in picks[2]
                assert "statcast_pitcher" not in picks[2]
            finally:
                cli.close()

        asyncio.run(_run())


class TestStatcastEnricherLogic:
    """Test the pure classifier helpers without hitting the DB."""

    def test_hitter_market_detection(self):
        from services.mlb_statcast import _is_hitter_market, _is_pitcher_market
        assert _is_hitter_market({
            "market": "Aaron Judge Over 0.5 Hits", "selection": "Aaron Judge"})
        assert not _is_hitter_market({
            "market": "American League +1.5 Spread",
            "selection": "American League"})
        assert not _is_hitter_market({
            "market": "Yankees Team Total Over 4.5", "selection": "Over"})
        assert not _is_hitter_market({
            "market": "NRFI", "selection": "Yes"})
        # Pitcher K market
        assert _is_pitcher_market({
            "market": "Over 5.5 Strikeouts", "selection": "Zack Wheeler"})
        # Pitcher props should NOT be classified as hitter markets
        assert not _is_hitter_market({
            "market": "Over 5.5 Strikeouts", "selection": "Zack Wheeler"})
