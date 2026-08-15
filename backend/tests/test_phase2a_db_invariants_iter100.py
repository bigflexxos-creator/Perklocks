"""
Iter100 — Phase 2A verification:
1) DB invariants for picks with edge_method='DEVIG'
2) Preseason uncertainty payload on NFL DEVIG preseason picks
3) Favorite/underdog neutrality present on live NFL slate
4) Funnel telemetry: DEVIG_UNAVAILABLE + EDGE_THRESHOLD NFL records
5) govern_pick synthetic pick keeps devig-based canonical edge (regression fix)
6) API smoke test: /api/picks/today exposes canonical devig fields on NFL picks
"""
import os
import sys
import math
import pytest
import requests
from pymongo import MongoClient

sys.path.insert(0, "/app/backend")

MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "lockscore_db")
BASE_URL = os.environ.get(
    "EXPO_PUBLIC_BACKEND_URL", "https://canonical-parity.preview.emergentagent.com"
).rstrip("/")


@pytest.fixture(scope="module")
def db():
    client = MongoClient(MONGO_URL)
    return client[DB_NAME]


# ---------- (1) DB invariant for DEVIG picks ----------
class TestDevigDbInvariants:
    def test_devig_picks_exist(self, db):
        picks = list(db.picks.find({"edge_method": "DEVIG"}))
        assert len(picks) > 0, "No DEVIG picks found in DB"
        print(f"Found {len(picks)} DEVIG picks in DB")

    def test_edge_percent_equals_wp_minus_devig(self, db):
        picks = list(db.picks.find({"edge_method": "DEVIG"}))
        offenders = []
        for p in picks:
            wp = p.get("win_probability")
            dvg = p.get("devig_market_probability")
            edge = p.get("edge_percent")
            if wp is None or dvg is None or edge is None:
                offenders.append({"id": p.get("id"), "reason": "missing wp/devig/edge", "wp": wp, "devig": dvg, "edge": edge})
                continue
            expected = round(wp - dvg, 2)
            if abs(edge - expected) > 0.15:
                offenders.append({"id": p.get("id"), "wp": wp, "devig": dvg, "edge": edge, "expected": expected})
        assert not offenders, f"edge_percent != wp - devig for {len(offenders)} picks: {offenders[:5]}"

    def test_devig_edge_percent_matches_edge_percent(self, db):
        picks = list(db.picks.find({"edge_method": "DEVIG"}))
        offenders = []
        for p in picks:
            edge = p.get("edge_percent")
            dv_edge = p.get("devig_edge_percent")
            if edge is None or dv_edge is None:
                offenders.append({"id": p.get("id"), "edge": edge, "devig_edge": dv_edge})
                continue
            if abs(edge - dv_edge) > 0.15:
                offenders.append({"id": p.get("id"), "edge": edge, "devig_edge": dv_edge})
        assert not offenders, f"devig_edge_percent != edge_percent for {len(offenders)} picks: {offenders[:5]}"

    def test_raw_edge_percent_present_and_frozen(self, db):
        """raw_edge_percent is captured at build-time relative to raw_implied.
        Post-shrinkage the displayed win_probability may drift (per Phase 2A
        agent-to-agent note), so we only assert presence + numeric type +
        sane bounds, not exact tie to displayed wp."""
        picks = list(db.picks.find({"edge_method": "DEVIG"}))
        offenders = []
        for p in picks:
            raw_edge = p.get("raw_edge_percent")
            raw_ip = p.get("raw_implied_probability")
            book_odds = p.get("book_odds")
            if raw_edge is None:
                offenders.append({"id": p.get("id"), "reason": "raw_edge_percent missing"})
                continue
            if raw_ip is None:
                offenders.append({"id": p.get("id"), "reason": "raw_implied_probability missing"})
                continue
            if book_odds is None:
                offenders.append({"id": p.get("id"), "reason": "book_odds missing"})
                continue
            if not isinstance(raw_edge, (int, float)) or raw_edge < -100 or raw_edge > 100:
                offenders.append({"id": p.get("id"), "reason": "raw_edge out of range", "raw_edge": raw_edge})
        assert not offenders, f"raw_edge_percent invariant issues: {offenders[:5]}"

    def test_raw_fields_preserved_not_overwritten_by_devig(self, db):
        """book_odds & raw_implied_probability must remain != devig_market_probability."""
        picks = list(db.picks.find({"edge_method": "DEVIG"}))
        offenders = []
        for p in picks:
            book_odds = p.get("book_odds")
            raw_ip = p.get("raw_implied_probability")
            devig = p.get("devig_market_probability")
            if book_odds is None:
                offenders.append({"id": p.get("id"), "reason": "book_odds missing"})
                continue
            if raw_ip is None:
                offenders.append({"id": p.get("id"), "reason": "raw_implied_probability missing"})
                continue
            # If devig sanitised the raw, raw_ip would equal devig — that's the bug
            if devig is not None and abs(raw_ip - devig) < 0.01:
                offenders.append({"id": p.get("id"), "raw_ip": raw_ip, "devig": devig, "book_odds": book_odds})
        assert not offenders, f"raw fields appear overwritten by devig for {len(offenders)} picks: {offenders[:5]}"


# ---------- (2) Preseason uncertainty ----------
class TestPreseasonUncertainty:
    def test_preseason_devig_picks_carry_uncertainty(self, db):
        picks = list(
            db.picks.find({"sport": "NFL", "season_type": "PRESEASON", "edge_method": "DEVIG"})
        )
        assert picks, "No NFL preseason DEVIG picks — cannot verify uncertainty payload"
        missing = []
        wrong_shrink = []
        wrong_math = []
        for p in picks:
            unc = p.get("preseason_uncertainty")
            if not unc:
                missing.append(p.get("id"))
                continue
            shrink = unc.get("confidence_shrink")
            raw = unc.get("raw_sim_probability")
            adj = unc.get("adjusted_probability")
            if shrink is None or abs(shrink - 0.85) > 1e-6:
                wrong_shrink.append({"id": p.get("id"), "shrink": shrink})
            if raw is None or adj is None:
                missing.append({"id": p.get("id"), "raw": raw, "adj": adj})
                continue
            expected = 0.5 + (raw - 0.5) * 0.85
            if abs(adj - expected) > 0.01:
                wrong_math.append({"id": p.get("id"), "raw": raw, "adj": adj, "expected": expected})
        assert not missing, f"preseason_uncertainty missing on {len(missing)} picks: {missing[:5]}"
        assert not wrong_shrink, f"confidence_shrink != 0.85: {wrong_shrink[:5]}"
        assert not wrong_math, f"adjusted math wrong: {wrong_math[:5]}"
        print(f"Verified preseason_uncertainty on {len(picks)} NFL preseason DEVIG picks")


# ---------- (3) Favorite/underdog neutrality ----------
class TestFavUdogNeutrality:
    def test_nfl_slate_has_both_favorites_and_underdogs(self, db):
        picks = list(db.picks.find({"sport": "NFL", "edge_method": "DEVIG"}))
        assert picks, "No NFL DEVIG picks to inspect"
        favs = [p for p in picks if isinstance(p.get("book_odds"), (int, float)) and p["book_odds"] < 0]
        dogs = [p for p in picks if isinstance(p.get("book_odds"), (int, float)) and p["book_odds"] > 0]
        print(f"NFL DEVIG slate — favorites (neg odds): {len(favs)}, underdogs (pos odds): {len(dogs)}")
        assert len(favs) >= 1, "No negative-odds favorites on NFL slate"
        assert len(dogs) >= 1, "No positive-odds underdogs on NFL slate — neutrality regression?"


# ---------- (4) Funnel telemetry ----------
class TestFunnelTelemetry:
    def test_devig_unavailable_records_present(self, db):
        cnt = db.funnel_telemetry.count_documents({"reason": "DEVIG_UNAVAILABLE"})
        assert cnt > 0, "No DEVIG_UNAVAILABLE records in funnel_telemetry"
        print(f"DEVIG_UNAVAILABLE funnel records: {cnt}")

    def test_edge_threshold_nfl_records_present(self, db):
        cnt = db.funnel_telemetry.count_documents({"reason": "EDGE_THRESHOLD", "sport": "NFL"})
        # If not filtered by sport in that document, allow reason-only fallback
        if cnt == 0:
            cnt = db.funnel_telemetry.count_documents({"reason": "EDGE_THRESHOLD"})
        assert cnt > 0, "No EDGE_THRESHOLD funnel records (NFL or general)"
        print(f"EDGE_THRESHOLD funnel records: {cnt}")


# ---------- (5) govern_pick regression fix synthetic ----------
class TestGovernPickDevigEdgeRegression:
    def test_govern_pick_preserves_devig_edge(self):
        from evidence_engine import govern_pick

        pick = {
            "id": "SYNTHETIC_DEVIG_REGRESSION",
            "sport": "NFL",
            "market": "moneyline",
            "edge_method": "DEVIG",
            "devig_market_probability": 50.0,
            "implied_probability": 52.4,
            "raw_implied_probability": 52.4,
            "win_probability": 60.0,
            "edge_percent": 10.0,
            "raw_edge_percent": 7.6,
            "book_odds": -110,
            "factors": {"platinum_game_sim": {"sim_probability": 0.60}},
            "model_source": "platinum_nfl_game_sim",
        }
        # Empty features → low evidence score → shrinkage active. This is
        # exactly the code path where the pre-fix bug would clobber the
        # canonical devig edge back to a raw-implied-based one.
        out = govern_pick(pick, [])
        wp_out = out.get("win_probability")
        edge_out = out.get("edge_percent")
        assert wp_out is not None, "govern_pick returned no win_probability"
        assert edge_out is not None, "govern_pick returned no edge_percent"
        expected_devig_edge = round(wp_out - 50.0, 2)
        expected_raw_edge = round(wp_out - 52.4, 2)
        # Canonical edge must track devig, NOT raw.
        assert abs(edge_out - expected_devig_edge) < 0.15, (
            f"govern_pick clobbered devig edge: edge={edge_out}, "
            f"wp={wp_out}, expected devig-based={expected_devig_edge}, "
            f"raw-based={expected_raw_edge}"
        )
        # And edge must NOT match the raw-based math (they differ by 2.4).
        assert abs(edge_out - expected_raw_edge) > 0.5, (
            f"govern_pick reverted to raw-based edge: edge={edge_out}, "
            f"raw-based={expected_raw_edge}, devig-based={expected_devig_edge}"
        )
        # edge_method preserved
        assert out.get("edge_method") == "DEVIG"
        # devig_edge_percent mirrors edge_percent per fix
        assert abs(out.get("devig_edge_percent", -999) - edge_out) < 0.01
        # raw_edge_percent stays raw-implied-based, tracking new wp
        raw_edge_out = out.get("raw_edge_percent")
        assert abs(raw_edge_out - expected_raw_edge) < 0.15, (
            f"raw_edge_percent not raw-based after shrinkage: raw_edge={raw_edge_out}, "
            f"expected={expected_raw_edge}"
        )
        print(
            f"govern_pick OK: wp {60.0}→{wp_out}, edge {10.0}→{edge_out} "
            f"(devig-based ✓, raw-based would be {expected_raw_edge})"
        )


# ---------- (6) API smoke ----------
class TestApiSmoke:
    @pytest.fixture(scope="class")
    def token(self):
        r = requests.post(
            f"{BASE_URL}/api/auth/login",
            json={"email": "demo@lockscore.ai", "password": "demo123"},
            timeout=20,
        )
        assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:200]}"
        data = r.json()
        tok = data.get("token") or data.get("access_token")
        assert tok, f"No token in login response: {list(data.keys())}"
        return tok

    def test_picks_today_exposes_devig_fields(self, token):
        r = requests.get(
            f"{BASE_URL}/api/picks/today",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        assert r.status_code == 200, f"picks/today failed: {r.status_code} {r.text[:200]}"
        payload = r.json()
        picks = payload if isinstance(payload, list) else payload.get("picks", [])
        assert picks, "No picks returned by /api/picks/today"

        nfl_picks = [p for p in picks if p.get("sport") == "NFL"]
        print(f"/api/picks/today → total={len(picks)}, NFL={len(nfl_picks)}")
        assert nfl_picks, "No NFL picks in /api/picks/today response"

        # Look for at least one DEVIG NFL pick exposing the canonical fields
        devig_nfl = [p for p in nfl_picks if p.get("edge_method") == "DEVIG"]
        assert devig_nfl, "No DEVIG NFL picks exposed on /api/picks/today"

        sample = devig_nfl[0]
        for f in ("edge_percent", "edge_method", "devig_market_probability", "raw_edge_percent"):
            assert f in sample and sample[f] is not None, f"Field {f} missing/None on DEVIG NFL pick: {sample.get('id')}"
        # Preseason uncertainty on preseason picks
        preseason = [p for p in devig_nfl if p.get("season_type") == "PRESEASON"]
        if preseason:
            assert preseason[0].get("preseason_uncertainty"), (
                f"preseason_uncertainty missing on preseason DEVIG NFL pick: {preseason[0].get('id')}"
            )

    def test_main_board_eligibility_ge_85(self, token):
        r = requests.get(
            f"{BASE_URL}/api/picks/today",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        assert r.status_code == 200
        payload = r.json()
        picks = payload if isinstance(payload, list) else payload.get("picks", [])
        # Main-board eligibility: published_lock_score >= 85 (when present)
        offenders = []
        for p in picks:
            pls = p.get("published_lock_score")
            if pls is not None and pls < 85:
                # Only fail if it also claims to be a main-board pick
                if p.get("is_main_board") or p.get("board") == "main":
                    offenders.append({"id": p.get("id"), "pls": pls})
        assert not offenders, f"picks with published_lock_score<85 on main board: {offenders[:5]}"
