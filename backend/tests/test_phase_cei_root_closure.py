"""Phase C-E-H Root-Closure runtime & unit proofs.

Covers:
  §§11-12  O/U conservation contract
  §13      Directional evidence aggregation
  §14      Alt-line monotonicity
  §15      Provenance taxonomy invariants
  §§27-44  Universal Soccer history provider registry + coverage matrix
  §18-19   Bounded worker cardinality (starvation harness)
  §19      Canonical parity engineering proof for representative picks
"""
from __future__ import annotations

import asyncio
import sys
sys.path.insert(0, "/app/backend")

import pytest


# ─────────────────────────────────────────────────────────────────────
# §11-12 — O/U conservation
# ─────────────────────────────────────────────────────────────────────

class TestOUConservation:
    def test_non_pushable_conservation(self):
        from services.universal_market_truth import check_ou_conservation
        r = check_ou_conservation(0.55, 0.45)
        assert r.valid and abs(r.total - 1.0) < 0.001
        assert not r.is_pushable

    def test_pushable_conservation(self):
        from services.universal_market_truth import check_ou_conservation
        r = check_ou_conservation(0.48, 0.48, 0.04)
        assert r.valid and abs(r.total - 1.0) < 0.001
        assert r.is_pushable

    def test_conservation_violation_detected(self):
        from services.universal_market_truth import check_ou_conservation
        r = check_ou_conservation(0.7, 0.7)   # sums to 1.4
        assert not r.valid
        assert "conservation_violation" in r.error

    def test_percentage_scale_accepted(self):
        from services.universal_market_truth import check_ou_conservation
        r = check_ou_conservation(55.0, 45.0)
        assert r.valid, r

    def test_non_finite_rejected(self):
        from services.universal_market_truth import check_ou_conservation
        r = check_ou_conservation(float("nan"), 0.5)
        assert not r.valid
        assert "non_finite" in r.error


# ─────────────────────────────────────────────────────────────────────
# §13 — Directional evidence
# ─────────────────────────────────────────────────────────────────────

class TestDirectionalEvidence:
    def test_over_positive_does_not_boost_under(self):
        from services.universal_market_truth import (
            DirectionalFactor, aggregate_directional_evidence,
            DIRECTION_OVER, DIRECTION_UNDER,
        )
        factors = [
            DirectionalFactor(name="xg_high",   direction=DIRECTION_OVER,
                              strength=0.8, quality=0.9),
            DirectionalFactor(name="xg_high_2", direction=DIRECTION_OVER,
                              strength=0.7, quality=0.9),
        ]
        agg = aggregate_directional_evidence(DIRECTION_UNDER, factors)
        assert agg.aligned_strength == 0.0
        assert agg.contradictory_strength > 0.0
        assert agg.aligned_count == 0
        assert agg.contradictory_count == 2
        assert "xg_high" in agg.contradictions
        assert "xg_high_2" in agg.contradictions

    def test_under_positive_does_not_boost_over(self):
        from services.universal_market_truth import (
            DirectionalFactor, aggregate_directional_evidence,
            DIRECTION_OVER, DIRECTION_UNDER,
        )
        factors = [
            DirectionalFactor(name="slow_pace", direction=DIRECTION_UNDER,
                              strength=0.9, quality=0.8),
        ]
        agg = aggregate_directional_evidence(DIRECTION_OVER, factors)
        assert agg.aligned_strength == 0.0
        assert agg.contradictory_strength > 0.0

    def test_aligned_evidence_counts_correctly(self):
        from services.universal_market_truth import (
            DirectionalFactor, aggregate_directional_evidence,
            DIRECTION_OVER,
        )
        factors = [
            DirectionalFactor("a", DIRECTION_OVER, 0.9, 1.0),
            DirectionalFactor("b", DIRECTION_OVER, 0.6, 1.0),
        ]
        agg = aggregate_directional_evidence(DIRECTION_OVER, factors)
        assert agg.aligned_count == 2
        assert agg.contradictory_count == 0
        assert abs(agg.aligned_strength - 1.5) < 0.001

    def test_evidence_count_alone_not_agreement(self):
        """5 factors pointing AGAINST the selection are not 5 positive
        signals — the aggregation must reflect that."""
        from services.universal_market_truth import (
            DirectionalFactor, aggregate_directional_evidence,
            DIRECTION_OVER, DIRECTION_UNDER,
        )
        five_under = [
            DirectionalFactor(f"under_{i}", DIRECTION_UNDER, 0.8, 0.9)
            for i in range(5)
        ]
        agg = aggregate_directional_evidence(DIRECTION_OVER, five_under)
        # 5 under-positive factors against Over selection → 0 aligned.
        assert agg.aligned_count == 0
        assert agg.contradictory_count == 5


# ─────────────────────────────────────────────────────────────────────
# §14 — Alt-line monotonicity
# ─────────────────────────────────────────────────────────────────────

class TestAltLineMonotonicity:
    def test_monotonic_ladder_passes(self):
        from services.universal_market_truth import (
            AltLineRung, assert_alt_monotonicity,
        )
        ladder = [
            AltLineRung(line=1.5, p_over=0.85, p_under=0.15, distribution_id="d1",
                        distribution_version="v1"),
            AltLineRung(line=2.5, p_over=0.60, p_under=0.40, distribution_id="d1",
                        distribution_version="v1"),
            AltLineRung(line=3.5, p_over=0.30, p_under=0.70, distribution_id="d1",
                        distribution_version="v1"),
        ]
        assert assert_alt_monotonicity(ladder) == []

    def test_non_monotonic_over_detected(self):
        from services.universal_market_truth import (
            AltLineRung, assert_alt_monotonicity,
        )
        ladder = [
            AltLineRung(line=1.5, p_over=0.60, distribution_id="d1",
                        distribution_version="v1"),
            AltLineRung(line=2.5, p_over=0.80,   # WRONG — higher line, higher P(Over)
                        distribution_id="d1", distribution_version="v1"),
        ]
        v = assert_alt_monotonicity(ladder)
        assert len(v) == 1
        assert "p_over_increasing_with_higher_line" == v[0].detail

    def test_mixed_distribution_ids_rejected(self):
        from services.universal_market_truth import (
            AltLineRung, assert_alt_monotonicity,
        )
        ladder = [
            AltLineRung(line=1.5, p_over=0.80, distribution_id="d1",
                        distribution_version="v1"),
            AltLineRung(line=2.5, p_over=0.70, distribution_id="d2",
                        distribution_version="v1"),
        ]
        v = assert_alt_monotonicity(ladder)
        assert len(v) == 1 and "mixed_distribution_ids" in v[0].detail


# ─────────────────────────────────────────────────────────────────────
# §15 — Provenance taxonomy
# ─────────────────────────────────────────────────────────────────────

class TestProvenance:
    def test_stamp_provenance_idempotent(self):
        from services.universal_market_truth import stamp_provenance
        pick = {"probability_provenance": "CAUSAL_INDEPENDENT"}
        stamp_provenance(pick, probability_provenance="BOOK_IMPLIED_SEED")
        # existing value preserved
        assert pick["probability_provenance"] == "CAUSAL_INDEPENDENT"

    def test_stamp_fills_when_absent(self):
        from services.universal_market_truth import stamp_provenance
        pick = {}
        stamp_provenance(pick, probability_provenance="MODEL_CONDITIONED",
                          distribution_id="d1", distribution_version="v1",
                          evidence_sources=["mlb_shared_run_dist"])
        assert pick["probability_provenance"] == "MODEL_CONDITIONED"
        assert pick["distribution_id"] == "d1"
        assert pick["evidence_sources"] == ["mlb_shared_run_dist"]

    def test_book_implied_seed_is_market_conditioned(self):
        from services.universal_market_truth import (
            is_market_conditioned, is_independent,
        )
        assert is_market_conditioned("BOOK_IMPLIED_SEED")
        assert is_market_conditioned("MARKET_CONDITIONED")
        assert is_market_conditioned("BOOK_ANCHORED")
        assert not is_independent("BOOK_IMPLIED_SEED")

    def test_causal_is_independent(self):
        from services.universal_market_truth import (
            is_market_conditioned, is_independent,
        )
        assert is_independent("CAUSAL_INDEPENDENT")
        assert is_independent("EMPIRICAL_INDEPENDENT")
        assert not is_market_conditioned("CAUSAL_INDEPENDENT")


# ─────────────────────────────────────────────────────────────────────
# §27-44 — Universal Soccer History provider registry + coverage
# ─────────────────────────────────────────────────────────────────────

class _StubProvider:
    """Fake provider — used to exercise the universal dispatcher without
    live HTTP or DB.  Reports supported vs unsupported competitions
    truthfully."""
    def __init__(self, name, supported, rows=None, raise_on=None):
        from services.soccer_universal_history import HistoryQueryResult
        self.name = name
        self.supported_competitions = frozenset(supported)
        self._rows = rows or []
        self._raise_on = raise_on
        self._HistoryQueryResult = HistoryQueryResult

    async def team_h2h(self, **kwargs):
        if self._raise_on == "team_h2h":
            raise RuntimeError("provider outage")
        from services.soccer_universal_history import (
            STATUS_FULL, STATUS_NO_MATCHES,
        )
        rows = list(self._rows)
        return self._HistoryQueryResult(
            status=STATUS_FULL if rows else STATUS_NO_MATCHES,
            rows=rows, sample_size=len(rows), provenance=self.name,
        )

    async def team_history(self, **kwargs):
        return await self.team_h2h(**kwargs)

    async def player_history(self, **kwargs):
        return await self.team_h2h(**kwargs)

    async def player_vs_opp(self, **kwargs):
        return await self.team_h2h(**kwargs)


class TestUniversalSoccerHistory:
    def setup_method(self):
        from services import soccer_universal_history as ush
        # Snapshot + clear the module registry so tests don't collide.
        self._saved = dict(ush._PROVIDERS)
        ush._PROVIDERS.clear()

    def teardown_method(self):
        from services import soccer_universal_history as ush
        ush._PROVIDERS.clear()
        ush._PROVIDERS.update(self._saved)

    def test_multi_league_registry(self):
        from services.soccer_universal_history import register_provider
        register_provider(_StubProvider("provider_a", ["EPL", "USA_MLS"]))
        register_provider(_StubProvider("provider_b", ["EPL", "LA_LIGA"]))
        from services.soccer_universal_history import (
            get_providers_for_competition, registered_providers,
        )
        # EPL served by both providers, deterministic sort by name.
        epl = get_providers_for_competition("EPL")
        assert [p.name for p in epl] == ["provider_a", "provider_b"]
        # LA_LIGA served only by provider_b.
        laliga = get_providers_for_competition("LA_LIGA")
        assert [p.name for p in laliga] == ["provider_b"]
        assert set(registered_providers()) == {"provider_a", "provider_b"}

    def test_team_h2h_merges_providers(self):
        from services.soccer_universal_history import (
            register_provider, get_team_h2h, STATUS_FULL,
        )
        register_provider(_StubProvider(
            "provider_a", ["EPL"],
            rows=[{"provider_event_id": "evt-1", "date": "2024-01-15"}],
        ))
        register_provider(_StubProvider(
            "provider_b", ["EPL"],
            rows=[
                {"provider_event_id": "evt-1", "date": "2024-01-15"},  # dup
                {"provider_event_id": "evt-2", "date": "2024-05-20"},
            ],
        ))
        res = asyncio.run(get_team_h2h(
            canonical_home_team_id="home", canonical_away_team_id="away",
            canonical_competition_id="EPL",
        ))
        # Merged and de-duped by provider_event_id.
        assert res.status == STATUS_FULL
        assert res.sample_size == 2

    def test_provider_failure_not_zero_matchups(self):
        from services.soccer_universal_history import (
            register_provider, get_team_h2h, STATUS_PROVIDER_FAILURE,
        )
        register_provider(_StubProvider(
            "provider_a", ["EPL"], raise_on="team_h2h",
        ))
        res = asyncio.run(get_team_h2h(
            canonical_home_team_id="home", canonical_away_team_id="away",
            canonical_competition_id="EPL",
        ))
        assert res.status == STATUS_PROVIDER_FAILURE
        assert res.sample_size == 0
        assert res.error is not None

    def test_no_provider_registered_returns_unavailable(self):
        from services.soccer_universal_history import (
            get_team_h2h, STATUS_UNAVAILABLE,
        )
        res = asyncio.run(get_team_h2h(
            canonical_home_team_id="home", canonical_away_team_id="away",
            canonical_competition_id="UNKNOWN_COMP",
        ))
        assert res.status == STATUS_UNAVAILABLE

    def test_coverage_matrix_enumerates_all(self):
        from services.soccer_universal_history import (
            register_provider, build_coverage_matrix, STATUS_UNAVAILABLE,
        )
        register_provider(_StubProvider("provider_a", ["EPL", "USA_MLS"]))
        matrix = asyncio.run(build_coverage_matrix(
            ["EPL", "USA_MLS", "SAUDI_PRO", "ARG_PRIMERA"],
        ))
        by_comp = {m["competition"]: m for m in matrix}
        assert set(by_comp.keys()) == {"EPL", "USA_MLS", "SAUDI_PRO", "ARG_PRIMERA"}
        # SAUDI_PRO / ARG_PRIMERA have no registered provider → UNAVAILABLE.
        assert by_comp["SAUDI_PRO"]["team_h2h"] == STATUS_UNAVAILABLE
        assert by_comp["ARG_PRIMERA"]["team_history"] == STATUS_UNAVAILABLE

    def test_cross_competition_h2h_when_no_comp_specified(self):
        """§32 — same clubs meeting in multiple competitions merge into
        ONE H2H set with provenance preserved."""
        from services.soccer_universal_history import (
            register_provider, get_team_h2h, STATUS_FULL,
        )
        register_provider(_StubProvider("provider_a", ["EPL"], rows=[
            {"provider_event_id": "epl-1", "date": "2024-03-01",
             "canonical_competition_id": "EPL"},
        ]))
        register_provider(_StubProvider("provider_b", ["UCL"], rows=[
            {"provider_event_id": "ucl-1", "date": "2024-04-15",
             "canonical_competition_id": "UCL"},
        ]))
        # Query with no comp_id → cross-competition.
        res = asyncio.run(get_team_h2h(
            canonical_home_team_id="home", canonical_away_team_id="away",
            canonical_competition_id=None,
        ))
        assert res.status == STATUS_FULL
        assert res.sample_size == 2
        comps = {r.get("canonical_competition_id") for r in res.rows}
        assert comps == {"EPL", "UCL"}


# ─────────────────────────────────────────────────────────────────────
# §§9 / §59 — Bounded worker cardinality (starvation harness)
# ─────────────────────────────────────────────────────────────────────

class TestBoundedCardinality:
    def test_peak_live_tasks_bounded_at_1000_inputs(self):
        """Prove peak scheduled Tasks scales with WORKER LIMIT, not
        input cardinality — 1000 inputs must not create > limit+few
        concurrent inflight coroutines."""
        from services.soccer_universal_history import bounded_history_scan

        peak = 0
        current = 0

        async def _slow_factory():
            nonlocal peak, current
            current += 1
            peak = max(peak, current)
            await asyncio.sleep(0.001)
            current -= 1
            return {"ok": True}

        factories = [_slow_factory for _ in range(1000)]

        async def _run():
            await bounded_history_scan(factories, limit=16)

        asyncio.run(_run())
        # peak must be <= worker limit + tiny slack (scheduling races).
        assert peak <= 20, f"peak concurrent = {peak} (limit=16)"

    def test_ordering_preserved(self):
        """Order-preservation contract (§9) — outputs must line up with
        input positions so zip(input,output) works."""
        from services.soccer_universal_history import bounded_history_scan

        async def _make(i):
            async def _inner():
                await asyncio.sleep((10 - (i % 10)) * 0.001)
                return i
            return _inner

        async def _run():
            factories = [await _make(i) for i in range(50)]
            results = await bounded_history_scan(factories, limit=8)
            return results

        results = asyncio.run(_run())
        assert results == list(range(50)), "ordering violated"


# ─────────────────────────────────────────────────────────────────────
# §19 — Canonical parity engineering proof (DTO → wire truth)
# ─────────────────────────────────────────────────────────────────────

class TestCanonicalParity:
    def _canonical_pick(self, **overrides):
        base = {
            "id":                    "pick-1",
            "canonical_pick_id":     "pick-1",
            "sport":                 "NFL",
            "market":                "spread",
            "selection":             "Home -3.5",
            "line":                  -3.5,
            "book_odds":             -110,
            "win_probability":       58.7,
            "edge_percent":          4.2,
            "lock_score":            88.0,
            "published_lock_score":  88.0,
            "implied_probability":   52.38,
            "probability_provenance": "CAUSAL_INDEPENDENT",
            "distribution_id":       "nfl.spread.v4",
            "distribution_version":  "v4.2026-06",
            "model_source":          "nfl_independent_simulator",
        }
        base.update(overrides)
        return base

    def test_dto_preserves_canonical_truth(self):
        from services.board_pick_dto import project_board_dto
        pick = self._canonical_pick()
        dto = project_board_dto(pick)
        # Canonical fields must be preserved verbatim.
        for k in ("id", "canonical_pick_id", "sport", "market",
                   "selection", "line", "book_odds", "lock_score",
                   "published_lock_score", "win_probability",
                   "edge_percent", "implied_probability",
                   "probability_provenance", "distribution_id",
                   "distribution_version", "model_source"):
            assert dto.get(k) == pick[k], f"{k}: {dto.get(k)} vs {pick[k]}"

    def test_dto_derives_implied_when_absent(self):
        from services.board_pick_dto import project_board_dto
        pick = self._canonical_pick(implied_probability=None)
        dto = project_board_dto(pick)
        # -110 → 52.38 %
        assert dto.get("implied_probability") is not None
        assert abs(dto["implied_probability"] - 52.38) < 0.05

    def test_dto_never_emits_undefined_implied(self):
        """Even with a wildly bad input, DTO returns a number or
        None — never a string that would surface as 'undefined%'."""
        from services.board_pick_dto import project_board_dto
        for bad in (None, "", "N/A"):
            pick = self._canonical_pick(implied_probability=bad, book_odds=None)
            dto = project_board_dto(pick)
            # implied_probability must be None OR a finite number.
            ip = dto.get("implied_probability")
            assert ip is None or isinstance(ip, (int, float))
