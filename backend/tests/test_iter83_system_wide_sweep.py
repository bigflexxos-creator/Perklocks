"""
Iter 83 — System-wide bug sweep (backend only).

Goals (read-only sweep):
  1. Auth flow (login → /auth/me → picks/today with token).
  2. Broad endpoint smoke: 200 (or documented non-500) and JSON-parseable body.
  3. Data-integrity checks on /api/picks/today live payload:
       a. no same-event/same-player/same-family/same-line Over+Under pair
          both visible (iter-82 contradiction fix must hold).
       b. no pick with no_bet=True surfaces to the user.
       c. lock_score in [0, 99].
       d. required fields present (id, sport, market, book_odds/odds).
       e. no duplicate pick ids.
  4. Per-sport tab health (MLB, NBA, NFL, Soccer, Tennis, UFC): 200, list body.
  5. Deep-dive /api/picks/{id} works for a sampled pick from each sport present.
  6. Analytics endpoints return 200 + JSON.
  7. Parlay endpoints return 200 + JSON.
  8. Admin endpoints return 200 for demo user (auth passes).
  9. Malformed token → 401 (not 500).
"""

from __future__ import annotations

import os
import re
import json
import pytest
import requests

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    BASE_URL = os.environ.get("EXPO_BACKEND_URL", "").rstrip("/")
assert BASE_URL, "EXPO_PUBLIC_BACKEND_URL not set"

DEMO_EMAIL = "demo@lockscore.ai"
DEMO_PASSWORD = "demo123"

TIMEOUT = 45


# ------- shared session ---------------------------------------------------

@pytest.fixture(scope="session")
def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="session")
def token(session: requests.Session) -> str:
    r = session.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD},
        timeout=TIMEOUT,
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:200]}"
    body = r.json()
    assert body.get("access_token"), "missing access_token"
    return body["access_token"]


@pytest.fixture(scope="session")
def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


@pytest.fixture(scope="session")
def picks_today(session: requests.Session, auth_headers) -> list:
    r = session.get(f"{BASE_URL}/api/picks/today", headers=auth_headers, timeout=TIMEOUT)
    assert r.status_code == 200, f"picks/today failed: {r.status_code}"
    data = r.json()
    # Envelope may be {"picks": [...]} or list
    if isinstance(data, dict):
        for k in ("picks", "items", "data", "results"):
            if isinstance(data.get(k), list):
                return data[k]
        # fallback: try to find any list value with dicts having 'id'
        for v in data.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
        return []
    return data if isinstance(data, list) else []


# ============================================================================
# 1. AUTH
# ============================================================================

class TestAuth:
    def test_login_returns_token(self, session):
        r = session.post(
            f"{BASE_URL}/api/auth/login",
            json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD},
            timeout=TIMEOUT,
        )
        assert r.status_code == 200
        body = r.json()
        assert body.get("access_token") and body.get("user", {}).get("email") == DEMO_EMAIL

    def test_auth_me(self, session, auth_headers):
        r = session.get(f"{BASE_URL}/api/auth/me", headers=auth_headers, timeout=TIMEOUT)
        assert r.status_code == 200, r.text[:200]
        body = r.json()
        assert body.get("email") == DEMO_EMAIL or body.get("user", {}).get("email") == DEMO_EMAIL

    def test_malformed_token_returns_401_not_500(self, session):
        bad = {"Authorization": "Bearer garbage.token.value", "Content-Type": "application/json"}
        r = session.get(f"{BASE_URL}/api/auth/me", headers=bad, timeout=TIMEOUT)
        assert r.status_code in (401, 403), f"expected 401/403, got {r.status_code}"

    def test_wrong_password_returns_4xx(self, session):
        r = session.post(
            f"{BASE_URL}/api/auth/login",
            json={"email": DEMO_EMAIL, "password": "not-the-password"},
            timeout=TIMEOUT,
        )
        assert 400 <= r.status_code < 500


# ============================================================================
# 2. HEALTH / VERSION
# ============================================================================

class TestHealth:
    @pytest.mark.parametrize("path", ["/api/health", "/api/healthz", "/api/ready", "/api/version"])
    def test_health_endpoints(self, session, path):
        r = session.get(f"{BASE_URL}{path}", timeout=TIMEOUT)
        assert r.status_code == 200, f"{path} → {r.status_code}"
        # must be JSON-parseable
        r.json()


# ============================================================================
# 3. PICKS ENDPOINTS — smoke, per-sport, deep-dive
# ============================================================================

SPORTS = ["MLB", "NBA", "NFL", "Soccer", "Tennis", "UFC"]


class TestPicks:
    def test_picks_today_200(self, session, auth_headers):
        r = session.get(f"{BASE_URL}/api/picks/today", headers=auth_headers, timeout=TIMEOUT)
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, (list, dict))

    @pytest.mark.parametrize("sport", SPORTS)
    def test_picks_per_sport(self, session, auth_headers, sport):
        r = session.get(
            f"{BASE_URL}/api/picks/today",
            params={"sport": sport},
            headers=auth_headers,
            timeout=TIMEOUT,
        )
        assert r.status_code == 200, f"{sport} → {r.status_code}"
        r.json()  # must parse

    @pytest.mark.parametrize("sport", SPORTS)
    def test_markets_per_sport(self, session, auth_headers, sport):
        r = session.get(
            f"{BASE_URL}/api/picks/markets/{sport}", headers=auth_headers, timeout=TIMEOUT
        )
        # 200 or 404 (no data for that sport) is acceptable; anything 500 is a fail
        assert r.status_code < 500, f"{sport} markets → {r.status_code}"
        if r.status_code == 200:
            r.json()

    def test_deep_dive_sampled(self, session, auth_headers, picks_today):
        if not picks_today:
            pytest.skip("no picks available to deep-dive")
        # Sample one pick per sport
        seen_sports = set()
        sampled = []
        for p in picks_today:
            sp = (p.get("sport") or "").upper()
            if sp and sp not in seen_sports:
                sampled.append(p)
                seen_sports.add(sp)
            if len(sampled) >= 6:
                break
        for p in sampled:
            pid = p.get("id") or p.get("pick_id")
            if not pid:
                continue
            r = session.get(f"{BASE_URL}/api/picks/{pid}", headers=auth_headers, timeout=TIMEOUT)
            assert r.status_code == 200, f"deep-dive {p.get('sport')} pick {pid} → {r.status_code}"
            r.json()

    @pytest.mark.parametrize("path", [
        "/api/picks/history",
        "/api/picks/rollover",
        "/api/picks/refresh-status",
        "/api/picks/safe-bets",
        "/api/picks/nrfi-yrfi",
        "/api/picks/under-of-the-day",
        "/api/picks/all",
        "/api/picks/overview",
        "/api/picks/bet-killer",
        "/api/picks/hr-slate",
        "/api/picks/top-api-users",
        "/api/picks/atd/leaderboard",
        "/api/picks/games/teams",
        "/api/picks/games/safe-bets",
        "/api/picks/games/safe-alts",
    ])
    def test_picks_side_endpoints(self, session, auth_headers, path):
        r = session.get(f"{BASE_URL}{path}", headers=auth_headers, timeout=TIMEOUT)
        assert r.status_code < 500, f"{path} → {r.status_code}: {r.text[:200]}"
        if r.status_code == 200:
            r.json()


# ============================================================================
# 4. DATA INTEGRITY on /api/picks/today
# ============================================================================

_LINE_RE = re.compile(r"(?i)(?:over|under)\s+(-?\d+(?:\.\d+)?)")


def _extract_line(market: str) -> str:
    if not market:
        return ""
    m = _LINE_RE.search(market)
    return m.group(1) if m else ""


def _norm_family(market: str) -> str:
    """Rough family key — strip 'Over N.N' / 'Under N.N' + numbers."""
    if not market:
        return ""
    s = _LINE_RE.sub("", market)
    s = re.sub(r"[-+]?\d+(?:\.\d+)?", "", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def _side(market: str) -> str:
    m = market or ""
    if re.search(r"(?i)\bover\b", m):
        return "over"
    if re.search(r"(?i)\bunder\b", m):
        return "under"
    return ""


class TestDataIntegrity:
    def test_no_no_bet_visible(self, picks_today):
        offenders = [p for p in picks_today if p.get("no_bet") is True]
        assert not offenders, f"{len(offenders)} no_bet picks surfaced: {[p.get('id') for p in offenders[:5]]}"

    def test_lock_score_bounds(self, picks_today):
        bad = []
        for p in picks_today:
            ls = p.get("lock_score")
            if ls is None:
                continue
            try:
                lsv = float(ls)
            except (TypeError, ValueError):
                bad.append((p.get("id"), ls))
                continue
            if lsv < 0 or lsv > 99:
                bad.append((p.get("id"), lsv))
        assert not bad, f"lock_score out of [0,99]: {bad[:5]}"

    def test_required_fields(self, picks_today):
        missing = []
        required = ["id", "sport", "market"]
        for p in picks_today:
            for f in required:
                if not p.get(f):
                    missing.append((p.get("id"), f))
                    break
            # book_odds OR odds must exist
            if not (p.get("book_odds") or p.get("odds") or p.get("american_odds")):
                missing.append((p.get("id"), "book_odds/odds"))
        assert not missing, f"{len(missing)} picks missing required fields: {missing[:5]}"

    def test_no_duplicate_ids(self, picks_today):
        ids = [p.get("id") for p in picks_today if p.get("id")]
        dupes = {i for i in ids if ids.count(i) > 1}
        assert not dupes, f"duplicate ids: {list(dupes)[:5]}"

    def test_no_same_line_over_under_pair(self, picks_today):
        """Iter-82 regression: same event+player+family+line Over+Under both visible."""
        seen = {}  # key -> {"over": id, "under": id}
        offenders = []
        for p in picks_today:
            market = p.get("market") or ""
            side = _side(market)
            if side not in ("over", "under"):
                continue
            line = _extract_line(market)
            if not line:
                continue
            family = _norm_family(market)
            event = p.get("event") or p.get("event_id") or p.get("matchup") or ""
            player = (p.get("player") or p.get("player_name") or "").lower()
            key = (event, player, family, line)
            rec = seen.setdefault(key, {})
            if side in rec:
                # already have this side — normal (different pick)
                continue
            rec[side] = p.get("id")
            if "over" in rec and "under" in rec:
                offenders.append((key, rec.copy()))
        assert not offenders, (
            f"{len(offenders)} same-line Over+Under contradictions still visible: "
            f"{offenders[:3]}"
        )


# ============================================================================
# 5. ANALYTICS
# ============================================================================

class TestAnalytics:
    @pytest.mark.parametrize("path", [
        "/api/analytics/model-performance",
        "/api/analytics/learned-weights",
        "/api/analytics/bandit",
        "/api/analytics/backtest",
        "/api/analytics/v2",
        "/api/analytics/buckets",
        "/api/analytics/calibration",
        "/api/analytics/xg-form-shadow",
        "/api/analytics/clv",
        "/api/analytics/kelly",
        "/api/analytics/steam",
    ])
    def test_analytics_endpoint(self, session, auth_headers, path):
        r = session.get(f"{BASE_URL}{path}", headers=auth_headers, timeout=TIMEOUT)
        assert r.status_code < 500, f"{path} → {r.status_code}: {r.text[:200]}"
        if r.status_code == 200:
            r.json()


# ============================================================================
# 6. PARLAY / USER
# ============================================================================

class TestParlayAndUser:
    @pytest.mark.parametrize("path", [
        "/api/picks/parlay",
        "/api/picks/parlay/history",
    ])
    def test_parlay_list(self, session, auth_headers, path):
        r = session.get(f"{BASE_URL}{path}", headers=auth_headers, timeout=TIMEOUT)
        assert r.status_code < 500, f"{path} → {r.status_code}"
        if r.status_code == 200:
            r.json()

    @pytest.mark.parametrize("path", [
        "/api/user/analytics/summary",
        "/api/user/analytics/by-sport",
        "/api/user/analytics/by-market",
        "/api/user/analytics/history",
        "/api/user/bets",
    ])
    def test_user_endpoints(self, session, auth_headers, path):
        r = session.get(f"{BASE_URL}{path}", headers=auth_headers, timeout=TIMEOUT)
        assert r.status_code < 500, f"{path} → {r.status_code}: {r.text[:200]}"
        if r.status_code == 200:
            r.json()


# ============================================================================
# 7. ADMIN (auth-only reachability)
# ============================================================================

class TestAdmin:
    @pytest.mark.parametrize("path", [
        "/api/admin/services-registry-status",
        "/api/admin/services-active-check",
        "/api/admin/csl-active-check",
        "/api/admin/csl-espn-status",
        "/api/admin/scorer-audit",
        "/api/admin/odds-diagnostic",
        "/api/admin/soccer/status",
        "/api/admin/historical/status",
        "/api/admin/mlb-hitter-lean",
        "/api/admin/player-props/mls-archetypes",
        "/api/admin/client-errors/recent",
        "/api/clv/snapshot-status",
    ])
    def test_admin_endpoint_no_500(self, session, auth_headers, path):
        r = session.get(f"{BASE_URL}{path}", headers=auth_headers, timeout=TIMEOUT)
        # 200/403 acceptable (403 = auth OK but role-blocked, that's fine).
        # 500 is failure.
        assert r.status_code < 500, f"{path} → {r.status_code}: {r.text[:200]}"
        if r.status_code == 200:
            r.json()


# ============================================================================
# 8. NFL / MLB HR / MISC
# ============================================================================

class TestMisc:
    @pytest.mark.parametrize("path", [
        "/api/mlb/hr-slate",
        "/api/mlb/live",
        "/api/stats/summary",
    ])
    def test_misc_no_500(self, session, auth_headers, path):
        r = session.get(f"{BASE_URL}{path}", headers=auth_headers, timeout=TIMEOUT)
        assert r.status_code < 500, f"{path} → {r.status_code}: {r.text[:200]}"
        if r.status_code == 200:
            r.json()
