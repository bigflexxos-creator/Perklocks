"""PERKLOCKS-MAIN 35 · P0-1 END-TO-END WIRED PROOF.

Proves that `_build_tennis_alt_picks` (the ACTUAL production writer)
consumes the format resolver + empirical CDF and NEVER re-emits the
false 99% Under-line probability that produced the persisted row
`9b41dbd5-604b-5748-b081-abcd6a0e6d21`.

The stale row was invalidated in the DB directly by this session
(off_board=True, publication_state=INVALIDATED). This test locks
in the CODE-path correctness so a future regression cannot
resurface the same 99% wager.
"""
from __future__ import annotations

import pytest


def test_e2e_active_writer_produces_corrected_bo5_probability():
    """Full-fidelity probe of the exact ATP US Open matchup that
    produced the false 99% Under 42.5 persisted row."""
    import services.tennis_math_engine as _tme
    import sports_engine

    # Real Elo pipeline supplied a competitive signal at production
    # time (Popyrin ~55% home_win_prob).  Feed the equivalent so the
    # writer's `if _sig and has_real_tennis_signal(_sig)` branch is
    # taken and the format-aware distribution is built.
    def _fake_score(*a, **kw):
        return {
            "home_win_prob": 0.55,
            "contributions": {"elo": 0.03},
            "used_signals": ["elo"],
            "signals_count": 1,
            "has_elo_baseline": True,
        }
    orig_score = _tme.score_tennis_matchup
    orig_real  = _tme.has_real_tennis_signal
    _tme.score_tennis_matchup   = _fake_score
    _tme.has_real_tennis_signal = lambda s: True

    # Capture every `_build_pick` invocation so we can inspect the
    # writer's computed model_win_prob for the 42.5 Under threshold.
    captured: list[dict] = []
    orig_bp = sports_engine._build_pick
    def _trace_bp(*a, **kw):
        captured.append({"market": kw.get("market"),
                          "model_win_prob": kw.get("model_win_prob"),
                          "book_odds": kw.get("book_odds"),
                          "lock": kw.get("lock")})
        return orig_bp(*a, **kw)
    sports_engine._build_pick = _trace_bp

    try:
        alt_payload = {
            "id": "e-tabilo-popyrin",
            "home_team": "Alexei Popyrin",
            "away_team": "Alejandro Tabilo",
            "commence_time": "2026-09-04T18:00:00Z",
            "bookmakers": [{"key": "fanduel", "markets": [{
                "key": "alternate_totals",
                "outcomes": [
                    {"name": "Under", "point": 42.5, "price": -147},
                    {"name": "Under", "point": 44.5, "price": -240},
                    {"name": "Under", "point": 46.5, "price": -400},
                ]}]}],
        }
        event_payload = dict(alt_payload)
        event_payload["surface"] = "hard"
        event_payload["_ctx"] = {"surface": "hard"}

        sports_engine._build_tennis_alt_picks(
            sport_key="tennis_atp_us_open",
            league="Tennis",
            event_payload=event_payload,
            alt_payload=alt_payload,
            date_str="2026-09-04",
        )
    finally:
        _tme.score_tennis_matchup = orig_score
        _tme.has_real_tennis_signal = orig_real
        sports_engine._build_pick = orig_bp

    # The writer must have priced EVERY threshold from the same
    # empirical distribution — 3 Under picks captured.
    unders = [c for c in captured if "Under" in (c["market"] or "")]
    assert len(unders) == 3, unders

    # Extract 42.5 Under.
    p_425 = next(c for c in unders if "42.5" in c["market"])
    p_445 = next(c for c in unders if "44.5" in c["market"])
    p_465 = next(c for c in unders if "46.5" in c["market"])

    # Corrected BO5 math: Under 42.5 must be well below 0.99.
    assert p_425["model_win_prob"] < 0.90, (
        f"REGRESSION — Under 42.5 model_win_prob={p_425['model_win_prob']:.4f} "
        "≥ 0.90; the BO5 fix is not wired at the active writer."
    )

    # Monotonicity — higher Under threshold => higher Under probability
    # (drawn from the same sorted empirical CDF).
    assert p_425["model_win_prob"] <= p_445["model_win_prob"] <= p_465["model_win_prob"], (
        "Ladder monotonicity broken",
        p_425["model_win_prob"], p_445["model_win_prob"], p_465["model_win_prob"],
    )

    # Book odds preserved verbatim from the real provider outcomes.
    assert p_425["book_odds"] == -147
    assert p_445["book_odds"] == -240
    assert p_465["book_odds"] == -400
