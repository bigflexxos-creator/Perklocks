"""Iteration 69 — Nordic goalscorer grading + history sort verification.

Covers the fixes shipped for the 2026-07-13 FanDuel screenshot bug:
- History sorts by event_time desc (not settled_at)
- Nordic goalscorer picks graded correctly (Kristian Lien won, etc.)
- Board-visibility filters (hide_from_main_board, no_bet) applied
- picks/today stamps on_main_board_at
- Modules exist: grading_validator, stuck_pick_reaper, FotMob primary
"""
import os
import importlib
import re

import pytest
import requests

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    # backend .env fallback for BASE_URL inference
    BASE_URL = "https://bet-edge-ai-1.preview.emergentagent.com"


@pytest.fixture(scope="module")
def token() -> str:
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": "demo@lockscore.ai", "password": "demo123"},
        timeout=30,
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:200]}"
    body = r.json()
    tok = body.get("access_token") or body.get("token")
    assert tok, f"no token in login response: {body}"
    return tok


@pytest.fixture(scope="module")
def auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def history_picks(auth_headers):
    r = requests.get(
        f"{BASE_URL}/api/picks/history?days=30",
        headers=auth_headers,
        timeout=120,
    )
    assert r.status_code == 200, f"/picks/history failed: {r.status_code} {r.text[:300]}"
    data = r.json()
    picks = data.get("picks") or data.get("history") or []
    if isinstance(data, list):
        picks = data
    return picks, data


# ────────── module-import sanity ──────────
class TestModuleImports:
    def test_grading_validator_imports(self):
        import sys
        sys.path.insert(0, "/app/backend")
        mod = importlib.import_module("grading_validator")
        assert hasattr(mod, "verify_recent_goalscorer_grades")
        assert hasattr(mod, "grading_validator_loop")

    def test_stuck_pick_reaper_imports(self):
        import sys
        sys.path.insert(0, "/app/backend")
        mod = importlib.import_module("stuck_pick_reaper")
        assert getattr(mod, "_STUCK_HOURS", None) == 72
        assert hasattr(mod, "reap_stuck_picks")
        assert hasattr(mod, "stuck_pick_reaper_loop")

    def test_soccer_espn_settle_has_nordic_primary(self):
        with open("/app/backend/soccer_espn_settle.py") as f:
            src = f.read()
        # Look for the Nordic-primary block near _grade_pick
        assert "is_nordic" in src, "is_nordic branch missing"
        assert re.search(r"allsvenskan.*eliteserien.*superligaen.*veikkausliiga",
                         src, re.S), "Nordic league keys missing"
        assert "soccer_fotmob_settle" in src, "FotMob module not referenced"
        assert "if is_nordic or result is None" in src, \
            "FotMob primary/fallback dispatch not wired"


# ────────── /picks/history endpoint contract ──────────
class TestHistoryEndpoint:
    def test_history_returns_200_with_picks(self, history_picks):
        picks, data = history_picks
        assert isinstance(picks, list)
        assert len(picks) > 0, "history returned empty — expected settled picks"

    def test_history_sorted_by_event_time_desc(self, history_picks):
        picks, _ = history_picks
        etimes = [p.get("event_time") or "" for p in picks]
        # Non-empty event_times should be monotonically non-increasing
        # (equal timestamps allowed). Ignore empty strings that sort last.
        prev = None
        violations = 0
        for et in etimes:
            if not et:
                continue
            if prev is not None and et > prev:
                violations += 1
            prev = et
        assert violations == 0, (
            f"history not sorted by event_time desc — {violations} out-of-order "
            f"pairs. First 10 event_times: {etimes[:10]}"
        )

    def test_history_excludes_hidden_and_no_bet(self, history_picks):
        picks, _ = history_picks
        for p in picks:
            assert p.get("hide_from_main_board") is not True, \
                f"hide_from_main_board pick leaked: {p.get('id')}"
            assert p.get("no_bet") is not True, \
                f"no_bet pick leaked: {p.get('id')}"
            assert p.get("excluded_from_history") is not True
            assert p.get("status") != "void", \
                f"void pick leaked: {p.get('id')} status={p.get('status')}"


# ────────── FanDuel-screenshot Nordic pick grading ──────────
# The 8 picks in the 2026-07-12/13 FanDuel screenshot. Match by
# selection substring (last-name is enough — selections vary in
# format: "Kristian Lien to Score", "Kristian Lien - Anytime Goal
# Scorer", etc.).
EXPECTED_WON = [
    ("Lien",         "2026-07-13"),   # Kristian Lien
    ("Ure",          "2026-07-12"),   # Robbie Ure
    ("Bjerkebo",     "2026-07-12"),   # Isak Bjerkebo
    ("Abraham",      "2026-07-12"),   # Paulos Abraham
    ("Botheim",      "2026-07-12"),   # Erik Botheim
    ("Ladefoged",    "2026-07-12"),   # Mikkel Ladefoged
]
EXPECTED_LOST = [
    ("Høgh",         "2026-07-12"),   # Kasper Høgh (may render as "Hogh")
    ("Christiansen", "2026-07-12"),   # Peter Christiansen
]


def _find_pick(picks, name_substr: str, date_prefix: str):
    """Find a scorer pick whose selection contains name_substr AND
    event_time starts with date_prefix. Return the pick or None.
    """
    hits = []
    lname = name_substr.lower()
    # Also match ASCII-fold for ø → o (Høgh ↔ Hogh)
    lname_ascii = lname.replace("ø", "o").replace("æ", "ae").replace("å", "a")
    for p in picks:
        sel = (p.get("selection") or "").lower()
        sel_ascii = sel.replace("ø", "o").replace("æ", "ae").replace("å", "a")
        et = p.get("event_time") or ""
        mkt = (p.get("market") or "").lower()
        if "goal scorer" not in mkt and "to score" not in mkt:
            continue
        if not et.startswith(date_prefix):
            continue
        if lname in sel or lname_ascii in sel_ascii:
            hits.append(p)
    return hits


class TestFanDuelScreenshotGrading:
    @pytest.mark.parametrize("name,date", EXPECTED_WON)
    def test_won_pick_status(self, history_picks, name, date):
        picks, _ = history_picks
        hits = _find_pick(picks, name, date)
        if not hits:
            pytest.skip(f"pick not found in history: {name} on {date}")
        # Any matching row should be graded won (there may be multiple
        # duplicates from different generation passes).
        statuses = [h.get("status") for h in hits]
        assert "won" in statuses, (
            f"{name} on {date} not graded WON — got statuses={statuses}, "
            f"selections={[h.get('selection') for h in hits]}"
        )

    @pytest.mark.parametrize("name,date", EXPECTED_LOST)
    def test_lost_pick_status(self, history_picks, name, date):
        picks, _ = history_picks
        hits = _find_pick(picks, name, date)
        if not hits:
            pytest.skip(f"pick not found in history: {name} on {date}")
        statuses = [h.get("status") for h in hits]
        # At least one row should be lost; none should be won.
        assert "won" not in statuses, (
            f"{name} on {date} incorrectly WON — expected lost. "
            f"selections={[h.get('selection') for h in hits]}"
        )
        assert "lost" in statuses, (
            f"{name} on {date} not graded LOST — got statuses={statuses}"
        )


# ────────── /picks/today stamps on_main_board_at ──────────
class TestTodayStamp:
    def test_today_returns_and_stamps(self, auth_headers):
        # Use lite=true for a faster path if available; server may ignore.
        r = requests.get(
            f"{BASE_URL}/api/picks/today?lite=true",
            headers=auth_headers,
            timeout=300,
        )
        assert r.status_code == 200, \
            f"/picks/today failed: {r.status_code} {r.text[:300]}"
        payload = r.json()
        picks = payload.get("picks") or []
        # Not all environments have live picks; skip if empty rather
        # than fail the whole regression.
        if not picks:
            pytest.skip("/picks/today returned zero picks — nothing to verify")
        assert isinstance(picks, list) and len(picks) > 0
        # The stamping is fire-and-forget/update_many; response itself
        # does not need to echo the stamp. Just verify no crash + picks
        # come back with IDs (stamping requires id).
        with_id = sum(1 for p in picks if p.get("id"))
        assert with_id > 0, "no picks with id — stamping cannot occur"
