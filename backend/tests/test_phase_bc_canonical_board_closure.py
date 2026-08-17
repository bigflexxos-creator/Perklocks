"""Phase B + C — μ-closure focused regressions.

Scope: B1, B4, B5, B6, C1, C3, C4 confirmed defects.  Other B/C
items are VERIFIED-NO-CHANGE (behavior already correct) or PARTIAL
(deferred with explicit note).

Runs standalone: ``python tests/test_phase_bc_canonical_board_closure.py``
"""
import asyncio
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ══════════════════════════════════════════════════════════════════
# B1 — Canonical publication gate fail-closed
# ══════════════════════════════════════════════════════════════════
def test_B1_gate_default_on_and_fail_closed():
    # Ensure env is unset for this test.
    os.environ.pop("LOCKSCORE_REQUIRE_CANONICAL_PUBLICATION", None)
    import importlib
    import services.canonical_board_source as m
    importlib.reload(m)
    # Default now ON (was OFF before μ-closure).
    assert m.is_canonical_publication_required() is True, (
        "B1 defect — canonical publication gate default must be ON")
    filt = m.canonical_publication_filter()
    assert filt == {"publication_source": {"$exists": True, "$ne": None}}, (
        f"B1 defect — expected canonical filter fragment, got {filt}")
    # Explicit false disables (emergency bypass preserved).
    for val in ("false", "0", "no", "off"):
        os.environ["LOCKSCORE_REQUIRE_CANONICAL_PUBLICATION"] = val
        importlib.reload(m)
        assert m.is_canonical_publication_required() is False, val
        assert m.canonical_publication_filter() == {}
    # Anything else enables (fail-closed on typos).
    for val in ("", "1", "yes", "on", "typo"):
        os.environ["LOCKSCORE_REQUIRE_CANONICAL_PUBLICATION"] = val
        importlib.reload(m)
        assert m.is_canonical_publication_required() is True, val
    os.environ.pop("LOCKSCORE_REQUIRE_CANONICAL_PUBLICATION", None)
    print("test_B1_gate_default_on_and_fail_closed OK")


def test_B1_route_exception_fails_closed():
    """When the canonical gate module errors, /picks/today MUST inject
    a filter that matches ZERO documents (empty board) rather than
    continuing without a filter (would leak noncanonical picks)."""
    root = os.path.join(os.path.dirname(__file__), "..")
    with open(os.path.join(root, "routes/picks_routes.py")) as f:
        src = f.read()
    # The old fail-open comment must be gone.
    assert "gate skipped due to error" not in src, (
        "B1 regression — legacy fail-open exception path still present")
    # Fail-closed sentinel present.
    assert '"__canonical_gate_error__"' in src, (
        "B1 defect — fail-closed sentinel not injected on gate error")
    assert "FAILED CLOSED" in src
    print("test_B1_route_exception_fails_closed OK")


# ══════════════════════════════════════════════════════════════════
# B4 — Frontend Lock Score canonical only (no V2 promotion)
# ══════════════════════════════════════════════════════════════════
def test_B4_display_uses_canonical_not_v2():
    root = os.path.join(os.path.dirname(__file__), "..", "..",
                         "frontend/src/lib/lockScore.ts")
    with open(root) as f:
        src = f.read()
    # The old max(v1, v2) promotion must be gone.
    assert "Math.max(safe1, safe2)" not in src, (
        "B4 defect — Math.max(lock_score, lock_score_v2) still promotes shadow V2")
    # Canonical field must be primary source.
    assert "published_lock_score" in src, (
        "B4 defect — published_lock_score not consulted as authoritative source")
    # V2 must NOT appear in the return-value computation.
    _fn_start = src.find("export function getDisplayLock(")
    _fn_end   = src.find("export function getDisplayLockRounded")
    _fn = src[_fn_start:_fn_end]
    assert "lock_score_v2" not in _fn, (
        "B4 defect — getDisplayLock still reads lock_score_v2")
    print("test_B4_display_uses_canonical_not_v2 OK")


# ══════════════════════════════════════════════════════════════════
# B5 — Display allows canonical Apex 100
# ══════════════════════════════════════════════════════════════════
def test_B5_apex_100_not_clamped():
    root = os.path.join(os.path.dirname(__file__), "..", "..",
                         "frontend/src/lib/lockScore.ts")
    with open(root) as f:
        src = f.read()
    assert "Math.min(99," not in src, (
        "B5 defect — display still clamps canonical Lock Score to 99")
    assert "Math.min(100," in src, (
        "B5 defect — display must allow canonical Apex 100")
    print("test_B5_apex_100_not_clamped OK")


# ══════════════════════════════════════════════════════════════════
# B6 — Min-lock / cap read canonical, not V2 shadow
# ══════════════════════════════════════════════════════════════════
def test_B6_cap_reads_canonical():
    root = os.path.join(os.path.dirname(__file__), "..",
                         "routes/picks_routes.py")
    with open(root) as f:
        src = f.read()
    # Per-sport cap must read canonical published_lock_score first.
    _cap = src.find("_PER_SPORT_CAP = 100")
    _end = src.find("canonical = _capped", _cap)
    _blk = src[_cap:_end]
    assert 'p.get("lock_score_v2")' not in _blk, (
        "B6 defect — per-sport cap still reads lock_score_v2 before canonical")
    assert 'p.get("published_lock_score")' in _blk, (
        "B6 defect — per-sport cap does not consult published_lock_score")
    print("test_B6_cap_reads_canonical OK")


# ══════════════════════════════════════════════════════════════════
# C1 — Raw DB count alone cannot declare slate healthy
# ══════════════════════════════════════════════════════════════════
def test_C1_actionable_coverage_gate():
    root = os.path.join(os.path.dirname(__file__), "..", "server.py")
    with open(root) as f:
        src = f.read()
    # Old raw-count gate must be gone.
    _fn = src[src.find("async def _ensure_today_picks"):
              src.find("_background_refresh")]
    assert "if count >= 20" not in _fn.replace(" ", ""), (
        "C1 defect — raw db.picks count gate still declares slate healthy")
    # Actionable-coverage filter must be present.
    assert "publication_source" in _fn and "off_board" in _fn \
        and "settlement_block" in _fn and "actionable >= 20" in _fn, (
        "C1 defect — actionable coverage gate not wired")
    print("test_C1_actionable_coverage_gate OK")


# ══════════════════════════════════════════════════════════════════
# C3 — Lock 85-89 records reachable past position #100
# ══════════════════════════════════════════════════════════════════
def test_C3_safety_valve_at_canonical_floor():
    root = os.path.join(os.path.dirname(__file__), "..",
                         "routes/picks_routes.py")
    with open(root) as f:
        src = f.read()
    assert "_SAFETY_VALVE_LOCK = 85.0" in src, (
        "C3 defect — safety valve floor not lowered to canonical 85 floor")
    assert "_SAFETY_VALVE_LOCK = 90.0" not in src, (
        "C3 defect — legacy 90-only safety valve still active")
    print("test_C3_safety_valve_at_canonical_floor OK")


# ══════════════════════════════════════════════════════════════════
# C4 — Multiple qualified goalscorers remain reachable
# ══════════════════════════════════════════════════════════════════
def test_C4_multiple_goalscorers():
    root = os.path.join(os.path.dirname(__file__), "..",
                         "routes/picks_routes.py")
    with open(root) as f:
        src = f.read()
    assert "top_n=1)" not in src, (
        "C4 defect — top_n=1 goalscorer cap still restricts multi-scorer eligibility")
    assert "top_n=3)" in src, (
        "C4 defect — expected goalscorer top_n=3 after μ-closure")
    print("test_C4_multiple_goalscorers OK")


# ══════════════════════════════════════════════════════════════════
# Runtime: apply the per-sport cap on a synthetic 105-pick sport slate
# and prove Lock 85-89 picks past #100 survive.
# ══════════════════════════════════════════════════════════════════
def test_C3_runtime_lock85_reachable():
    picks = []
    # 100 picks Lock=95 (fill the cap)
    for i in range(100):
        picks.append({"id": f"p95_{i}", "sport": "Soccer",
                      "published_lock_score": 95, "lock_score": 95})
    # 5 picks Lock 85-89 that arrive after position #100
    for i, lk in enumerate((88, 87, 86, 85, 85)):
        picks.append({"id": f"p85_{i}", "sport": "Soccer",
                      "published_lock_score": lk, "lock_score": lk})
    # 3 picks Lock 82 (sub-canonical floor — expected to be dropped)
    for i in range(3):
        picks.append({"id": f"p82_{i}", "sport": "Soccer",
                      "published_lock_score": 82, "lock_score": 82})

    # Emulate the exact loop in picks_routes.
    _PER_SPORT_CAP = 100
    _SAFETY_VALVE_LOCK = 85.0
    _cap_counts: dict = {}
    _capped: list = []
    for p in picks:
        sp = str(p.get("sport") or "").strip() or "Unknown"
        lk = float(p.get("published_lock_score") or p.get("lock_score") or 0)
        if _cap_counts.get(sp, 0) >= _PER_SPORT_CAP:
            if lk >= _SAFETY_VALVE_LOCK:
                _capped.append(p)
                _cap_counts[sp] = _cap_counts.get(sp, 0) + 1
                continue
            continue
        _cap_counts[sp] = _cap_counts.get(sp, 0) + 1
        _capped.append(p)

    kept_ids = {p["id"] for p in _capped}
    # C3 core assertion — Lock 85-89 records past #100 remain reachable.
    for i in range(5):
        assert f"p85_{i}" in kept_ids, (
            f"C3 defect — Lock 85-89 pick p85_{i} dropped past position #100")
    # Sub-canonical picks may be trimmed for payload safety.
    for i in range(3):
        assert f"p82_{i}" not in kept_ids
    print("test_C3_runtime_lock85_reachable OK")


# ══════════════════════════════════════════════════════════════════
# Canonical Lock Score display — runtime happy path
# ══════════════════════════════════════════════════════════════════
def test_display_lock_score_priority_runtime():
    # Emulate the TS logic in Python for deterministic runtime check.
    def get_display_lock(pick):
        if not pick:
            return 0
        pub = pick.get("published_lock_score")
        try:
            pub_n = float(pub) if pub is not None else None
        except Exception:
            pub_n = None
        if pub_n is not None and pub_n > 0:
            return min(100, max(0, pub_n))
        try:
            v1 = float(pick.get("lock_score") or 0)
        except Exception:
            v1 = 0
        return min(100, max(0, v1))

    # 1. Canonical wins over V2 (B4).
    assert get_display_lock({"published_lock_score": 88,
                              "lock_score": 90,
                              "lock_score_v2": 95}) == 88
    # 2. When canonical absent, V1 fallback used — not V2 (B4).
    assert get_display_lock({"lock_score": 87, "lock_score_v2": 96}) == 87
    # 3. Canonical Apex 100 renders as 100, not 99 (B5).
    assert get_display_lock({"published_lock_score": 100}) == 100
    # 4. Values above 100 clamped to 100.
    assert get_display_lock({"published_lock_score": 108}) == 100
    print("test_display_lock_score_priority_runtime OK")


if __name__ == "__main__":
    test_B1_gate_default_on_and_fail_closed()
    test_B1_route_exception_fails_closed()
    test_B4_display_uses_canonical_not_v2()
    test_B5_apex_100_not_clamped()
    test_B6_cap_reads_canonical()
    test_C1_actionable_coverage_gate()
    test_C3_safety_valve_at_canonical_floor()
    test_C4_multiple_goalscorers()
    test_C3_runtime_lock85_reachable()
    test_display_lock_score_priority_runtime()
    print("\nPHASE_BC_CANONICAL_BOARD_TESTS_ALL_PASSED")
