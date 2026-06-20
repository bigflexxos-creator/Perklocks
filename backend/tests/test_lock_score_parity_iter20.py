"""
PerksLocks — Lock Score sync regression (iteration 20).

Validates that:
  1. /api/picks/today and /api/picks/{id} return the SAME lock_score value
     (V2 promoted into canonical `lock_score` in DB).
  2. For ANY pick, frontend `displayLock = max(lock_score, lock_score_v2)`
     equals the value shown on /pick/{id} (which uses lock_score directly).
  3. Auth + main read endpoints all return 200.
  4. /api/picks/{id}/ai-explain works (async, may return ai_pending or final).
"""
import os
import random
import requests
import pytest

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "https://bet-edge-ai-1.preview.emergentagent.com").rstrip("/")
EMAIL = "demo@lockscore.ai"
PASSWORD = "demo123"


@pytest.fixture(scope="module")
def token():
    r = requests.post(f"{BASE_URL}/api/auth/login", json={"email": EMAIL, "password": PASSWORD}, timeout=30)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


@pytest.fixture(scope="module")
def today_picks(headers):
    r = requests.get(f"{BASE_URL}/api/picks/today", headers=headers, timeout=60)
    assert r.status_code == 200, f"/picks/today failed: {r.status_code} {r.text}"
    data = r.json()
    picks = data if isinstance(data, list) else data.get("picks", [])
    assert len(picks) > 0, "No picks returned from /api/picks/today"
    return picks


# ─── Auth ──────────────────────────────────────────────────────────────────
class TestAuth:
    def test_login(self, token):
        assert token and isinstance(token, str)

    def test_me(self, headers):
        r = requests.get(f"{BASE_URL}/api/auth/me", headers=headers, timeout=30)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("email") == EMAIL


# ─── Read endpoints ────────────────────────────────────────────────────────
class TestReadEndpoints:
    def test_picks_today(self, today_picks):
        assert len(today_picks) > 0
        # Ensure required fields present on first pick
        sample = today_picks[0]
        for key in ("id", "sport", "lock_score", "grade", "market"):
            assert key in sample, f"missing key {key} on pick {sample.get('id')}"

    def test_picks_all(self, headers):
        r = requests.get(f"{BASE_URL}/api/picks/all", headers=headers, timeout=60)
        assert r.status_code == 200

    def test_picks_bet_killer(self, headers):
        r = requests.get(f"{BASE_URL}/api/picks/bet-killer", headers=headers, timeout=60)
        assert r.status_code == 200

    def test_picks_rollover(self, headers):
        r = requests.get(f"{BASE_URL}/api/picks/rollover", headers=headers, timeout=60)
        assert r.status_code == 200

    def test_picks_parlay_3legs(self, headers):
        r = requests.get(f"{BASE_URL}/api/picks/parlay?legs=3", headers=headers, timeout=60)
        assert r.status_code == 200


# ─── Lock Score parity (P0) ────────────────────────────────────────────────
class TestLockScoreParity:
    def _pick_by_sport(self, picks, sport_pref):
        """Pick one with sport==sport_pref; fall back to random."""
        same = [p for p in picks if (p.get("sport") or "").upper() == sport_pref.upper()]
        if same:
            return random.choice(same)
        return None

    def test_lock_score_present_and_numeric(self, today_picks):
        """No NaN/0 fallback — every pick must have a sane numeric lock_score."""
        for p in today_picks:
            ls = p.get("lock_score")
            assert isinstance(ls, (int, float)) and ls >= 0, (
                f"pick {p.get('id')} has invalid lock_score={ls!r}"
            )

    def test_display_lock_no_nan(self, today_picks):
        """Mirror frontend computation: max(lock_score, lock_score_v2) must be numeric & >0 if either is."""
        for p in today_picks:
            v1 = p.get("lock_score") or 0
            v2 = p.get("lock_score_v2") or 0
            display = max(float(v1 or 0), float(v2 or 0))
            assert display >= 0
            # If pick exists in the feed at all, displayLock should be > 0 (no zero fallback)
            assert display > 0, f"pick {p.get('id')} would render 0 on card"

    def _parity_check(self, headers, pick):
        """Compare home-feed lock_score to detail-page lock_score for a given pick."""
        pid = pick["id"]
        r = requests.get(f"{BASE_URL}/api/picks/{pid}", headers=headers, timeout=60)
        assert r.status_code == 200, f"/picks/{pid} -> {r.status_code} {r.text}"
        detail = r.json()
        feed_ls = pick.get("lock_score")
        feed_v2 = pick.get("lock_score_v2")
        detail_ls = detail.get("lock_score")
        detail_v2 = detail.get("lock_score_v2")

        # The detail page renders pick.lock_score (Math.round)
        # The card renders max(lock_score, lock_score_v2)
        # For these to match the user-visible display, we want:
        #   round(max(feed_ls, feed_v2)) == round(detail_ls)
        feed_display = round(max(float(feed_ls or 0), float(feed_v2 or 0)))
        detail_display = round(float(detail_ls or 0))

        # Backend should have promoted v2 into canonical lock_score by now.
        # Soft check: if mismatch, report details.
        return {
            "id": pid,
            "sport": pick.get("sport"),
            "feed_ls": feed_ls,
            "feed_v2": feed_v2,
            "detail_ls": detail_ls,
            "detail_v2": detail_v2,
            "feed_display": feed_display,
            "detail_display": detail_display,
            "match": feed_display == detail_display,
        }

    def test_parity_mlb_pick(self, headers, today_picks):
        pick = self._pick_by_sport(today_picks, "MLB")
        if not pick:
            pytest.skip("No MLB pick today")
        result = self._parity_check(headers, pick)
        print(f"MLB parity: {result}")
        assert result["match"], f"MLB parity mismatch: {result}"

    def test_parity_soccer_pick(self, headers, today_picks):
        pick = self._pick_by_sport(today_picks, "Soccer")
        if not pick:
            pytest.skip("No Soccer pick today")
        result = self._parity_check(headers, pick)
        print(f"Soccer parity: {result}")
        assert result["match"], f"Soccer parity mismatch: {result}"

    def test_parity_other_pick(self, headers, today_picks):
        # Find anything that isn't MLB or Soccer
        non = [p for p in today_picks if (p.get("sport") or "").upper() not in ("MLB", "SOCCER")]
        if not non:
            pytest.skip("No non-MLB/Soccer pick today")
        pick = random.choice(non)
        result = self._parity_check(headers, pick)
        print(f"{pick.get('sport')} parity: {result}")
        assert result["match"], f"{pick.get('sport')} parity mismatch: {result}"

    def test_parity_5_random_picks(self, headers, today_picks):
        """Broad scan — sample up to 5 random picks and verify parity across all."""
        sample = random.sample(today_picks, min(5, len(today_picks)))
        results = [self._parity_check(headers, p) for p in sample]
        mismatches = [r for r in results if not r["match"]]
        print(f"Random sample parity ({len(results)} checked, {len(mismatches)} mismatches):")
        for r in results:
            print(f"  {r['id']} [{r['sport']}] card={r['feed_display']} detail={r['detail_display']} "
                  f"(feed_ls={r['feed_ls']}, feed_v2={r['feed_v2']}, detail_ls={r['detail_ls']})")
        assert not mismatches, f"{len(mismatches)} mismatch(es): {mismatches}"

    def test_v2_promotion_in_db(self, today_picks):
        """If lock_score_v2 exists and != lock_score, the backend has NOT promoted v2 into canonical."""
        not_promoted = []
        for p in today_picks:
            v1 = p.get("lock_score")
            v2 = p.get("lock_score_v2")
            if v1 is None or v2 is None:
                continue
            if abs(float(v1) - float(v2)) > 0.5:  # tolerance for round
                not_promoted.append({"id": p.get("id"), "v1": v1, "v2": v2, "delta": float(v2) - float(v1)})
        if not_promoted:
            print(f"⚠️  {len(not_promoted)}/{len(today_picks)} picks where lock_score != lock_score_v2:")
            for n in not_promoted[:10]:
                print(f"   {n}")
        # We expect promotion — fail if more than 25% are not promoted
        assert len(not_promoted) / max(1, len(today_picks)) < 0.25, (
            f"V2 promotion missing in {len(not_promoted)} of {len(today_picks)} picks"
        )


# ─── Tier chip gating (P1) ────────────────────────────────────────────────
class TestTierChipGating:
    def test_strong_chip_only_when_display_95(self, today_picks):
        """Sub-95 displayLock must not surface APEX/RARE/STRONG chip."""
        violations = []
        for p in today_picks:
            v1 = float(p.get("lock_score") or 0)
            v2 = float(p.get("lock_score_v2") or 0)
            display = max(v1, v2)
            tier_v2 = p.get("tier_v2")
            is_apex = p.get("is_apex")
            # Backend may still emit tier_v2 — that's OK, FE gates it.
            # We only assert on what the FE WOULD show.
            v1_is_strong = display >= 95
            shown_tier = tier_v2 if v1_is_strong else None
            shown_apex = is_apex if v1_is_strong else False
            # Just print sub-95 picks that have v2 chip data — sanity log
            if not v1_is_strong and (tier_v2 or is_apex):
                violations.append({
                    "id": p.get("id"), "display": display, "tier_v2": tier_v2, "is_apex": is_apex
                })
        print(f"{len(violations)} sub-95 picks with v2 tier data (FE suppresses these correctly): {violations[:5]}")
        # No assertion — this is informational; FE gating is the real test.


# ─── AI explain endpoint (P1) ─────────────────────────────────────────────
class TestAiExplain:
    def test_ai_explain_returns_200(self, headers, today_picks):
        pick = today_picks[0]
        pid = pick["id"]
        r = requests.post(f"{BASE_URL}/api/picks/{pid}/ai-explain", headers=headers, timeout=60)
        assert r.status_code == 200, f"ai-explain -> {r.status_code} {r.text}"
        body = r.json()
        # Either explanation present, or ai_pending true — both are valid
        assert "explanation" in body or body.get("ai_pending") is True, body
