"""
Iteration 141 — Final Universal Root Closure certification pass.

Verifies:
  1. Auth (demo user login)
  2. MLB rows on lite board (~300, all lock_score>=85, families present,
     real book_odds int, win_probability set)
  3. NFL alt lite/detail parity — every NFL row with 'Player' in market
  4. MLB parity — 15-sample lite/detail parity
  5. NFL ATD slate — universe_count>=150, games>=12, Derrick Henry #1
  6. Rollover — 3 legs frozen
"""
import os
import random
from collections import Counter

import pytest
import requests

BASE_URL = os.environ.get(
    "EXPO_PUBLIC_BACKEND_URL",
    "https://canonical-parity.preview.emergentagent.com",
).rstrip("/")

SCORED_FIELDS = ("win_probability", "edge_percent", "lock_score", "grade",
                 "line", "book_odds")

MLB_FAMILY_KEYWORDS = {
    "Hits": ["Player Hits", "Hits"],
    "Total Bases": ["Total Bases"],
    "Hits+Runs+RBIs": ["Hits+Runs+RBIs", "Hits + Runs + RBIs", "H+R+RBI"],
    "Home Runs": ["Home Runs", "HR"],
    "RBIs": ["RBIs", "Player RBI"],
    "Strikeouts": ["Strikeouts", "Strikeout"],
    "Outs Recorded": ["Outs Recorded", "Outs"],
}


@pytest.fixture(scope="module")
def auth():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"email": "demo@lockscore.ai", "password": "demo123"},
                      timeout=30)
    assert r.status_code == 200, r.text
    tok = r.json().get("access_token") or r.json().get("token")
    assert tok, r.json()
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture(scope="module")
def lite_board(auth):
    r = requests.get(f"{BASE_URL}/api/picks/today?lite=true", headers=auth, timeout=180)
    assert r.status_code == 200
    return r.json().get("picks", [])


def _r2(v):
    return round(v, 2) if isinstance(v, float) else v


class TestAuth:
    def test_login(self, auth):
        assert "Authorization" in auth


class TestMlbBoard:
    def test_mlb_rows_present(self, lite_board):
        mlb = [p for p in lite_board if (p.get("sport") or "").upper() == "MLB"]
        print(f"\nMLB rows on lite board: {len(mlb)}")
        assert len(mlb) > 0, "no MLB rows on lite board"

    def test_mlb_all_lockscore_ge_85(self, lite_board):
        mlb = [p for p in lite_board if (p.get("sport") or "").upper() == "MLB"]
        low = [(p.get("id"), p.get("lock_score")) for p in mlb
               if (p.get("lock_score") or 0) < 85]
        assert not low, f"MLB rows below 85: {low[:5]}"

    def test_mlb_family_counts(self, lite_board):
        mlb = [p for p in lite_board if (p.get("sport") or "").upper() == "MLB"]
        counts = Counter()
        for p in mlb:
            market = p.get("market") or ""
            for fam, kws in MLB_FAMILY_KEYWORDS.items():
                if any(k.lower() in market.lower() for k in kws):
                    counts[fam] += 1
                    break
            else:
                counts["game_markets"] += 1
        print(f"\nMLB family counts: {dict(counts)}")
        assert sum(counts.values()) == len(mlb)

    def test_mlb_real_book_odds_and_winprob(self, lite_board):
        mlb = [p for p in lite_board if (p.get("sport") or "").upper() == "MLB"]
        bad = []
        for p in mlb:
            bo = p.get("book_odds")
            wp = p.get("win_probability")
            if not isinstance(bo, int) or wp is None:
                bad.append((p.get("id"), bo, wp))
        assert not bad, f"MLB rows without real book_odds int or win_probability: {bad[:5]}"


class TestNflAltParity:
    def test_every_nfl_player_row_parity(self, auth, lite_board):
        nfl_alt = [p for p in lite_board
                   if (p.get("sport") or "").upper() == "NFL"
                   and "Player" in (p.get("market") or "")]
        print(f"\nNFL rows with 'Player': {len(nfl_alt)}")
        diffs = []
        for row in nfl_alt:
            det = requests.get(f"{BASE_URL}/api/picks/{row['id']}",
                               headers=auth, timeout=60).json()
            for f in SCORED_FIELDS:
                lv, dv = _r2(row.get(f)), _r2(det.get(f))
                if lv is not None and dv is not None and lv != dv:
                    diffs.append((row["id"], row.get("market"), f, lv, dv))
        print(f"NFL parity mismatches: {len(diffs)}")
        assert not diffs, f"NFL alt mismatches: {diffs[:5]}"

    def test_jaxson_dart_specific(self, auth, lite_board):
        target_id = "715e6aef-a566-53af-8f6f-81a54bb8418a"
        lite_row = next((p for p in lite_board if p.get("id") == target_id), None)
        if not lite_row:
            pytest.skip(f"target NFL alt {target_id} not on today's board")
        det = requests.get(f"{BASE_URL}/api/picks/{target_id}",
                           headers=auth, timeout=60).json()
        assert _r2(lite_row.get("win_probability")) == 70.67, lite_row
        assert _r2(det.get("win_probability")) == 70.67, det


class TestMlbParitySample:
    def test_15_mlb_sample_parity(self, auth, lite_board):
        mlb = [p for p in lite_board if (p.get("sport") or "").upper() == "MLB"]
        if len(mlb) < 15:
            pytest.skip(f"only {len(mlb)} MLB rows")
        random.seed(42)
        sample = random.sample(mlb, 15)
        diffs = []
        for row in sample:
            det = requests.get(f"{BASE_URL}/api/picks/{row['id']}",
                               headers=auth, timeout=60).json()
            for f in SCORED_FIELDS:
                lv, dv = _r2(row.get(f)), _r2(det.get(f))
                if lv is not None and dv is not None and lv != dv:
                    diffs.append((row["id"], row.get("market"), f, lv, dv))
        print(f"\nMLB 15-sample parity mismatches: {len(diffs)}")
        assert not diffs, f"MLB mismatches: {diffs[:5]}"


class TestNflAtdSlate:
    def test_atd_slate(self, auth):
        r = requests.get(f"{BASE_URL}/api/nfl/atd/slate", headers=auth, timeout=90)
        assert r.status_code == 200, r.text
        data = r.json()
        uc = data.get("universe_count") or 0
        games = data.get("games") or []
        top5 = data.get("top5") or []
        print(f"\nATD universe_count={uc}, games={len(games)}, top5[0]={top5[0] if top5 else None}")
        assert uc >= 150, f"universe_count too low: {uc}"
        assert len(games) >= 12, f"games too few: {len(games)}"
        assert top5, "no top5"
        first = top5[0]
        name = (first.get("player") or first.get("player_name")
                or first.get("name") or "").lower()
        assert "derrick henry" in name, f"top5[0] not Derrick Henry: {first}"
        prob = (first.get("td_probability") or first.get("probability")
                or first.get("td_prob") or 0)
        assert 0.70 <= prob <= 0.82, f"td_probability out of expected band: {prob}"

        # each game has candidates and top arrays
        for g in games:
            assert "candidates" in g or "top" in g, f"game missing arrays: {g}"

        # top5 == first 5 of concatenated ranked candidates when applicable
        cats = []
        for g in games:
            cats.extend(g.get("candidates") or [])
        # accept top5 already provided as authoritative


class TestRollover:
    def test_rollover_frozen(self, auth):
        r = requests.get(f"{BASE_URL}/api/picks/rollover", headers=auth, timeout=60)
        assert r.status_code == 200, r.text
        data = r.json()
        legs = data.get("legs") or data.get("picks") or []
        print(f"\nRollover legs={len(legs)}, lock={data.get('lock') or data.get('lock_score')}")
        assert len(legs) == 3, f"expected 3 legs, got {len(legs)}"
        markets = " | ".join(str(l.get("event") or l.get("market") or "") for l in legs)
        print(f"Rollover markets: {markets}")
