"""Regression tests for ``backend/brain/candidates.py`` hardening.

Goals (per user directive):
  1. Prove that valid, all-numeric picks produce the SAME
     `candidate_score`, `candidate_components`, and `candidate_rank`
     as before the hardening change (fault tolerance must NOT change
     normal ranking behaviour).
  2. Prove that malformed factor values (strings, None, NaN, inf,
     wrong container types) no longer crash `_consistency()`.
  3. Prove that a single malformed pick in a batch is logged and
     SKIPPED (score = 0.0, candidate_error set) while the rest of
     the batch is scored and ranked normally.
  4. Prove that multiple malformed picks do not abort the batch.
"""
from __future__ import annotations

import logging
import statistics

import pytest

from brain.candidates import (
    W,
    _as_float,
    _consistency,
    _data_completeness,
    _normalize_edge,
    rank_candidates,
)


# --------------------------------------------------------------------------- #
#  Fake BrainMemory (rank_candidates only calls memory.market)
# --------------------------------------------------------------------------- #
class _FakeMemory:
    def market(self, sport, family):  # noqa: D401 – minimal stub
        return None


# --------------------------------------------------------------------------- #
#  _as_float – the coercion primitive
# --------------------------------------------------------------------------- #
class TestAsFloat:
    def test_int(self):
        assert _as_float(3) == 3.0
        assert _as_float(0) == 0.0
        assert _as_float(-5) == -5.0

    def test_float(self):
        assert _as_float(1.5) == 1.5
        assert _as_float(-0.25) == -0.25

    def test_numeric_string(self):
        assert _as_float("1.5") == 1.5
        assert _as_float("3") == 3.0
        assert _as_float("  2.0 ") == 2.0

    def test_none_returns_none(self):
        assert _as_float(None) is None

    def test_empty_string(self):
        assert _as_float("") is None
        assert _as_float("   ") is None

    def test_invalid_string(self):
        assert _as_float("Wikipedia top scorer table") is None
        assert _as_float("hot streak") is None
        assert _as_float("N/A") is None

    def test_nan_and_inf(self):
        assert _as_float(float("nan")) is None
        assert _as_float(float("inf")) is None
        assert _as_float(float("-inf")) is None

    def test_bool_rejected(self):
        # bool is a subclass of int in Python but has no place in factor
        # arithmetic; ensure we reject it explicitly.
        assert _as_float(True) is None
        assert _as_float(False) is None

    def test_arbitrary_object(self):
        assert _as_float({"a": 1}) is None
        assert _as_float([1, 2, 3]) is None
        assert _as_float(object()) is None


# --------------------------------------------------------------------------- #
#  _consistency – the actual crash site
# --------------------------------------------------------------------------- #
class TestConsistency:
    def test_all_numeric_matches_legacy_math(self):
        """When every factor is a valid number, output MUST equal the
        legacy formula (regression-safety guarantee)."""
        factors = {"a": 0.7, "b": 0.8, "c": 0.6}
        vals = [v / 100.0 if v > 1 else v for v in factors.values()]
        expected = max(0.0, min(1.0, 1.0 - statistics.pstdev(vals) * 3.3))
        assert _consistency({"factors": factors}) == pytest.approx(expected)

    def test_ints_and_floats_mixed(self):
        assert 0.0 <= _consistency({"factors": {"a": 1, "b": 0.5}}) <= 1.0

    def test_numeric_strings_no_crash_and_coerce(self):
        # Legacy code raised TypeError on `"0.5" > 1`.  New code coerces
        # the string and returns a valid number in [0, 1].
        out = _consistency({"factors": {"a": "0.5", "b": "0.7"}})
        assert 0.0 <= out <= 1.0

    def test_none_value_skipped(self):
        out = _consistency({"factors": {"a": 0.5, "b": None, "c": 0.6}})
        assert 0.0 <= out <= 1.0

    def test_empty_string_skipped(self):
        out = _consistency({"factors": {"a": 0.5, "b": "", "c": 0.6}})
        assert 0.0 <= out <= 1.0

    def test_narrative_string_no_crash(self):
        # The exact scenario from the production crash.
        pick = {
            "factors": {
                "hot_scorer": "Wikipedia top scorer table (season 2025-26)",
                "form": 0.7,
                "matchup": 0.8,
            }
        }
        out = _consistency(pick)
        assert 0.0 <= out <= 1.0

    def test_nan_and_inf_skipped(self):
        out = _consistency({
            "factors": {"a": 0.5, "b": float("nan"), "c": float("inf"), "d": 0.6}
        })
        assert 0.0 <= out <= 1.0

    def test_all_bad_falls_back_to_neutral(self):
        # If <2 usable values remain, function returns 0.5 (neutral).
        assert _consistency({"factors": {"a": "junk", "b": None}}) == 0.5
        assert _consistency({"factors": {"a": "only-one-num"}}) == 0.5

    def test_wrong_container_type(self):
        assert _consistency({"factors": ["not", "a", "dict"]}) == 0.5
        assert _consistency({"factors": "totally-broken"}) == 0.5

    def test_no_factors_field(self):
        assert _consistency({}) == 0.5


# --------------------------------------------------------------------------- #
#  rank_candidates – regression + fault-tolerance
# --------------------------------------------------------------------------- #
def _valid_pick(pid: str, edge: float = 3.0, conf: float = 0.7,
                sport: str = "MLB") -> dict:
    return {
        "pick_id": pid,
        "sport": sport,
        "edge_percent": edge,
        "win_probability": conf,
        "factors": {"form": 0.7, "matchup": 0.8, "trend": 0.75},
        "key_insights": ["insight-a", "insight-b"],
        "selection_v2": {"market": {"family": "moneyline"}},
    }


class TestRankCandidatesRegression:
    """The score/component/rank output for VALID picks must not change."""

    def _expected_components(self, pick: dict) -> dict:
        edge_n = _normalize_edge(pick["edge_percent"])
        conf = pick["win_probability"]
        conf = conf / 100.0 if conf > 1 else conf
        conf = max(0.0, min(1.0, conf))
        roi_n = 0.5  # _FakeMemory returns None
        data = _data_completeness(pick)
        cons = _consistency(pick)
        return {
            "edge": round(edge_n, 3),
            "confidence": round(conf, 3),
            "roi": round(roi_n, 3),
            "data": round(data, 3),
            "consistency": round(cons, 3),
        }, round(
            W["edge"] * edge_n + W["confidence"] * conf + W["roi"] * roi_n +
            W["data"] * data + W["consistency"] * cons, 4,
        )

    def test_single_valid_pick_unchanged(self):
        p = _valid_pick("p1", edge=3.0, conf=0.72)
        exp_components, exp_score = self._expected_components(p)
        out = rank_candidates([p], _FakeMemory())
        assert out["failed"] == 0
        assert p["brain"]["candidate_score"] == exp_score
        assert p["brain"]["candidate_components"] == exp_components
        assert p["brain"]["candidate_rank"] == 1
        assert p["brain"]["top_k"] is True
        assert "candidate_error" not in p["brain"]

    def test_batch_all_valid_ranks_deterministically(self):
        picks = [
            _valid_pick("low",  edge=-1.0, conf=0.55),
            _valid_pick("high", edge=4.5,  conf=0.85),
            _valid_pick("mid",  edge=2.0,  conf=0.70),
        ]
        # capture expected scores BEFORE calling rank_candidates
        expected = {p["pick_id"]: self._expected_components(p)[1] for p in picks}

        out = rank_candidates(picks, _FakeMemory())
        assert out["failed"] == 0
        for p in picks:
            assert p["brain"]["candidate_score"] == expected[p["pick_id"]]

        by_id = {p["pick_id"]: p for p in picks}
        assert by_id["high"]["brain"]["candidate_rank"] == 1
        assert by_id["mid"]["brain"]["candidate_rank"] == 2
        assert by_id["low"]["brain"]["candidate_rank"] == 3


class _ExplodingMemory:
    """Memory whose ``market()`` raises for a chosen sport, letting us
    exercise the per-pick isolation path with a realistic crash."""

    def __init__(self, boom_sport: str = "SOCCER"):
        self.boom_sport = boom_sport

    def market(self, sport, family):
        if sport == self.boom_sport:
            raise RuntimeError(f"memory corrupt for {sport}")
        return None


class TestRankCandidatesFaultTolerance:
    def test_production_crash_scenario_narrative_factor_no_longer_crashes(self):
        """The exact production crash: a soccer pick whose ``factors``
        contains a narrative string.  Batch must survive and this pick
        must score successfully (narrative simply skipped)."""
        picks = [
            _valid_pick("good-1", edge=3.5, conf=0.75),
            {
                "pick_id": "soccer-hot-scorer",
                "sport": "SOCCER",
                "edge_percent": 2.5,
                "win_probability": 0.68,
                "factors": {
                    "hot_scorer": "Wikipedia top scorer table (season 2025-26)",
                    "form": 0.7,
                    "matchup": 0.8,
                },
                "selection_v2": {"market": {"family": "goalscorer"}},
            },
            _valid_pick("good-2", edge=2.0, conf=0.60),
        ]
        out = rank_candidates(picks, _FakeMemory())
        assert out["ranked"] == 3
        assert out["failed"] == 0
        for p in picks:
            assert p["brain"]["candidate_score"] > 0
            assert "candidate_error" not in p["brain"]

    def test_single_malformed_pick_in_batch_is_isolated(self, caplog):
        """Force one pick to raise inside the ranking loop and prove
        the rest of the batch survives, is scored, and ranked."""
        good1 = _valid_pick("good-1", edge=3.5, conf=0.75, sport="MLB")
        good2 = _valid_pick("good-2", edge=2.0, conf=0.60, sport="NBA")
        # SOCCER pick crashes because _ExplodingMemory raises for SOCCER.
        bad = _valid_pick("bad-soccer", edge=2.5, conf=0.68, sport="SOCCER")

        picks = [good1, bad, good2]
        with caplog.at_level(logging.WARNING, logger="lockscore.brain.candidates"):
            out = rank_candidates(picks, _ExplodingMemory("SOCCER"))

        assert out["ranked"] == 3
        assert out["failed"] == 1

        by_id = {p["pick_id"]: p for p in picks}
        # Good picks scored normally.
        assert by_id["good-1"]["brain"]["candidate_score"] > 0
        assert by_id["good-2"]["brain"]["candidate_score"] > 0
        assert "candidate_error" not in by_id["good-1"]["brain"]
        assert "candidate_error" not in by_id["good-2"]["brain"]
        # Bad pick isolated + marked.
        assert by_id["bad-soccer"]["brain"]["candidate_score"] == 0.0
        assert "candidate_error" in by_id["bad-soccer"]["brain"]
        assert by_id["bad-soccer"]["brain"]["candidate_rank"] == 3  # sinks

        # WARNING logged with correct context – but NO full dict dump.
        warn_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("bad-soccer" in r.getMessage() for r in warn_records)
        assert any("sport=SOCCER" in r.getMessage() for r in warn_records)
        for r in warn_records:
            msg = r.getMessage()
            # No raw dict/JSON dump of the pick body.
            assert "'edge_percent':" not in msg
            assert "'factors':" not in msg
            assert "'selection_v2':" not in msg

    def test_multiple_malformed_picks_all_isolated(self, caplog):
        picks = [
            _valid_pick("good-1", edge=3.0, conf=0.70, sport="MLB"),
            _valid_pick("bad-1",  edge=2.0, conf=0.60, sport="SOCCER"),
            _valid_pick("good-2", edge=2.5, conf=0.65, sport="NBA"),
            _valid_pick("bad-2",  edge=1.5, conf=0.55, sport="SOCCER"),
            _valid_pick("bad-3",  edge=1.0, conf=0.50, sport="SOCCER"),
        ]
        with caplog.at_level(logging.WARNING, logger="lockscore.brain.candidates"):
            out = rank_candidates(picks, _ExplodingMemory("SOCCER"))

        assert out["ranked"] == 5
        assert out["failed"] == 3

        by_id = {p["pick_id"]: p for p in picks}
        assert by_id["good-1"]["brain"]["candidate_score"] > 0
        assert by_id["good-2"]["brain"]["candidate_score"] > 0
        for pid in ("bad-1", "bad-2", "bad-3"):
            assert by_id[pid]["brain"]["candidate_score"] == 0.0
            assert "candidate_error" in by_id[pid]["brain"]

        # Ranks 1 and 2 must be the two good picks.
        top_two = sorted(picks, key=lambda p: p["brain"]["candidate_rank"])[:2]
        assert {p["pick_id"] for p in top_two} == {"good-1", "good-2"}

    def test_corrupt_selection_v2_does_not_crash(self):
        """selection_v2 that isn't a dict should be tolerated (not crash)."""
        p = _valid_pick("stringy-sv2")
        p["selection_v2"] = "totally-not-a-dict"
        out = rank_candidates([p], _FakeMemory())
        assert out["failed"] == 0
        assert p["brain"]["candidate_score"] > 0

    def test_non_dict_factors_does_not_crash_data_completeness(self):
        """factors as an int/string must not crash _data_completeness."""
        p = _valid_pick("weird-factors")
        p["factors"] = 12345  # not a dict, not a container
        out = rank_candidates([p], _FakeMemory())
        assert out["failed"] == 0
        # _data_completeness returns 0 for factors (not a container),
        # _consistency returns 0.5 (neutral), pick still scores.
        assert p["brain"]["candidate_score"] >= 0

    def test_string_edge_percent_does_not_crash(self):
        # Odds parser sometimes leaves edge_percent as a string.
        p = _valid_pick("stringy", edge=0.0, conf=0.7)
        p["edge_percent"] = "2.5"
        out = rank_candidates([p], _FakeMemory())
        assert out["failed"] == 0
        assert p["brain"]["candidate_components"]["edge"] == round(
            _normalize_edge(2.5), 3
        )

    def test_invalid_edge_percent_defaults_to_zero(self):
        p = _valid_pick("bad-edge", conf=0.7)
        p["edge_percent"] = "not-a-number"
        out = rank_candidates([p], _FakeMemory())
        assert out["failed"] == 0  # falls back to 0.0 without crashing
        assert p["brain"]["candidate_components"]["edge"] == round(
            _normalize_edge(0.0), 3
        )

    def test_string_confidence_ok(self):
        p = _valid_pick("stringy-conf")
        p["win_probability"] = "0.68"
        out = rank_candidates([p], _FakeMemory())
        assert out["failed"] == 0
        assert p["brain"]["candidate_components"]["confidence"] == pytest.approx(
            0.68, abs=1e-3
        )

    def test_win_probability_on_0_100_scale_still_normalises(self):
        # Regression: existing behaviour normalises 0..100 → 0..1.
        p = _valid_pick("hundred-scale")
        p["win_probability"] = 72.0
        out = rank_candidates([p], _FakeMemory())
        assert out["failed"] == 0
        assert p["brain"]["candidate_components"]["confidence"] == pytest.approx(
            0.72, abs=1e-3
        )


# --------------------------------------------------------------------------- #
#  (No extra helpers below — _ExplodingMemory above covers the crash path.)
# --------------------------------------------------------------------------- #
