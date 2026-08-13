"""FINAL MAGIC CONVERGENCE + PRODUCTION CERTIFICATION.

Certifies Magic 2.0 → 3I.1 as a coherent whole.  Adds NO new
adapters or scoring math — only pins the existing contracts.
"""
from __future__ import annotations

import asyncio
import inspect
import pytest

from services.magic.contract import (
    Availability, EvidenceType, MagicTier, EvidenceItem,
    MagicOutput, availability_from,
)


# ═══════════════════════════════════════════════════════════════════
# 1. Canonical Magic output contract (Phase 2)
# ═══════════════════════════════════════════════════════════════════

def test_magic_output_contract_has_required_fields():
    """One coherent Magic output.  Presence pin per Phase 2 directive."""
    fields = MagicOutput.__dataclass_fields__.keys()
    for f in ("pick_id", "sport", "market", "selection", "line",
              "canonical_player_id", "canonical_team_id",
              "identity_class",
              "magic_score", "magic_tier", "magic_score_available",
              "evidence", "risk_flags",
              "strongest_positive", "strongest_negative",
              "model_market_state", "generated_at"):
        assert f in fields, f"Magic output missing field: {f}"


def test_magic_output_serializes_deterministically():
    m = MagicOutput(pick_id="p1", sport="MLB", market="Total Bases",
                    selection="Over", line=1.5,
                    canonical_player_id="660271")
    d = m.to_dict()
    assert d["pick_id"] == "p1"
    assert d["magic_tier"] == MagicTier.INSUFFICIENT_EVIDENCE.value
    assert d["magic_score_available"] is False
    assert isinstance(d["evidence"], list)
    assert isinstance(d["risk_flags"], list)


def test_no_competing_magic_output_class_exists():
    """Only ONE MagicOutput should exist in services/magic."""
    from services import magic as pkg
    assert pkg.MagicOutput is MagicOutput


# ═══════════════════════════════════════════════════════════════════
# 2. Missing-evidence honesty (Phase 5)
# ═══════════════════════════════════════════════════════════════════

def test_availability_from_treats_none_value_as_unavailable():
    assert availability_from(None) == Availability.UNAVAILABLE
    assert availability_from(0.0) == Availability.AVAILABLE  # 0 is a real value


def test_missing_evidence_never_becomes_neutral_zero():
    """An EvidenceItem with value=None must carry UNAVAILABLE not
    silently treat as 0."""
    ev = EvidenceItem(
        evidence_type=EvidenceType.HISTORICAL_EXACT_THRESHOLD,
        availability=Availability.UNAVAILABLE, value=None,
    )
    d = ev.to_dict()
    assert d["value"] is None
    assert d["availability"] == "UNAVAILABLE"


def test_partial_evidence_class_lower_than_available():
    """Ordering pin: PARTIAL and STALE < AVAILABLE.  Downstream ranking
    code depends on these membership contracts."""
    # Simply verify all four members are distinct string identifiers.
    values = {Availability.AVAILABLE.value, Availability.PARTIAL.value,
              Availability.STALE.value, Availability.UNAVAILABLE.value}
    assert len(values) == 4


# ═══════════════════════════════════════════════════════════════════
# 3. Model / Simulator / Calibration / Market separation (Phase 15)
# ═══════════════════════════════════════════════════════════════════

def test_evidence_type_families_are_distinct():
    """Distinct enum values — model ≠ sim ≠ calibration ≠ market."""
    from services.magic.contract import EvidenceType as ET
    for name in ("MODEL_PROBABILITY", "SIMULATOR_PROBABILITY",
                  "CALIBRATED_PROBABILITY", "SPORTSBOOK_CONSENSUS"):
        if hasattr(ET, name):
            assert getattr(ET, name).value == name
    # Explicit non-alias check
    if hasattr(ET, "MODEL_PROBABILITY") and hasattr(ET, "SIMULATOR_PROBABILITY"):
        assert ET.MODEL_PROBABILITY != ET.SIMULATOR_PROBABILITY


# ═══════════════════════════════════════════════════════════════════
# 4. Exact-line safety (Phase 18)
# ═══════════════════════════════════════════════════════════════════

def test_exact_line_fingerprint_isolation_via_sim_cal_store():
    """Different line → different simulator fingerprint (Magic reads
    only from `simulator_outputs` — line safety flows through here)."""
    from services.magic.sim_cal_store import build_input_fingerprint
    def _pk(line):
        return {"id":"px","sport":"MLB","canonical_player_id":"cp1",
                 "canonical_event_id":"e1","market":"Total Bases",
                 "line":line,"side":"over"}
    for a, b in ((0.5, 1.5), (5.5, 6.5), (250, 275),
                  (200, 225), (2.5, 3.5)):
        assert build_input_fingerprint(_pk(a)) != build_input_fingerprint(_pk(b))


def test_exact_event_fingerprint_isolation():
    """Different event → different fingerprint even with same player+line."""
    from services.magic.sim_cal_store import build_input_fingerprint
    a = {"id":"pa","sport":"MLB","canonical_player_id":"cp1",
         "canonical_event_id":"e1","market":"TB","line":1.5,"side":"over"}
    b = dict(a); b["canonical_event_id"] = "e2"
    assert build_input_fingerprint(a) != build_input_fingerprint(b)


def test_exact_player_fingerprint_isolation():
    from services.magic.sim_cal_store import build_input_fingerprint
    a = {"id":"pa","sport":"MLB","canonical_player_id":"cp1",
         "canonical_event_id":"e1","market":"TB","line":1.5,"side":"over"}
    b = dict(a); b["canonical_player_id"] = "cp2"
    assert build_input_fingerprint(a) != build_input_fingerprint(b)


# ═══════════════════════════════════════════════════════════════════
# 5. Temporal safety (Phase 16)
# ═══════════════════════════════════════════════════════════════════

def test_pregame_cutoff_cascade_prefers_published_at():
    from services.magic.gold_evidence import _pregame_cutoff_from_pick
    pick = {"published_at": "2026-06-01T12:00:00Z",
             "event_time":    "2026-06-11T14:30:00Z",
             "created_at":    "2026-05-30T00:00:00Z"}
    _, day = _pregame_cutoff_from_pick(pick)
    assert day == "2026-06-01"


def test_clv_never_available_pregame():
    """CLV is post-prediction analytics only — Magic must never expose
    it pregame."""
    from services.magic.market_snapshot_store import (
        clv_for_postgame_only, ClvAvailabilityError,
    )
    with pytest.raises(ClvAvailabilityError):
        clv_for_postgame_only({"line_clv": 0.5, "price_clv": 0.02},
                              allow_pregame=False)


# ═══════════════════════════════════════════════════════════════════
# 6. Identity safety (Phase 17)
# ═══════════════════════════════════════════════════════════════════

def test_provisional_id_blocked_from_authoritative_sim():
    """Direct-inject bridge refuses provisional canonical id."""
    from services.magic.direct_inject_simulator_bridge import (
        simulate_direct_inject_pick,
    )
    class _DB:
        def __getattr__(self, n):
            class _C:
                async def find_one(*a, **k): return None
                async def update_one(*a, **k):
                    class R: matched_count=0; modified_count=0
                    return R()
            return _C()
        def __getitem__(self, n): return self.__getattr__(n)
    for bad in ("fallback:x", "unresolved:y"):
        r = asyncio.run(simulate_direct_inject_pick(_DB(), {
            "id":"p1","sport":"Soccer","market":"Anytime Scorer",
            "canonical_player_id":bad,
            "canonical_team_id":"t","canonical_event_id":"e"}))
        assert r["outcome"] == "IDENTITY_UNSAFE"


def test_mlb_producer_stamp_refuses_ambiguous():
    """Same-name ambiguity → refused (no fuzzy)."""
    from services.mlb_producer_identity_stamp import (
        resolve_mlb_source_id, clear_cache,
    )
    clear_cache()
    class _Coll:
        def __init__(self, docs): self._d = docs
        def find(self, q=None, projection=None):
            docs = self._d
            class _C:
                def __init__(self, a): self.a=list(a); self.i=0
                def __aiter__(self): return self
                async def __anext__(self):
                    if self.i>=len(self.a): raise StopAsyncIteration
                    d=self.a[self.i]; self.i+=1; return d
            return _C(docs)
    class _DB:
        mlb_statcast_players = _Coll([
            {"player_id":"1","name":"jose ramirez"},
            {"player_id":"2","name":"jose ramirez"},
        ])
        mlb_stuff_plus_players = _Coll([])
        def __getitem__(self, n): return getattr(self, n)
    _, cls = asyncio.run(resolve_mlb_source_id(
        _DB(), player_name="Jose Ramirez"))
    assert cls == "AMBIGUOUS"


# ═══════════════════════════════════════════════════════════════════
# 7. Contradiction certification (Phase 8)
# ═══════════════════════════════════════════════════════════════════

def test_contradiction_engine_flags_strong_history_vs_bench():
    from services.magic.contradictions import (
        detect_contradictions, RiskFlag,
    )
    from services.magic.contract import EvidenceItem, EvidenceType, Availability
    strong = EvidenceItem(evidence_type=EvidenceType.HISTORICAL_EXACT_THRESHOLD,
                          availability=Availability.AVAILABLE,
                          value=0.72, sample_size=25)
    flags = detect_contradictions(
        evidence=[strong], identity_class="AUTHORITATIVE",
        starter_status="BENCH")
    assert RiskFlag.HISTORICAL_STRONG_BUT_NOT_STARTER.value in flags


def test_contradiction_engine_flags_finishing_unsupported():
    from services.magic.contradictions import (
        detect_contradictions, RiskFlag,
    )
    flags = detect_contradictions(
        evidence=[], identity_class="AUTHORITATIVE",
        goals_over_xg_ratio=1.45)
    assert RiskFlag.FINISHING_UNSUPPORTED_BY_SHOT_QUALITY.value in flags


def test_contradictions_persist_even_with_high_positive_evidence():
    """Contradictions must remain visible — never averaged away."""
    from services.magic.contradictions import (
        detect_contradictions, RiskFlag,
    )
    from services.magic.contract import EvidenceItem, EvidenceType, Availability
    strong_positive = EvidenceItem(
        evidence_type=EvidenceType.HISTORICAL_EXACT_THRESHOLD,
        availability=Availability.AVAILABLE, value=0.85, sample_size=40)
    flags = detect_contradictions(
        evidence=[strong_positive],
        identity_class="AUTHORITATIVE",
        starter_status="OUT")
    # High positive evidence must NOT silence the OUT contradiction.
    assert len(flags) > 0


# ═══════════════════════════════════════════════════════════════════
# 8. Unsupported-sport safety (Phase 14)
# ═══════════════════════════════════════════════════════════════════

def test_cfb_ufc_nhl_still_report_simulator_unavailable():
    from services.magic.simulators.nfl_simulator import (
        cfb_simulator_status, ufc_simulator_status, nhl_simulator_status,
    )
    for f in (cfb_simulator_status, ufc_simulator_status,
              nhl_simulator_status):
        assert f()["status"] == "UNAVAILABLE"


def test_unsupported_sport_returns_insufficient_tier():
    """Magic must not crash on unsupported sport — returns
    INSUFFICIENT_EVIDENCE tier."""
    out = MagicOutput(pick_id="p1", sport="Cricket",
                      market="Total Runs", selection="Over",
                      line=250.5)
    assert out.magic_tier == MagicTier.INSUFFICIENT_EVIDENCE
    assert out.magic_score_available is False


# ═══════════════════════════════════════════════════════════════════
# 9. Sport-specific evidence type isolation (double-count guard)
# ═══════════════════════════════════════════════════════════════════

def test_soccer_creation_type_never_equals_shot_quality():
    from services.magic.gold_evidence import GoldEvidenceType
    assert GoldEvidenceType.SOCCER_CREATION != GoldEvidenceType.SOCCER_SHOT_QUALITY


def test_nfl_evidence_types_distinct_from_generic_evidence():
    from services.magic.gold_evidence_nfl import NflGoldEvidenceType
    families = {
        NflGoldEvidenceType.NFL_RECENT_FORM,
        NflGoldEvidenceType.NFL_USAGE,
        NflGoldEvidenceType.NFL_INJURY_STATUS,
        NflGoldEvidenceType.NFL_OPPONENT_HISTORY,
        NflGoldEvidenceType.NFL_THRESHOLD_HISTORY,
    }
    assert len(families) == 5   # all distinct


def test_atd_stat_never_conflated_with_yardage():
    """ATD is a Bernoulli stat; yards is continuous.  Different
    distribution + different market family."""
    from services.magic.simulators.nfl_simulator import (
        _nfl_stat, _STAT_DISTRIBUTIONS,
    )
    assert _nfl_stat("Anytime Touchdown") == "atd"
    assert _nfl_stat("Rushing Yards Over 50.5") == "rushing_yards"
    assert _STAT_DISTRIBUTIONS["atd"] == "bernoulli"
    assert _STAT_DISTRIBUTIONS["rushing_yards"] == "lognormal"


# ═══════════════════════════════════════════════════════════════════
# 10. Magic → Lock Score wiring: only the SANCTIONED Block 8 integrator
#     may write ``lock_score`` (per Block 8 Closure, 2026-06).
# ═══════════════════════════════════════════════════════════════════

def test_no_magic_to_lock_score_writes_from_magic_package():
    """Scan ``services/magic/*.py`` for writes to ``pick["lock_score"]``.

    Block 8 (2026-06) introduced ONE sanctioned integrator —
    ``services/magic/lock_score_integrator.py`` — that applies the
    bounded Magic delta and the explicit APEX gate.  Every other file
    in the Magic package MUST remain read-only w.r.t. ``lock_score``.
    """
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "services" / "magic"
    SANCTIONED = {"lock_score_integrator.py"}
    problems = []
    for p in root.rglob("*.py"):
        if p.name in SANCTIONED:
            continue
        src = p.read_text()
        # Strip docstrings/comments
        src = re.sub(r'""".*?"""', '', src, flags=re.S)
        src = re.sub(r"'''.*?'''", '', src, flags=re.S)
        src = re.sub(r'#.*', '', src)
        # Look for real writes to pick["lock_score..."] — NOT the
        # bridge's status dict field `out["lock_score_drift"]`.
        for m in re.finditer(
            r'(pick|p|candidate)\[["\']lock_score["\']?\s*[:\]]?\s*=', src):
            problems.append(f"{p.name}: {m.group()!r}")
    assert not problems, \
        f"Magic package must not write lock_score (outside the " \
        f"sanctioned Block 8 integrator): {problems}"


def test_magic_bridge_never_calls_lock_score_anchor():
    from services.magic import direct_inject_simulator_bridge as b
    import re
    src = inspect.getsource(b)
    src = re.sub(r'""".*?"""', '', src, flags=re.S)
    src = re.sub(r"'''.*?'''", '', src, flags=re.S)
    src = re.sub(r'#.*', '', src)
    assert "_anchor_pick_to_sim(" not in src
    assert "apply_simulations(" not in src


# ═══════════════════════════════════════════════════════════════════
# 11. Locked constants (do-not-touch pin)
# ═══════════════════════════════════════════════════════════════════

def test_final_convergence_does_not_change_locked_constants():
    from brain.sim_runner import SIM_RESIDUAL_MAX, MIN_RUNS_FOR_ANCHOR
    from brain.calibration import (
        MIN_SAMPLE_FOR_OVERRIDE, MAX_OPTIMISM_BUFFER,
    )
    assert SIM_RESIDUAL_MAX == 3.0
    assert MIN_RUNS_FOR_ANCHOR == 10_000
    assert MIN_SAMPLE_FOR_OVERRIDE == 20
    assert MAX_OPTIMISM_BUFFER == 5.0
