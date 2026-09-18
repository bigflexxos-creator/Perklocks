"""Iteration 143 — Probability Authority shadow-mode HTTP contract + board regression."""
import os
import pytest
import requests

BASE = "https://canonical-parity.preview.emergentagent.com"
EMAIL = "demo@lockscore.ai"
PWD = "demo123"

REC_PICK = "6d76acfa-6c03-560f-b0b8-8a1626adeee6"
REC_LOCK = 97.1


@pytest.fixture(scope="module")
def token():
    r = requests.post(f"{BASE}/api/auth/login", json={"email": EMAIL, "password": PWD}, timeout=30)
    assert r.status_code == 200, r.text
    j = r.json()
    return j.get("access_token") or j.get("token")


@pytest.fixture(scope="module")
def auth_h(token):
    return {"Authorization": f"Bearer {token}"}


# ── Shadow report ──────────────────────────────────────────────────────
def test_shadow_report_unauth_returns_401():
    r = requests.get(f"{BASE}/api/probability-authority/shadow-report", timeout=30)
    assert r.status_code == 401, r.text


def test_shadow_report_shape_and_shadow_mode(auth_h):
    r = requests.get(f"{BASE}/api/probability-authority/shadow-report", headers=auth_h, timeout=60)
    assert r.status_code == 200, r.text
    body = r.json()

    top_keys = {"generated_at", "authority_version", "closure_version", "mode",
                "families", "cfb_total_sigma", "summary"}
    missing = top_keys - set(body.keys())
    assert not missing, f"missing top-level keys: {missing}"
    assert body["mode"] == "shadow", body["mode"]

    fams = body["families"]
    assert isinstance(fams, list) and len(fams) > 0
    fam0 = fams[0]
    required_fam_keys = {"family", "live", "registry", "raw_metrics",
                          "calibrated_metrics", "readiness"}
    missing_f = required_fam_keys - set(fam0.keys())
    assert not missing_f, f"family missing keys: {missing_f}"

    live_keys = {"n", "lock85", "eligible", "ineligible_reasons",
                 "mean_raw", "mean_after_closure", "mean_calibrated",
                 "max_abs_delta_pts", "closure_methods", "evidence"}
    missing_l = live_keys - set(fam0["live"].keys())
    assert not missing_l, f"live missing: {missing_l}"

    assert "verdict" in fam0["readiness"]

    # No family promoted; summary.promoted == 0
    for f in fams:
        assert f["readiness"]["verdict"] != "PROMOTED", f
    assert body["summary"].get("promoted", -1) == 0, body["summary"]


def test_shadow_report_soccer_closure_applied(auth_h):
    r = requests.get(f"{BASE}/api/probability-authority/shadow-report", headers=auth_h, timeout=60)
    assert r.status_code == 200
    fams = r.json()["families"]
    soccer_fams = [f for f in fams if f["family"] in ("SOCCER_SHOTS", "SOCCER_GOALSCORER")]
    assert soccer_fams, "no SOCCER_SHOTS/SOCCER_GOALSCORER family in shadow report"
    matches = [
        f for f in soccer_fams
        if "soccer_minutes_lineup.v1" in (f["live"].get("closure_methods") or [])
        and f["live"].get("mean_after_closure") is not None
        and f["live"].get("mean_raw") is not None
        and f["live"]["mean_after_closure"] < f["live"]["mean_raw"]
    ]
    assert matches, f"no soccer family with method=soccer_minutes_lineup.v1 AND mean_after_closure<mean_raw. Got: {[(f['family'], f['live'].get('closure_methods'), f['live'].get('mean_raw'), f['live'].get('mean_after_closure')) for f in soccer_fams]}"


def test_shadow_report_cfb_total_closure(auth_h):
    r = requests.get(f"{BASE}/api/probability-authority/shadow-report", headers=auth_h, timeout=60)
    assert r.status_code == 200
    fams = r.json()["families"]
    cfb_total = [f for f in fams if f["family"] == "CFB_TOTAL"]
    assert cfb_total, "CFB_TOTAL family missing"
    methods = cfb_total[0]["live"].get("closure_methods") or []
    assert "cfb_total_predictive.v1" in methods, f"expected cfb_total_predictive.v1 in {methods}"


# ── Refit endpoint ─────────────────────────────────────────────────────
def test_refit_returns_families_and_cfb_sigma(auth_h):
    r = requests.post(f"{BASE}/api/probability-authority/refit", headers=auth_h, timeout=180)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "families" in body and isinstance(body["families"], dict), body
    cts = body.get("cfb_total_sigma")
    assert isinstance(cts, dict), cts
    for k in ("n", "sigma", "sigma_raw", "status"):
        assert k in cts, f"cfb_total_sigma missing {k}: {cts}"

    # Shadow report still healthy afterwards
    r2 = requests.get(f"{BASE}/api/probability-authority/shadow-report", headers=auth_h, timeout=60)
    assert r2.status_code == 200


# ── Board / pick regressions (shadow mode → unchanged) ─────────────────
def test_picks_today_lite_all_lock_ge_85(auth_h):
    r = requests.get(f"{BASE}/api/picks/today?lite=true", headers=auth_h, timeout=60)
    assert r.status_code == 200, r.text
    body = r.json()
    picks = body.get("picks") if isinstance(body, dict) else body
    assert isinstance(picks, list) and len(picks) > 0, body
    bad = [p for p in picks if float(p.get("lock_score", 0)) < 85.0]
    assert not bad, f"{len(bad)} picks below 85 lock_score. Sample: {bad[:3]}"


def test_record_pick_unchanged(auth_h):
    r = requests.get(f"{BASE}/api/picks/{REC_PICK}", headers=auth_h, timeout=30)
    assert r.status_code == 200, r.text
    pick = r.json()
    ls = float(pick.get("lock_score"))
    assert abs(ls - REC_LOCK) < 0.05, f"lock_score changed: {ls} vs {REC_LOCK}"
    # win_probability still present
    assert pick.get("win_probability") is not None


def test_record_pick_hi_available_with_data(auth_h):
    r = requests.get(
        f"{BASE}/api/picks/{REC_PICK}/historical-intelligence",
        headers=auth_h, timeout=30
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("status") == "AVAILABLE_WITH_DATA", body
    assert body.get("sample_size") == 10, body


# ── Direct python check requested in review ────────────────────────────
def test_cfb_over_probability_student_t_range():
    from services.cfb_game_model import cfb_over_probability
    from services import cfb_total_residuals as cres

    # Legacy fixed-sigma normal was 0.9266; Student-t must be lower but within 0.89-0.92
    cres._cache.update({"sigma_raw": None, "n": 0})
    p = cfb_over_probability(70.0, 46.5, True, 16.2)
    assert 0.89 <= p <= 0.92, f"cfb_over_probability(70,46.5,True,16.2)={p:.4f} not in [0.89,0.92]"

    # Over + under sums to 1.0 on (55,52,13.5)
    o = cfb_over_probability(55, 52, True, 13.5)
    u = cfb_over_probability(55, 52, False, 13.5)
    assert abs(o + u - 1.0) < 1e-6, f"{o}+{u}={o+u}"


def test_soccer_lineup_out_and_confirmed_evaluate():
    from services import probability_authority as pa

    base = {
        "sport": "Soccer",
        "market": "Diego Rossi Anytime Goal Scorer",
        "player_name": "Diego Rossi",
        "win_probability": 35.0,
        "book_odds": 180,
    }

    out = pa.evaluate({**base, "lineup_status": {"status": "out"}})
    assert out.publication_eligible is False
    assert out.fallback_reason == "LINEUP_OUT"

    conf = pa.evaluate({**base, "lineup_status": {"status": "confirmed"}})
    assert conf.publication_eligible is True
    assert 0.30 <= conf.closure_probability <= 0.35, conf.closure_probability
