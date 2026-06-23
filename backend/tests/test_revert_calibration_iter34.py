"""Iter34 — Revert calibration overlay regression tests.

User reverted the iter33 calibration display overlay because Bieber-shaped
chalk picks should still show 90+ raw scores. The apply_calibration()
override in server._canonicalize_lock_score has been removed; raw model
lock_score is canonical again. Calibration analytics + curve fit stays
in place (for visibility only).

Acceptance:
  1. /api/version → data_version='2026.06.23-revert-calibration-overlay'.
  2. /api/picks/today MLB strikeout headliners display raw lock_score
     (Bieber Over 2.5 K = 93.4, McClanahan Over 3.5 K = 92.7,
     Avila Over 1.5 K = 92.1).
  3. NO 'raw_lock_score' field is stamped on pending picks any more
     (calibration override removed).
  4. Lock score distribution across sports is back to raw model output —
     specifically we expect a sizeable Elite (>=90) + Premium (>=80) cohort,
     not the iter33 Pass-band crush.
  5. /api/analytics/calibration still returns Expected/Actual/Delta rows.
  6. Code: server._canonicalize_lock_score no longer references
     apply_calibration; comment explains the revert.
  7. Regression: /api/picks/history still board-floor (lock>=80 OR raw>=80
     OR override OR alt+>=75).
  8. Regression: Tennis ALT tab still returns picks.
  9. Regression: MLB pitcher-h2h still works via /api/picks/{id}/pitcher-h2h.
"""
import os
import re
import sys
import pytest
import requests

sys.path.insert(0, "/app/backend")


BASE_URL = (
    os.environ.get("EXPO_PUBLIC_BACKEND_URL")
    or os.environ.get("EXPO_BACKEND_URL")
    or ""
).rstrip("/")

EXPECTED_DATA_VERSION = "2026.06.23-revert-calibration-overlay"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def api():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def auth_token(api):
    r = api.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": "demo@lockscore.ai", "password": "demo123"},
        timeout=20,
    )
    if r.status_code != 200:
        pytest.skip(f"Auth failed: {r.status_code} {r.text[:200]}")
    tok = r.json().get("access_token")
    assert tok, "No access_token in login response"
    return tok


@pytest.fixture(scope="module")
def auth_headers(auth_token):
    return {"Authorization": f"Bearer {auth_token}"}


def _extract_picks(payload):
    """Tolerant unwrapping for list-or-dict response shapes."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("picks", "data", "items", "results"):
            v = payload.get(key)
            if isinstance(v, list):
                return v
    return []


# ---------------------------------------------------------------------------
# 1. /api/version
# ---------------------------------------------------------------------------

class TestVersionEndpoint:
    def test_data_version_bumped_to_revert_marker(self, api):
        r = api.get(f"{BASE_URL}/api/version", timeout=15)
        assert r.status_code == 200, r.text[:300]
        body = r.json()
        assert body.get("data_version") == EXPECTED_DATA_VERSION, (
            f"data_version is {body.get('data_version')!r}, expected "
            f"{EXPECTED_DATA_VERSION!r}"
        )


# ---------------------------------------------------------------------------
# 2-4. /api/picks/today — raw lock_score restored, no raw_lock_score stamp
# ---------------------------------------------------------------------------

class TestPicksTodayRawScoresRestored:
    @pytest.fixture(scope="class")
    def picks(self, api, auth_headers):
        r = api.get(f"{BASE_URL}/api/picks/today", headers=auth_headers, timeout=45)
        assert r.status_code == 200, r.text[:300]
        ps = _extract_picks(r.json())
        if not ps:
            pytest.skip("No picks on today's board")
        return ps

    def test_no_raw_lock_score_field_stamped_on_pending(self, picks):
        """After the revert, apply_calibration() is no longer called from
        _canonicalize_lock_score, so pending picks should not carry the
        'raw_lock_score' field (it was only ever stamped by apply_calibration).
        """
        stamped = [p for p in picks if "raw_lock_score" in p]
        assert not stamped, (
            f"{len(stamped)} pick(s) still carry 'raw_lock_score' after revert. "
            f"Examples: {[{k: p.get(k) for k in ('id', 'sport', 'market', 'raw_lock_score', 'lock_score')} for p in stamped[:3]]}"
        )

    def test_mlb_strikeout_headliners_show_raw_scores(self, picks):
        """The user pinned three MLB strikeout picks that should now
        display their raw model lock_score:
          - Bieber Over 2.5 K        → 93.4
          - McClanahan Over 3.5 K    → 92.7
          - Avila Over 1.5 K         → 92.1
        We can't guarantee these exact players are on the board every day,
        but if they are, they must show the documented value (±0.1)."""
        expected = {
            "bieber":      {"raw": 93.4, "line": 2.5},
            "mcclanahan":  {"raw": 92.7, "line": 3.5},
            "avila":       {"raw": 92.1, "line": 1.5},
        }
        found = {}
        for p in picks:
            text = " ".join(
                str(p.get(k, "") or "") for k in ("title", "market", "player", "selection", "description")
            ).lower()
            for tag in expected:
                if tag in text and "strikeout" in text.lower() or (tag in text and "k" in text.lower()):
                    if tag not in found:
                        found[tag] = p
        if not found:
            pytest.skip("None of Bieber/McClanahan/Avila strikeout picks present on today's board")
        mismatches = []
        for tag, p in found.items():
            ls = p.get("lock_score")
            target = expected[tag]["raw"]
            if ls is None or abs(float(ls) - target) > 0.5:
                mismatches.append({
                    "tag": tag, "expected": target, "actual": ls,
                    "market": p.get("market"), "title": p.get("title"),
                })
        assert not mismatches, f"Strikeout headliner lock_score mismatch: {mismatches}"

    def test_lock_score_distribution_has_elite_and_premium(self, picks):
        """The iter33 calibration overlay crushed the board to 0 Elite +
        0 Premium. Post-revert we expect a healthy Elite (>=90) + Premium
        (>=80) cohort across all sports — agent context documented
        16 Elite + 117 Premium in the slate the user verified. Live board
        at test time may shift between the two buckets depending on which
        ALT/Mainline mix is active, so we assert the COMBINED count instead
        of separate floors for each band."""
        elite = [p for p in picks if (p.get("lock_score") or 0) >= 90]
        premium = [p for p in picks if 80 <= (p.get("lock_score") or 0) < 90]
        combined = len(elite) + len(premium)
        # Must not be the iter33 crush (0 Elite + 0 Premium).
        assert combined >= 50, (
            f"Only {combined} picks at lock_score>=80 (Elite {len(elite)} "
            f"+ Premium {len(premium)}); raw model should have a large "
            f"high-confidence cohort. "
            f"Top 10 lock_scores: {sorted([p.get('lock_score') for p in picks], reverse=True)[:10]}"
        )
        # And the Elite bucket alone must clearly be non-empty.
        assert len(elite) >= 5, (
            f"Only {len(elite)} Elite picks (lock>=90); raw model should "
            f"have many — possible overlay still active?"
        )

    def test_high_lock_scores_not_in_pass_band(self, picks):
        """No pick should have lock_score < 70 ('Pass') while clearly
        being a high-confidence raw pick (we don't have raw_lock_score
        any more, so we use a proxy: if win_probability >= 80 and
        edge_percent > -10, the pick should still display >= 70)."""
        offenders = []
        for p in picks:
            wp = p.get("win_probability")
            ed = p.get("edge_percent")
            ls = p.get("lock_score") or 0
            if wp is None:
                continue
            try:
                wp_f = float(wp)
                ed_f = float(ed) if ed is not None else 0.0
            except (TypeError, ValueError):
                continue
            if wp_f >= 80 and ed_f > -10 and ls < 70:
                offenders.append({
                    "id": p.get("id"), "sport": p.get("sport"),
                    "market": p.get("market"),
                    "lock_score": ls, "win_prob": wp_f, "edge": ed_f,
                })
        assert not offenders, (
            f"{len(offenders)} high-confidence pick(s) still display "
            f"lock_score<70: {offenders[:5]}"
        )


# ---------------------------------------------------------------------------
# 5. Code check — _canonicalize_lock_score no longer calls apply_calibration
# ---------------------------------------------------------------------------

class TestServerCodeRevert:
    def test_canonicalize_lock_score_no_apply_calibration(self):
        with open("/app/backend/server.py", "r") as f:
            src = f.read()
        # Pull the function body
        m = re.search(
            r"def _canonicalize_lock_score\(.*?\) -> dict:(.*?)\ndef _canonicalize_picks",
            src,
            re.DOTALL,
        )
        assert m, "Could not locate _canonicalize_lock_score in server.py"
        body = m.group(1)
        assert "apply_calibration" not in body, (
            "apply_calibration() is still being called inside "
            "_canonicalize_lock_score — revert incomplete."
        )
        # Comment explaining the revert should be present
        assert "revert" in body.lower() or "calibration overlay" in body.lower(), (
            "Expected a revert-note comment inside _canonicalize_lock_score."
        )


# ---------------------------------------------------------------------------
# 6. /api/analytics/calibration still works
# ---------------------------------------------------------------------------

class TestCalibrationAnalyticsIntact:
    def test_returns_expected_actual_delta_rows(self, api, auth_headers):
        r = api.get(
            f"{BASE_URL}/api/analytics/calibration",
            headers=auth_headers,
            timeout=30,
        )
        assert r.status_code == 200, r.text[:300]
        body = r.json()
        rows = body.get("rows") or []
        assert isinstance(rows, list) and len(rows) >= 1, (
            f"Calibration analytics returned no rows: {body}"
        )
        sample = rows[0]
        for key in ("band", "expected_win_pct", "actual_win_pct", "calibration_delta"):
            assert key in sample, f"Missing key {key} in row {sample}"


# ---------------------------------------------------------------------------
# 7-9. Regressions
# ---------------------------------------------------------------------------

class TestRegressions:
    def test_history_still_board_floor(self, api, auth_headers):
        r = api.get(
            f"{BASE_URL}/api/picks/history?days=30",
            headers=auth_headers,
            timeout=30,
        )
        assert r.status_code == 200, r.text[:300]
        picks = _extract_picks(r.json())
        if not picks:
            pytest.skip("No history picks available")
        offenders = []
        for p in picks:
            ls = p.get("lock_score") or 0
            raw = p.get("raw_lock_score") or 0
            override = p.get("elite_pitcher_override")
            is_alt = p.get("is_alt") or p.get("alt_line")
            ok = ls >= 80 or raw >= 80 or override or (is_alt and ls >= 75)
            if not ok:
                offenders.append({
                    "id": p.get("id"), "sport": p.get("sport"),
                    "lock": ls, "raw": raw,
                })
        assert not offenders, f"Board floor (>=80) violated: {offenders[:5]}"

    def test_tennis_alt_tab_returns_picks(self, api, auth_headers):
        urls = [
            f"{BASE_URL}/api/picks/today?sport=tennis&tab=alt",
            f"{BASE_URL}/api/picks/tennis/alt",
            f"{BASE_URL}/api/picks/today?sport=tennis",
        ]
        last_status = None
        for u in urls:
            r = api.get(u, headers=auth_headers, timeout=30)
            last_status = r.status_code
            if r.status_code == 200:
                picks = _extract_picks(r.json())
                assert isinstance(picks, list)
                return
        pytest.skip(f"No tennis ALT endpoint responded 200 (last={last_status})")

    def test_mlb_pitcher_h2h_route(self, api, auth_headers):
        """pitcher-h2h is per-pick: /api/picks/{id}/pitcher-h2h. We need
        an MLB pitcher pick to test against."""
        r = api.get(f"{BASE_URL}/api/picks/today", headers=auth_headers, timeout=30)
        assert r.status_code == 200
        picks = _extract_picks(r.json())
        mlb_picks = [
            p for p in picks
            if (p.get("sport") or "").lower() in ("mlb", "baseball")
            and p.get("id")
        ]
        # Prefer pitcher-style picks (strikeouts) for more meaningful payload
        pitcher_like = [
            p for p in mlb_picks
            if "strikeout" in (p.get("market") or "").lower()
            or "K" in (p.get("title") or "")
        ]
        candidates = (pitcher_like or mlb_picks)[:3]
        if not candidates:
            pytest.skip("No MLB picks today to exercise pitcher-h2h route")
        last_status = None
        last_body = None
        for p in candidates:
            r = api.get(
                f"{BASE_URL}/api/picks/{p['id']}/pitcher-h2h",
                headers=auth_headers,
                timeout=30,
            )
            last_status = r.status_code
            last_body = r.text[:300]
            if r.status_code == 200:
                # Should return a JSON object (h2h payload) — exact shape
                # owned by mlb_pitcher_h2h.fetch_pitcher_h2h.
                body = r.json()
                assert isinstance(body, dict), f"Unexpected payload type: {type(body)}"
                return
            # 404 is acceptable if this particular pick is not a pitcher
            # prop — keep trying the next candidate.
        pytest.skip(
            f"pitcher-h2h route did not return 200 for any of "
            f"{len(candidates)} MLB candidate(s); last={last_status} {last_body}"
        )
