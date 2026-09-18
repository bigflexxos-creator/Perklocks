"""
Iteration 139 — PerkLocks Final Universal Root Closure verification.

Covers:
  - Truth manifest on /api/picks/today?lite=true
  - Board <-> detail parity (list row vs /api/picks/{id})
  - NFL ATD single slate endpoint /api/nfl/atd/slate
  - Rollover immutable official slate
"""
import os
import time
import pytest
import requests

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "https://canonical-parity.preview.emergentagent.com").rstrip("/")
EMAIL = "demo@lockscore.ai"
PASSWORD = "demo123"

_TOKEN = None
_HEADERS = None


def _auth_headers():
    global _TOKEN, _HEADERS
    if _HEADERS:
        return _HEADERS
    r = requests.post(f"{BASE_URL}/api/auth/login", json={"email": EMAIL, "password": PASSWORD}, timeout=30)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:200]}"
    _TOKEN = r.json()["access_token"]
    _HEADERS = {"Authorization": f"Bearer {_TOKEN}", "Content-Type": "application/json"}
    return _HEADERS


@pytest.fixture(scope="module")
def headers():
    return _auth_headers()


@pytest.fixture(scope="module")
def today_lite(headers):
    # cold call may take 10-20s
    r = requests.get(f"{BASE_URL}/api/picks/today?lite=true", headers=headers, timeout=90)
    assert r.status_code == 200, f"picks/today lite failed: {r.status_code} {r.text[:200]}"
    return r.json()


# ------------------------- Truth manifest ------------------------- #
class TestTruthManifest:
    def test_manifest_present(self, today_lite):
        tm = today_lite.get("truth_manifest")
        assert isinstance(tm, dict), "truth_manifest missing"
        for k in ("api_origin", "environment", "board_version", "generated_at", "data_as_of"):
            assert k in tm, f"truth_manifest missing key {k}"

    def test_board_version_top_level(self, today_lite):
        assert "board_version" in today_lite, "top-level board_version missing"
        assert today_lite["board_version"], "board_version empty"

    def test_pick_truth_fingerprint(self, today_lite):
        picks = today_lite.get("picks") or []
        assert picks, "no picks returned"
        sample = picks[:10]
        for p in sample:
            assert "truth_fingerprint" in p, f"pick missing truth_fingerprint: {p.get('id')}"
            assert "publication_version" in p, f"pick missing publication_version: {p.get('id')}"


# ------------------------- List <-> detail parity ------------------------- #
class TestBoardDetailParity:
    def _keys(self, p):
        return (
            p.get("lock_score"),
            p.get("win_probability"),
            p.get("edge_percent"),
            p.get("grade"),
            p.get("truth_fingerprint"),
        )

    def test_parity_first_5_picks(self, headers, today_lite):
        picks = today_lite.get("picks") or []
        assert len(picks) >= 5, "need at least 5 picks"
        mismatches = []
        checked = 0
        for row in picks:
            pid = row.get("id") or row.get("pick_id")
            if not pid:
                continue
            r = requests.get(f"{BASE_URL}/api/picks/{pid}", headers=headers, timeout=30)
            if r.status_code != 200:
                mismatches.append({"id": pid, "status": r.status_code, "body": r.text[:120]})
                continue
            detail = r.json()
            # Detail endpoint returns pick fields at top level (nested "pick" is a legacy string field)
            det = detail if "lock_score" in detail else (detail.get("pick") or detail)
            row_tuple = self._keys(row)
            det_tuple = self._keys(det)
            if row_tuple != det_tuple:
                mismatches.append({
                    "id": pid,
                    "row": row_tuple,
                    "detail": det_tuple,
                })
            checked += 1
            if checked >= 5:
                break
        assert checked >= 5, f"only checked {checked} picks"
        assert not mismatches, f"parity mismatches: {mismatches}"

    def test_dortmund_btts_yes(self, headers, today_lite):
        """Locate Dortmund @ Stuttgart BTTS Yes and verify list=detail."""
        picks = today_lite.get("picks") or []
        target = None
        for p in picks:
            hay = " ".join([
                str(p.get("home_team", "")),
                str(p.get("away_team", "")),
                str(p.get("event", "")),
                str(p.get("event_name", "")),
                str(p.get("match", "")),
                str(p.get("market", "")),
                str(p.get("selection", "")),
            ]).lower()
            if ("dortmund" in hay and "stuttgart" in hay and "btts" in hay and "yes" in hay):
                target = p
                break
        if not target:
            pytest.skip("Dortmund @ Stuttgart BTTS Yes not on live slate")
        pid = target.get("id") or target.get("pick_id")
        r = requests.get(f"{BASE_URL}/api/picks/{pid}", headers=headers, timeout=30)
        assert r.status_code == 200, r.text[:200]
        det_resp = r.json()
        det = det_resp if "lock_score" in det_resp else (det_resp.get("pick") or det_resp)
        # Report values, allow tolerance on floats
        row_ls = target.get("lock_score")
        det_ls = det.get("lock_score")
        row_wp = target.get("win_probability")
        det_wp = det.get("win_probability")
        row_edge = target.get("edge_percent")
        det_edge = det.get("edge_percent")
        row_grade = target.get("grade")
        det_grade = det.get("grade")
        print(f"[DORTMUND_BTTS] row LS={row_ls} WP={row_wp} EDGE={row_edge} GRADE={row_grade}")
        print(f"[DORTMUND_BTTS] det LS={det_ls} WP={det_wp} EDGE={det_edge} GRADE={det_grade}")
        assert row_ls == det_ls, f"lock_score mismatch: {row_ls} vs {det_ls}"
        assert row_wp == det_wp, f"win_probability mismatch: {row_wp} vs {det_wp}"
        assert row_edge == det_edge, f"edge_percent mismatch: {row_edge} vs {det_edge}"
        assert row_grade == det_grade, f"grade mismatch: {row_grade} vs {det_grade}"


# ------------------------- ATD slate ------------------------- #
class TestNflAtdSlate:
    def test_slate_shape(self, headers):
        r = requests.get(f"{BASE_URL}/api/nfl/atd/slate", headers=headers, timeout=60)
        assert r.status_code == 200, f"slate failed: {r.status_code} {r.text[:200]}"
        data = r.json()
        for k in ("board_version", "publication_version", "generated_at", "data_as_of", "universe_count", "top5", "games"):
            assert k in data, f"missing key {k}"
        assert isinstance(data["top5"], list), "top5 not a list"
        assert isinstance(data["games"], list), "games not a list"

    def test_top5_consistency_with_leaderboard(self, headers):
        r_slate = requests.get(f"{BASE_URL}/api/nfl/atd/slate", headers=headers, timeout=60)
        r_leader = requests.get(f"{BASE_URL}/api/nfl/atd/leaderboard", headers=headers, timeout=60)
        assert r_slate.status_code == 200
        assert r_leader.status_code == 200, r_leader.text[:200]
        slate = r_slate.json()
        leader = r_leader.json()
        leader_picks = leader.get("picks") or leader.get("leaderboard") or []
        assert leader_picks, "leaderboard empty"
        # top5 first-5 comparison by player identity
        def _id(x):
            return (
                x.get("player_id")
                or x.get("player_name")
                or x.get("player")
                or x.get("name")
            )
        top5_ids = [_id(x) for x in slate["top5"][:5]]
        leader_ids = [_id(x) for x in leader_picks[:5]]
        assert top5_ids == leader_ids, f"top5 mismatch: {top5_ids} vs {leader_ids}"

    def test_games_no_duplicate_players(self, headers):
        r = requests.get(f"{BASE_URL}/api/nfl/atd/slate", headers=headers, timeout=60)
        assert r.status_code == 200
        for g in r.json().get("games", []):
            cands = g.get("candidates") or []
            names = [c.get("player_name") or c.get("player") or c.get("name") for c in cands]
            names = [n for n in names if n]
            assert len(names) == len(set(names)), f"duplicate players in game {g.get('canonical_event_id')}: {names}"

    def test_games_shape(self, headers):
        r = requests.get(f"{BASE_URL}/api/nfl/atd/slate", headers=headers, timeout=60)
        for g in r.json().get("games", []):
            for k in ("canonical_event_id", "away_team", "home_team", "commence_time", "candidates", "top"):
                assert k in g, f"game missing key {k}"


# ------------------------- Rollover immutability ------------------------- #
class TestRolloverImmutable:
    def test_rollover_shape(self, headers):
        r = requests.get(f"{BASE_URL}/api/picks/rollover", headers=headers, timeout=60)
        assert r.status_code == 200, f"rollover failed {r.status_code} {r.text[:200]}"
        data = r.json()
        assert data.get("rollover_version") == "v6-official-slate", f"unexpected rollover_version {data.get('rollover_version')}"
        assert isinstance(data.get("slate"), dict), "missing slate object"
        legs = data.get("picks") or data.get("slate", {}).get("legs") or []
        assert len(legs) == 3, f"expected 3 legs, got {len(legs)}"

    def test_rollover_stable_across_calls(self, headers):
        r1 = requests.get(f"{BASE_URL}/api/picks/rollover", headers=headers, timeout=60)
        time.sleep(2)
        r2 = requests.get(f"{BASE_URL}/api/picks/rollover", headers=headers, timeout=60)
        assert r1.status_code == 200 and r2.status_code == 200
        d1, d2 = r1.json(), r2.json()

        def _leg_ids(d):
            legs = d.get("picks") or d.get("slate", {}).get("legs") or d.get("legs") or []
            return [l.get("id") or l.get("pick_id") or l.get("leg_id") for l in legs]

        ids1, ids2 = _leg_ids(d1), _leg_ids(d2)
        assert ids1 == ids2 and len(ids1) == 3, f"rollover legs changed: {ids1} vs {ids2}"
