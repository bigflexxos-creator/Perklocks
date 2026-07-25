"""iter-94 backend verification.

Fix under test:
  A) `_soccer_family_rank` hoisted out of the sort switch and applied as the
     tertiary tiebreaker in EVERY sort mode (time / edge / win / implied /
     lock). Iter-92 only patched sort=lock; iter-93 added 'Anytime Goal
     Involvement' as a rank-1 pattern.
  B) Odds provider fallback + circuit breaker + decorate_pick (from iter-93,
     re-verified end-to-end).
  C) MLS data-quality gate spot check (from iter-93, re-verified).

Six checks required by the review request:
  1. sort=lock top-5 all Anytime Goal Scorer AND lock_score>=95
  2. sort=time — within an event with both scorer & assist at same lock tier,
     scorer index < assist index
  3. default (no ?sort=) — family tiebreaker still applies within each
     event/lock bucket
  4. Odds fallback fields on every pick
  5. Circuit breaker in-process simulation (report_failure×3 → degraded →
     report_success → live)
  6. Perf: MLB<8s, Soccer<12s
"""
import os
import sys
import time
import asyncio
from collections import defaultdict

import pytest
import requests

sys.path.insert(0, "/app/backend")
try:
    from dotenv import load_dotenv
    load_dotenv("/app/backend/.env")
    load_dotenv("/app/frontend/.env")
except Exception:
    pass

BASE = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "").rstrip("/")
assert BASE, "EXPO_PUBLIC_BACKEND_URL missing from frontend/.env"

EMAIL = "demo@lockscore.ai"
PASSWORD = "demo123"


# ── shared auth ─────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    r = s.post(f"{BASE}/api/auth/login",
               json={"email": EMAIL, "password": PASSWORD}, timeout=30)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:200]}"
    tok = r.json().get("access_token") or r.json().get("token")
    assert tok, r.json()
    s.headers.update({"Authorization": f"Bearer {tok}",
                      "Content-Type": "application/json"})
    return s


def _get(session, path, timeout=45):
    t0 = time.perf_counter()
    r = session.get(f"{BASE}{path}", timeout=timeout)
    elapsed = time.perf_counter() - t0
    assert r.status_code == 200, f"{path} -> {r.status_code} {r.text[:200]}"
    body = r.json()
    return body, elapsed


def _market(p):
    return (p.get("market") or "").strip()


def _mt(p):
    return (p.get("market_type") or "").lower()


def _is_scorer(p):
    return "Anytime Goal Scorer" in _market(p) or _mt(p) == "anytime_goal_scorer"


def _is_assist(p):
    return "Anytime Assist" in _market(p) or _mt(p) == "anytime_assist"


def _family(p):
    """Return the iter-94 family rank (0=scorer, 1=involvement/score-or-assist,
    2=first/last, 3=assist, 4=other)."""
    mm = _market(p)
    mt = _mt(p)
    if "Anytime Goal Scorer" in mm or mt == "anytime_goal_scorer":
        return 0
    if "Goal Involvement" in mm or mt == "anytime_goal_involvement":
        return 1
    if "To Score or Assist" in mm or "Score or Assist" in mm or mt == "to_score_or_assist":
        return 1
    if "First Goal Scorer" in mm or "Last Goal Scorer" in mm:
        return 2
    if "Anytime Assist" in mm or mt == "anytime_assist":
        return 3
    return 4


# ────────────────────────────────────────────────────────────────────
# CHECK 1 — sort=lock top-5 all Anytime Goal Scorer AND lock_score>=95
# ────────────────────────────────────────────────────────────────────
class TestSortLockTop5:

    def test_mls_sort_lock_top5_all_scorer_lock95plus(self, session):
        body, _ = _get(session, "/api/picks/today?sport=Soccer&leagues=MLS&sort=lock&limit=25")
        picks = body.get("picks", [])
        if len(picks) < 5:
            pytest.skip(f"MLS slate too small ({len(picks)} picks)")
        top5 = picks[:5]
        # Every top-5 must be Anytime Goal Scorer per review request
        markets = [_market(p) for p in top5]
        locks = [p.get("lock_score", 0) for p in top5]
        scorer_flags = [_is_scorer(p) for p in top5]

        assert all(scorer_flags), (
            f"top-5 not all 'Anytime Goal Scorer'. Markets: {markets}"
        )
        assert all(ls >= 95 for ls in locks), (
            f"top-5 lock_score not all >=95: {locks}"
        )


# ────────────────────────────────────────────────────────────────────
# CHECK 2 — sort=time: within an event with both scorer & assist at
# same lock tier, scorer precedes assist in the returned order.
# ────────────────────────────────────────────────────────────────────
class TestSortTimeFamilyTiebreaker:

    def _check_family_ordering(self, picks):
        """For each (event_id, lock_score) bucket that has both a scorer/
        involvement pick AND an assist pick, verify the scorer/involvement
        appears at a lower index than the assist."""
        buckets = defaultdict(list)  # (event_id, lock) -> [(idx, family)]
        for idx, p in enumerate(picks):
            if (p.get("sport") or "") != "Soccer":
                continue
            eid = p.get("event_id") or p.get("event") or p.get("game_id")
            lock = p.get("lock_score", 0)
            buckets[(eid, lock)].append((idx, _family(p), _market(p)))

        # Pick any bucket that contains BOTH a family<=2 (scorer/involvement/
        # score-or-assist/first/last) and a family==3 (assist).
        buckets_with_both = 0
        violations = []
        for key, entries in buckets.items():
            has_low = any(f <= 2 for _, f, _ in entries)
            has_assist = any(f == 3 for _, f, _ in entries)
            if not (has_low and has_assist):
                continue
            buckets_with_both += 1
            first_assist = min((i for i, f, _ in entries if f == 3), default=None)
            last_low = max((i for i, f, _ in entries if f <= 2), default=None)
            if first_assist is not None and last_low is not None and first_assist < last_low:
                violations.append(
                    f"{key}: assist at pos {first_assist} before scorer/inv at {last_low}. "
                    f"entries={entries}"
                )
        return buckets_with_both, violations

    def test_mls_sort_time_scorer_precedes_assist_in_same_event_lock(self, session):
        body, _ = _get(session, "/api/picks/today?sport=Soccer&leagues=MLS&sort=time&limit=200")
        picks = body.get("picks", [])
        buckets, violations = self._check_family_ordering(picks)
        if buckets == 0:
            pytest.skip("No MLS event/lock bucket has BOTH a scorer/involvement pick AND an assist pick")
        assert not violations, "; ".join(violations[:5])


# ────────────────────────────────────────────────────────────────────
# CHECK 3 — default (no ?sort=) — family tiebreaker still applies
# ────────────────────────────────────────────────────────────────────
class TestDefaultSortFamilyTiebreaker:

    def test_mls_default_sort_family_tiebreaker(self, session):
        body, _ = _get(session, "/api/picks/today?sport=Soccer&leagues=MLS&limit=200")
        picks = body.get("picks", [])
        # Reuse the check-2 logic; the review request says regression from
        # iter-92 test 6 must now pass.
        buckets = defaultdict(list)
        for idx, p in enumerate(picks):
            if (p.get("sport") or "") != "Soccer":
                continue
            eid = p.get("event_id") or p.get("event") or p.get("game_id")
            lock = p.get("lock_score", 0)
            buckets[(eid, lock)].append((idx, _family(p), _market(p)))
        buckets_with_both = 0
        violations = []
        for key, entries in buckets.items():
            has_low = any(f <= 2 for _, f, _ in entries)
            has_assist = any(f == 3 for _, f, _ in entries)
            if not (has_low and has_assist):
                continue
            buckets_with_both += 1
            first_assist = min((i for i, f, _ in entries if f == 3), default=None)
            last_low = max((i for i, f, _ in entries if f <= 2), default=None)
            if first_assist is not None and last_low is not None and first_assist < last_low:
                violations.append(f"{key}: assist@{first_assist} < scorer@{last_low}")
        if buckets_with_both == 0:
            pytest.skip("No qualifying bucket in default-sort response")
        assert not violations, (
            f"default-sort family tiebreaker regression: {'; '.join(violations[:5])}"
        )


# ────────────────────────────────────────────────────────────────────
# CHECK 4 — odds fallback fields populated on every pick
# ────────────────────────────────────────────────────────────────────
class TestOddsFallbackFields:

    def test_every_soccer_pick_has_odds_tags(self, session):
        body, _ = _get(session, "/api/picks/today?sport=Soccer&leagues=MLS&limit=200")
        picks = body.get("picks", [])
        if not picks:
            pytest.skip("No MLS picks")
        missing = []
        for p in picks:
            for k in ("odds_source", "odds_status", "confidence_penalty"):
                if k not in p or p.get(k) is None:
                    missing.append((p.get("id") or p.get("pick_id") or p.get("selection"), k))
                    break
        assert not missing, f"{len(missing)} picks missing odds tags: {missing[:5]}"

    def test_envelope_carries_odds_provider(self, session):
        body, _ = _get(session, "/api/picks/today?sport=Soccer&leagues=MLS&limit=25")
        env = body.get("odds_provider")
        assert env is not None, "envelope odds_provider missing"
        assert "state" in env and "active_source" in env, env

    def test_live_path_values(self, session):
        body, _ = _get(session, "/api/picks/today?sport=Soccer&leagues=MLS&limit=200")
        env = body.get("odds_provider") or {}
        if env.get("state") != "live" or env.get("active_source") != "odds_api":
            pytest.skip(f"Envelope not on live path: {env}")
        picks = body.get("picks", [])
        for p in picks:
            assert p.get("odds_source") == "odds_api", (p.get("id"), p.get("odds_source"))
            assert p.get("odds_status") == "live", (p.get("id"), p.get("odds_status"))
            assert p.get("confidence_penalty") == 0, (p.get("id"), p.get("confidence_penalty"))

    def test_admin_odds_health(self, session):
        r = session.get(f"{BASE}/api/admin/odds-health", timeout=20)
        assert r.status_code == 200, r.text[:200]
        data = r.json()
        for k in ("state", "active_source", "primary_provider",
                  "api_sports_keys_configured", "failures_in_window"):
            assert k in data, f"missing {k}: {data}"
        assert data["api_sports_keys_configured"] == 3


# ────────────────────────────────────────────────────────────────────
# CHECK 5 — in-process circuit breaker simulation
# ────────────────────────────────────────────────────────────────────
class TestCircuitBreakerInProcess:

    def test_failure3_flips_to_degraded_then_success_restores(self):
        from services import odds_provider

        # Baseline
        odds_provider.report_success()
        assert odds_provider.get_state() == "live"
        assert odds_provider.get_active_source() == "odds_api"

        # 3× 429 → degraded
        odds_provider.report_failure(429, "test1")
        odds_provider.report_failure(429, "test2")
        odds_provider.report_failure(429, "test3")

        # Skip probe so state stays degraded during snapshot
        odds_provider._last_probe_ts = time.time()
        loop = asyncio.new_event_loop()
        try:
            snap = loop.run_until_complete(odds_provider.status())
        finally:
            loop.close()
        assert snap["state"] == "degraded", snap
        assert snap["active_source"] == "api_sports", snap

        # decorate_pick in degraded state
        p = {"lock_score": 95, "edge_percent": 8.5, "id": "iter94_pick"}
        d = odds_provider.decorate_pick(p)
        assert d["odds_source"] == "api_sports", d
        assert d["odds_status"] == "backup", d
        assert d["confidence_penalty"] == -10, d
        assert d["edge_percent"] is None, d
        assert d["lock_score"] == 85.0, d

        # Restore
        odds_provider.report_success()
        odds_provider._last_probe_ts = time.time()
        loop = asyncio.new_event_loop()
        try:
            snap2 = loop.run_until_complete(odds_provider.status())
        finally:
            loop.close()
        assert snap2["state"] == "live", snap2
        assert snap2["active_source"] == "odds_api", snap2

        # decorate on live
        p2 = {"lock_score": 92, "edge_percent": 5.0, "id": "iter94_pick2"}
        d2 = odds_provider.decorate_pick(p2)
        assert d2["odds_source"] == "odds_api"
        assert d2["odds_status"] == "live"
        assert d2["confidence_penalty"] == 0
        assert d2["edge_percent"] == 5.0
        assert d2["lock_score"] == 92


# ────────────────────────────────────────────────────────────────────
# CHECK 6 — data-quality gate spot check
# ────────────────────────────────────────────────────────────────────
class TestMLSDataQualityGateSpot:

    def test_no_mls_scorer_pick_with_zero_samples(self, session):
        body, _ = _get(session, "/api/picks/today?sport=Soccer&leagues=MLS&limit=200")
        picks = body.get("picks", [])
        target = {"anytime_goal_scorer", "anytime_assist",
                  "anytime_goal_involvement", "to_score_or_assist"}
        offenders = []
        for p in picks:
            if _mt(p) not in target:
                continue
            s = p.get("samples") or {}
            games = s.get("games") or 0
            minutes = s.get("minutes") or 0
            gp90 = s.get("goals_per_90") or 0
            ap90 = s.get("assists_per_90") or 0
            npxg = s.get("npxg_per_90") or 0
            if games == 0 and minutes < 180 and gp90 == 0 and ap90 == 0 and npxg == 0:
                offenders.append({
                    "player": p.get("selection"),
                    "market_type": _mt(p),
                    "samples": s,
                })
        assert not offenders, f"{len(offenders)} zero-sample scorer picks slipped through: {offenders[:3]}"


# ────────────────────────────────────────────────────────────────────
# CHECK 7 — perf budgets
# ────────────────────────────────────────────────────────────────────
class TestPerf:

    def test_mlb_under_8s(self, session):
        _, elapsed = _get(session, "/api/picks/today?sport=MLB", timeout=30)
        assert elapsed < 8.0, f"MLB /picks/today took {elapsed:.2f}s (budget 8.0s)"

    def test_soccer_under_12s(self, session):
        _, elapsed = _get(session, "/api/picks/today?sport=Soccer", timeout=45)
        assert elapsed < 12.0, f"Soccer /picks/today took {elapsed:.2f}s (budget 12.0s)"
