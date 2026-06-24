"""
Iter-50 regression suite for the lock_score > lock_score_raw fix.

Validates the 4 fixes:
  (a) pick_validator governance ordering — drift cap + CLV demotion run BEFORE govern,
      and governance writes lock_score AND lock_score_raw together every cycle.
  (b) Unconditional re-governance coherence pass (raw >= governed always).
  (c) evidence_engine.govern_pick — when V2_governed > V1_governed, lock_score_raw aligns to V2_raw.
  (d) server._canonicalize_lock_score — at read-time, lock_score_raw promotes alongside lock_score.

Targets:
  - 0 picks with lock_score > lock_score_raw + 0.5 on /api/picks/today
  - canary pick 0d09e267-1a61-5e48-832b-30499b0f9985 → lock_score == lock_score_raw (both 92)
  - audit math: lock_score_raw × multiplier ≈ lock_score within 0.5 on random samples
  - parity between /api/picks/{id} and /api/admin/pick-evidence/{id}
"""
from __future__ import annotations

import os
import random
import pytest
import requests

CANARY_PICK_ID = "0d09e267-1a61-5e48-832b-30499b0f9985"

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
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


# ───────────────────────── fixtures ─────────────────────────
@pytest.fixture(scope="module")
def auth_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD}, timeout=20)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:200]}"
    tok = r.json().get("access_token")
    assert tok, "no access_token"
    s.headers.update({"Authorization": f"Bearer {tok}"})
    return s


@pytest.fixture(scope="module")
def picks_today(auth_session) -> list[dict]:
    r = auth_session.get(f"{BASE_URL}/api/picks/today?limit=400", timeout=60)
    assert r.status_code == 200, f"picks/today failed: {r.status_code}"
    data = r.json()
    picks = data.get("picks") if isinstance(data, dict) else data
    assert isinstance(picks, list) and len(picks) > 0
    return picks


# ───────────── primary fix: sweep entire board for coherence ─────────────
class TestSweepCoherence:
    def test_zero_picks_with_governed_gt_raw(self, picks_today):
        """The headline assertion: across the entire board, no pick may
        have lock_score > lock_score_raw beyond a rounding tolerance."""
        offenders = []
        for p in picks_today:
            raw = p.get("lock_score_raw")
            gov = p.get("lock_score")
            if raw is None or gov is None:
                continue
            if float(gov) > float(raw) + 0.5:
                offenders.append({
                    "id": p.get("id"),
                    "sport": p.get("sport"),
                    "lock_score": gov,
                    "lock_score_raw": raw,
                    "evidence_score": p.get("evidence_score"),
                    "multiplier": (p.get("evidence_breakdown") or {}).get("multiplier"),
                })
        assert not offenders, (
            f"{len(offenders)}/{len(picks_today)} picks have lock_score > "
            f"lock_score_raw + 0.5. First 5: {offenders[:5]}"
        )

    def test_evidence_envelope_present_on_majority(self, picks_today):
        with_raw = sum(1 for p in picks_today if p.get("lock_score_raw") is not None)
        with_ev = sum(1 for p in picks_today if p.get("evidence_score") is not None)
        total = len(picks_today)
        # Allow a small tail (e.g. settled picks) but the vast majority
        # of an active board must carry the envelope.
        assert with_raw >= int(total * 0.95), \
            f"only {with_raw}/{total} picks have lock_score_raw"
        assert with_ev >= int(total * 0.95), \
            f"only {with_ev}/{total} picks have evidence_score"


# ───────────── audit math: raw × mult ≈ governed ─────────────
class TestAuditMath:
    def test_random_picks_reconcile(self, picks_today):
        candidates = [p for p in picks_today
                      if p.get("lock_score_raw") is not None
                      and p.get("lock_score") is not None
                      and (p.get("evidence_breakdown") or {}).get("multiplier") is not None]
        assert len(candidates) >= 5, f"need >=5 governed picks, got {len(candidates)}"

        random.seed(42)
        sample = random.sample(candidates, k=min(10, len(candidates)))
        bad = []
        for p in sample:
            raw = float(p["lock_score_raw"])
            gov = float(p["lock_score"])
            mult = float(p["evidence_breakdown"]["multiplier"])
            expected = raw * mult
            # Clamp to 99 to mirror apply_lock_governor's ceiling.
            expected_clamped = min(99.0, max(0.0, expected))
            if abs(expected_clamped - gov) > 0.6:
                bad.append({
                    "id": p["id"], "raw": raw, "mult": mult,
                    "gov": gov, "expected": round(expected_clamped, 2),
                })
        assert not bad, f"audit math failed for: {bad}"


# ───────────── canary pick: lock_score == lock_score_raw == 92 ─────────────
class TestCanaryPick:
    def test_canary_pick_detail_endpoint(self, auth_session):
        r = auth_session.get(f"{BASE_URL}/api/picks/{CANARY_PICK_ID}", timeout=20)
        if r.status_code == 404:
            pytest.skip(f"canary pick {CANARY_PICK_ID} not on board today")
        assert r.status_code == 200, f"{r.status_code} {r.text[:200]}"
        p = r.json()
        raw = p.get("lock_score_raw")
        gov = p.get("lock_score")
        assert raw is not None and gov is not None, f"missing raw/gov: {p}"
        # Per main agent: lock=92, raw=92, multiplier=1.0
        assert abs(float(gov) - float(raw)) <= 0.5, \
            f"canary still mismatched: lock={gov} raw={raw}"

    def test_canary_pick_inspector_parity(self, auth_session):
        r1 = auth_session.get(f"{BASE_URL}/api/picks/{CANARY_PICK_ID}", timeout=20)
        r2 = auth_session.get(f"{BASE_URL}/api/admin/pick-evidence/{CANARY_PICK_ID}", timeout=30)
        if r1.status_code == 404 or r2.status_code == 404:
            pytest.skip("canary pick not on board today")
        assert r1.status_code == 200 and r2.status_code == 200
        p = r1.json()
        ins = r2.json()
        # Both endpoints should agree on the four core metrics.
        assert abs(float(p["lock_score"]) - float(ins["lock_score"])) <= 0.5, \
            f"lock_score divergence: pick={p['lock_score']} inspector={ins['lock_score']}"
        assert abs(float(p["lock_score_raw"]) - float(ins["lock_score_raw"])) <= 0.5, \
            f"lock_score_raw divergence: pick={p['lock_score_raw']} inspector={ins['lock_score_raw']}"
        assert int(p["evidence_score"]) == int(ins["evidence_score"]), \
            f"evidence_score divergence: pick={p['evidence_score']} inspector={ins['evidence_score']}"


# ───────────── inspector parity on 5 random picks ─────────────
class TestInspectorParityRandom:
    def test_five_random_picks_consistent(self, auth_session, picks_today):
        random.seed(101)
        sample = random.sample(picks_today, k=min(5, len(picks_today)))
        mismatches = []
        for p in sample:
            pid = p["id"]
            r = auth_session.get(f"{BASE_URL}/api/admin/pick-evidence/{pid}", timeout=20)
            if r.status_code != 200:
                continue
            ins = r.json()
            ls_pick = p.get("lock_score")
            ls_ins = ins.get("lock_score")
            raw_pick = p.get("lock_score_raw")
            raw_ins = ins.get("lock_score_raw")
            if ls_pick is None or ls_ins is None:
                continue
            if abs(float(ls_pick) - float(ls_ins)) > 0.6:
                mismatches.append({"id": pid, "field": "lock_score",
                                    "pick": ls_pick, "inspector": ls_ins})
            if raw_pick is not None and raw_ins is not None:
                if abs(float(raw_pick) - float(raw_ins)) > 0.6:
                    mismatches.append({"id": pid, "field": "lock_score_raw",
                                        "pick": raw_pick, "inspector": raw_ins})
        assert not mismatches, f"inspector parity broken: {mismatches}"


# ───────────── win_probability untouched ─────────────
class TestWinProbabilityUntouched:
    def test_win_probability_stable(self, picks_today):
        """win_probability is exposed on the 0-100 scale in /api/picks/today."""
        for p in picks_today[:20]:
            wp = p.get("win_probability")
            if wp is None:
                continue
            assert 0.0 <= float(wp) <= 100.0, f"win_probability out of range: {wp}"

    def test_win_probability_not_mutated_by_govern(self, picks_today):
        """Re-running govern_pick must not touch win_probability (rule 7)."""
        import sys
        sys.path.insert(0, "/app/backend")
        from evidence_engine import govern_pick, build_features_from_pick
        checked = 0
        for p in picks_today[:10]:
            wp = p.get("win_probability")
            if wp is None:
                continue
            clone = dict(p)
            govern_pick(clone, build_features_from_pick(clone))
            assert clone.get("win_probability") == wp, \
                f"win_probability mutated for pick {p.get('id')}"
            checked += 1
        assert checked >= 3


# ───────────── hype-word filtering ─────────────
class TestHypeFiltering:
    def test_low_evidence_no_hype_in_explanation(self, picks_today):
        """For low-evidence picks the explanation should NOT contain unguarded hype."""
        hype = ["elite", "dominant", "automatic", "massive edge"]
        violations = []
        for p in picks_today:
            es = p.get("evidence_score")
            if es is None or es >= 40:
                continue
            blobs = []
            for k in ("explanation", "summary", "reasoning"):
                v = p.get(k)
                if isinstance(v, str):
                    blobs.append(v.lower())
                elif isinstance(v, list):
                    blobs.extend(str(x).lower() for x in v)
            text = " ".join(blobs)
            for h in hype:
                if h in text:
                    violations.append({"id": p["id"], "evidence": es, "word": h})
                    break
        # Allow tiny leakage (rounding around 40) but flag systemic leaks.
        assert len(violations) <= 2, f"hype leaked into low-evidence picks: {violations[:5]}"
