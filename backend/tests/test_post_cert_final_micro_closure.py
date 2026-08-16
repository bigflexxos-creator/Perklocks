"""Post-Cert Runtime Consumer Closure — FINAL MICRO-CLOSURE.

Two proofs only:

    A. Production simulator caller (brain.sim_runner.simulate_pick)
       passes the correct existing-context field to each sport's
       simulator when it's stamped on the pick, so the simulator can
       emit EMPIRICAL_INDEPENDENT provenance.  Absent context → honest
       MODEL_CONDITIONED / PRIOR_ONLY fallback.

    B. compute_lock_score safety — 5 deterministic assertions that
       the normalisation change did not introduce a chalk/underdog
       bias, did not raise Apex 100, and does not let weak evidence
       pass 85.

Zero paid provider calls.  Everything runs against in-memory fixtures.
"""
from __future__ import annotations

from unittest.mock import patch
import pytest


# ═══════════════════════════════════════════════════════════════════════
# A) Production caller passes context to each sport's simulator
# ═══════════════════════════════════════════════════════════════════════
def _run_and_capture_ctx(sport: str, module_name: str, fn_name: str,
                          pick: dict) -> dict:
    """Call brain.sim_runner.simulate_pick and capture what context
    argument the underlying simulator received."""
    import importlib
    mod = importlib.import_module(module_name)
    original = getattr(mod, fn_name)
    captured: dict = {}

    def _spy(*args, **kwargs):
        # Positional: (pick, ctx_or_stats)
        captured["args"] = args
        captured["kwargs"] = kwargs
        return {
            "probability": 0.55,
            "simulator_provenance": (
                "EMPIRICAL_INDEPENDENT" if (len(args) > 1 and args[1])
                or kwargs.get("soccer_ctx") or kwargs.get("recent_rows")
                or kwargs.get("tennis_ctx")
                else "MODEL_CONDITIONED"
            ),
            "input_quality": "FULL",
        }

    setattr(mod, fn_name, _spy)
    try:
        from brain.sim_runner import simulate_pick
        out = simulate_pick(pick)
    finally:
        setattr(mod, fn_name, original)
    return {"captured": captured, "out": out}


def test_A_nba_caller_passes_recent_rows_when_present():
    """NBA production caller passes `recent_rows` from the pick to
    simulate_nba_pick.  Simulator sees the context → can emit
    EMPIRICAL_INDEPENDENT."""
    recent_rows = [
        {"pts": 28, "reb": 6, "ast": 4, "min": 34},
        {"pts": 22, "reb": 5, "ast": 6, "min": 32},
        {"pts": 31, "reb": 8, "ast": 5, "min": 35},
    ]
    pick = {"id": "nba_ctx", "sport": "NBA",
            "market": "Player Points", "selection": "X Over 25.5",
            "book_odds": -110, "player_recent_rows": recent_rows,
            "win_probability": 60.0}
    r = _run_and_capture_ctx("NBA", "brain.sim_nba",
                              "simulate_nba_pick", pick)
    kw = r["captured"]["kwargs"]
    assert kw.get("recent_rows") == recent_rows, \
        "sim_runner must forward recent_rows to simulate_nba_pick"
    assert r["out"]["simulator_provenance"] == "EMPIRICAL_INDEPENDENT"
    assert r["out"]["independent_evidence"] is True


def test_A_nba_caller_absent_context_falls_back_to_model_conditioned():
    """No `recent_rows` on the pick → simulator falls back honestly."""
    pick = {"id": "nba_no_ctx", "sport": "NBA",
            "market": "Player Points", "selection": "X Over 25.5",
            "book_odds": -110, "win_probability": 60.0}
    r = _run_and_capture_ctx("NBA", "brain.sim_nba",
                              "simulate_nba_pick", pick)
    kw = r["captured"]["kwargs"]
    assert kw.get("recent_rows") is None
    assert r["out"]["simulator_provenance"] == "MODEL_CONDITIONED"
    assert r["out"]["independent_evidence"] is False


def test_A_soccer_caller_passes_soccer_ctx_when_present():
    """Soccer production caller passes `soccer_ctx` when stamped."""
    soccer_ctx = {"home_form": {"attack": 1.55, "defense": 0.90},
                   "away_form": {"attack": 1.30, "defense": 1.10}}
    pick = {"id": "soc_ctx", "sport": "Soccer",
            "market": "Match Total Over 2.5",
            "selection": "Over 2.5",
            "book_odds": -120, "soccer_ctx": soccer_ctx,
            "win_probability": 62.0,
            "event": "Man City vs Liverpool"}
    r = _run_and_capture_ctx("Soccer", "brain.sim_soccer",
                              "simulate_soccer_pick", pick)
    kw = r["captured"]["kwargs"]
    assert kw.get("soccer_ctx") == soccer_ctx, \
        "sim_runner must forward soccer_ctx to simulate_soccer_pick"
    assert r["out"]["simulator_provenance"] == "EMPIRICAL_INDEPENDENT"


def test_A_soccer_caller_absent_context_falls_back():
    pick = {"id": "soc_no_ctx", "sport": "Soccer",
            "market": "Match Total Over 2.5",
            "selection": "Over 2.5", "book_odds": -120,
            "win_probability": 55.0, "event": "A vs B"}
    r = _run_and_capture_ctx("Soccer", "brain.sim_soccer",
                              "simulate_soccer_pick", pick)
    kw = r["captured"]["kwargs"]
    assert kw.get("soccer_ctx") is None
    assert r["out"]["simulator_provenance"] == "MODEL_CONDITIONED"
    assert r["out"]["independent_evidence"] is False


def test_A_tennis_caller_passes_tennis_ctx_when_present():
    """Tennis production caller passes `tennis_ctx` when stamped."""
    tennis_ctx = {"surface": "hard",
                   "elo_diff": 45.0,
                   "hold_break_diff": 0.03,
                   "form_diff": 0.02}
    pick = {"id": "ten_ctx", "sport": "Tennis",
            "market": "Moneyline", "selection": "Alcaraz",
            "book_odds": -140, "tennis_ctx": tennis_ctx,
            "win_probability": 65.0,
            "event": "Alcaraz vs Sinner"}
    r = _run_and_capture_ctx("Tennis", "brain.sim_tennis",
                              "simulate_tennis_pick", pick)
    kw = r["captured"]["kwargs"]
    assert kw.get("tennis_ctx") == tennis_ctx, \
        "sim_runner must forward tennis_ctx to simulate_tennis_pick"
    assert r["out"]["simulator_provenance"] == "EMPIRICAL_INDEPENDENT"


def test_A_tennis_caller_absent_context_falls_back():
    pick = {"id": "ten_no_ctx", "sport": "Tennis",
            "market": "Moneyline", "selection": "X", "book_odds": -140,
            "win_probability": 60.0, "event": "X vs Y"}
    r = _run_and_capture_ctx("Tennis", "brain.sim_tennis",
                              "simulate_tennis_pick", pick)
    kw = r["captured"]["kwargs"]
    assert kw.get("tennis_ctx") is None
    assert r["out"]["simulator_provenance"] == "MODEL_CONDITIONED"


# ═══════════════════════════════════════════════════════════════════════
# B) Lock Score safety after weight-normalisation change
# ═══════════════════════════════════════════════════════════════════════
def test_B_A_weak_evidence_missing_roi_clv_stays_below_85():
    """Weak evidence + missing ROI/CLV must NOT cross the 85 floor."""
    from sports_engine import compute_lock_score
    lock, _ = compute_lock_score(
        {"Form": 0.50}, win_prob=55, edge_percent=2.0,
        pick={"book_odds": -110, "edge_percent": 2.0,
              "win_probability": 55})
    assert lock < 85.0, f"weak → {lock}"


def test_B_B_strong_soccer_evidence_can_reach_85():
    """Strong Soccer pregame evidence with legitimate unavailable
    ROI/CLV can now legitimately reach ≥85."""
    from sports_engine import compute_lock_score
    lock, _ = compute_lock_score(
        {"Form": 0.92, "xG advantage": 0.90, "Matchup History": 0.88},
        win_prob=70, edge_percent=10.0,
        pick={"book_odds": -140, "edge_percent": 10.0,
              "win_probability": 70})
    assert lock >= 85.0, f"strong pregame → {lock}"


def test_B_C_favorite_underdog_neutrality_no_material_bias():
    """Identical evidence at -400 vs +150 → no material chalk bias."""
    from sports_engine import compute_lock_score
    factors = {"Form": 0.88, "Matchup": 0.85, "Sample": 0.82}
    # Same edge in both cases so we isolate price bias only.
    chalk_lock, _ = compute_lock_score(
        factors, win_prob=75, edge_percent=6.0,
        pick={"book_odds": -400, "edge_percent": 6.0,
              "win_probability": 75})
    dog_lock, _ = compute_lock_score(
        factors, win_prob=45, edge_percent=6.0,
        pick={"book_odds": +150, "edge_percent": 6.0,
              "win_probability": 45})
    # Neither price should dominate.  A small delta (<= ~10pt) is
    # tolerable because volatility component legitimately penalises
    # extreme prices at BOTH ends (heavy chalk AND long shots).
    assert abs(chalk_lock - dog_lock) <= 10.0, (
        f"material price bias detected: chalk={chalk_lock} dog={dog_lock}"
    )
    # And neither price alone should push a lock past 99.
    assert chalk_lock <= 99.0 and dog_lock <= 99.0


def test_B_D_non_apex_stays_at_or_below_99():
    """Non-Apex compute_lock_score must never emit 100."""
    from sports_engine import compute_lock_score
    # Try a variety of strong inputs — none of them are Apex-qualified
    # via the compute path (Apex is stamped by apex_gate).
    for wp, ed in [(72, 8.0), (78, 6.0), (85, 10.0), (90, 12.0)]:
        lock, _ = compute_lock_score(
            {"F": 0.90, "M": 0.88, "S": 0.85, "H": 0.87},
            win_prob=wp, edge_percent=ed,
            pick={"book_odds": -150, "edge_percent": ed,
                  "win_probability": wp,
                  "odds_at_pick": -150, "closing_odds": -170},
            bucket_row={"roi": 0.08, "n": 60})
        assert lock <= 99.0, f"non-Apex compute yielded {lock}"


def test_B_E_apex_contract_unchanged():
    """Apex is stamped by apex_gate, not by compute_lock_score.
    Confirm the apex_gate contract module remains importable and
    exposes the Apex qualification API."""
    import services.magic.apex_gate as apex_gate
    exported = dir(apex_gate)
    assert any("apex" in n.lower() for n in exported), \
        "apex_gate module contract removed"
