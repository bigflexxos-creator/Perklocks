"""Iteration 49 — Regression tests for the user-reported MLB "FINAL leaking
onto today's pick" bug.

Two-layered fix:
  1) Backend /api/mlb/live: dated-key writes are now signal-ranked
     (LIVE > pre-game > FINAL) so yesterday's late-PT FINAL (which carries
     today's UTC date) can't overwrite today's same-matchup game on the
     dated key.
  2) Frontend MLBLiveContext: ±6h window match between live commence_time
     and the pick's event_time disambiguates same-matchup games stamped
     with the same UTC date.

These tests cover (1) directly and simulate (2) against the live payload.
"""
import os
import requests
import pytest
from datetime import datetime, timezone, timedelta

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "https://canonical-parity.preview.emergentagent.com").rstrip("/")
CREDENTIALS = {"email": "demo@lockscore.ai", "password": "demo123"}


@pytest.fixture(scope="module")
def token():
    r = requests.post(f"{BASE_URL}/api/auth/login", json=CREDENTIALS, timeout=30)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    tok = r.json().get("access_token")
    assert tok, "no access_token on login"
    return tok


@pytest.fixture(scope="module")
def auth(token):
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------------
# /api/mlb/live shape contract
# ------------------------------------------------------------------
class TestMLBLiveShape:
    def test_endpoint_200(self, auth):
        r = requests.get(f"{BASE_URL}/api/mlb/live", headers=auth, timeout=30)
        assert r.status_code == 200, r.text
        data = r.json()
        assert "games" in data
        assert "as_of" in data
        assert isinstance(data["games"], dict)

    def test_keys_have_expected_variants(self, auth):
        """Each game should produce at least one of:
           'Away @ Home', 'Away @ Home|YYYY-MM-DD', 'mlb_<id>'."""
        r = requests.get(f"{BASE_URL}/api/mlb/live", headers=auth, timeout=30)
        games = r.json()["games"]
        if not games:
            pytest.skip("no MLB games on the board")
        has_bare = any(("@" in k and "|" not in k and not k.startswith("mlb_")) for k in games)
        has_dated = any(("|" in k and k.count("-") >= 2) for k in games)
        has_id   = any(k.startswith("mlb_") for k in games)
        assert has_bare or has_dated or has_id, f"unexpected key shapes: {list(games)[:5]}"

    def test_game_entry_fields(self, auth):
        r = requests.get(f"{BASE_URL}/api/mlb/live", headers=auth, timeout=30)
        games = r.json()["games"]
        if not games:
            pytest.skip("no MLB games on the board")
        k = next(iter(games))
        g = games[k]
        for fld in ("home", "away", "status", "abstract_status",
                    "is_live", "is_final", "commence_time"):
            assert fld in g, f"missing {fld} in {k}: {g}"


# ------------------------------------------------------------------
# Signal-ranking on the DATED key — the core of the backend fix
# ------------------------------------------------------------------
class TestDatedKeySignalRanking:
    def test_dated_key_is_not_final_when_same_date_has_non_final_entry(self, auth):
        """If today's UTC date has BOTH a FINAL (yesterday-PT) and a non-FINAL
        (today's afternoon) game for the same matchup, the dated key must
        carry the non-FINAL entry.

        We can't force this collision on demand, but we can verify the
        INVARIANT: for every dated key whose matchup ALSO appears as a
        non-dated bare key, if the bare key is non-FINAL then the dated
        key must NOT be FINAL either (signal ranking matches).
        """
        r = requests.get(f"{BASE_URL}/api/mlb/live", headers=auth, timeout=30)
        games = r.json()["games"]
        if not games:
            pytest.skip("no MLB games on the board")
        # Build matchup → dated entries map
        violations = []
        for k, g in games.items():
            if "|" not in k or k.startswith("mlb_"):
                continue
            matchup, date_part = k.rsplit("|", 1)
            bare = games.get(matchup)
            if not bare:
                continue
            # If bare picked a LIVE/pre-game, the dated entry for THAT date
            # should also be LIVE/pre-game if it represents the same game.
            # We can't perfectly tell without the schedule, but commence
            # alignment within 6h is the same heuristic the frontend uses.
            bare_t = bare.get("commence_time") or ""
            dated_t = g.get("commence_time") or ""
            try:
                if bare_t and dated_t:
                    bt = datetime.fromisoformat(bare_t.replace("Z", "+00:00"))
                    dt = datetime.fromisoformat(dated_t.replace("Z", "+00:00"))
                    same_game = abs((bt - dt).total_seconds()) <= 6 * 3600
                    if same_game and not bare.get("is_final") and g.get("is_final"):
                        violations.append((k, bare.get("status"), g.get("status")))
            except Exception:
                continue
        assert not violations, f"dated key carries FINAL where bare key has non-FINAL same-game: {violations}"


# ------------------------------------------------------------------
# Frontend ±6h lookup simulation against today's picks
# ------------------------------------------------------------------
def _simulate_lookup(games: dict, event: str, event_time: str | None):
    """Mirror of MLBLiveContext.lookup — used to verify the fix from
    the frontend's perspective without spinning up the RN runtime."""
    def same_scheduled(g):
        if not g:
            return False
        if not event_time or len(event_time) < 10:
            return False
        try:
            pt = datetime.fromisoformat(event_time.replace("Z", "+00:00"))
            lt = datetime.fromisoformat((g.get("commence_time") or "").replace("Z", "+00:00"))
        except Exception:
            return False
        return abs((pt - lt).total_seconds()) <= 6 * 3600

    if event_time and len(event_time) >= 10:
        dated = games.get(f"{event}|{event_time[:10]}")
        if dated and same_scheduled(dated):
            return dated
    g = games.get(event)
    if g and same_scheduled(g):
        return g
    return None


class TestFrontendLookupSimulation:
    def test_no_pick_gets_a_stale_final_badge(self, auth):
        """For every MLB pick on /api/picks/today, simulate the FIXED
        frontend lookup. Pre-game picks (event_time > now) must NEVER
        return a FINAL game from the live map."""
        # Pull today's picks
        r = requests.get(f"{BASE_URL}/api/picks/today?sport=MLB&limit=100", headers=auth, timeout=60)
        assert r.status_code == 200, r.text
        payload = r.json()
        picks = payload.get("picks", payload) if isinstance(payload, dict) else payload
        mlb_picks = [p for p in picks if isinstance(p, dict) and (
            (p.get("sport_key") or "").startswith("baseball")
            or (p.get("sport") or "").lower() in {"mlb", "baseball"}
        )]
        if not mlb_picks:
            pytest.skip("no MLB picks on the board today")

        r2 = requests.get(f"{BASE_URL}/api/mlb/live", headers=auth, timeout=30)
        games = r2.json()["games"]

        now = datetime.now(timezone.utc)
        leaks = []
        live_or_pregame_hits = 0
        for p in mlb_picks:
            event = p.get("event") or p.get("matchup")
            ev_time = p.get("event_time") or p.get("commence_time")
            if not event:
                continue
            g = _simulate_lookup(games, event, ev_time)
            if not g:
                continue
            try:
                evt_dt = datetime.fromisoformat((ev_time or "").replace("Z", "+00:00"))
            except Exception:
                continue
            # Bug pattern: pick is in the future but lookup returned a FINAL game.
            if g.get("is_final") and evt_dt > now + timedelta(minutes=10):
                leaks.append({
                    "pick_id": p.get("pick_id") or p.get("id"),
                    "event": event,
                    "event_time": ev_time,
                    "matched_live_commence": g.get("commence_time"),
                    "status": g.get("status"),
                })
            elif g.get("is_live") or (not g.get("is_final")):
                live_or_pregame_hits += 1
        assert not leaks, f"FINAL leaked onto upcoming pick(s): {leaks}"
        # Soft assertion — informational
        print(f"  → simulated {len(mlb_picks)} MLB picks, {live_or_pregame_hits} legit live/pre-game matches, 0 leaks")

    def test_window_does_not_break_legit_live_matches(self, auth):
        """For any LIVE game in the map, simulating a lookup with the
        game's own commence_time MUST return that game (±6h is generous,
        same matchup never plays twice within that window)."""
        r = requests.get(f"{BASE_URL}/api/mlb/live", headers=auth, timeout=30)
        games = r.json()["games"]
        live_games = [(k, g) for k, g in games.items()
                      if g.get("is_live") and "|" not in k and not k.startswith("mlb_")]
        if not live_games:
            pytest.skip("no LIVE MLB games right now")
        for k, g in live_games:
            res = _simulate_lookup(games, k, g.get("commence_time"))
            assert res is not None, f"legit live lookup returned None for {k}"
            assert res.get("is_live"), f"expected LIVE for {k} but got {res.get('status')}"


# ------------------------------------------------------------------
# Backwards compatibility — picks/today still serves an MLB slate
# ------------------------------------------------------------------
class TestPicksTodayRegression:
    def test_picks_today_ok(self, auth):
        r = requests.get(f"{BASE_URL}/api/picks/today", headers=auth, timeout=60)
        assert r.status_code == 200
        data = r.json()
        # Accept either bare list or {"picks": [...]} envelope
        picks = data.get("picks", data) if isinstance(data, dict) else data
        assert isinstance(picks, list)
        if picks:
            p = picks[0]
            assert "pick_id" in p or "id" in p
