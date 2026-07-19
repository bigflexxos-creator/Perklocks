"""Iter 79 — Phase 2 Data Enrichment backend regression suite.

Verifies:
  1. No regression on /api/picks/today (200, populated, <10s).
  2. Signal engine version bumped to 5 (via /api/picks/{id} deep-dive).
  3. Signal_score distribution stays healthy (min<40, max>80, multi-sport spread).
  4. Backend logs contain no unhandled exceptions from Phase 2 modules.
  5. Enrichers no-op gracefully when upstream data is missing (fields=None, no crash).
  6. Static-data enrichers: /api/picks/{id} returns valid mlb_deep/soccer_deep/tennis_deep
     signal-engine components for each sport.
  7. Phase 2 enricher unit tests (isolated function calls with synthetic picks).
"""
from __future__ import annotations

import os
import re
import time
import pytest
import requests

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    # Fall back to the value shipped in the frontend .env for local runs.
    try:
        with open("/app/frontend/.env") as f:
            for line in f:
                if line.startswith("EXPO_PUBLIC_BACKEND_URL="):
                    BASE_URL = line.split("=", 1)[1].strip().rstrip("/")
                    break
    except Exception:
        pass
assert BASE_URL, "Backend URL not resolved from env"

DEMO_EMAIL = "demo@lockscore.ai"
DEMO_PASSWORD = "demo123"


@pytest.fixture(scope="module")
def token():
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD},
        timeout=15,
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:200]}"
    tok = r.json().get("access_token")
    assert tok, "no access_token in login response"
    return tok


@pytest.fixture(scope="module")
def auth_headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


@pytest.fixture(scope="module")
def picks_today(auth_headers):
    t0 = time.time()
    r = requests.get(
        f"{BASE_URL}/api/picks/today", headers=auth_headers, timeout=15
    )
    elapsed = time.time() - t0
    assert r.status_code == 200, f"/picks/today {r.status_code} {r.text[:200]}"
    body = r.json()
    picks = body if isinstance(body, list) else body.get("picks") or body.get("items") or []
    return {"picks": picks, "elapsed": elapsed, "raw": body}


# ── 1. No regression: /api/picks/today ────────────────────────────
class TestPicksToday:
    def test_status_200_and_populated(self, picks_today):
        picks = picks_today["picks"]
        assert len(picks) > 0, "picks list is empty"

    def test_response_time_under_10s(self, picks_today):
        # Warm cache — allow up to 10s per PRD.
        assert picks_today["elapsed"] < 10.0, (
            f"/picks/today took {picks_today['elapsed']:.2f}s (>10s budget)"
        )

    def test_picks_have_expected_fields(self, picks_today):
        picks = picks_today["picks"]
        sample = picks[0]
        for field in ("id", "sport", "market"):
            assert field in sample, f"missing {field} in pick sample: keys={list(sample.keys())[:10]}"

    def test_multi_sport_coverage(self, picks_today):
        sports = {(p.get("sport") or "").upper() for p in picks_today["picks"]}
        sports.discard("")
        assert len(sports) >= 2, f"expected multi-sport slate, got {sports}"


# ── 2. Signal engine version bump ─────────────────────────────────
class TestSignalEngineVersion:
    def test_version_bumped_to_5_on_deep_dive(self, picks_today, auth_headers):
        picks = picks_today["picks"]
        assert picks, "no picks to inspect"
        versions = []
        checked_ids = []
        # Sample first 8 picks across the slate.
        sample = picks[:8]
        for p in sample:
            pid = p.get("id")
            if not pid:
                continue
            r = requests.get(
                f"{BASE_URL}/api/picks/{pid}", headers=auth_headers, timeout=15
            )
            if r.status_code != 200:
                continue
            body = r.json()
            se = body.get("signal_engine") or {}
            v = se.get("version")
            if v is not None:
                versions.append(v)
                checked_ids.append(pid)
        assert versions, f"no signal_engine block returned across {len(sample)} deep-dives"
        # Majority (>=60%) must be v5.
        v5_frac = sum(1 for v in versions if v == 5) / len(versions)
        assert v5_frac >= 0.6, (
            f"only {v5_frac*100:.0f}% of deep-dives on v5 "
            f"(versions={versions[:10]}, ids={checked_ids[:4]})"
        )


# ── 3. Signal score distribution ──────────────────────────────────
class TestSignalScoreDistribution:
    def test_score_spread_not_collapsed(self, picks_today):
        scores = [
            p.get("signal_score") for p in picks_today["picks"]
            if isinstance(p.get("signal_score"), (int, float))
        ]
        assert len(scores) >= 10, f"only {len(scores)} picks have signal_score"
        lo, hi = min(scores), max(scores)
        assert lo < 40, f"signal_score min {lo} — floor collapsed"
        assert hi > 80, f"signal_score max {hi} — ceiling collapsed"

    def test_score_range_0_to_99(self, picks_today):
        scores = [
            p.get("signal_score") for p in picks_today["picks"]
            if isinstance(p.get("signal_score"), (int, float))
        ]
        bad = [s for s in scores if s < 0 or s > 100]
        assert not bad, f"signal_score outside 0-100 band: {bad[:5]}"

    def test_score_spread_across_sports(self, picks_today):
        by_sport: dict[str, list] = {}
        for p in picks_today["picks"]:
            s = (p.get("sport") or "").upper()
            v = p.get("signal_score")
            if isinstance(v, (int, float)):
                by_sport.setdefault(s, []).append(v)
        with_data = {k: v for k, v in by_sport.items() if len(v) >= 3}
        assert len(with_data) >= 2, (
            f"scores not spread across sports: {[(k, len(v)) for k, v in by_sport.items()]}"
        )


# ── 4. Backend log inspection — no Phase 2 exceptions ─────────────
class TestBackendLogsClean:
    _MODULES = (
        "mlb_park_hand", "mlb_pitch_mix", "soccer_rolling_xg",
        "soccer_context", "tennis_first_set",
    )

    def test_no_unhandled_exceptions_in_stderr(self, picks_today):
        # Trigger a fresh log entry first.
        _ = picks_today
        try:
            with open("/var/log/supervisor/backend.err.log") as f:
                # Read only the last ~500 KB to avoid old noise.
                f.seek(0, 2)
                end = f.tell()
                start = max(0, end - 500_000)
                f.seek(start)
                tail = f.read()
        except FileNotFoundError:
            pytest.skip("backend.err.log not present in this environment")
        # An exception-caused traceback would contain 'Traceback' plus the module name.
        for mod in self._MODULES:
            pattern = re.compile(
                rf"Traceback[\s\S]{{0,1500}}{mod}", re.MULTILINE
            )
            matches = pattern.findall(tail)
            assert not matches, (
                f"Traceback referencing {mod} found in backend.err.log:\n"
                f"{matches[0][:600]}"
            )


# ── 5. Enrichers no-op cleanly when upstream data is missing ──────
class TestGracefulNoOp:
    def test_soccer_pick_without_form_has_no_xg_rolling_crash(self, picks_today):
        soccer_picks = [
            p for p in picks_today["picks"]
            if (p.get("sport") or "").lower() == "soccer"
        ]
        if not soccer_picks:
            pytest.skip("no soccer picks on slate")
        # xg_rolling either absent, None, or a dict — never a crash marker.
        for p in soccer_picks[:20]:
            xgr = p.get("xg_rolling", None)
            assert xgr is None or isinstance(xgr, dict), (
                f"xg_rolling has bad type: {type(xgr).__name__} on pick {p.get('id')}"
            )

    def test_mlb_pick_without_batter_hand_has_no_park_hand_crash(self, picks_today):
        mlb_picks = [
            p for p in picks_today["picks"]
            if (p.get("sport") or "").upper() == "MLB"
        ]
        if not mlb_picks:
            pytest.skip("no MLB picks on slate")
        for p in mlb_picks[:20]:
            fac = p.get("park_hr_hand_factor", None)
            # Either not attached, None, or a number in the sane range.
            assert fac is None or (isinstance(fac, (int, float)) and 70 <= fac <= 140), (
                f"park_hr_hand_factor out of range: {fac} on pick {p.get('id')}"
            )

    def test_tennis_pick_first_set_shape_ok(self, picks_today):
        tennis_picks = [
            p for p in picks_today["picks"]
            if (p.get("sport") or "").lower() == "tennis"
        ]
        if not tennis_picks:
            pytest.skip("no tennis picks on slate")
        for p in tennis_picks[:20]:
            fs = p.get("tennis_first_set", None)
            assert fs is None or isinstance(fs, dict), (
                f"tennis_first_set bad type: {type(fs).__name__}"
            )


# ── 6. Static-data enrichers still return valid deep-dive blocks ──
class TestDeepComponents:
    _COMPONENT_KEYS = {
        "MLB":    "mlb_deep",
        "SOCCER": "soccer_deep",
        "TENNIS": "tennis_deep",
    }

    def _find_component(self, components, key):
        if isinstance(components, list):
            for c in components:
                if isinstance(c, dict) and c.get("key") == key:
                    return c
        elif isinstance(components, dict):
            return components.get(key)
        return None

    @pytest.mark.parametrize("sport", ["MLB", "SOCCER", "TENNIS"])
    def test_deep_component_present_and_valid(self, picks_today, auth_headers, sport):
        picks = [
            p for p in picks_today["picks"]
            if (p.get("sport") or "").upper() == sport
        ]
        if not picks:
            pytest.skip(f"no {sport} picks on slate")
        key = self._COMPONENT_KEYS[sport]
        checked = 0
        for p in picks[:6]:
            pid = p.get("id")
            if not pid:
                continue
            r = requests.get(
                f"{BASE_URL}/api/picks/{pid}", headers=auth_headers, timeout=15
            )
            if r.status_code != 200:
                continue
            body = r.json()
            se = body.get("signal_engine") or {}
            comps = se.get("components")
            comp = self._find_component(comps, key)
            if comp is None:
                continue
            checked += 1
            pts = comp.get("points")
            if pts is not None:
                assert isinstance(pts, (int, float)), (
                    f"{key} points has bad type: {type(pts).__name__}"
                )
                assert -100 <= pts <= 100, (
                    f"{key} points out of range: {pts} on pick {pid}"
                )
        assert checked >= 1, (
            f"no {sport} pick had a '{key}' component in signal_engine.components "
            f"(sampled {len(picks[:6])} picks)"
        )


# ── 7. Isolated unit tests on the Phase 2 modules (synthetic data) ─
class TestPhase2ModuleUnits:
    def test_park_hand_yankees_lhb_boost(self):
        import sys
        sys.path.insert(0, "/app/backend")
        from services.enrichment.mlb_park_hand import (
            enrich_pick_with_hand_factor, park_hr_by_hand,
        )
        assert park_hr_by_hand("New York Yankees", "L") == 118
        assert park_hr_by_hand("San Francisco Giants", "L") == 85
        assert park_hr_by_hand("Unknown Team", "L") is None
        assert park_hr_by_hand("New York Yankees", "X") is None

        pick = {
            "sport": "MLB", "batter_hand": "L",
            "event": "Boston Red Sox @ New York Yankees",
        }
        out = enrich_pick_with_hand_factor(pick)
        assert out.get("park_hr_hand_factor") == 118
        assert out.get("park_hr_hand_side") == "L"

    def test_park_hand_no_op_missing_hand(self):
        import sys
        sys.path.insert(0, "/app/backend")
        from services.enrichment.mlb_park_hand import enrich_pick_with_hand_factor
        pick = {"sport": "MLB", "event": "Boston Red Sox @ New York Yankees"}
        out = enrich_pick_with_hand_factor(pick)
        assert "park_hr_hand_factor" not in out

    def test_pitch_mix_edge_computes(self):
        import sys
        sys.path.insert(0, "/app/backend")
        from services.enrichment.mlb_pitch_mix import enrich_pick_with_pitch_mix
        pick = {
            "sport": "MLB", "market": "Home Runs 0.5",
            "stuff_plus": {"pitch_mix": {"4-Seam": 0.60, "Slider": 0.30, "Changeup": 0.10}},
            "statcast_batter": {"xwoba_vs": {"fastball": 0.400, "breaking": 0.250, "offspeed": 0.300}},
        }
        out = enrich_pick_with_pitch_mix(pick)
        assert isinstance(out.get("pitch_mix_edge"), float)
        # Batter crushes fastballs (+.090 vs .310), pitcher throws 60% FB → positive edge.
        assert out["pitch_mix_edge"] > 0

    def test_pitch_mix_no_op_wrong_market(self):
        import sys
        sys.path.insert(0, "/app/backend")
        from services.enrichment.mlb_pitch_mix import enrich_pick_with_pitch_mix
        pick = {
            "sport": "MLB", "market": "Strikeouts 6.5",
            "stuff_plus": {"pitch_mix": {"4-Seam": 0.60}},
            "statcast_batter": {"xwoba_vs": {"fastball": 0.400}},
        }
        out = enrich_pick_with_pitch_mix(pick)
        assert "pitch_mix_edge" not in out

    def test_soccer_context_set_piece(self):
        import sys
        sys.path.insert(0, "/app/backend")
        from services.enrichment.soccer_context import (
            enrich_pick_with_context, set_piece_duties, manager_style,
            high_pressure_context,
        )
        assert "PK" in set_piece_duties("Harry Kane")
        assert manager_style("Jurgen Klopp") == "attacking"
        assert manager_style("Diego Simeone") == "defensive"

        pick = {
            "sport": "soccer",
            "player_name": "Harry Kane",
            "home_manager": "Pep Guardiola",
            "away_manager": "Diego Simeone",
            "event": "Man City vs Atletico Madrid",
            "round": "Champions League Final",
        }
        out = enrich_pick_with_context(pick)
        ctx = out.get("soccer_context") or {}
        assert "PK" in (ctx.get("set_piece_duties") or [])
        assert ctx.get("manager_style_home") == "attacking"
        assert ctx.get("manager_style_away") == "defensive"
        assert ctx.get("pressure") == "high"

    def test_soccer_context_no_op(self):
        import sys
        sys.path.insert(0, "/app/backend")
        from services.enrichment.soccer_context import enrich_pick_with_context
        pick = {"sport": "soccer", "player_name": "Unknown Guy"}
        out = enrich_pick_with_context(pick)
        assert "soccer_context" not in out

    def test_soccer_rolling_xg_form_proxy_fallback(self):
        import sys, asyncio
        sys.path.insert(0, "/app/backend")
        from services.enrichment.soccer_rolling_xg import enrich_pick_with_rolling_xg
        pick = {
            "sport": "soccer",
            "home_team": "Real Madrid", "away_team": "Barcelona",
            "soccer_form": {
                "home": {"gf_avg": 2.3, "ga_avg": 0.8, "n_matches": 10},
                "away": {"gf_avg": 2.5, "ga_avg": 1.1, "n_matches": 10},
            },
        }
        # No DB — fallback proxy should still fire.
        out = asyncio.run(enrich_pick_with_rolling_xg(None, pick))
        xgr = out.get("xg_rolling") or {}
        assert xgr.get("home", {}).get("xg_avg") == 2.3
        assert xgr.get("away", {}).get("xg_avg") == 2.5

    def test_soccer_rolling_xg_no_op_missing_form(self):
        import sys, asyncio
        sys.path.insert(0, "/app/backend")
        from services.enrichment.soccer_rolling_xg import enrich_pick_with_rolling_xg
        pick = {
            "sport": "soccer",
            "home_team": "Team A", "away_team": "Team B",
        }
        out = asyncio.run(enrich_pick_with_rolling_xg(None, pick))
        assert "xg_rolling" not in out

    def test_tennis_first_set_computes(self):
        import sys
        sys.path.insert(0, "/app/backend")
        from services.enrichment.tennis_first_set import enrich_pick_with_first_set
        pick = {
            "sport": "tennis",
            "tennis_sackmann_stats": {
                "pick":     {"return_points_won_pct": 40.0, "break_pct": 25.0, "return_games_won_pct": 30.0},
                "opponent": {"return_points_won_pct": 32.0, "break_pct": 18.0, "return_games_won_pct": 22.0},
            },
        }
        out = enrich_pick_with_first_set(pick)
        fs = out.get("tennis_first_set") or {}
        assert isinstance(fs.get("pick_rpw_1st"), float)
        assert isinstance(fs.get("opp_rpw_1st"), float)
        assert isinstance(fs.get("edge_1st"), float)
        assert fs["edge_1st"] > 0  # pick strictly stronger

    def test_tennis_first_set_no_op(self):
        import sys
        sys.path.insert(0, "/app/backend")
        from services.enrichment.tennis_first_set import enrich_pick_with_first_set
        pick = {"sport": "tennis"}
        out = enrich_pick_with_first_set(pick)
        assert "tennis_first_set" not in out


# ── Signal engine integration: full compute path smoke ────────────
class TestSignalEngineWiring:
    def test_signal_version_constant(self):
        import sys
        sys.path.insert(0, "/app/backend")
        from services.signal_engine import engine
        assert engine.SIGNAL_VERSION == 5

    def test_compute_signals_does_not_crash_on_empty_pick(self):
        import sys, asyncio
        sys.path.insert(0, "/app/backend")
        from services.signal_engine.engine import compute_signals
        pick = {"id": "TEST_empty", "sport": "MLB", "market": "Home Runs 0.5"}
        # No DB, no upstream data — should return gracefully without raising.
        try:
            out = asyncio.run(compute_signals(None, pick))
            assert isinstance(out, dict)
        except Exception as e:
            pytest.fail(f"compute_signals raised on empty pick: {e!r}")
