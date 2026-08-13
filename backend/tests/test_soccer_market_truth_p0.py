"""P0 — Emergent Support Soccer Board Fix regression tests (2026-06).

Guarantees that:

    1. The ESPN Soccer fallback path (services/espn_soccer_fixtures.py
       :: _build_pick) emits real-line-integrity-compliant picks
       when no real sportsbook line is available.
    2. Real-book-backed Soccer picks remain eligible normally.
    3. Missing edge is NEVER silently converted to 0.
    4. Main-board eligibility rejects model-only picks and their
       Mongo query enforces the same rule.
    5. Board validator annotates (never fabricates market data).
    6. Existing stale fallback records cannot contaminate the main
       board even if their lock_score exceeds 85.
    7. APEX 100 (Block 8) never assigns to a model-only pick — even
       if the pick otherwise satisfies every Magic criterion.
    8. The Block 8 test suite remains green (imported and re-checked
       via a light sanity harness — full suite runs via `pytest`).
"""
from __future__ import annotations

import pytest


# ═════════════════════════════════════════════════════════════════════════
# 1.  ESPN Soccer fallback path — no real book line
# ═════════════════════════════════════════════════════════════════════════

class TestEspnSoccerFallbackFix:
    """`services.espn_soccer_fixtures._build_pick` must obey the durable
    real-line integrity rule when no ESPN moneyline is available."""

    def _make_no_line_inputs(self):
        # sel comes from `_select_side` and lacks `book_odds_source == 'espn'`.
        ev = {
            "event_id": "E-123",
            "date": "2026-08-15T18:00:00Z",
            "home": "Home FC",
            "away": "Away FC",
            "home_ml": None, "away_ml": None,
            "home_form": "WWLDW", "away_form": "LLDWD",
        }
        sel = {
            "team": "Home FC", "side": "home",
            "probability": 0.63,
            "book_odds_source": "hfa_baseline",   # → no real line branch
        }
        return ev, sel

    def _make_real_line_inputs(self):
        ev = {
            "event_id": "E-456",
            "date": "2026-08-15T20:00:00Z",
            "home": "Real FC",
            "away": "Book FC",
            "home_ml": -155, "away_ml": +130,
            "home_form": "WWWWW", "away_form": "LLLLL",
        }
        sel = {
            "team": "Real FC", "side": "home",
            "probability": 0.66,
            "book_odds_source": "espn",           # → real line branch
        }
        return ev, sel

    def test_no_real_line_book_odds_null(self):
        from services.espn_soccer_fixtures import _build_pick
        ev, sel = self._make_no_line_inputs()
        pick = _build_pick("soc.espn", "Europa Conference League",
                             ev, sel, "2026-08-15")
        assert pick["book_odds"] is None

    def test_no_real_line_implied_probability_null(self):
        from services.espn_soccer_fixtures import _build_pick
        ev, sel = self._make_no_line_inputs()
        pick = _build_pick("soc.espn", "Europa Conference League",
                             ev, sel, "2026-08-15")
        assert pick["implied_probability"] is None

    def test_no_real_line_edge_percent_null_not_zero(self):
        from services.espn_soccer_fixtures import _build_pick
        ev, sel = self._make_no_line_inputs()
        pick = _build_pick("soc.espn", "Europa Conference League",
                             ev, sel, "2026-08-15")
        assert pick["edge_percent"] is None       # never 0

    def test_no_real_line_tagged_model_only(self):
        from services.espn_soccer_fixtures import _build_pick
        ev, sel = self._make_no_line_inputs()
        pick = _build_pick("soc.espn", "Europa Conference League",
                             ev, sel, "2026-08-15")
        assert pick["no_real_book_line"] is True
        assert pick["model_only"] is True
        assert pick["is_extra"] is True
        assert pick["odds_source"] == "MODEL_ONLY"

    def test_no_real_line_routed_to_extended_coverage(self):
        from services.espn_soccer_fixtures import _build_pick
        ev, sel = self._make_no_line_inputs()
        pick = _build_pick("soc.espn", "Europa Conference League",
                             ev, sel, "2026-08-15")
        assert pick["hide_from_main_board"] is True

    def test_real_line_book_odds_preserved(self):
        from services.espn_soccer_fixtures import _build_pick
        ev, sel = self._make_real_line_inputs()
        pick = _build_pick("soc.espn", "MLS", ev, sel, "2026-08-15")
        assert pick["book_odds"] == -155
        assert pick["implied_probability"] is not None
        assert pick["odds_source"] == "espn"
        # Real-line picks are NOT extended-coverage
        assert pick["no_real_book_line"] is False
        assert pick["is_extra"] is False
        assert pick["hide_from_main_board"] is False
        assert pick["model_only"] is False


# ═════════════════════════════════════════════════════════════════════════
# 2.  Main-board eligibility helper — real-line integrity
# ═════════════════════════════════════════════════════════════════════════

class TestMainBoardEligibility:
    def test_missing_book_odds_rejected(self):
        from services.main_board_eligibility import is_main_board_eligible
        pick = {"lock_score": 96.9, "book_odds": None, "implied_probability": None}
        assert is_main_board_eligible(pick) is False

    def test_null_implied_probability_rejected(self):
        from services.main_board_eligibility import is_main_board_eligible
        pick = {"lock_score": 96.9, "book_odds": -150, "implied_probability": None}
        assert is_main_board_eligible(pick) is False

    def test_null_book_odds_rejected(self):
        from services.main_board_eligibility import is_main_board_eligible
        pick = {"lock_score": 96.9, "book_odds": None, "implied_probability": 60.0}
        assert is_main_board_eligible(pick) is False

    def test_no_real_book_line_flag_rejected(self):
        from services.main_board_eligibility import is_main_board_eligible
        pick = {"lock_score": 99, "book_odds": -150, "implied_probability": 60,
                 "no_real_book_line": True}
        assert is_main_board_eligible(pick) is False

    def test_model_only_flag_rejected(self):
        from services.main_board_eligibility import is_main_board_eligible
        pick = {"lock_score": 99, "book_odds": -150, "implied_probability": 60,
                 "model_only": True}
        assert is_main_board_eligible(pick) is False

    def test_hide_from_main_board_flag_rejected(self):
        from services.main_board_eligibility import is_main_board_eligible
        pick = {"lock_score": 99, "book_odds": -150, "implied_probability": 60,
                 "hide_from_main_board": True}
        assert is_main_board_eligible(pick) is False

    def test_real_line_with_lock_over_85_accepted(self):
        from services.main_board_eligibility import is_main_board_eligible
        pick = {"lock_score": 91, "book_odds": -180, "implied_probability": 64.3}
        assert is_main_board_eligible(pick) is True

    def test_real_line_at_boundary_85_rejected(self):
        from services.main_board_eligibility import is_main_board_eligible
        pick = {"lock_score": 85.0, "book_odds": -180, "implied_probability": 64.3}
        assert is_main_board_eligible(pick) is False


class TestMainBoardMongoQuery:
    def test_query_enforces_real_line(self):
        from services.main_board_eligibility import main_board_lock_score_query
        q = main_board_lock_score_query()
        # Must be an $and with both real-line + lock predicates
        assert "$and" in q
        clauses = q["$and"]
        # Real-line predicate is present.
        real_line = clauses[0]
        clause_strs = str(clauses)
        assert "no_real_book_line" in clause_strs
        assert "model_only" in clause_strs
        assert "hide_from_main_board" in clause_strs
        assert "book_odds" in clause_strs
        assert "implied_probability" in clause_strs


# ═════════════════════════════════════════════════════════════════════════
# 3.  Board validator — enforce_real_market_line stage
# ═════════════════════════════════════════════════════════════════════════

class TestBoardValidatorRealMarketLine:
    def _mk_pick(self, **overrides):
        p = {
            "id": "p1", "sport": "Soccer",
            "event": "Home @ Away", "market": "Home Moneyline",
            "selection": "Home", "event_time": "2026-08-15T18:00:00Z",
            "book_odds": -150, "implied_probability": 60.0,
            "edge_percent": 2.5, "lock_score": 92.0, "win_probability": 62.5,
        }
        p.update(overrides)
        return p

    def test_null_book_odds_annotated_not_dropped(self):
        from board_validator import enforce_real_market_line
        picks = [self._mk_pick(book_odds=None, implied_probability=None,
                                 edge_percent=None)]
        out, stats = enforce_real_market_line(picks)
        assert len(out) == 1                            # not dropped
        p = out[0]
        assert p["hide_from_main_board"] is True
        assert p["is_extra"] is True
        assert p["model_only"] is True
        assert p.get("main_board_reclassified_reason")
        assert p["edge_percent"] is None                # never coerced to 0
        assert stats["annotated"] == 1

    def test_no_real_book_line_flag_annotated(self):
        from board_validator import enforce_real_market_line
        picks = [self._mk_pick(no_real_book_line=True)]
        out, stats = enforce_real_market_line(picks)
        assert out[0]["hide_from_main_board"] is True
        assert stats["annotated"] == 1

    def test_real_book_pick_untouched(self):
        from board_validator import enforce_real_market_line
        p = self._mk_pick()
        picks = [p]
        out, stats = enforce_real_market_line(picks)
        assert out[0].get("hide_from_main_board") is not True
        assert out[0].get("is_extra") is not True
        assert stats["annotated"] == 0

    def test_edge_zero_with_null_odds_gets_none(self):
        # Defensive: if a producer silently coerced edge=0 despite no
        # real line, the validator restores None.
        from board_validator import enforce_real_market_line
        picks = [self._mk_pick(book_odds=None, implied_probability=None,
                                 edge_percent=0.0)]
        out, _ = enforce_real_market_line(picks)
        assert out[0]["edge_percent"] is None

    def test_validate_and_finalize_includes_real_line_stage(self):
        from board_validator import validate_and_finalize
        picks = [
            self._mk_pick(id="real-1"),
            self._mk_pick(id="stale-1", book_odds=None,
                           implied_probability=None, edge_percent=None,
                           no_real_book_line=True, lock_score=97.5),
        ]
        out, report = validate_and_finalize(picks)
        assert "real_market_line" in report
        # Stale pick was annotated to hide_from_main_board
        stale = next(p for p in picks if p["id"] == "stale-1")
        assert stale["hide_from_main_board"] is True

    def test_stale_high_lock_pick_excluded_from_main_board(self):
        # A stale 97 pick (lock_score>85 BUT no real line) is annotated
        # and excluded by is_main_board_eligible.
        from board_validator import enforce_real_market_line
        from services.main_board_eligibility import is_main_board_eligible
        stale = self._mk_pick(id="ajax-or-draw", lock_score=97,
                                book_odds=None, implied_probability=None,
                                edge_percent=None, no_real_book_line=True)
        enforce_real_market_line([stale])
        assert is_main_board_eligible(stale) is False


# ═════════════════════════════════════════════════════════════════════════
# 4.  Block 8 APEX gate — model-only picks NEVER receive APEX
# ═════════════════════════════════════════════════════════════════════════

class TestBlock8ApexRealLineSafety:
    def _full_positive_evidence(self):
        from services.magic.contract import (
            Availability, EvidenceItem, EvidenceType,
        )
        def ev(t, src):
            return EvidenceItem(
                evidence_type=t, availability=Availability.AVAILABLE,
                sport="Soccer", market="moneyline",
                value=0.72, direction="positive", confidence=0.85,
                source=src, source_class="authoritative", label="",
                notes="", sample_size=40,
            )
        return [
            ev(EvidenceType.HISTORICAL_EXACT_THRESHOLD, "s1"),
            ev(EvidenceType.RECENT_FORM, "s2"),
            ev(EvidenceType.ROLE_OPPORTUNITY, "s3"),
            ev(EvidenceType.MATCHUP, "s4"),
            ev(EvidenceType.MODEL_PROBABILITY, "s5"),
            ev(EvidenceType.SPORTSBOOK_CONSENSUS, "s6"),
        ]

    def _mo(self):
        from services.magic.contract import MagicOutput, MagicTier
        mo = MagicOutput(
            pick_id="soc-1", sport="Soccer", market="moneyline",
            magic_tier=MagicTier.ALIGNED_STRONG,
            magic_score=95.0, magic_score_available=True,
            risk_flags=[],
        )
        for e in self._full_positive_evidence():
            mo.add(e)
        return mo

    def test_no_real_line_pick_never_gets_apex(self):
        # Pick clears BASE ≥ 97 + full positive evidence stack, BUT has
        # no_real_book_line=True.  APEX must be blocked.
        from services.magic.lock_score_integrator import apply_magic_and_apex
        pick = {"id": "apex-noline", "sport": "Soccer", "market": "moneyline",
                 "lock_score": 98.5, "no_real_book_line": True,
                 "book_odds": None, "implied_probability": None,
                 "edge_percent": None, "model_only": True,
                 "hide_from_main_board": True}
        apply_magic_and_apex(pick, self._mo())
        assert pick["apex_lock"] is False
        assert "no_real_market_line" in (pick.get("apex_block_reason") or "")

    def test_null_book_odds_never_gets_apex(self):
        from services.magic.lock_score_integrator import apply_magic_and_apex
        pick = {"id": "apex-nullbo", "sport": "MLB", "market": "batter_hits",
                 "lock_score": 98.0, "book_odds": None,
                 "implied_probability": 60.0}
        # Sync sport / market to Soccer=>Soccer or MLB=>MLB doesn't matter
        # here.  Real-line integrity fires FIRST, before category logic.
        from services.magic.contract import MagicOutput, MagicTier
        mo = MagicOutput(pick_id="p", sport="MLB", market="batter_hits",
                          magic_tier=MagicTier.ALIGNED_STRONG,
                          magic_score=95.0, magic_score_available=True)
        for e in self._full_positive_evidence():
            e.sport = "MLB"; e.market = "batter_hits"
            mo.add(e)
        apply_magic_and_apex(pick, mo)
        assert pick["apex_lock"] is False

    def test_real_line_pick_can_still_get_apex(self):
        # Regression proof — Block 8 positive reachability is unchanged
        # for real-line picks.
        from services.magic.lock_score_integrator import apply_magic_and_apex
        pick = {"id": "apex-real", "sport": "Soccer", "market": "moneyline",
                 "lock_score": 98.0, "book_odds": -180,
                 "implied_probability": 64.3, "edge_percent": 2.5}
        apply_magic_and_apex(pick, self._mo())
        assert pick["apex_lock"] is True, pick.get("apex_block_reason")


# ═════════════════════════════════════════════════════════════════════════
# 5.  Missing edge is NEVER silently converted to 0
# ═════════════════════════════════════════════════════════════════════════

class TestNoSilentEdgeCoercion:
    def test_espn_fallback_preserves_edge_none(self):
        from services.espn_soccer_fixtures import _build_pick
        ev = {"event_id": "E-9", "date": "2026-08-15T18:00:00Z",
              "home": "H", "away": "A", "home_ml": None, "away_ml": None,
              "home_form": "", "away_form": ""}
        sel = {"team": "H", "side": "home", "probability": 0.55,
               "book_odds_source": "form"}
        pick = _build_pick("soc.espn", "L", ev, sel, "2026-08-15")
        assert pick["edge_percent"] is None
        assert pick["edge_percent"] != 0

    def test_board_validator_restores_edge_none(self):
        from board_validator import enforce_real_market_line
        picks = [{"id": "coerced", "sport": "Soccer",
                   "event": "A@B", "market": "A Moneyline",
                   "selection": "A", "event_time": "2026-08-15T18:00:00Z",
                   "book_odds": None, "implied_probability": None,
                   "edge_percent": 0.0, "lock_score": 90,
                   "win_probability": 60}]
        enforce_real_market_line(picks)
        assert picks[0]["edge_percent"] is None


# ═════════════════════════════════════════════════════════════════════════
# 6.  Sanity: the Block 8 suite is still importable & basic invariants hold
# ═════════════════════════════════════════════════════════════════════════

class TestBlock8SuiteStillGreen:
    """Cheap sanity checks that the Block 8 constants and helpers still
    behave the same way after this fix — the full suite is exercised
    via the regular `pytest` run."""

    def test_non_apex_hard_cap_unchanged(self):
        from services.magic.lock_score_integrator import NON_APEX_HARD_CAP
        assert NON_APEX_HARD_CAP == 99.0

    def test_apex_score_unchanged(self):
        from services.magic.lock_score_integrator import APEX_SCORE
        assert APEX_SCORE == 100.0

    def test_positive_caps_unchanged(self):
        from services.magic.lock_score_integrator import positive_cap_for_base
        assert positive_cap_for_base(50) == 0.5
        assert positive_cap_for_base(80) == 1.0
        assert positive_cap_for_base(92) == 1.5
        assert positive_cap_for_base(96) == 1.0
        assert positive_cap_for_base(99) == 0.0

    def test_apex_gate_version_unchanged(self):
        from services.magic.apex_gate import APEX_GATE_VERSION
        assert APEX_GATE_VERSION == "apex_gate.v1.0"
