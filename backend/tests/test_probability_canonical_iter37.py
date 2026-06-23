"""Iter 37 — Unified Probability Engine as PRIMARY source of truth.

Validates the same-source-of-truth invariant: every pick in /api/picks/today
carries an inline `probability` block produced by
`probability_engine.unified_probability_report`, AND that block matches
byte-for-byte the response of GET /api/picks/{pick_id}/probability.

Also re-validates spec fields, legacy field preservation (lock_score,
edge_percent), data_version stamp, and the relevant regressions
(pitcher-h2h, tennis ALT, history board floor >= 80).
"""
from __future__ import annotations

import os
import re
import sys
import pathlib
import random
import pytest
import requests

BASE_URL = os.environ["EXPO_PUBLIC_BACKEND_URL"].rstrip("/")
sys.path.insert(0, "/app/backend")

EMAIL = "demo@lockscore.ai"
PASSWORD = "demo123"

SPEC_FIELDS = {
    "p_v1", "p_v2", "sim_probability",
    "p_final", "p_calibrated", "edge",
    "classification", "simulator_variance",
}
AUX_FIELDS = {"stability_score", "implied_probability", "weights", "calibration"}


# ──────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────
@pytest.fixture(scope="session")
def session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="session")
def auth_session(session):
    r = session.post(f"{BASE_URL}/api/auth/login",
                     json={"email": EMAIL, "password": PASSWORD},
                     timeout=20)
    if r.status_code != 200:
        pytest.skip(f"Login failed: {r.status_code} {r.text}")
    tok = r.json().get("access_token")
    assert tok, "no access_token returned"
    session.headers.update({"Authorization": f"Bearer {tok}"})
    return session


@pytest.fixture(scope="session")
def todays_picks(auth_session):
    r = auth_session.get(f"{BASE_URL}/api/picks/today", timeout=60)
    assert r.status_code == 200, r.text
    body = r.json()
    picks = body if isinstance(body, list) else (body.get("picks") or body.get("data") or [])
    assert picks, "no picks returned by /api/picks/today"
    return picks


# ──────────────────────────────────────────────────────────────────────────
# /api/version
# ──────────────────────────────────────────────────────────────────────────
class TestVersion:
    def test_data_version_iter37(self, session):
        r = session.get(f"{BASE_URL}/api/version", timeout=15)
        assert r.status_code == 200
        body = r.json()
        assert body.get("data_version") == "2026.06.23-probability-canonical", body


# ──────────────────────────────────────────────────────────────────────────
# Inline probability block on every pick (NEW iter37)
# ──────────────────────────────────────────────────────────────────────────
class TestInlineProbabilityBlock:
    def test_every_pick_has_probability_block(self, todays_picks):
        missing = [p.get("id") for p in todays_picks if "probability" not in p]
        assert not missing, f"{len(missing)} picks missing inline probability block, e.g. {missing[:3]}"

    def test_inline_block_has_spec_fields(self, todays_picks):
        for p in todays_picks[:50]:
            blk = p.get("probability") or {}
            missing = SPEC_FIELDS - set(blk.keys())
            assert not missing, f"pick {p.get('id')} missing spec fields {missing}"

    def test_inline_block_has_aux_fields(self, todays_picks):
        for p in todays_picks[:50]:
            blk = p.get("probability") or {}
            missing = AUX_FIELDS - set(blk.keys())
            assert not missing, f"pick {p.get('id')} missing aux fields {missing}"

    def test_inline_block_bounds(self, todays_picks):
        for p in todays_picks[:50]:
            blk = p["probability"]
            for k in ("p_v1", "p_v2", "sim_probability", "p_final",
                      "p_calibrated", "stability_score", "implied_probability"):
                v = blk[k]
                assert 0.0 <= v <= 1.0, f"pick {p.get('id')} {k}={v} not in [0,1]"
            e = blk["edge"]
            assert -0.15 <= e <= 0.40, f"pick {p.get('id')} edge={e} out of clamp"
            assert blk["classification"] in {"LOCK_99", "PREMIUM", "NORMAL", "CHALK"}, blk

    def test_inline_block_weights_constant(self, todays_picks):
        for p in todays_picks[:25]:
            assert p["probability"]["weights"] == {"v1": 0.30, "v2": 0.45, "sim": 0.25}

    def test_inline_block_ensemble_math(self, todays_picks):
        """p_final == 0.30*p_v1 + 0.45*p_v2 + 0.25*sim_probability."""
        for p in todays_picks[:25]:
            b = p["probability"]
            expected = 0.30 * b["p_v1"] + 0.45 * b["p_v2"] + 0.25 * b["sim_probability"]
            assert abs(expected - b["p_final"]) < 0.005, \
                f"pick {p.get('id')} ensemble mismatch: {expected:.4f} vs {b['p_final']:.4f}"


# ──────────────────────────────────────────────────────────────────────────
# Same-source-of-truth invariant: inline block == standalone endpoint
# ──────────────────────────────────────────────────────────────────────────
class TestSameSourceOfTruth:
    @pytest.fixture(scope="class")
    def sample_picks(self, todays_picks):
        rng = random.Random(0xC0DE)
        ids_with_blocks = [p for p in todays_picks if p.get("id") and "probability" in p]
        assert len(ids_with_blocks) >= 10, "need at least 10 picks with inline probability"
        return rng.sample(ids_with_blocks, k=min(15, len(ids_with_blocks)))

    def test_invariant_inline_eq_endpoint(self, auth_session, sample_picks):
        """For 15 random picks, inline `probability` must equal standalone endpoint output."""
        mismatches = []
        for p in sample_picks:
            pid = p["id"]
            inline = p["probability"]
            r = auth_session.get(f"{BASE_URL}/api/picks/{pid}/probability", timeout=15)
            assert r.status_code == 200, f"{pid}: {r.status_code} {r.text}"
            standalone = r.json()
            if inline != standalone:
                # collect a small diff snapshot
                diff = {k: (inline.get(k), standalone.get(k))
                        for k in set(inline) | set(standalone)
                        if inline.get(k) != standalone.get(k)}
                mismatches.append({"pick_id": pid, "diff": diff})
        assert not mismatches, f"{len(mismatches)} pick(s) have inline != endpoint: {mismatches[:3]}"

    def test_bieber_specific_pick(self, auth_session, todays_picks):
        """Spot-check the Bieber pick referenced in the agent context note."""
        target_id = "1ba18d22-ec1e-54c3-86bf-cc601a72acd5"
        match = next((p for p in todays_picks if p.get("id") == target_id), None)
        if not match:
            pytest.skip("Bieber pick id not in today's slate")
        inline = match["probability"]
        r = auth_session.get(f"{BASE_URL}/api/picks/{target_id}/probability", timeout=15)
        assert r.status_code == 200, r.text
        standalone = r.json()
        assert inline == standalone, f"Bieber inline != endpoint diff: {inline} vs {standalone}"
        # lock_score must still display 90+ per user requirement
        assert float(match.get("lock_score") or 0) >= 90.0, \
            f"Bieber lock_score crushed to {match.get('lock_score')}, expected >=90"


# ──────────────────────────────────────────────────────────────────────────
# Legacy field preservation
# ──────────────────────────────────────────────────────────────────────────
class TestLegacyFieldsPreserved:
    def test_lock_score_present_on_every_pick(self, todays_picks):
        missing = [p.get("id") for p in todays_picks if "lock_score" not in p]
        assert not missing, f"{len(missing)} picks missing lock_score"

    def test_edge_percent_present_on_every_pick(self, todays_picks):
        missing = [p.get("id") for p in todays_picks if "edge_percent" not in p]
        assert not missing, f"{len(missing)} picks missing edge_percent"

    def test_lock_score_realistic_range(self, todays_picks):
        # lock_score should still be in 0..100, and we expect chalk-style locks 90+
        scores = [float(p.get("lock_score") or 0) for p in todays_picks]
        assert all(0 <= s <= 100 for s in scores)
        assert max(scores) >= 90, "no high-chalk locks 90+ found — calibration may have crushed them"

    def test_no_inline_lock_score_mutation(self, todays_picks):
        """Inline probability.p_calibrated should NOT replace lock_score:
        they are independent surfaces (lock_score = display, p_calibrated = engine).
        Specifically, they should not be forced equal (except by coincidence).
        """
        # If lock_score were being clobbered by p_calibrated * 100 the values
        # would equal exactly. We just assert that not 100% of picks have
        # that exact identity (some divergence is expected/required).
        equal_count = 0
        for p in todays_picks:
            try:
                ls = float(p["lock_score"])
                pc = float(p["probability"]["p_calibrated"]) * 100.0
                if abs(ls - pc) < 0.05:
                    equal_count += 1
            except Exception:
                continue
        n = len(todays_picks)
        # It would be very suspicious if ≥95% of picks have lock_score==p_cal*100
        assert equal_count < 0.95 * n, \
            f"{equal_count}/{n} picks have lock_score==p_calibrated*100 — engine is overwriting display"


# ──────────────────────────────────────────────────────────────────────────
# Source-code invariants per spec
# ──────────────────────────────────────────────────────────────────────────
class TestSourceCodeInvariants:
    def test_canonicalize_calls_unified_report(self):
        src = pathlib.Path("/app/backend/server.py").read_text()
        lines = src.splitlines()
        # Locate the _canonicalize_lock_score def
        start = next((i for i, l in enumerate(lines)
                      if l.startswith("def _canonicalize_lock_score(")), None)
        assert start is not None, "could not locate _canonicalize_lock_score"
        # Body runs until the next top-level def/class/blank-at-col-0 statement
        end = start + 1
        while end < len(lines):
            ln = lines[end]
            if ln and not ln.startswith((" ", "\t")) and ln.startswith(("def ", "class ", "async def ")):
                break
            end += 1
        body = "\n".join(lines[start:end])
        assert "unified_probability_report" in body, \
            "_canonicalize_lock_score does NOT call unified_probability_report"
        assert 'pick["probability"]' in body or "pick['probability']" in body, \
            "_canonicalize_lock_score does not attach result to pick['probability']"

    def test_compute_edge_is_only_edge_fn_in_engine(self):
        src = pathlib.Path("/app/backend/probability_engine.py").read_text()
        defs = re.findall(r"^def\s+(\w+)\s*\(", src, flags=re.M)
        edgey = [d for d in defs if "edge" in d.lower()]
        assert edgey == ["compute_edge"], f"unexpected edge fns in engine: {edgey}"

    def test_no_v1_v2_subtraction_in_engine(self):
        src = pathlib.Path("/app/backend/probability_engine.py").read_text()
        bad = re.findall(r"(?:p_)?v1\s*-\s*(?:p_)?v2|(?:p_)?v2\s*-\s*(?:p_)?v1", src)
        assert not bad, f"forbidden v1/v2 subtraction in engine: {bad}"


# ──────────────────────────────────────────────────────────────────────────
# Regression — pitcher-h2h, tennis ALT, /picks/history floor
# ──────────────────────────────────────────────────────────────────────────
class TestRegressions:
    def test_pitcher_h2h_endpoint(self, auth_session, todays_picks):
        mlb_k = next(
            (p for p in todays_picks
             if (p.get("sport") or "") == "MLB"
             and "strikeout" in (p.get("market") or "").lower()),
            None)
        if not mlb_k:
            pytest.skip("no MLB strikeout pick in today's slate")
        pid = mlb_k["id"]
        r = auth_session.get(f"{BASE_URL}/api/picks/{pid}/pitcher-h2h", timeout=20)
        assert r.status_code == 200, r.text
        body = r.json()
        # Spec says pitcher-h2h returns history rows + summary stats.
        # Accept either a list, or dict with rows key — just assert it's non-empty.
        if isinstance(body, dict):
            keys = list(body.keys())
            assert keys, "empty pitcher-h2h payload"
        else:
            assert isinstance(body, list)

    def test_tennis_alt_tab_endpoint(self, auth_session):
        # Tennis ALT tab — exposed as /api/picks/today?sport=Tennis or similar.
        # Try the dedicated alt-tennis path first.
        candidates = [
            f"{BASE_URL}/api/picks/tennis-alt",
            f"{BASE_URL}/api/picks/today?sport=Tennis",
        ]
        ok = False
        for url in candidates:
            r = auth_session.get(url, timeout=30)
            if r.status_code == 200:
                ok = True
                break
        assert ok, "neither tennis-alt nor sport=Tennis endpoint responded 200"

    def test_tennis_ml_edge_carve_out(self, auth_session, todays_picks):
        """User spec: tennis ML picks should still have non-zero edge (the
        zero-edge carve-out for tennis ALT was the bug fixed in iter35)."""
        tennis_ml = [p for p in todays_picks
                     if (p.get("sport") or "").lower() == "tennis"
                     and (p.get("market_type") or p.get("market") or "").lower().find("alt") < 0]
        if not tennis_ml:
            pytest.skip("no tennis ML picks today")
        non_zero = [p for p in tennis_ml if float(p.get("edge_percent") or 0) != 0]
        assert non_zero, "all tennis ML picks have edge_percent=0 — carve-out regression"

    def test_history_board_floor_80(self, auth_session):
        r = auth_session.get(f"{BASE_URL}/api/picks/history?limit=200", timeout=60)
        if r.status_code == 404:
            # Alternate path
            r = auth_session.get(f"{BASE_URL}/api/picks/history", timeout=60)
        assert r.status_code == 200, r.text
        body = r.json()
        rows = body if isinstance(body, list) else (body.get("picks") or body.get("data") or [])
        if not rows:
            pytest.skip("history endpoint returned no rows")
        floor_violations = [r for r in rows if float(r.get("lock_score") or 0) < 80]
        assert not floor_violations, \
            f"{len(floor_violations)} history rows below floor 80: " \
            f"e.g. {[r.get('lock_score') for r in floor_violations[:5]]}"
