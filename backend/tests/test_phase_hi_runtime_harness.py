"""Phase H+I runtime harness — score distribution + starvation cardinality
+ bounded-worker sanity.

These are RUNTIME harnesses that produce artefacts + summary reports.
They do NOT require a live DB / provider — the bounded-worker harness
uses in-process asyncio primitives to prove Task-cardinality scaling
against the worker limit; the score-distribution harness synthesizes
representative picks and exercises the authoritative scoring path.
"""
from __future__ import annotations

import asyncio
import time
from collections import Counter
from typing import Any

import pytest


# ─────────────────────────────────────────────────────────────────────
# STARVATION CARDINALITY HARNESS (§18, §59)
# ─────────────────────────────────────────────────────────────────────

class TestStarvationCardinality:
    """Prove peak-live-Task cardinality scales with WORKER LIMIT, not
    input cardinality.  Cases: 10 / 100 / 500 / 1000 / 5000 inputs."""

    @pytest.mark.parametrize("n_inputs", [10, 100, 500, 1000, 5000])
    def test_bounded_scan_scales_with_limit_not_input(self, n_inputs):
        from services.soccer_universal_history import bounded_history_scan

        peak = 0
        current = 0
        limit = 16

        async def _slow_factory():
            nonlocal peak, current
            current += 1
            peak = max(peak, current)
            await asyncio.sleep(0.0002)
            current -= 1
            return {"ok": True}

        factories = [_slow_factory for _ in range(n_inputs)]

        async def _run():
            t0 = time.time()
            outs = await bounded_history_scan(factories, limit=limit)
            return outs, time.time() - t0

        outs, elapsed = asyncio.run(_run())

        assert len(outs) == n_inputs, f"{len(outs)} outputs for {n_inputs} inputs"
        assert peak <= limit + 4, (
            f"peak concurrent = {peak} for {n_inputs} inputs (limit={limit})"
        )


# ─────────────────────────────────────────────────────────────────────
# CANONICAL PICK PARITY (§19) — DTO → wire truth
# ─────────────────────────────────────────────────────────────────────

class TestParityAcrossRepresentativeMarkets:
    """Every MODEL_AVAILABLE market must round-trip pick → DTO with
    canonical betting truth preserved.  Uses the frozen freeze-safe
    field list from the DTO doc-header."""

    _CANONICAL_KEEP = [
        "id", "canonical_pick_id", "sport", "market", "selection",
        "line", "book_odds", "lock_score", "published_lock_score",
        "win_probability", "edge_percent", "implied_probability",
        "probability_provenance", "distribution_id",
        "distribution_version", "model_source",
    ]

    @pytest.mark.parametrize("sport,market,selection,book_odds,line,wp,edge", [
        ("NFL",    "spread",             "Home -3.5",  -110,  -3.5, 58.7, 4.2),
        ("NFL",    "total",              "Over 47.5",  -105,  47.5, 55.0, 3.0),
        ("CFB",    "spread",             "Home -7",    -110,  -7.0, 62.0, 5.5),
        ("MLB",    "run_line",           "Home -1.5",  -140,  -1.5, 54.0, 2.0),
        ("MLB",    "total",              "Over 8.5",   -115,   8.5, 52.5, 1.2),
        ("Soccer", "totals",             "Over 2.5",   -110,   2.5, 55.0, 2.5),
        ("Soccer", "moneyline",          "Home",       +115,  None, 47.0, 3.5),
        ("Tennis", "total_games",        "Over 22.5",  -110,  22.5, 54.0, 2.0),
    ])
    def test_dto_parity(self, sport, market, selection, book_odds, line, wp, edge):
        from services.board_pick_dto import project_board_dto
        pick = {
            "id":                    f"{sport}-{market}-1",
            "canonical_pick_id":     f"{sport}-{market}-1",
            "sport":                  sport,
            "market":                 market,
            "selection":              selection,
            "book_odds":              book_odds,
            "line":                   line,
            "win_probability":        wp,
            "edge_percent":           edge,
            "lock_score":             88.0,
            "published_lock_score":   88.0,
            "probability_provenance": "CAUSAL_INDEPENDENT",
            "distribution_id":        f"{sport.lower()}.{market}.v1",
            "distribution_version":   "v1.2026-06",
            "model_source":           f"{sport.lower()}_independent_model",
        }
        dto = project_board_dto(pick)
        for k in self._CANONICAL_KEEP:
            pv = pick.get(k)
            if pv is None:
                # implied_probability is derived when None + book_odds present
                if k == "implied_probability" and book_odds is not None:
                    assert dto.get(k) is not None
                continue
            assert dto.get(k) == pv, f"{k}: DTO {dto.get(k)} vs pick {pv}"


# ─────────────────────────────────────────────────────────────────────
# SCORE DISTRIBUTION SANITY (§17) — evidence bonus does NOT create 99s
# ─────────────────────────────────────────────────────────────────────

class TestScoreDistributionSanity:
    """Prove the Phase B evidence bonus never manufactures 99 from
    thin factors, and that the market-only cap never surfaces 85+."""

    def _score(self, factors, *, sport, market, win_prob, edge_percent, book_odds):
        from sports_engine import compute_lock_score
        ls, _ = compute_lock_score(
            factors, win_prob=win_prob,
            pick={"book_odds": book_odds, "edge_percent": edge_percent,
                  "win_probability": win_prob, "sport": sport,
                  "market": market,
                  "data_quality": "sp_plus|returning_prod_both|portal_both",
                  "probability_provenance": "CAUSAL_INDEPENDENT"},
            edge_percent=edge_percent,
        )
        return ls

    def test_thin_evidence_does_not_reach_99(self):
        # Weak setup (wp=50 %, edge=0, no factors) must NEVER reach 99.
        ls = self._score({}, sport="CFB", market="spread",
                          win_prob=50.0, edge_percent=0.0, book_odds=-110)
        assert ls < 90.0, f"weak setup reached elite tier: {ls}"

    def test_market_only_evidence_stays_below_85(self):
        # Sportsbook-implied-only cannot reach 85 (§7).
        ls = self._score({"Sportsbook Implied Prob": 65.0},
                         sport="CFB", market="spread",
                         win_prob=65.0, edge_percent=0.0, book_odds=-186)
        assert ls < 85.0, f"market-only reached 85+: {ls}"

    def test_score_distribution_over_random_slate(self):
        """Sample a synthetic slate and verify the DISTRIBUTION shape
        looks sane (no runaway 99s from the bonus)."""
        import random
        rng = random.Random(1234)
        bands = {
            "<85": 0, "85-89": 0, "90-92": 0, "93-95": 0,
            "96-98": 0, "99": 0, "100": 0,
        }
        for _ in range(200):
            wp = rng.uniform(40.0, 90.0)
            edge = rng.uniform(-3.0, 15.0)
            n = rng.randint(0, 5)
            factors = {f"factor_{i}": rng.uniform(0.3, 0.9) for i in range(n)}
            ls = self._score(factors, sport="CFB", market="spread",
                              win_prob=wp, edge_percent=edge,
                              book_odds=-110)
            if   ls <  85: bands["<85"]  += 1
            elif ls <= 89: bands["85-89"] += 1
            elif ls <= 92: bands["90-92"] += 1
            elif ls <= 95: bands["93-95"] += 1
            elif ls <= 98: bands["96-98"] += 1
            elif ls <  100: bands["99"]  += 1
            else:           bands["100"] += 1
        # 100 must NEVER be reached without Apex — final clamp is [55, 99].
        assert bands["100"] == 0, bands
        # 99s should be rare — Phase B bonus is capped below 90, so
        # only intrinsically strong picks can reach 99.
        total = sum(bands.values())
        assert bands["99"] <= max(1, total // 20), (
            f"too many 99s: {bands}"
        )


# ─────────────────────────────────────────────────────────────────────
# UEA / UNIVERSAL_MARKET_TRUTH INTEGRATION
# ─────────────────────────────────────────────────────────────────────

class TestUniversalMarketTruthIntegration:
    def test_ou_conservation_matches_derived_from_odds(self):
        """When both sides carry real book odds, derived implied
        probabilities should sum to > 1 (vig).  After de-vig they
        should sum to 1 ± tolerance."""
        from services.probability_units import implied_probability_from_odds
        from services.universal_market_truth import check_ou_conservation
        # Real book pair: Over 8.5 -115, Under 8.5 -105
        p_over_book = implied_probability_from_odds(-115)
        p_under_book = implied_probability_from_odds(-105)
        raw_total = p_over_book + p_under_book
        assert raw_total > 1.0, f"expected vig, got {raw_total}"
        # De-vig proportionally.
        p_over  = p_over_book / raw_total
        p_under = p_under_book / raw_total
        r = check_ou_conservation(p_over, p_under)
        assert r.valid, r

    def test_alt_ladder_from_book_prices(self):
        """A synthetic 3-rung alt ladder from a monotonic distribution
        passes the guard; a corrupted one fails."""
        from services.universal_market_truth import (
            AltLineRung, assert_alt_monotonicity,
        )
        good = [
            AltLineRung(line=6.5, p_over=0.90, p_under=0.10, distribution_id="d",
                        distribution_version="v"),
            AltLineRung(line=8.5, p_over=0.55, p_under=0.45, distribution_id="d",
                        distribution_version="v"),
            AltLineRung(line=10.5, p_over=0.15, p_under=0.85, distribution_id="d",
                        distribution_version="v"),
        ]
        assert assert_alt_monotonicity(good) == []
        bad = [
            AltLineRung(line=6.5, p_over=0.55, p_under=0.45, distribution_id="d",
                        distribution_version="v"),
            AltLineRung(line=8.5, p_over=0.70, p_under=0.30, distribution_id="d",
                        distribution_version="v"),   # WRONG — Over increases AND Under decreases
        ]
        # A corrupted ladder violates both Over-monotonic AND Under-monotonic — 2 violations expected.
        v = assert_alt_monotonicity(bad)
        assert len(v) == 2
        details = {viol.detail for viol in v}
        assert "p_over_increasing_with_higher_line" in details
        assert "p_under_decreasing_with_higher_line" in details
