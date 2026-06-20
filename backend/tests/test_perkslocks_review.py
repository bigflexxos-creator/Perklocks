"""Backend review tests for PerksLocks.
Covers: conflicting picks dedupe, Soccer Over 1.5 Poisson synthesis,
MLB live shape, Player Intel endpoints, Auto-Elite MLB, full-slate dedupe.
"""
import os
import re
import pytest
import requests

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "https://bet-edge-ai-1.preview.emergentagent.com").rstrip("/")

# Market patterns that count as "game-outcome" picks (Moneyline / Win-or-Draw / Double Chance / etc.)
GAME_OUTCOME_PATTERNS = [
    re.compile(r"moneyline", re.I),
    re.compile(r"win or draw", re.I),
    re.compile(r"double chance", re.I),
    re.compile(r"\bto win\b", re.I),
    re.compile(r"match result", re.I),
    re.compile(r"match winner", re.I),
    re.compile(r"\bh2h\b", re.I),
]


def _is_game_outcome(market: str) -> bool:
    if not market:
        return False
    return any(p.search(market) for p in GAME_OUTCOME_PATTERNS)


@pytest.fixture(scope="module")
def auth_token():
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": "demo@lockscore.ai", "password": "demo123"},
        timeout=30,
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
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
        timeout=60,
    )
    assert r.status_code == 200, f"soccer picks failed: {r.status_code} {r.text[:300]}"
    return r.json()


def _picks_list(payload):
    """Normalize response into a flat list of pick dicts."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("picks", "items", "data", "results"):
            if key in payload and isinstance(payload[key], list):
                return payload[key]
    return []


def _event_key(p):
    # Try common fields used to group picks per event
    for k in ("event", "match", "game", "event_key", "matchup"):
        if p.get(k):
            return p[k]
    home = p.get("home_team") or p.get("home")
    away = p.get("away_team") or p.get("away")
    if home and away:
        return f"{away} @ {home}"
    return p.get("game_id") or p.get("event_id") or str(p.get("id", ""))


# ---------- Test 1: Conflicting picks fix ----------
class TestGameOutcomeDedupeSoccer:
    def test_no_event_has_more_than_one_game_outcome_pick(self, soccer_picks):
        picks = _picks_list(soccer_picks)
        assert picks, "no soccer picks returned"
        from collections import defaultdict
        counts = defaultdict(list)
        for p in picks:
            mkt = p.get("market") or p.get("market_name") or ""
            if _is_game_outcome(mkt):
                counts[_event_key(p)].append(mkt)
        offenders = {k: v for k, v in counts.items() if len(v) > 1}
        print(f"\n[T1] events with >1 game-outcome pick: {len(offenders)}")
        for k, v in list(offenders.items())[:5]:
            print(f"   {k}: {v}")
        assert not offenders, f"events with conflicting picks: {offenders}"

    def test_sweden_netherlands_has_exactly_one_game_outcome(self, soccer_picks):
        picks = _picks_list(soccer_picks)
        target = [p for p in picks
                  if "Sweden" in _event_key(p) and "Netherlands" in _event_key(p)]
        print(f"\n[T1b] Sweden/Netherlands total picks: {len(target)}")
        if not target:
            pytest.skip("Sweden @ Netherlands not on slate today")
        go = [p for p in target if _is_game_outcome(p.get("market") or p.get("market_name") or "")]
        print(f"   game-outcome picks: {[p.get('market') for p in go]}")
        assert len(go) == 1, f"expected exactly 1 game-outcome pick, got {len(go)}"


# ---------- Test 2: Soccer Over 1.5 Poisson synthesis ----------
class TestOver15Poisson:
    def test_at_least_10_over_15_picks(self, soccer_picks):
        picks = _picks_list(soccer_picks)
        over15 = [p for p in picks
                  if "total goals over 1.5" in (p.get("market") or p.get("market_name") or "").lower()]
        print(f"\n[T2] Over 1.5 picks: {len(over15)}")
        assert len(over15) >= 10, f"expected >=10 Over 1.5 picks, got {len(over15)}"
        # All carry model_line / model_source
        missing_flag = [p for p in over15 if not p.get("model_line")]
        missing_src = [p for p in over15 if p.get("model_source") != "poisson_from_main_total"]
        print(f"   missing model_line: {len(missing_flag)}, wrong/missing model_source: {len(missing_src)}")
        assert not missing_flag, "some Over 1.5 picks missing model_line=true"
        assert not missing_src, "some Over 1.5 picks missing model_source=poisson_from_main_total"

    def test_sweden_netherlands_over_15_lock_score(self, soccer_picks):
        picks = _picks_list(soccer_picks)
        over15 = [p for p in picks
                  if "total goals over 1.5" in (p.get("market") or p.get("market_name") or "").lower()
                  and "Sweden" in _event_key(p) and "Netherlands" in _event_key(p)]
        print(f"\n[T2b] Sweden/Netherlands Over 1.5 picks: {len(over15)}")
        if not over15:
            pytest.skip("Sweden @ Netherlands not on slate today")
        scores = [p.get("lock_score") for p in over15]
        print(f"   lock_scores: {scores}")
        assert any((s or 0) >= 90 for s in scores), f"no Over 1.5 with lock_score>=90, got {scores}"


# ---------- Test 3: MLB live shape ----------
class TestMLBLiveShape:
    def test_reds_yankees_has_plain_and_dated_keys(self, headers):
        r = requests.get(f"{BASE_URL}/api/mlb/live", headers=headers, timeout=60)
        assert r.status_code == 200, f"mlb/live failed: {r.status_code} {r.text[:300]}"
        data = r.json()
        # Response is {"games": {...}, "as_of": "..."} — descend into games map.
        if isinstance(data, dict) and "games" in data and isinstance(data["games"], dict):
            games = data["games"]
        elif isinstance(data, dict):
            games = data
        else:
            pytest.fail(f"expected dict, got {type(data)}")
        keys = list(games.keys())
        print(f"\n[T3] /api/mlb/live games total keys: {len(keys)}")
        plain_key = "Cincinnati Reds @ New York Yankees"
        plain_match = [k for k in keys if k == plain_key]
        dated_match = [k for k in keys if k.startswith(plain_key + "|")]
        print(f"   plain matches: {plain_match}")
        print(f"   dated matches: {dated_match[:5]}")
        if not plain_match and not dated_match:
            pytest.skip("Reds @ Yankees not on slate")
        assert plain_match, f"plain key '{plain_key}' missing"
        plain_payload = games[plain_key]
        assert isinstance(plain_payload, dict), f"plain payload not dict: {type(plain_payload)}"
        assert "commence_time" in plain_payload, f"plain payload missing commence_time. keys={list(plain_payload.keys())}"
        ct = plain_payload["commence_time"]
        assert isinstance(ct, str) and ("T" in ct), f"commence_time not ISO: {ct}"
        assert dated_match, "no dated Reds @ Yankees key found"
        # verify dated key format
        for k in dated_match:
            suffix = k.split("|", 1)[1]
            assert re.match(r"^\d{4}-\d{2}-\d{2}$", suffix), f"bad dated suffix: {suffix}"


# ---------- Test 4: Player Intelligence ----------
class TestPlayerIntel:
    def test_refresh_returns_counts(self, headers):
        r = requests.post(f"{BASE_URL}/api/player-intel/refresh", headers=headers, timeout=120)
        assert r.status_code == 200, f"refresh failed: {r.status_code} {r.text[:300]}"
        body = r.json()
        print(f"\n[T4-refresh] {body}")
        for key in ("seeded_new", "learned_updates", "total_profiles"):
            assert key in body, f"missing key {key} in {body}"
        assert body["total_profiles"] >= 100, f"total_profiles {body['total_profiles']} < 100"

    @pytest.mark.parametrize("name,sport,expected_name,expected_archetype", [
        ("Mbappe", "Soccer", "Kylian Mbappé", "high-xG attacker"),
        ("Mahomes", "NFL", "Patrick Mahomes", "dual-threat QB"),
        ("Sinner", "Tennis", "Jannik Sinner", None),
        ("LeBron James", "NBA", "LeBron James", "two-way wing"),
    ])
    def test_profile_lookup(self, headers, name, sport, expected_name, expected_archetype):
        r = requests.get(
            f"{BASE_URL}/api/player-intel/profile",
            params={"name": name, "sport": sport},
            headers=headers,
            timeout=30,
        )
        assert r.status_code == 200, f"profile {name}/{sport} failed: {r.status_code} {r.text[:300]}"
        body = r.json()
        # Endpoint wraps response in {"profile": {...}}; fall back to flat shape just in case.
        prof = body.get("profile") if isinstance(body, dict) and "profile" in body else body
        print(f"\n[T4-profile {name}] {prof}")
        assert prof and prof.get("canonical_name") == expected_name, \
            f"canonical_name mismatch: got {prof.get('canonical_name') if prof else None!r}, expected {expected_name!r}"
        if expected_archetype:
            assert prof.get("archetype") == expected_archetype, \
                f"archetype mismatch for {name}: got {prof.get('archetype')!r}, expected {expected_archetype!r}"

    def test_list_soccer(self, headers):
        r = requests.get(
            f"{BASE_URL}/api/player-intel/list",
            params={"sport": "Soccer", "limit": 10},
            headers=headers,
            timeout=30,
        )
        assert r.status_code == 200, f"list failed: {r.status_code} {r.text[:300]}"
        body = r.json()
        items = body if isinstance(body, list) else (body.get("items") or body.get("players") or body.get("data") or [])
        print(f"\n[T4-list soccer] count={len(items)}, sample={items[0] if items else None}")
        assert len(items) >= 10, f"expected >=10 players, got {len(items)}"
        # All should have archetype, team, position
        for it in items:
            assert it.get("archetype"), f"missing archetype: {it}"
            assert it.get("team"), f"missing team: {it}"
            assert it.get("position"), f"missing position: {it}"


# ---------- Test 5: Auto-Elite MLB ----------
class TestAutoEliteMLB:
    def test_auto_elite_mlb_no_500(self, headers):
        r = requests.get(f"{BASE_URL}/api/auto-elite", params={"sport": "MLB"}, headers=headers, timeout=60)
        print(f"\n[T5] auto-elite MLB status={r.status_code}, body_preview={r.text[:200]}")
        assert r.status_code == 200, f"auto-elite MLB returned {r.status_code}: {r.text[:300]}"
        body = r.json()
        # Acceptable: list or dict; empty is fine
        assert isinstance(body, (list, dict)), f"unexpected type {type(body)}"


# ---------- Test 6: Full-slate game-outcome dedupe ----------
class TestFullSlateDedupe:
    def test_no_duplicate_game_outcome_picks_full_slate(self, headers):
        r = requests.get(f"{BASE_URL}/api/picks/today", headers=headers, timeout=120)
        assert r.status_code == 200, f"full slate failed: {r.status_code} {r.text[:300]}"
        picks = _picks_list(r.json())
        print(f"\n[T6] full-slate total picks: {len(picks)}")
        from collections import defaultdict
        counts = defaultdict(list)
        for p in picks:
            mkt = p.get("market") or p.get("market_name") or ""
            if _is_game_outcome(mkt):
                counts[(p.get("sport"), _event_key(p))].append(mkt)
        offenders = {k: v for k, v in counts.items() if len(v) > 1}
        print(f"   events with >1 game-outcome pick: {len(offenders)}")
        for k, v in list(offenders.items())[:10]:
            print(f"   {k}: {v}")
        assert not offenders, f"full-slate dedupe violations: {offenders}"
