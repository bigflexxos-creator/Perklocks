"""
iter-92 verification tests for Soccer market-family tiebreaker in
`/api/picks/today?sort=lock` (default).

Change under test: `_soccer_family_rank` in
`/app/backend/routes/picks_routes.py` (~line 1806) — within same lock_score
bucket, Soccer picks now order:
  0=Anytime Goal Scorer
  1=To Score or Assist / Score or Assist
  2=First / Last Goal Scorer
  3=Anytime Assist
  4=Other

Non-Soccer picks get rank 0 → unchanged behaviour.
"""

import os
import time
import pytest
import requests

BASE = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "https://canonical-parity.preview.emergentagent.com").rstrip("/")
EMAIL = "demo@lockscore.ai"
PASSWORD = "demo123"


@pytest.fixture(scope="module")
def auth_session():
    s = requests.Session()
    r = s.post(f"{BASE}/api/auth/login",
               json={"email": EMAIL, "password": PASSWORD}, timeout=30)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:200]}"
    tok = r.json()["access_token"]
    s.headers.update({"Authorization": f"Bearer {tok}",
                      "Content-Type": "application/json"})
    return s


def _fetch(session, path):
    r = session.get(f"{BASE}{path}", timeout=60)
    assert r.status_code == 200, f"{path} -> {r.status_code} {r.text[:200]}"
    body = r.json()
    picks = body.get("picks") if isinstance(body, dict) else body
    assert isinstance(picks, list), f"no picks list in {path}"
    return picks


def _market(p):
    return (p.get("market") or "").strip()


# ─── PRIMARY FIX: MLS goalscorers surface in top-10 ─────────────────
class TestSoccerMLSGoalscorerSurfacing:

    def test_mls_top10_has_at_least_3_anytime_goal_scorers(self, auth_session):
        # NOTE: backend default sort is "time" but frontend calls with sort=lock
        # (see useFilters.tsx default sortKey="lock"). The iter-92 fix is scoped
        # to the sort=lock branch. Explicit sort=lock matches real user traffic.
        picks = _fetch(auth_session, "/api/picks/today?sport=Soccer&leagues=MLS&sort=lock&limit=25")
        if len(picks) < 10:
            pytest.skip(f"MLS slate too small ({len(picks)} picks) — cannot assess top-10")
        top10 = picks[:10]
        goalscorer_count = sum(1 for p in top10 if "Anytime Goal Scorer" in _market(p))
        markets_seen = [_market(p) for p in top10]
        assert goalscorer_count >= 3, (
            f"expected >=3 'Anytime Goal Scorer' in top10, got {goalscorer_count}. "
            f"Markets: {markets_seen}"
        )

    def test_mls_top5_not_exclusively_anytime_assist(self, auth_session):
        picks = _fetch(auth_session, "/api/picks/today?sport=Soccer&leagues=MLS&sort=lock&limit=10")
        if len(picks) < 5:
            pytest.skip(f"MLS slate too small ({len(picks)} picks)")
        top5_markets = [_market(p) for p in picks[:5]]
        all_assist = all("Anytime Assist" in m for m in top5_markets)
        assert not all_assist, (
            f"top5 MLS is exclusively 'Anytime Assist' — regression! Markets: {top5_markets}"
        )

    def test_mls_goalscorer_before_assist_within_same_lock_bucket(self, auth_session):
        """Within the same lock_score, no Anytime Assist may precede any Anytime Goal Scorer."""
        picks = _fetch(auth_session, "/api/picks/today?sport=Soccer&leagues=MLS&sort=lock&limit=50")
        # Group by lock_score
        from collections import defaultdict
        by_lock = defaultdict(list)
        for idx, p in enumerate(picks):
            by_lock[p.get("lock_score", 0)].append((idx, _market(p)))
        violations = []
        for lock, entries in by_lock.items():
            first_assist_idx = None
            for idx, mkt in entries:
                if "Anytime Assist" in mkt and first_assist_idx is None:
                    first_assist_idx = idx
                if "Anytime Goal Scorer" in mkt and first_assist_idx is not None and idx > first_assist_idx:
                    violations.append(f"lock={lock}: Assist at pos {first_assist_idx} before Goal Scorer at {idx}")
        assert not violations, "; ".join(violations)


# ─── SECONDARY: Soccer no-league-filter also gets goalscorer priority ─
class TestSoccerAllLeagues:

    def test_soccer_top10_includes_scorer_or_score_or_assist(self, auth_session):
        picks = _fetch(auth_session, "/api/picks/today?sport=Soccer&sort=lock&limit=25")
        if len(picks) < 10:
            pytest.skip(f"Soccer slate too small ({len(picks)} picks)")
        top10 = picks[:10]
        has_scorer = any(
            "Anytime Goal Scorer" in _market(p)
            or "Score or Assist" in _market(p)
            or "First Goal Scorer" in _market(p)
            or "Last Goal Scorer" in _market(p)
            for p in top10
        )
        markets = [_market(p) for p in top10]
        assert has_scorer, f"no scorer/score-or-assist in top10 Soccer: {markets}"


# ─── REGRESSION: MLB order stable ───────────────────────────────────
class TestMLBRegression:

    def test_mlb_sort_lock_still_descends_by_lock_score(self, auth_session):
        picks = _fetch(auth_session, "/api/picks/today?sport=MLB&sort=lock&limit=25")
        if len(picks) < 5:
            pytest.skip(f"MLB slate too small")
        locks = [p.get("lock_score", 0) for p in picks]
        # Monotonically non-increasing
        for i in range(len(locks) - 1):
            assert locks[i] >= locks[i + 1], (
                f"MLB order broken at index {i}: {locks[i]} < {locks[i+1]}. Full: {locks}"
            )


# ─── REGRESSION: alternate sort modes unaffected ────────────────────
class TestOtherSortModes:

    def test_soccer_sort_edge_orders_by_edge_percent_desc(self, auth_session):
        picks = _fetch(auth_session, "/api/picks/today?sport=Soccer&sort=edge&limit=20")
        if len(picks) < 3:
            pytest.skip("Soccer slate too small for sort=edge test")
        edges = [p.get("edge_percent", 0) for p in picks]
        for i in range(len(edges) - 1):
            assert edges[i] >= edges[i + 1] - 0.001, (
                f"sort=edge broken at {i}: {edges[i]} < {edges[i+1]}. Full: {edges}"
            )

    def test_soccer_sort_time_orders_by_kickoff(self, auth_session):
        # sort=time uses backend default direction=desc → latest kickoff first.
        # Only asserts monotonic ordering, doesn't dictate direction.
        picks = _fetch(auth_session, "/api/picks/today?sport=Soccer&sort=time&limit=20")
        if len(picks) < 3:
            pytest.skip("Soccer slate too small for sort=time test")
        times = []
        for p in picks:
            t = p.get("kickoff") or p.get("start_time") or p.get("event_time")
            if t:
                times.append(t)
        if len(times) < 3:
            pytest.skip("no kickoff/start_time on picks — cannot assess sort=time")
        asc = all(times[i] <= times[i + 1] for i in range(len(times) - 1))
        desc = all(times[i] >= times[i + 1] for i in range(len(times) - 1))
        assert asc or desc, (
            f"sort=time not monotonic. Sample: {times[:6]}"
        )


# ─── REGRESSION: iter-91 H2H bullets still appear ───────────────────
class TestIter91H2HRegression:

    def test_mlb_picks_carry_h2h_tailwind_or_headwind(self, auth_session):
        picks = _fetch(auth_session, "/api/picks/today?sport=MLB&sort=lock&limit=100")
        # iter-91 H2H bullets attach to batter picks (Hits/HR/RBI etc.). Skip if
        # today's MLB slate has no batter picks (team totals / moneyline only).
        batter_markets = ("Hits", "HR", "Home Run", "RBI", "Total Bases",
                          "Runs", "Strikeouts", "Walks")
        has_batter = any(
            any(m in (p.get("market") or "") for m in batter_markets)
            for p in picks
        )
        if not has_batter:
            pytest.skip(
                f"MLB slate has no batter picks ({len(picks)} team-market picks only); "
                f"iter-91 H2H bullets not applicable today."
            )
        h2h_count = 0
        for p in picks:
            bullets = p.get("why_this_pick") or []
            for b in bullets:
                text = b if isinstance(b, str) else (b.get("text") or "")
                if "H2H tailwind" in text or "H2H headwind" in text:
                    h2h_count += 1
                    break
        assert h2h_count >= 1, (
            f"iter-91 regression: no H2H tailwind/headwind bullet across {len(picks)} MLB picks"
        )


# ─── REGRESSION: iter-82 no opposite Over/Under on same event ───────
class TestIter82ContradictionRegression:

    def test_no_line_specific_contradictions_in_soccer(self, auth_session):
        picks = _fetch(auth_session, "/api/picks/today?sport=Soccer&sort=lock&limit=100")
        # signature: (event_id, selection, market_family_key, numeric_line)
        seen = {}
        contradictions = []
        for p in picks:
            eid = p.get("event_id") or p.get("event")
            sel = p.get("selection") or p.get("player") or ""
            mkt = _market(p)
            side = None
            if " Over " in mkt or mkt.endswith(" Over") or "Over " in mkt:
                side = "Over"
            elif " Under " in mkt or mkt.endswith(" Under") or "Under " in mkt:
                side = "Under"
            if not side:
                continue
            # strip side
            fam = mkt.replace(" Over", "").replace(" Under", "").strip()
            line = p.get("line") or p.get("point")
            key = (eid, sel, fam, line)
            if key in seen and seen[key] != side:
                contradictions.append(f"{sel} {mkt} vs {seen[key]} on {eid}")
            seen[key] = side
        assert not contradictions, "; ".join(contradictions[:5])


# ─── PERF: Soccer response time within 3× baseline (< ~9s) ──────────
class TestSoccerPerf:

    def test_soccer_response_time_under_9s(self, auth_session):
        t = time.perf_counter()
        _fetch(auth_session, "/api/picks/today?sport=Soccer&sort=lock&limit=25")
        elapsed = time.perf_counter() - t
        assert elapsed < 9.0, f"Soccer /picks/today took {elapsed:.2f}s (budget 9.0s)"
