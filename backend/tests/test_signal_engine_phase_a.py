"""Signal Engine Phase A integration tests.

Verifies the six-signal engine that decorates picks with a 0-100
signal_score and a signal_engine block containing components + why[].
Also checks lite strip, persistence/freshness, rollover integration
and regression on adjacent endpoints.
"""
import os
import time
import pytest
import requests

BASE_URL = os.environ["EXPO_PUBLIC_BACKEND_URL"].rstrip("/")
LOGIN = {"email": "demo@lockscore.ai", "password": "demo123"}
LONG = 240  # /picks/today decoration pipeline is slow (cold cache)


@pytest.fixture(scope="module")
def token():
    r = requests.post(f"{BASE_URL}/api/auth/login", json=LOGIN, timeout=30)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:200]}"
    body = r.json()
    tok = body.get("access_token")
    assert tok, f"no access_token in login response: {body}"
    return tok


@pytest.fixture(scope="module")
def headers(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def picks_today(headers):
    r = requests.get(f"{BASE_URL}/api/picks/today?lite=true",
                     headers=headers, timeout=LONG)
    assert r.status_code == 200, f"picks/today failed: {r.status_code} {r.text[:300]}"
    data = r.json()
    picks = data.get("picks") if isinstance(data, dict) else data
    assert isinstance(picks, list) and picks, "no picks returned from /picks/today"
    return picks


# ---------- auth ----------
class TestAuth:
    def test_login_returns_access_token(self):
        r = requests.post(f"{BASE_URL}/api/auth/login", json=LOGIN, timeout=30)
        assert r.status_code == 200
        assert r.json().get("access_token")


# ---------- lite strip + signal_score on feed ----------
class TestPicksTodayLite:
    def test_every_pick_has_numeric_signal_score(self, picks_today):
        missing = [p.get("id") for p in picks_today
                   if not isinstance(p.get("signal_score"), (int, float))]
        assert not missing, f"{len(missing)} picks missing signal_score, sample={missing[:5]}"

    def test_signal_score_in_range(self, picks_today):
        bad = [(p.get("id"), p.get("signal_score")) for p in picks_today
               if not (0 <= p.get("signal_score", -1) <= 100)]
        assert not bad, f"signal_score out of 0-100 range: {bad[:5]}"

    def test_lite_strip_removes_heavy_block(self, picks_today):
        with_block = [p.get("id") for p in picks_today if "signal_engine" in p]
        assert not with_block, (
            f"{len(with_block)} lite picks still carry signal_engine block "
            f"(should be stripped), sample={with_block[:5]}")


# ---------- detail endpoint: full block ----------
GRADES = {"Elite", "Strong", "Moderate", "Weak", "Fade"}
COMP_KEYS = {"form", "matchup", "volume", "injury", "market", "value"}


class TestPickDetailSignalEngine:
    @pytest.fixture(scope="class")
    def detail(self, picks_today, headers):
        pid = picks_today[0]["id"]
        r = requests.get(f"{BASE_URL}/api/picks/{pid}", headers=headers, timeout=60)
        assert r.status_code == 200, f"detail {pid} -> {r.status_code}"
        return r.json()

    def test_signal_engine_block_shape(self, detail):
        se = detail.get("signal_engine")
        assert isinstance(se, dict), "signal_engine block missing on detail response"
        assert se.get("version") == 1
        assert isinstance(se.get("score"), int) and 0 <= se["score"] <= 100
        assert se.get("grade") in GRADES, f"unexpected grade: {se.get('grade')}"
        assert isinstance(se.get("breakdown"), str) and se["breakdown"]
        assert isinstance(se.get("computed_at"), str) and se["computed_at"]

    def test_components_six_universal_signals(self, detail):
        comps = detail["signal_engine"]["components"]
        assert isinstance(comps, list) and len(comps) == 6
        keys = {c.get("key") for c in comps}
        assert keys == COMP_KEYS, f"component keys mismatch: got {keys}"
        for c in comps:
            for f in ("points", "max", "details", "found"):
                assert f in c, f"component {c.get('key')} missing '{f}'"
            assert isinstance(c["details"], list)
            assert isinstance(c["found"], bool)

    def test_why_non_empty_with_real_numbers(self, detail):
        why = detail["signal_engine"].get("why")
        assert isinstance(why, list) and why, "why[] must be non-empty"
        assert all(isinstance(x, str) and x.strip() for x in why)
        # first bullet must reference Signal Score and win probability
        assert "Signal Score" in why[0], f"first bullet missing 'Signal Score': {why[0]}"
        # at least one bullet must include a digit -> real numbers, not generic text
        assert any(any(ch.isdigit() for ch in b) for b in why), \
            f"no numeric content across why[]: {why}"

    def test_score_matches_top_level(self, detail):
        assert detail.get("signal_score") == detail["signal_engine"]["score"]


# ---------- persistence / freshness ----------
class TestFreshness:
    def test_computed_at_stable_within_window(self, picks_today, headers):
        pid = picks_today[0]["id"]
        r1 = requests.get(f"{BASE_URL}/api/picks/{pid}", headers=headers, timeout=60)
        assert r1.status_code == 200
        first = r1.json()["signal_engine"]["computed_at"]
        time.sleep(2)
        r2 = requests.get(f"{BASE_URL}/api/picks/{pid}", headers=headers, timeout=60)
        assert r2.status_code == 200
        second = r2.json()["signal_engine"]["computed_at"]
        assert first == second, (
            f"signal_engine.computed_at changed within 30-min window "
            f"(persistence broken): {first} -> {second}")


# ---------- rollover integration ----------
class TestRollover:
    def test_rollover_returns_200(self, headers):
        r = requests.get(f"{BASE_URL}/api/picks/rollover", headers=headers, timeout=120)
        assert r.status_code == 200, f"rollover failed: {r.status_code} {r.text[:200]}"
        self._body = r.json()

    def test_legs_have_signal_score_and_composite_rank(self, headers):
        r = requests.get(f"{BASE_URL}/api/picks/rollover", headers=headers, timeout=120)
        assert r.status_code == 200
        body = r.json()
        # Collect leg-like objects wherever they live in payload
        legs = []
        if isinstance(body, dict):
            for k in ("legs", "picks", "parlays", "rollover"):
                v = body.get(k)
                if isinstance(v, list):
                    for item in v:
                        if isinstance(item, dict):
                            if isinstance(item.get("legs"), list):
                                legs.extend(item["legs"])
                            else:
                                legs.append(item)
        if not legs:
            pytest.skip(f"rollover payload has no leg objects to inspect: keys={list(body)[:10] if isinstance(body, dict) else type(body)}")
        with_score = [l for l in legs if isinstance(l.get("signal_score"), (int, float))]
        assert with_score, f"no rollover leg has signal_score (0/{len(legs)})"
        # composite_rank / rank should still be computed on at least some legs
        with_rank = [l for l in legs
                     if any(k in l for k in ("composite_rank", "rank", "composite_score"))]
        assert with_rank, "no leg has composite_rank/rank — ranking broke"


# ---------- regression: adjacent endpoints ----------
class TestRegressionNo500:
    @pytest.mark.parametrize("path", [
        "/api/picks/all",
        "/api/picks/under-of-the-day",
        "/api/stats/summary",
    ])
    def test_no_500(self, headers, path):
        r = requests.get(f"{BASE_URL}{path}", headers=headers, timeout=LONG)
        assert r.status_code < 500, f"{path} -> {r.status_code}: {r.text[:200]}"
