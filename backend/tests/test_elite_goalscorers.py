"""Elite striker goalscorer verification.

Validates the Harry Kane / Haaland / Mbappé / Messi / Ronaldo fix:
- Soccer picks ≥ 80
- Each elite anchor present with the expected market trio
- Goalscorer market filter works
- Stats summary elite_count ≥ 30
- Rollover / Bet-killer endpoints don't 500
- No regression for Vinicius / Yamal / Musiala
"""
import os
import pytest
import requests

BASE_URL = os.environ.get(
    "EXPO_PUBLIC_BACKEND_URL",
    "https://canonical-parity.preview.emergentagent.com",
).rstrip("/")

DEMO_EMAIL = "demo@lockscore.ai"
DEMO_PASSWORD = "demo123"

GOAL_MARKETS = {
    "anytime goal scorer",
    "first goal scorer",
    "to score or assist",
    "goal scorer",
}


@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def auth_headers(session):
    r = session.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD},
        timeout=20,
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    body = r.json()
    assert "access_token" in body and body["access_token"]
    return {
        "Authorization": f"Bearer {body['access_token']}",
        "Content-Type": "application/json",
    }


@pytest.fixture(scope="module")
def soccer_picks(session, auth_headers):
    r = session.get(
        f"{BASE_URL}/api/picks/today?sport=Soccer",
        headers=auth_headers,
        timeout=90,
    )
    assert r.status_code == 200, r.text
    picks = r.json().get("picks", [])
    assert isinstance(picks, list)
    return picks


def _player_picks(picks, *name_tokens):
    """Return picks whose market OR selection contains ALL the tokens (case-insensitive)."""
    out = []
    for p in picks:
        haystack = (
            (p.get("market") or "") + " " + (p.get("selection") or "")
        ).lower()
        if all(tok.lower() in haystack for tok in name_tokens):
            out.append(p)
    return out


def _market_family(market_str: str) -> str:
    """Reduce a market string like 'Harry Kane Anytime Goal Scorer' to the family suffix."""
    ms = (market_str or "").lower()
    if "first goal scorer" in ms:
        return "first goal scorer"
    if "anytime goal scorer" in ms or ("goal scorer" in ms and "first" not in ms):
        return "anytime goal scorer"
    if "to score or assist" in ms:
        return "to score or assist"
    return ms.strip()


def _assert_three_markets(player_picks, player_name):
    families = {_market_family(p.get("market", "")) for p in player_picks}
    expected = {
        "anytime goal scorer",
        "first goal scorer",
        "to score or assist",
    }
    missing = expected - families
    assert not missing, (
        f"{player_name}: missing market families {missing} — found families "
        f"{families}, pick count={len(player_picks)}, raw markets="
        f"{sorted({p.get('market','') for p in player_picks})}"
    )


# ──────────────── Login sanity ────────────────
class TestAuthBasic:
    def test_demo_login_returns_jwt(self, session):
        r = session.post(
            f"{BASE_URL}/api/auth/login",
            json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD},
            timeout=20,
        )
        assert r.status_code == 200
        body = r.json()
        assert body.get("access_token")
        assert body.get("token_type") == "bearer"
        assert body["user"]["email"] == DEMO_EMAIL


# ──────────────── Soccer feed volume ────────────────
class TestSoccerFeed:
    def test_soccer_returns_at_least_80_picks(self, soccer_picks):
        assert len(soccer_picks) >= 80, (
            f"expected ≥80 soccer picks, got {len(soccer_picks)}"
        )

    def test_picks_have_required_fields(self, soccer_picks):
        sample = soccer_picks[:25]
        required = ("id", "sport", "market", "selection", "event",
                    "lock_score", "win_probability", "grade")
        for p in sample:
            for k in required:
                assert k in p, f"pick {p.get('id')} missing field {k}"
            assert "_id" not in p
            assert p["sport"] == "Soccer"


# ──────────────── Elite anchor strikers ────────────────
class TestEliteStrikers:
    def test_harry_kane_has_three_markets(self, soccer_picks):
        picks = _player_picks(soccer_picks, "Harry", "Kane")
        assert picks, "Harry Kane not found in Soccer picks"
        _assert_three_markets(picks, "Harry Kane")
        events = {p.get("event", "") for p in picks}
        joined = " | ".join(events).lower()
        assert ("croatia" in joined) or ("ghana" in joined) or ("england" in joined), (
            f"Harry Kane event(s) unexpected: {events}"
        )

    def test_haaland_has_three_markets(self, soccer_picks):
        picks = _player_picks(soccer_picks, "Haaland")
        assert picks, "Haaland not found in Soccer picks"
        _assert_three_markets(picks, "Erling Haaland")
        joined = " | ".join({p.get("event", "") for p in picks}).lower()
        assert ("senegal" in joined) or ("norway" in joined), (
            f"Haaland event(s) unexpected: {joined}"
        )

    def test_mbappe_has_three_markets(self, soccer_picks):
        picks = _player_picks(soccer_picks, "Mbapp")
        assert picks, "Mbappe not found in Soccer picks"
        _assert_three_markets(picks, "Kylian Mbappe")
        joined = " | ".join({p.get("event", "") for p in picks}).lower()
        assert ("iraq" in joined) or ("france" in joined), (
            f"Mbappe event(s) unexpected: {joined}"
        )

    def test_messi_has_three_markets(self, soccer_picks):
        picks = _player_picks(soccer_picks, "Messi")
        assert picks, "Messi not found in Soccer picks"
        _assert_three_markets(picks, "Lionel Messi")
        joined = " | ".join({p.get("event", "") for p in picks}).lower()
        assert ("algeria" in joined) or ("austria" in joined) or ("argentina" in joined), (
            f"Messi event(s) unexpected: {joined}"
        )

    def test_ronaldo_has_three_markets(self, soccer_picks):
        picks = _player_picks(soccer_picks, "Ronaldo")
        assert picks, "Ronaldo not found in Soccer picks"
        _assert_three_markets(picks, "Cristiano Ronaldo")
        joined = " | ".join({p.get("event", "") for p in picks}).lower()
        assert (
            "congo" in joined or "uzbekistan" in joined or "portugal" in joined
        ), f"Ronaldo event(s) unexpected: {joined}"


# ──────────────── Other elite anchors (no regression) ────────────────
class TestNoRegression:
    @pytest.mark.parametrize(
        "tokens,label",
        [
            (("Vinic",), "Vinicius Junior"),
            (("Yamal",), "Lamine Yamal"),
            (("Musiala",), "Jamal Musiala"),
        ],
    )
    def test_other_elite_anchors_present(self, soccer_picks, tokens, label):
        picks = _player_picks(soccer_picks, *tokens)
        assert picks, f"{label} (tokens={tokens}) missing from Soccer feed"


# ──────────────── Goalscorer market filter ────────────────
class TestGoalscorerMarket:
    def test_goalscorer_filter_returns_non_empty(self, session, auth_headers):
        r = session.get(
            f"{BASE_URL}/api/picks/today?sport=Soccer&market=goalscorer",
            headers=auth_headers,
            timeout=60,
        )
        assert r.status_code == 200, r.text
        picks = r.json().get("picks", [])
        assert len(picks) > 0, "goalscorer filter returned empty"
        # Every pick's market string must include a goal-scorer family suffix.
        bad = []
        for p in picks:
            ms = (p.get("market") or "").lower()
            if not any(suffix in ms for suffix in (
                "goal scorer", "to score or assist",
            )):
                bad.append(p["market"])
        assert not bad, f"non-goalscorer markets returned: {set(bad)}"


# ──────────────── Stats ────────────────
class TestStatsElite:
    def test_elite_count_at_least_30(self, session, auth_headers):
        r = session.get(
            f"{BASE_URL}/api/stats/summary",
            headers=auth_headers,
            timeout=60,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert "elite_count" in body, f"elite_count missing: {body}"
        assert body["elite_count"] >= 30, (
            f"elite_count={body['elite_count']} < 30"
        )


# ──────────────── Other endpoints — no 500 ────────────────
class TestOtherEndpointsAlive:
    def test_rollover_no_500(self, session, auth_headers):
        r = session.get(
            f"{BASE_URL}/api/picks/rollover",
            headers=auth_headers,
            timeout=60,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert "pick" in body  # may be None, that's OK

    def test_killer_no_500(self, session, auth_headers):
        # Endpoint is /api/picks/bet-killer per existing route
        r = session.get(
            f"{BASE_URL}/api/picks/bet-killer",
            headers=auth_headers,
            timeout=60,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert "picks" in body
        assert isinstance(body["picks"], list)
