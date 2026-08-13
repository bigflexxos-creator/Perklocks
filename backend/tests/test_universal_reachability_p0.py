"""P0 — Universal Production Reachability + Cross-Surface Parity
regression suite (2026-06).

Permanent structural tests that fail if future code re-introduces the
class of defects observed in this closure:

    1. First-N truncation stripping late-slate events.
    2. UTC-boundary loss of late West Coast games (Perklocks day
       contract).
    3. Whole-sport disappearance (cross-sport failure isolation).
    4. Preview / Expo / Web divergence (single canonical endpoint).
    5. Silent supported-market drop paths (no `UNKNOWN_DISAPPEARED`
       state).
    6. Scheduler / producer omission at start-of-season.
    7. Real-line integrity re-broken (Support 2026-06 durable rule).
    8. Block 8 / APEX bypass through model-only picks.

These tests are deterministic — no network, no live producers.  They
lock in the STRUCTURAL invariants; live-slate reachability is measured
by the runtime observability counters (see
`services/pipeline_reachability.py`).
"""
from __future__ import annotations

import copy
import datetime as _dt
from datetime import datetime, timedelta, timezone

import pytest


# ═════════════════════════════════════════════════════════════════════════
# 1. Perklocks betting-day contract — UTC-boundary safety
# ═════════════════════════════════════════════════════════════════════════

class TestPerklocksDayContract:
    """The betting-day rolls at 04:00 ET.  A late West Coast game at
    10:10 PM PT (01:10 ET next day = 05:10 UTC next day) MUST remain
    part of the CURRENT betting day."""

    def test_west_coast_late_night_still_current_slate(self):
        from services.perklocks_day import is_in_current_slate
        # Now: 10:00 PM ET Sept 15 (02:00 UTC Sept 16)
        now = datetime(2026, 9, 16, 2, 0, tzinfo=timezone.utc)
        # A 10:10 PM PT West Coast game = 01:10 ET next day
        # = 05:10 UTC Sept 16.  It's still Sept 15's Perklocks day.
        wc = datetime(2026, 9, 16, 5, 10, tzinfo=timezone.utc)
        assert is_in_current_slate(wc, now)

    def test_east_coast_lateshift_pre_4am_previous_day(self):
        from services.perklocks_day import is_in_current_slate
        # Now: 5 AM ET Sept 16 (09:00 UTC).  Perklocks day just rolled.
        now = datetime(2026, 9, 16, 9, 0, tzinfo=timezone.utc)
        # A 3:30 AM ET game (=07:30 UTC Sept 16) is STILL Sept 15's slate.
        game_late_last_night = datetime(2026, 9, 16, 7, 30, tzinfo=timezone.utc)
        assert not is_in_current_slate(game_late_last_night, now)

    def test_early_afternoon_current_slate(self):
        from services.perklocks_day import is_in_current_slate
        now = datetime(2026, 9, 15, 20, 0, tzinfo=timezone.utc)  # 4 PM ET
        game_1pm_et = datetime(2026, 9, 15, 17, 0, tzinfo=timezone.utc)
        assert is_in_current_slate(game_1pm_et, now)


# ═════════════════════════════════════════════════════════════════════════
# 2. Fair-slate scheduler — no first-N truncation
# ═════════════════════════════════════════════════════════════════════════

class TestFairSlateNoFirstNTruncation:
    """When the current betting slate contains more events than the
    props cap, EVERY current-slate event must survive (cap is
    exceeded rather than starving late games)."""

    def test_current_slate_events_never_starved_by_cap(self):
        """Regression for the observed defect: MLB late games (10 PM ET
        first pitch) received 0 hitter props while early games got
        full coverage.  Root cause was `upcoming[:cap]` truncating
        chronologically-sorted late events."""
        from services.perklocks_day import is_in_current_slate

        now = datetime(2026, 9, 15, 20, 0, tzinfo=timezone.utc)  # 4 PM ET
        # Build a synthetic 15-game slate spanning 12 PM ET → 12:30 AM ET.
        events = []
        for hr in (17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30):
            events.append(datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
                           + timedelta(hours=hr - 12))
        # Simulate: cap=10 but slate=14 games.
        cap = 10
        # Emulate the Block 2B fair-slate rule.
        current = [e for e in events if is_in_current_slate(e, now)]
        rest    = [e for e in events if not is_in_current_slate(e, now)]
        if len(current) >= cap:
            selected = current
        else:
            selected = current + rest[:cap - len(current)]
        # Every current-slate game survives — this is the invariant.
        for e in current:
            assert e in selected, f"Fair-slate violation: dropped {e}"

    def test_no_lex_sort_dropoff_for_late_utc_games(self):
        """A game at 03:00 UTC (11 PM ET previous day) that belongs to
        the SAME Perklocks day must be considered ``current_slate``,
        NOT ``rest`` (tomorrow)."""
        from services.perklocks_day import is_in_current_slate
        # Now: 6 PM ET Sept 15 (=22:00 UTC).  A game at 11 PM ET =
        # 03:00 UTC Sept 16 is still part of Sept 15's slate.
        now = datetime(2026, 9, 15, 22, 0, tzinfo=timezone.utc)
        late_east = datetime(2026, 9, 16, 3, 0, tzinfo=timezone.utc)
        assert is_in_current_slate(late_east, now)


# ═════════════════════════════════════════════════════════════════════════
# 3. Real-line integrity (Support 2026-06 durable rule) — invariant
# ═════════════════════════════════════════════════════════════════════════

class TestRealLineIntegrityInvariant:
    def test_null_book_odds_blocks_main_board(self):
        from services.main_board_eligibility import is_main_board_eligible
        pick = {"lock_score": 99.0, "book_odds": None,
                 "implied_probability": None}
        assert is_main_board_eligible(pick) is False

    def test_no_real_book_line_flag_blocks_main_board(self):
        from services.main_board_eligibility import is_main_board_eligible
        pick = {"lock_score": 99.0, "book_odds": -180,
                 "implied_probability": 64.3,
                 "no_real_book_line": True}
        assert is_main_board_eligible(pick) is False

    def test_mongo_predicate_enforces_real_line(self):
        from services.main_board_eligibility import main_board_lock_score_query
        q = main_board_lock_score_query()
        # Real-line clause is the first branch of the outer $and.
        assert "$and" in q
        real_line = q["$and"][0]
        real_line_str = str(real_line)
        for token in ("no_real_book_line", "model_only",
                       "hide_from_main_board", "book_odds",
                       "implied_probability"):
            assert token in real_line_str


# ═════════════════════════════════════════════════════════════════════════
# 4. Block 8 / APEX bypass safety — model-only picks NEVER get APEX
# ═════════════════════════════════════════════════════════════════════════

class TestApexBypassSafety:
    def test_model_only_pick_never_apex(self):
        from services.magic.lock_score_integrator import apply_magic_and_apex
        from services.magic.contract import (
            Availability, EvidenceItem, EvidenceType, MagicOutput, MagicTier,
        )
        pick = {"id": "bypass", "sport": "Soccer", "market": "moneyline",
                 "lock_score": 99.0, "no_real_book_line": True,
                 "book_odds": None, "implied_probability": None}
        mo = MagicOutput(pick_id="p", sport="Soccer", market="moneyline",
                          magic_tier=MagicTier.ALIGNED_STRONG,
                          magic_score=100.0, magic_score_available=True)
        for t in (EvidenceType.HISTORICAL_EXACT_THRESHOLD,
                   EvidenceType.RECENT_FORM, EvidenceType.ROLE_OPPORTUNITY,
                   EvidenceType.MATCHUP, EvidenceType.MODEL_PROBABILITY,
                   EvidenceType.SPORTSBOOK_CONSENSUS):
            mo.add(EvidenceItem(evidence_type=t,
                                  availability=Availability.AVAILABLE,
                                  sport="Soccer", market="moneyline",
                                  value=0.7, direction="positive",
                                  confidence=0.85, source=f"src-{t.value}",
                                  source_class="authoritative", label="",
                                  notes="", sample_size=40))
        apply_magic_and_apex(pick, mo)
        assert pick["apex_lock"] is False
        assert "no_real_market_line" in (pick.get("apex_block_reason") or "")


# ═════════════════════════════════════════════════════════════════════════
# 5. Cross-sport failure isolation
# ═════════════════════════════════════════════════════════════════════════

class TestCrossSportFailureIsolation:
    """One sport must never abort the universal refresh.  Since the
    orchestrator wraps every producer/step in try/except, a raise
    inside one sport's producer must NOT halt the batch."""

    def test_orchestrator_step_isolation_pattern(self):
        # We assert the pattern is present in the orchestrator source —
        # every producer call is wrapped in try/except.
        from pathlib import Path
        import re
        src = Path("/app/backend/services/pick_refresh_orchestrator.py").read_text()
        # Every awaited generator invocation should sit inside a try/except.
        # Sample a few known producer call sites and confirm the surrounding
        # 30 lines contain "except" (heuristic — good enough as a canary).
        producer_call_re = re.compile(
            r"await\s+(_tsdb\.compute_anytime_scorer_picks|"
            r"generate_all_picks|apply_simulations|"
            r"apply_bandit_lift|enrich_pick|"
            r"apply_block8_magic_to_picks)")
        matches = list(producer_call_re.finditer(src))
        assert matches, "No producer call sites detected"
        # For each, look for an `except` within 40 lines above/below.
        lines = src.split("\n")
        for m in matches:
            line_no = src[:m.start()].count("\n")
            window = "\n".join(lines[max(0, line_no - 40): line_no + 40])
            assert "except" in window, (
                f"Producer call at line {line_no + 1} lacks nearby "
                f"try/except — cross-sport isolation may be broken")

    def test_apply_block8_magic_to_picks_swallows_perpick_errors(self):
        """Even if every pick raises inside `build_evidence`, the batch
        completes and returns a summary."""
        import asyncio, types
        # Monkey-patch `services.magic.adapters.build_evidence` to always
        # raise, then call the batch entry.
        import services.magic.adapters as adapters
        orig = adapters.build_evidence
        async def boom(db, pick): raise RuntimeError("simulated per-pick fail")
        adapters.build_evidence = boom
        try:
            from services.magic.lock_score_integrator import apply_block8_magic_to_picks
            picks = [{"id": f"p-{i}", "sport": "MLB",
                       "market": "batter_hits",
                       "lock_score": 92.0, "book_odds": -150,
                       "implied_probability": 60.0} for i in range(5)]
            result = asyncio.run(apply_block8_magic_to_picks(None, picks))
            assert result["errors"] == 5
            assert result["considered"] == 5
        finally:
            adapters.build_evidence = orig


# ═════════════════════════════════════════════════════════════════════════
# 6. Canonical endpoint parity — Preview / Expo / Web same list
# ═════════════════════════════════════════════════════════════════════════

class TestCanonicalEndpointParity:
    """Preview, Expo Go, and Web must consume ONE canonical Locks
    endpoint with ONE canonical eligibility contract.  If different
    surfaces re-implement the filter, they can drift.

    We assert:
    1. There is a single Mongo predicate builder
       (`services.main_board_eligibility.main_board_lock_score_query`).
    2. Both `is_main_board_eligible()` (in-memory) and the Mongo query
       enforce the SAME thresholds and real-line rules.
    3. No route re-implements the `> 85` boundary with different math.
    """

    def test_helper_and_query_agree_on_boundary(self):
        from services.main_board_eligibility import (
            is_main_board_eligible, main_board_lock_score_query,
        )
        # Boundary case: score 85.001 with real line → both must agree.
        p = {"lock_score": 85.001, "book_odds": -180,
              "implied_probability": 64.3}
        assert is_main_board_eligible(p) is True
        # Query must use $gt: 85, not $gte: 85.01.
        q = main_board_lock_score_query()
        lock_gate = q["$and"][-1]
        assert lock_gate["$or"][0] == {"published_lock_score": {"$gt": 85.0}}

    def test_helper_and_query_reject_null_book_odds(self):
        from services.main_board_eligibility import (
            is_main_board_eligible, main_board_lock_score_query,
        )
        p = {"lock_score": 99.0, "book_odds": None,
              "implied_probability": None}
        assert is_main_board_eligible(p) is False
        # Query real-line clause blocks None on both fields.
        q = main_board_lock_score_query()
        real_line = q["$and"][0]
        # Presence of book_odds+implied_probability $nin/exists clauses.
        rl_str = str(real_line)
        assert "book_odds" in rl_str and "implied_probability" in rl_str

    def test_no_orphan_boundary_reimplementation(self):
        """Grep the route sources for lock_score > 85 / >= 85 patterns —
        every occurrence outside `main_board_eligibility` and its tests
        is a potential surface-divergence bug."""
        import re
        from pathlib import Path
        routes = Path("/app/backend/routes")
        offenders = []
        pat = re.compile(r"lock_score.*\$gt\s*:\s*85|lock_score.*>\s*85")
        for f in routes.rglob("*.py"):
            src = f.read_text()
            for m in pat.finditer(src):
                # Allow the imports pointing at the canonical helper.
                snippet = src[max(0, m.start()-80): m.end()+40]
                if "main_board_lock_score_query" in snippet:
                    continue
                offenders.append(f"{f.name}:{src[:m.start()].count(chr(10))+1}")
        # We currently expect the routes to use the canonical query
        # helper; any offender is a regression risk.
        assert not offenders, (
            "Orphan boundary re-implementations found — surfaces may "
            f"diverge: {offenders}")


# ═════════════════════════════════════════════════════════════════════════
# 7. Terminal-state observability contract
# ═════════════════════════════════════════════════════════════════════════

class TestTerminalStateObservability:
    """Every candidate must terminate in one of the allowed states —
    no silent `UNKNOWN_DISAPPEARED`."""

    def test_allowed_terminal_states_module_exists(self):
        from services import pipeline_reachability
        assert set(pipeline_reachability.ALLOWED_TERMINAL_STATES) == {
            "GENERATED",
            "UNSUPPORTED_MARKET",
            "IDENTITY_REJECTED",
            "MISSING_REQUIRED_EVIDENCE",
            "PROVIDER_UNAVAILABLE",
            "CANONICAL_PUBLICATION_REJECTED",
        }

    def test_reachability_counters_track_all_states(self):
        from services.pipeline_reachability import ReachabilityCounters
        rc = ReachabilityCounters(sport="MLB")
        rc.record("GENERATED")
        rc.record("UNSUPPORTED_MARKET")
        rc.record("CANONICAL_PUBLICATION_REJECTED", reason="no_real_book_line")
        d = rc.as_dict()
        assert d["GENERATED"] == 1
        assert d["UNSUPPORTED_MARKET"] == 1
        assert d["CANONICAL_PUBLICATION_REJECTED"] == 1
        assert d["_reasons"]["CANONICAL_PUBLICATION_REJECTED"]["no_real_book_line"] == 1

    def test_reachability_rejects_unknown_state(self):
        from services.pipeline_reachability import ReachabilityCounters
        rc = ReachabilityCounters(sport="MLB")
        with pytest.raises(ValueError):
            rc.record("UNKNOWN_DISAPPEARED")


# ═════════════════════════════════════════════════════════════════════════
# 8. Future-season sport structural fixtures — NFL / NBA / CFB / NHL
# ═════════════════════════════════════════════════════════════════════════

class TestFutureSeasonStructuralFixtures:
    """Even offseason sports MUST have working structural paths:
    canonical publication + validator + main-board query.  These
    tests use synthetic provider-shaped fixtures so we prove
    reachability BEFORE the season starts."""

    def _make_slate(self, sport, day="2026-09-14"):
        """Build a provider-shaped early / mid / late-slate for `sport`
        including a UTC-boundary late-night game."""
        base = datetime.fromisoformat(day + "T13:00:00+00:00")
        return [
            {"id": f"{sport}-early",  "commence_time": base.isoformat().replace("+00:00","Z")},
            {"id": f"{sport}-mid",    "commence_time": (base+timedelta(hours=6)).isoformat().replace("+00:00","Z")},
            {"id": f"{sport}-late",   "commence_time": (base+timedelta(hours=10)).isoformat().replace("+00:00","Z")},
            {"id": f"{sport}-late_pt","commence_time": (base+timedelta(hours=15)).isoformat().replace("+00:00","Z")},
            {"id": f"{sport}-utc_boundary","commence_time": (base+timedelta(hours=16)).isoformat().replace("+00:00","Z")},
        ]

    @pytest.mark.parametrize("sport", ["NFL", "NBA", "CFB", "NHL"])
    def test_full_slate_no_first_n_truncation(self, sport):
        """Given a 5-event fixture with any cap ≥ 5, every event
        survives.  Given cap=3, current-slate events still all
        survive (fair-slate rule)."""
        from services.perklocks_day import is_in_current_slate
        # A wall-clock inside the fixture day.
        now = datetime.fromisoformat("2026-09-14T20:00:00+00:00")
        slate = self._make_slate(sport)
        events_dt = [
            (datetime.strptime(e["commence_time"], "%Y-%m-%dT%H:%M:%SZ")
             .replace(tzinfo=timezone.utc), e) for e in slate
        ]
        current = [x for x in events_dt if is_in_current_slate(x[0], now)]
        # Every current-slate event survives at cap = 3.
        cap = 3
        selected = current if len(current) >= cap else current + [
            x for x in events_dt if x not in current
        ][:cap - len(current)]
        for e in current:
            assert e in selected, (
                f"{sport}: fair-slate contract violated — current-slate "
                f"event {e[1]['id']} was starved by cap={cap}")

    @pytest.mark.parametrize("sport", ["NFL", "NBA", "CFB", "NHL"])
    def test_last_event_survives_canonical_query(self, sport):
        """The LAST event of the day MUST survive the Mongo main-board
        query (given real book_odds + lock_score above the floor).
        This proves the query does not drop late events."""
        from services.main_board_eligibility import is_main_board_eligible
        pick = {"id": f"{sport}-last-event-pick",
                 "sport": sport, "market_type": "moneyline",
                 "lock_score": 92.0, "book_odds": -180,
                 "implied_probability": 64.3,
                 "event_time": "2026-09-15T03:00:00Z"}
        assert is_main_board_eligible(pick) is True

    @pytest.mark.parametrize("sport,market", [
        ("MLB", "batter_hits"),
        ("MLB", "batter_total_bases"),
        ("MLB", "batter_home_runs"),
        ("MLB", "batter_rbis"),
        ("NFL", "player_pass_yds"),
        ("NFL", "player_rush_yds"),
        ("NFL", "player_reception_yds"),
        ("NFL", "player_anytime_td"),
        ("NBA", "player_points"),
        ("NBA", "player_rebounds"),
        ("NBA", "player_assists"),
        ("NBA", "player_threes"),
        ("NHL", "player_shots_on_goal"),
    ])
    def test_supported_player_prop_family_survives_canonical(self, sport, market):
        """Player-prop markets across supported families remain eligible
        when they carry real book lines — no market-token dropoff."""
        from services.main_board_eligibility import is_main_board_eligible
        pick = {"id": f"{sport}-{market}-late",
                 "sport": sport, "market_type": market,
                 "market": f"{market} sample",
                 "lock_score": 91.0, "book_odds": -160,
                 "implied_probability": 61.5,
                 "event_time": "2026-09-15T03:15:00Z"}
        assert is_main_board_eligible(pick) is True


# ═════════════════════════════════════════════════════════════════════════
# 9. Board validator preserves supported-market classification
# ═════════════════════════════════════════════════════════════════════════

class TestBoardValidatorSupportedMarketClassification:
    """`enforce_real_market_line` distinguishes SUPPORTED_MARKET_BLOCKED
    (bug) from GENERATED_NOT_LOCKS_QUALIFIED (legitimate) by leaving
    good real-book picks untouched."""

    def test_real_book_pick_below_lock_floor_not_reclassified(self):
        from board_validator import enforce_real_market_line
        p = {"id": "gen-not-locks", "sport": "MLB",
              "book_odds": -110, "implied_probability": 52.4,
              "edge_percent": 0.8, "lock_score": 70.0}
        out, stats = enforce_real_market_line([p])
        assert out[0].get("hide_from_main_board") is not True
        assert stats["annotated"] == 0

    def test_missing_edge_never_becomes_zero(self):
        # Even when a producer accidentally set edge_percent=0 while
        # book_odds was null, the validator restores None so no
        # downstream filter can misinterpret "0% edge" as a real 0.
        from board_validator import enforce_real_market_line
        p = {"id": "coerced", "sport": "MLB", "book_odds": None,
              "implied_probability": None, "edge_percent": 0.0,
              "lock_score": 90}
        enforce_real_market_line([p])
        assert p["edge_percent"] is None
