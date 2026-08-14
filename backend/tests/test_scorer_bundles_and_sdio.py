"""Backend tests for PerksLocks NEW features:
  1) Scorer Bundles endpoint GET /api/picks/{id}/scorer-bundles
  2) SportsDataIO enrichment in /api/player-intel/profile
  3) Refresh idempotency POST /api/player-intel/refresh (x2)
  4) Resolver alias matching (Mbappe, Vinicius Jr)
"""
import os
import re

import pytest
import requests

BASE_URL = os.environ.get(
    "EXPO_PUBLIC_BACKEND_URL",
    "https://canonical-parity.preview.emergentagent.com",
).rstrip("/")


@pytest.fixture(scope="module")
def auth_token():
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": "demo@lockscore.ai", "password": "demo123"},
        timeout=30,
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:200]}"
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def headers(auth_token):
    return {"Authorization": f"Bearer {auth_token}", "Content-Type": "application/json"}


@pytest.fixture(scope="module")
def soccer_picks(headers):
    r = requests.get(
        f"{BASE_URL}/api/picks/today",
        params={"sport": "Soccer", "line_type": "both"},
        headers=headers,
        timeout=120,
    )
    assert r.status_code == 200, f"soccer picks failed: {r.status_code} {r.text[:200]}"
    body = r.json()
    picks = body.get("picks") if isinstance(body, dict) else body
    assert isinstance(picks, list) and picks, "no soccer picks returned"
    return picks


# ─────────────────────────────────────────────────────────────────────
# Test 1: Scorer Bundles
# ─────────────────────────────────────────────────────────────────────
class TestScorerBundles:
    def test_anytime_goal_scorer_returns_synth_bundles(self, soccer_picks, headers):
        # Find Anytime Goal Scorer (not First, not Last)
        target = None
        for p in soccer_picks:
            m = (p.get("market") or "").lower()
            if "anytime goal scorer" in m and "first" not in m and "last" not in m:
                target = p
                break
        if not target:
            pytest.skip("No Anytime Goal Scorer pick on slate today")

        pid = target.get("id")
        print(f"\n[T1] target pick id={pid} market={target.get('market')!r} odds={target.get('book_odds')!r}")
        r = requests.get(
            f"{BASE_URL}/api/picks/{pid}/scorer-bundles", headers=headers, timeout=30
        )
        assert r.status_code == 200, f"scorer-bundles failed: {r.status_code} {r.text[:200]}"
        body = r.json()
        print(f"[T1] response keys: {list(body.keys())}")
        print(f"[T1] eligible={body.get('eligible')} synthesizable={body.get('synthesizable')}")
        assert body.get("eligible") is True, f"expected eligible=True, got {body.get('eligible')}"
        # Synthesizable may be False for extreme odds
        synth = body.get("synthesizable")
        if synth is False:
            print(f"[T1] synthesizable=False (extreme odds path) note={body.get('note')!r}")
            return  # acceptable per spec
        assert synth is True, f"unexpected synthesizable value: {synth!r}"
        bundles = body.get("bundles")
        assert isinstance(bundles, list), f"bundles not list: {type(bundles)}"
        assert len(bundles) >= 3, f"expected >=3 bundle entries, got {len(bundles)}"
        for b in bundles:
            for k in ("name", "type", "probability", "fair_american", "decimal"):
                assert k in b, f"bundle entry missing key {k!r}: {b}"
        print(f"[T1] bundles ({len(bundles)}): {[(b['name'], b['probability'], b['fair_american']) for b in bundles]}")

    def test_non_eligible_pick_returns_eligible_false(self, soccer_picks, headers):
        # Find a non-goal-scorer pick (e.g. Total Goals / Match Winner)
        target = None
        for p in soccer_picks:
            m = (p.get("market") or "").lower()
            if "goal scorer" not in m:
                target = p
                break
        if not target:
            pytest.skip("No non-goal-scorer pick on slate")
        pid = target.get("id")
        print(f"\n[T1b] non-eligible pick id={pid} market={target.get('market')!r}")
        r = requests.get(
            f"{BASE_URL}/api/picks/{pid}/scorer-bundles", headers=headers, timeout=30
        )
        # MUST NOT crash
        assert r.status_code == 200, f"non-eligible scorer-bundles failed: {r.status_code} {r.text[:200]}"
        body = r.json()
        print(f"[T1b] body: {body}")
        assert body.get("eligible") is False, f"expected eligible=False, got {body.get('eligible')!r}"

    def test_first_or_last_scorer_is_not_eligible(self, soccer_picks, headers):
        """Sanity: 'First Goal Scorer' / 'Last Goal Scorer' picks are NOT eligible."""
        target = None
        for p in soccer_picks:
            m = (p.get("market") or "").lower()
            if ("first" in m or "last" in m) and "goal scorer" in m:
                target = p
                break
        if not target:
            pytest.skip("No First/Last goal scorer pick on slate")
        pid = target.get("id")
        r = requests.get(
            f"{BASE_URL}/api/picks/{pid}/scorer-bundles", headers=headers, timeout=30
        )
        assert r.status_code == 200
        body = r.json()
        print(f"\n[T1c] First/Last scorer market={target.get('market')!r} → eligible={body.get('eligible')}")
        assert body.get("eligible") is False, f"First/Last should not be eligible: {body}"


# ─────────────────────────────────────────────────────────────────────
# Test 2: SportsDataIO enrichment in player-intel profile
# ─────────────────────────────────────────────────────────────────────
class TestSportsDataIOEnrichment:
    def _profile(self, headers, name, sport):
        r = requests.get(
            f"{BASE_URL}/api/player-intel/profile",
            params={"name": name, "sport": sport},
            headers=headers,
            timeout=30,
        )
        assert r.status_code == 200, f"profile {name}/{sport} failed: {r.status_code} {r.text[:200]}"
        body = r.json()
        return body.get("profile") if isinstance(body, dict) and "profile" in body else body

    def test_juan_soto_mlb_enriched(self, headers):
        p = self._profile(headers, "Juan Soto", "MLB")
        print(f"\n[T2-Soto] keys={sorted(p.keys())}")
        print(f"[T2-Soto] team={p.get('team')!r} pos={p.get('position')!r} "
              f"inj={p.get('injury_status')!r} sdid={p.get('sportsdataio_player_id')!r} "
              f"photo={p.get('photo_url')!r}")
        team = p.get("team")
        assert team and isinstance(team, str) and 2 <= len(team) <= 4, \
            f"team should be short code, got {team!r}"
        pos = p.get("position") or ""
        # Real MLB positions: LF, 1B, RF, OF, DH, CF, etc. — short tokens, not 'forward'/'midfielder'
        assert pos and len(pos) <= 6, f"position not a real MLB code: {pos!r}"
        assert p.get("injury_status"), f"missing injury_status: {p.get('injury_status')!r}"
        sdid = p.get("sportsdataio_player_id")
        assert isinstance(sdid, int), f"sportsdataio_player_id not numeric: {sdid!r}"
        photo = p.get("photo_url") or ""
        assert isinstance(photo, str) and photo.startswith("http"), \
            f"photo_url not a URL: {photo!r}"

    def test_bryce_harper_mlb_team_phi(self, headers):
        p = self._profile(headers, "Bryce Harper", "MLB")
        print(f"\n[T2-Harper] team={p.get('team')!r} pos={p.get('position')!r} "
              f"sdid={p.get('sportsdataio_player_id')!r}")
        assert p.get("team") == "PHI", f"Harper team expected PHI, got {p.get('team')!r}"
        pos = p.get("position") or ""
        # Real MLB position — 1B / RF / DH / OF — short tokens
        assert pos and len(pos) <= 6 and pos.upper() == pos, \
            f"Harper position should be real MLB code, got {pos!r}"

    def test_mahomes_nfl_enriched(self, headers):
        p = self._profile(headers, "Mahomes", "NFL")
        print(f"\n[T2-Mahomes] canonical={p.get('canonical_name')!r} "
              f"team={p.get('team')!r} pos={p.get('position')!r} "
              f"sdid={p.get('sportsdataio_player_id')!r}")
        assert p.get("canonical_name") == "Patrick Mahomes", \
            f"canonical mismatch: {p.get('canonical_name')!r}"
        # If SportsDataIO enrichment ran, team/position must be real
        sdid = p.get("sportsdataio_player_id")
        if sdid is not None:
            assert p.get("team") == "KC", f"Mahomes team expected KC, got {p.get('team')!r}"
            assert p.get("position") == "QB", f"Mahomes position expected QB, got {p.get('position')!r}"
        else:
            print("[T2-Mahomes] SportsDataIO enrichment did NOT run for NFL — sdid missing")


# ─────────────────────────────────────────────────────────────────────
# Test 3: Refresh idempotency
# ─────────────────────────────────────────────────────────────────────
class TestRefreshIdempotency:
    def test_refresh_twice_does_not_decrease(self, headers):
        r1 = requests.post(f"{BASE_URL}/api/player-intel/refresh", headers=headers, timeout=180)
        assert r1.status_code == 200, f"refresh #1 failed: {r1.status_code} {r1.text[:200]}"
        b1 = r1.json()
        print(f"\n[T3] refresh #1: {b1}")
        for key in ("seeded_new", "learned_updates", "total_profiles"):
            assert key in b1, f"missing key {key} in refresh #1: {b1}"
        assert b1["total_profiles"] >= 180, \
            f"total_profiles < 180: {b1['total_profiles']}"

        r2 = requests.post(f"{BASE_URL}/api/player-intel/refresh", headers=headers, timeout=180)
        assert r2.status_code == 200, f"refresh #2 failed: {r2.status_code} {r2.text[:200]}"
        b2 = r2.json()
        print(f"[T3] refresh #2: {b2}")
        assert b2["total_profiles"] >= b1["total_profiles"], \
            f"total_profiles decreased: {b1['total_profiles']} → {b2['total_profiles']}"


# ─────────────────────────────────────────────────────────────────────
# Test 4: Resolver alias matching
# ─────────────────────────────────────────────────────────────────────
class TestResolverAliases:
    def _profile(self, headers, name, sport):
        r = requests.get(
            f"{BASE_URL}/api/player-intel/profile",
            params={"name": name, "sport": sport},
            headers=headers,
            timeout=30,
        )
        assert r.status_code == 200, f"profile {name}/{sport} failed: {r.status_code} {r.text[:200]}"
        body = r.json()
        return body.get("profile") if isinstance(body, dict) and "profile" in body else body

    def test_mbappe_alias(self, headers):
        p = self._profile(headers, "Mbappe", "Soccer")
        print(f"\n[T4-Mbappe] canonical={p.get('canonical_name')!r}")
        assert p.get("canonical_name") == "Kylian Mbappé", \
            f"Mbappe should resolve to 'Kylian Mbappé', got {p.get('canonical_name')!r}"

    def test_vinicius_jr_alias(self, headers):
        p = self._profile(headers, "Vinicius Jr", "Soccer")
        print(f"\n[T4-Vini] canonical={p.get('canonical_name')!r}")
        assert p.get("canonical_name") == "Vinícius Júnior", \
            f"Vinicius Jr should resolve to 'Vinícius Júnior', got {p.get('canonical_name')!r}"
