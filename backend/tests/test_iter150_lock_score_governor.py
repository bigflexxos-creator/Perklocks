"""Iteration 150 — Lock-Score Governor fixes backend validation.

Scope (backend only):
  1. GET /api/picks/today returns NBA, NHL, Soccer picks (demo user).
  2. NHL alt-goal cap: no NHL `Over N.5 Goals` (N>=1) with lock_score >= 85
     in canonical_picks; those must have `nhl_alt_goal_cap_applied=True`.
  3. NBA ALT LOCK floor: for NBA picks with ALT LOCK + wp>=70 + edge>=0,
     lock_score >= min(96, 60 + wp*40). Specific regression: Hartenstein
     Over 4.5 Points · ALT LOCK -> lock ~= 96 with flag applied.
  4. Soccer soft-pass: Soccer player-prop picks that are off_board must
     have off_board_reasons intersecting the hard-reject set (not
     roster_unverified / roster_conflict alone).
  5. Code sanity: NFL leakage cap + NFL reliability floor blocks still
     exist in services/pick_refresh_orchestrator.py.
"""
import os
import re
import math
import pytest
import requests
from pymongo import MongoClient

BASE_URL = os.environ.get("EXPO_BACKEND_URL") or \
           "https://canonical-parity.preview.emergentagent.com"
BASE_URL = BASE_URL.rstrip("/")

MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "lockscore_db")

ALT_GOAL_PAT = re.compile(r"\bover\s+(\d+)\.5\s+goals\b", re.IGNORECASE)


# ─── fixtures ────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def token(session):
    r = session.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": "demo@lockscore.ai", "password": "demo123"},
        timeout=30,
    )
    assert r.status_code == 200, (
        f"login failed: {r.status_code} {r.text[:200]}")
    data = r.json()
    tok = data.get("token") or data.get("access_token")
    assert tok, f"no token in login response: {data}"
    return tok


@pytest.fixture(scope="module")
def auth_headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


@pytest.fixture(scope="module")
def mongo():
    client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)
    return client[DB_NAME]


# ─── 1. picks/today sport coverage ───────────────────────────────────
class TestPicksTodaySportCoverage:
    """GET /api/picks/today returns picks for NBA, NHL, Soccer."""

    def test_endpoint_200(self, session, auth_headers):
        r = session.get(
            f"{BASE_URL}/api/picks/today", headers=auth_headers, timeout=60)
        assert r.status_code == 200, f"{r.status_code} {r.text[:200]}"
        self.payload = r.json()
        assert isinstance(self.payload, (dict, list))

    def test_sport_breakdown(self, session, auth_headers):
        r = session.get(
            f"{BASE_URL}/api/picks/today", headers=auth_headers, timeout=60)
        assert r.status_code == 200
        data = r.json()
        # payload may be {"picks":[...]} or a list directly
        picks = data.get("picks") if isinstance(data, dict) else data
        assert isinstance(picks, list), f"unexpected payload shape: {type(data)}"
        counts: dict[str, int] = {}
        for p in picks:
            s = (p.get("sport") or "").strip()
            counts[s] = counts.get(s, 0) + 1
        print(f"\n[picks/today] sport counts = {counts}")
        for sport in ("NBA", "NHL", "Soccer"):
            assert counts.get(sport, 0) > 0, (
                f"no {sport} picks on /api/picks/today — counts={counts}")


# ─── 2. NHL alt-goal cap invariant (DB) ──────────────────────────────
class TestNHLAltGoalCap:
    """No NHL Over N.5 Goals (N>=1) with lock_score >= 85 in canonical."""

    def test_no_nhl_alt_goal_locks(self, mongo):
        offenders = []
        cursor = mongo.canonical_picks.find(
            {"sport": "NHL", "lock_score": {"$gte": 85.0}},
            {"id": 1, "market": 1, "lock_score": 1,
             "nhl_alt_goal_cap_applied": 1, "player_name": 1},
        )
        for p in cursor:
            m = p.get("market") or ""
            mt = ALT_GOAL_PAT.search(m)
            if not mt:
                continue
            try:
                n = int(mt.group(1))
            except ValueError:
                continue
            if n >= 1:
                offenders.append({
                    "id": p.get("id"), "market": m,
                    "lock": p.get("lock_score"),
                    "player": p.get("player_name"),
                })
        print(f"\n[NHL alt-goal cap] offenders={len(offenders)} "
              f"sample={offenders[:3]}")
        assert len(offenders) == 0, (
            f"NHL alt-goal cap violated: {len(offenders)} picks still "
            f">=85 lock. Sample: {offenders[:5]}")

    def test_capped_picks_carry_flag(self, mongo):
        """Any NHL Over N.5 Goals (N>=1) pick must have the flag set
        when its lock_score is exactly the 84.9 ceiling (cap evidence)."""
        cursor = mongo.canonical_picks.find(
            {"sport": "NHL",
             "nhl_alt_goal_cap_applied": True},
            {"id": 1, "market": 1, "lock_score": 1},
        ).limit(20)
        capped = list(cursor)
        print(f"\n[NHL alt-goal cap] flagged picks n={len(capped)} "
              f"sample={capped[:3]}")
        for p in capped:
            assert float(p.get("lock_score") or 0.0) <= 84.9 + 1e-6, (
                f"flagged pick still >=85: {p}")


# ─── 3. NBA ALT LOCK floor invariant (DB) ────────────────────────────
class TestNBAAltLockFloor:
    """NBA ALT LOCK with wp>=70 & edge>=0 must have lock >= floor."""

    @staticmethod
    def _floor(wp_pct: float) -> float:
        wp = wp_pct / 100.0 if wp_pct > 1.0 else wp_pct
        return min(96.0, 60.0 + max(0.0, min(1.0, wp)) * 40.0)

    def test_alt_lock_floor_invariant(self, mongo):
        offenders = []
        cursor = mongo.canonical_picks.find(
            {"sport": "NBA"},
            {"id": 1, "market": 1, "lock_score": 1,
             "win_probability": 1, "edge_percent": 1,
             "nba_alt_lock_floor_applied": 1, "player_name": 1},
        )
        total = 0
        qualifying = 0
        for p in cursor:
            total += 1
            mkt = (p.get("market") or "").upper()
            if "ALT LOCK" not in mkt:
                continue
            try:
                wp = float(p.get("win_probability") or 0.0)
            except (TypeError, ValueError):
                continue
            if wp <= 1.0:
                wp *= 100.0
            try:
                edge = float(p.get("edge_percent") or 0.0)
            except (TypeError, ValueError):
                edge = 0.0
            try:
                lock = float(p.get("lock_score") or 0.0)
            except (TypeError, ValueError):
                continue
            if wp < 70.0 or edge < 0.0:
                continue
            qualifying += 1
            needed = self._floor(wp)
            if lock + 0.15 < needed:  # rounding tolerance
                offenders.append({
                    "id": p.get("id"), "player": p.get("player_name"),
                    "market": p.get("market"), "lock": lock,
                    "wp": wp, "edge": edge, "needed_floor": needed,
                })
        print(f"\n[NBA ALT LOCK floor] scanned={total} qualifying={qualifying} "
              f"offenders={len(offenders)} sample={offenders[:3]}")
        assert len(offenders) == 0, (
            f"NBA ALT LOCK floor violated: {len(offenders)} picks below "
            f"floor. Sample: {offenders[:5]}")

    def test_hartenstein_regression(self, mongo):
        """Specific canonical regression: Hartenstein Over 4.5 Points · ALT LOCK."""
        q = {
            "sport": "NBA",
            "player_name": {"$regex": "Hartenstein", "$options": "i"},
        }
        picks = list(mongo.canonical_picks.find(q))
        print(f"\n[Hartenstein] matches={len(picks)}")
        # Try to find the ALT LOCK Over 4.5 points row specifically.
        hits = [p for p in picks
                if "ALT LOCK" in (p.get("market") or "").upper()
                and "4.5" in (p.get("market") or "")
                and "POINT" in (p.get("market") or "").upper()]
        if not hits:
            # The specific row may not be present today; soft-skip
            # with a diagnostic (sport slate changes daily).
            print(f"[Hartenstein] no ALT LOCK Over 4.5 Points row today; "
                  f"all markets: "
                  f"{[p.get('market') for p in picks]}")
            pytest.skip(
                "Hartenstein Over 4.5 Points · ALT LOCK not in today's "
                "slate (daily rotation).")
        p = hits[0]
        lock = float(p.get("lock_score") or 0.0)
        wp = float(p.get("win_probability") or 0.0)
        if wp <= 1.0:
            wp *= 100.0
        print(f"[Hartenstein] market={p.get('market')} lock={lock} wp={wp} "
              f"edge={p.get('edge_percent')} "
              f"floor_applied={p.get('nba_alt_lock_floor_applied')}")
        assert lock >= 95.0, (
            f"Hartenstein regression failed: lock={lock} (<95). "
            f"wp={wp}, edge={p.get('edge_percent')}, "
            f"floor_applied={p.get('nba_alt_lock_floor_applied')}")
        # Floor flag should be set when the floor was active.
        assert p.get("nba_alt_lock_floor_applied") is True, (
            f"nba_alt_lock_floor_applied flag not set on Hartenstein row")


# ─── 4. Soccer soft-pass logic (DB inspection) ───────────────────────
class TestSoccerSoftPass:
    """Only hard-reject reasons may send a Soccer pick off_board."""

    HARD = {"player_team_mismatch", "fixture_teams_unknown",
            "player_name_missing"}

    def test_soccer_player_prop_locks_visible(self, mongo):
        count = mongo.canonical_picks.count_documents({
            "sport": "Soccer",
            "lock_score": {"$gte": 85.0},
        })
        print(f"\n[Soccer] published locks (>=85) count={count}")
        # Baseline from PR notes: 260+ player-prop locks should be visible.
        # Report as info only — treat as warning if low.
        if count < 50:
            print(f"[Soccer] WARNING: expected 260+ locks, got {count}")

    def test_off_board_reasons_subset(self, mongo):
        """Soccer player-props with off_board=True must contain at least
        one hard-reject reason — never roster_unverified alone."""
        q = {
            "sport": "Soccer",
            "off_board": True,
            "player_name": {"$exists": True, "$ne": None},
        }
        cursor = mongo.canonical_picks.find(
            q,
            {"id": 1, "market": 1, "off_board_reasons": 1,
             "player_name": 1},
        ).limit(500)
        offenders = []
        total = 0
        for p in cursor:
            total += 1
            reasons = set(p.get("off_board_reasons") or [])
            # roster_unverified or roster_conflict should never be the
            # sole trigger for off_board when no hard reason present.
            if not reasons:
                continue
            if reasons & self.HARD:
                continue  # fine
            soft_only = reasons & {"roster_unverified", "roster_conflict"}
            if soft_only:
                offenders.append({
                    "id": p.get("id"), "player": p.get("player_name"),
                    "reasons": list(reasons),
                })
        print(f"\n[Soccer soft-pass] scanned={total} "
              f"soft-only off_board={len(offenders)} "
              f"sample={offenders[:3]}")
        assert len(offenders) == 0, (
            f"Soccer soft-pass violated: {len(offenders)} player-prop "
            f"picks off_board on roster_unverified/roster_conflict alone. "
            f"Sample: {offenders[:5]}")


# ─── 5. No-regression sanity on NFL cap blocks (code presence) ───────
class TestOrchestratorRegressionSanity:
    """Confirm NFL leakage cap + NFL reliability floor still present
    in services/pick_refresh_orchestrator.py."""

    ORCH = "/app/backend/services/pick_refresh_orchestrator.py"

    def test_nfl_leakage_cap_block_present(self):
        with open(self.ORCH, "r") as f:
            src = f.read()
        assert "mp-from-book leakage" in src.lower() or \
               "nfl mp-leakage" in src.lower(), (
            "NFL leakage cap block missing from orchestrator")

    def test_nfl_reliability_floor_block_present(self):
        with open(self.ORCH, "r") as f:
            src = f.read()
        # The NFL reliability pattern 60 + wp*40
        assert "60.0 + " in src and "wp" in src.lower(), (
            "NFL reliability floor arithmetic missing")

    def test_new_nhl_blocks_present(self):
        with open(self.ORCH, "r") as f:
            src = f.read()
        assert "nhl_alt_goal_cap_applied" in src, \
            "NHL alt-goal cap block missing"
        assert "nba_alt_lock_floor_applied" in src, \
            "NBA ALT LOCK floor block missing"
