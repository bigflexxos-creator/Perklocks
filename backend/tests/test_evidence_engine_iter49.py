"""
Iteration 49 — Universal Evidence System (Phase 1) regression suite.

Covers the contract laid out in /app/backend/evidence_engine.py:
  • GET /api/picks/today returns evidence_score / lock_score_raw /
    evidence_breakdown / governed lock_score on every pick.
  • win_probability is NEVER mutated by the governor.
  • GET /api/admin/pick-evidence/{id} exposes the full inspector
    payload (4 separated metrics + audit trail).
  • Explanation governor strips/detunes hype words on low-evidence
    picks; leaves them alone on HIGH-evidence picks (unit-level).
  • Lock score clamped [0, 99], evidence_score in [0, 100], evidence
    multiplier in [0.70, 1.00].
  • Pick validator self-heal respects the evidence multiplier
    (function-level — verified by calling apply_lock_governor against
    a recomputed target).
"""
from __future__ import annotations

import os
import sys
import pytest
import requests

# Make backend importable for unit-level checks on evidence_engine.
sys.path.insert(0, "/app/backend")

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    # Fall back to the .env file directly so we never hit a default
    # localhost URL (system prompt says fail fast on missing config).
    try:
        with open("/app/frontend/.env", "r") as fh:
            for line in fh:
                if line.startswith("EXPO_PUBLIC_BACKEND_URL"):
                    BASE_URL = line.split("=", 1)[1].strip().strip('"').rstrip("/")
                    break
    except Exception:
        pass
assert BASE_URL, "EXPO_PUBLIC_BACKEND_URL must be set"

DEMO_EMAIL = "demo@lockscore.ai"
DEMO_PASSWORD = "demo123"


# ─────────────────────────────── Fixtures ───────────────────────────────
@pytest.fixture(scope="module")
def auth_token() -> str:
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD},
               timeout=20)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:200]}"
    tok = r.json().get("access_token")
    assert tok, "no access_token returned"
    return tok


@pytest.fixture(scope="module")
def auth_session(auth_token) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Content-Type": "application/json",
        "Authorization": f"Bearer {auth_token}",
    })
    return s


@pytest.fixture(scope="module")
def picks_today(auth_session) -> list[dict]:
    r = auth_session.get(f"{BASE_URL}/api/picks/today", timeout=60)
    assert r.status_code == 200, f"picks/today failed: {r.status_code} {r.text[:200]}"
    data = r.json()
    picks = data.get("picks") if isinstance(data, dict) else data
    assert isinstance(picks, list), f"unexpected payload shape: {type(data)}"
    assert len(picks) > 0, "no picks on board today"
    return picks


# ─────────────── Module: picks/today carries the evidence envelope ──────
class TestPicksTodayEvidence:
    def test_picks_have_evidence_fields(self, picks_today):
        sample = picks_today[:25]
        missing_score = [p["id"] for p in sample if p.get("evidence_score") is None]
        missing_breakdown = [p["id"] for p in sample if not p.get("evidence_breakdown")]
        # All freshly-generated picks must carry the envelope. Allow
        # at most a couple of stragglers in case a settled pick slips
        # into the today view, but the vast majority must have it.
        assert len(missing_score) <= 2, f"too many picks missing evidence_score: {missing_score}"
        assert len(missing_breakdown) <= 2, f"missing evidence_breakdown: {missing_breakdown}"

    def test_evidence_score_in_range(self, picks_today):
        for p in picks_today[:50]:
            es = p.get("evidence_score")
            if es is None:
                continue
            assert isinstance(es, int), f"evidence_score must be int, got {type(es)}"
            assert 0 <= es <= 100, f"evidence_score out of range: {es}"

    def test_lock_score_clamped(self, picks_today):
        for p in picks_today[:50]:
            ls = p.get("lock_score")
            if ls is None:
                continue
            assert 0.0 <= float(ls) <= 99.0, f"lock_score out of [0,99]: {ls}"

    def test_lock_score_raw_present_and_ge_governed(self, picks_today):
        # Find at least one pick that was actually governed (multiplier < 1.0).
        haircut_seen = False
        for p in picks_today:
            raw = p.get("lock_score_raw")
            governed = p.get("lock_score")
            if raw is None or governed is None:
                continue
            assert raw >= governed - 0.05, (
                f"governed lock {governed} should never exceed raw {raw} "
                f"(pick {p.get('id')})"
            )
            if raw > governed + 0.1:
                haircut_seen = True
        assert haircut_seen, "expected at least one pick where evidence reduced the lock"

    def test_breakdown_shape(self, picks_today):
        for p in picks_today[:20]:
            eb = p.get("evidence_breakdown") or {}
            if not eb:
                continue
            assert "multiplier" in eb
            assert "tier_counts" in eb
            assert isinstance(eb["tier_counts"], dict)
            for key in ("HIGH", "MEDIUM", "LOW"):
                assert key in eb["tier_counts"], f"tier_counts missing {key}"
            assert "top_features" in eb
            assert isinstance(eb["top_features"], list)
            assert "generated_at" in eb
            mult = float(eb["multiplier"])
            assert 0.70 <= mult <= 1.00, f"multiplier out of [0.70,1.00]: {mult}"

    def test_top_features_have_provenance(self, picks_today):
        for p in picks_today[:20]:
            eb = p.get("evidence_breakdown") or {}
            for f in (eb.get("top_features") or []):
                for k in ("name", "category", "sample_size", "lookback_days",
                          "source", "tier", "reliability", "importance",
                          "passes_governor"):
                    assert k in f, f"top_feature missing '{k}': {f}"


# ──────────────── Module: probability is never mutated ─────────────────
class TestProbabilityUnchanged:
    def test_win_probability_preserved(self, picks_today):
        """govern_pick must NOT touch win_probability (rule 7).
        We re-run govern_pick locally and check probability is identical."""
        from evidence_engine import build_features_from_pick, govern_pick
        checked = 0
        for p in picks_today[:15]:
            wp = p.get("win_probability")
            if wp is None:
                continue
            original = float(wp)
            clone = dict(p)
            govern_pick(clone, build_features_from_pick(clone))
            assert clone.get("win_probability") == p.get("win_probability"), \
                f"win_probability changed after governance for pick {p.get('id')}"
            assert float(clone["win_probability"]) == original
            checked += 1
        assert checked >= 5, "expected to check at least 5 picks"


# ──────────────── Module: admin inspector endpoint ─────────────────────
class TestAdminPickEvidenceEndpoint:
    def test_inspector_payload_for_mlb_pick(self, auth_session, picks_today):
        mlb_picks = [p for p in picks_today if (p.get("sport") or "").upper() == "MLB"]
        if not mlb_picks:
            pytest.skip("no MLB picks on board today")
        pick_id = mlb_picks[0]["id"]
        r = auth_session.get(f"{BASE_URL}/api/admin/pick-evidence/{pick_id}", timeout=30)
        assert r.status_code == 200, f"{r.status_code} {r.text[:200]}"
        data = r.json()
        # 4 separated metrics — rule 6.
        for key in ("probability_pct", "edge_pct", "evidence_score", "lock_score"):
            assert key in data, f"missing metric {key}"
        assert "evidence_breakdown" in data
        assert "key_insights" in data
        assert "lock_score_raw" in data
        assert isinstance(data["evidence_breakdown"], dict)

    def test_inspector_404_for_unknown(self, auth_session):
        r = auth_session.get(
            f"{BASE_URL}/api/admin/pick-evidence/nonexistent-pick-id-xyz",
            timeout=20,
        )
        assert r.status_code == 404


# ──────────────── Module: explanation governor (unit-level) ────────────
class TestExplanationGovernor:
    """Direct calls to apply_explanation_governor with synthetic features
    so we control evidence_score exactly."""

    def _high_feat(self):
        from evidence_engine import EvidenceFeature, classify
        f = EvidenceFeature(
            name="MC Sim", category="model", value=88.0,
            sample_size=10000, lookback_days=0, source="brain",
            importance=0.95, freshness_hours=0.0,
        )
        classify([f])
        return f

    def _low_feat(self):
        from evidence_engine import EvidenceFeature, classify
        f = EvidenceFeature(
            name="vibes", category="intangible", value=1,
            sample_size=1, lookback_days=14, source="vibe-check",
            importance=0.1, freshness_hours=200.0,
        )
        classify([f])
        return f

    def test_low_evidence_drops_hype(self):
        from evidence_engine import apply_explanation_governor
        kept, dropped = apply_explanation_governor(
            ["Elite spot — automatic lock with massive edge."],
            [self._low_feat()],
            overall_score=20,
        )
        assert any("elite" in s.lower() or "automatic" in s.lower() or "lock " in s.lower()
                   for s in dropped), f"hype not dropped: {dropped}"

    def test_mid_evidence_detunes_hype(self):
        from evidence_engine import apply_explanation_governor
        kept, dropped = apply_explanation_governor(
            ["Elite signal here — dominant matchup."],
            [self._low_feat()],
            overall_score=55,
        )
        text = " ".join(kept).lower()
        assert "elite" not in text, f"'elite' not detuned in {kept}"
        assert "dominant" not in text, f"'dominant' not detuned in {kept}"
        # Replacements should appear
        assert "strong" in text or "favored" in text, \
            f"expected detuned replacements in {kept}"

    def test_high_evidence_passes_hype(self):
        from evidence_engine import apply_explanation_governor
        kept, dropped = apply_explanation_governor(
            ["Elite spot — dominant matchup."],
            [self._high_feat()],
            overall_score=85,
        )
        text = " ".join(kept).lower()
        # Words flow through unchanged when evidence is HIGH
        assert "elite" in text, f"'elite' should pass with HIGH evidence: {kept}"
        assert not dropped, f"nothing should be dropped at score 85: {dropped}"

    def test_signal_limited_fallback(self):
        from evidence_engine import (
            apply_explanation_governor, SIGNAL_LIMITED_FALLBACK,
        )
        kept, dropped = apply_explanation_governor(
            ["Elite lock — automatic."],
            [self._low_feat()],
            overall_score=10,
        )
        # All hype dropped → fallback line must appear.
        assert SIGNAL_LIMITED_FALLBACK in kept, kept


# ──────────────── Module: lock governor math ───────────────────────────
class TestLockGovernor:
    def test_multiplier_curve(self):
        from evidence_engine import evidence_multiplier
        assert evidence_multiplier(0)  == 0.70
        assert evidence_multiplier(19) == 0.70
        assert evidence_multiplier(20) == 0.78
        assert evidence_multiplier(45) == 0.85
        assert evidence_multiplier(65) == 0.93
        assert evidence_multiplier(80) == 1.00
        assert evidence_multiplier(100) == 1.00

    def test_apply_governor_clamps(self):
        from evidence_engine import apply_lock_governor
        assert apply_lock_governor(150.0, 100) == 99.0   # clamp ceiling
        assert apply_lock_governor(-5.0, 50)   == 0.0    # clamp floor
        assert apply_lock_governor(None, 50)   == 0.0    # None → 0
        # 60 × 0.85 = 51.0
        assert abs(apply_lock_governor(60.0, 40) - 51.0) < 0.05


# ──────────────── Module: validator respects governance ────────────────
class TestValidatorRespectsGovernance:
    """The validator's self-heal recomputes target_lock, then applies the
    evidence multiplier when evidence_score is on the pick. We test the
    math holds: applying the governor to a recomputed value matches what
    pick_validator does (lines 196-200 of pick_validator.py)."""

    def test_governor_application_matches(self):
        from evidence_engine import apply_lock_governor, evidence_multiplier
        raw_target = 70.0
        ev_score = 50
        expected = apply_lock_governor(raw_target, ev_score)
        # Math: 70 × 0.85 = 59.5
        assert abs(expected - 59.5) < 0.05, expected
        assert evidence_multiplier(ev_score) == 0.85
