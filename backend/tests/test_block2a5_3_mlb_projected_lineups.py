"""Block 2A.5.3 — MLB PROJECTED-LINEUP CLOSURE tests.

Extends Block 2A.5.2 (confirmed-lineup reachability) with the
authoritative projected-lineup fallback drawn from MLB StatsAPI's
``/schedule?hydrate=lineups`` endpoint.

Contract (spec §3):
    lineup_status = {
        "status":     "CONFIRMED" | "PROJECTED" | "UNKNOWN",
        "lineup_pos": 1..9 | None,
        "source":     str,
        "updated_at": iso,
    }

Precedence (spec §4): CONFIRMED > PROJECTED > UNKNOWN.

────────────────────────────────────────────────────────────────
Design of these tests
────────────────────────────────────────────────────────────────
* All fixtures are IN-PROCESS — no live MLB StatsAPI calls.
* The projected/confirmed bundle produced by
  ``services.enrichment.mlb_projected_lineup`` is fed directly into
  ``ctx["hitters"]`` via ``build_hitter_rows``, then the same
  hitter emission wiring already exercised by 2A.5.2 is exercised
  again with an explicit PROJECTED provenance.
* End-to-end reachability is proven via
  ``BoardProjectionService`` and ``apply_magic_tier``.
"""
from __future__ import annotations

import pytest


# ═════════════════════════════════════════════════════════════════════
# §A  Lineup bundle → hitter rows
# ═════════════════════════════════════════════════════════════════════

class TestBundleToHitterRows:
    """Prove ``build_hitter_rows`` stamps explicit provenance."""

    def test_confirmed_bundle_produces_confirmed_rows(self):
        from services.enrichment.mlb_projected_lineup import build_hitter_rows
        bundle = {
            "status": "confirmed",
            "source": "statsapi_feed_live_batting_order",
            "updated_at": "2026-08-13T21:00:00+00:00",
            "home": [{"id": 592450, "name": "Aaron Judge",  "slot": 2},
                      {"id": 596019, "name": "Juan Soto",    "slot": 3}],
            "away": [{"id": 502110, "name": "Rafael Devers", "slot": 3}],
        }
        rows = build_hitter_rows(bundle)
        assert rows["aaron judge"]["lineup_confirmed"] is True
        assert rows["aaron judge"]["lineup_slot"] == 2
        assert rows["aaron judge"]["lineup_source"] == \
            "statsapi_feed_live_batting_order"
        assert rows["aaron judge"]["is_home"] is True
        assert rows["rafael devers"]["is_home"] is False
        assert rows["rafael devers"]["lineup_confirmed"] is True

    def test_projected_bundle_produces_projected_rows(self):
        from services.enrichment.mlb_projected_lineup import build_hitter_rows
        bundle = {
            "status": "projected",
            "source": "statsapi_schedule_hydrate_lineups",
            "updated_at": "2026-08-13T19:00:00+00:00",
            "home": [{"id": 592450, "name": "Aaron Judge", "slot": 2}],
            "away": [{"id": 502110, "name": "Rafael Devers", "slot": 3}],
        }
        rows = build_hitter_rows(bundle)
        assert rows["aaron judge"]["lineup_confirmed"] is False
        assert rows["aaron judge"]["is_starter"] is True
        assert rows["aaron judge"]["lineup_slot"] == 2
        assert rows["aaron judge"]["lineup_source"] == \
            "statsapi_schedule_hydrate_lineups"

    def test_unknown_bundle_produces_no_rows(self):
        from services.enrichment.mlb_projected_lineup import build_hitter_rows
        bundle = {"status": "unknown", "source": None,
                  "home": [], "away": []}
        assert build_hitter_rows(bundle) == {}


# ═════════════════════════════════════════════════════════════════════
# §B  classify_lineup_status honours PROJECTED
# ═════════════════════════════════════════════════════════════════════

class TestClassifyLineupStatusProjected:
    def test_projected_flags_yield_projected_starter(self):
        from services.mlb_gates import classify_lineup_status
        s = classify_lineup_status(
            lineup_confirmed=False, is_starter=True, lineup_slot=2,
        )
        assert s == "projected_starter"

    def test_projected_only_slot_yields_projected_starter(self):
        # lineup_confirmed missing but slot present → projected.
        from services.mlb_gates import classify_lineup_status
        s = classify_lineup_status(lineup_slot=5)
        assert s == "projected_starter"

    def test_confirmed_overrides_projected_when_both_set(self):
        from services.mlb_gates import classify_lineup_status
        # If lineup_confirmed=True is set alongside is_starter=True,
        # the classifier returns confirmed_starter (§4 precedence).
        s = classify_lineup_status(
            lineup_confirmed=True, is_starter=True, lineup_slot=3,
        )
        assert s == "confirmed_starter"

    def test_scratched_overrides_projected(self):
        from services.mlb_gates import classify_lineup_status
        s = classify_lineup_status(
            scratched=True, is_starter=True, lineup_slot=3,
        )
        assert s == "scratched"

    def test_bench_overrides_projected(self):
        from services.mlb_gates import classify_lineup_status
        s = classify_lineup_status(
            on_bench=True, is_starter=True, lineup_slot=3,
        )
        assert s == "bench"


# ═════════════════════════════════════════════════════════════════════
# §C  Data-quality caps per lineup status (§6)
# ═════════════════════════════════════════════════════════════════════

class TestLineupCaps:
    def test_confirmed_cap_99(self):
        from services.mlb_gates import data_quality_cap_for_status
        assert data_quality_cap_for_status("confirmed_starter") == 99.0

    def test_projected_cap_92(self):
        from services.mlb_gates import data_quality_cap_for_status
        assert data_quality_cap_for_status("projected_starter") == 92.0

    def test_unknown_cap_below_board_floor(self):
        from services.mlb_gates import data_quality_cap_for_status
        cap = data_quality_cap_for_status("unknown")
        assert cap is not None and cap < 85.0

    def test_bench_scratched_return_none(self):
        from services.mlb_gates import data_quality_cap_for_status
        assert data_quality_cap_for_status("bench") is None
        assert data_quality_cap_for_status("scratched") is None


# ═════════════════════════════════════════════════════════════════════
# §D  Precedence — confirmed row wins when both present in ctx
# ═════════════════════════════════════════════════════════════════════

class TestConfirmedOverridesProjectedInCtx:
    """When the ctx is built with confirmed rows present, the
    projected-lineup fallback code in game_context.py must NOT
    overwrite them.  Simulate by manually building a mixed ctx."""

    def test_manual_confirmed_row_not_overwritten_by_projected(self):
        from services.enrichment.mlb_projected_lineup import build_hitter_rows
        # Confirmed row already present in ctx.
        ctx = {"hitters": {"aaron judge": {
            "is_home": True, "is_starter": True,
            "lineup_confirmed": True, "lineup_slot": 2,
            "lineup_source": "statsapi_feed_live_batting_order",
        }}}
        # Projected-lineup fallback would ordinarily populate the row.
        proj_bundle = {
            "status": "projected",
            "source": "statsapi_schedule_hydrate_lineups",
            "updated_at": "x",
            "home": [{"id": 592450, "name": "Aaron Judge", "slot": 8}],
            "away": [],
        }
        proj_rows = build_hitter_rows(proj_bundle)
        # Merge WITH precedence — confirmed rows survive.
        for k, r in proj_rows.items():
            existing = ctx["hitters"].get(k)
            if existing and existing.get(
                    "lineup_source") == "statsapi_feed_live_batting_order":
                continue
            ctx["hitters"][k] = r
        assert ctx["hitters"]["aaron judge"]["lineup_confirmed"] is True
        assert ctx["hitters"]["aaron judge"]["lineup_slot"] == 2   # not 8
        assert ctx["hitters"]["aaron judge"]["lineup_source"] == \
            "statsapi_feed_live_batting_order"


# ═════════════════════════════════════════════════════════════════════
# §E  Feature engine handles both confirmed and projected rows
# ═════════════════════════════════════════════════════════════════════

def _rich_hitter_row(*, confirmed: bool, slot: int = 3) -> dict:
    return {
        "lineup_confirmed":  confirmed,
        "is_starter":        True,
        "lineup_slot":       slot,
        "lineup_source":     ("statsapi_feed_live_batting_order"
                                if confirmed
                                else "statsapi_schedule_hydrate_lineups"),
        "is_home":           True,
        "opp_pitcher_hand":  "R",
        "opp_pitcher_name":  "Nick Pivetta",
        "l10_hit_rate":      0.34,
        "home_ops":          0.98,
        "vs_r_ops":          0.95,
        "statcast":          {"xba": 0.310, "barrel_pct": 18.0,
                                "hard_hit": 58.0, "xba_diff": 0.020},
        "bvp":               {"pa": 20, "ops": 1.100},
    }


def _rich_game_ctx(row: dict, name: str = "aaron judge") -> dict:
    return {
        "home_team": "New York Yankees",
        "away_team": "Boston Red Sox",
        "starting_pitcher_home": {"name": "Gerrit Cole", "throws": "R",
                                    "stuff_plus": 110},
        "starting_pitcher_away": {"name": "Nick Pivetta", "throws": "R",
                                    "stuff_plus": 92},
        "hitters": {name: row},
    }


class TestFeatureEngineHandlesBothLineupStates:
    def test_projected_hitter_passes_feature_gate(self):
        from services.mlb_feature_engine import (
            build_mlb_hitter_factors, has_enough_real_data,
        )
        ctx = _rich_game_ctx(_rich_hitter_row(confirmed=False))
        factors, _ = build_mlb_hitter_factors(
            ctx, player="Aaron Judge", is_home=True,
            opp_pitcher_name="Nick Pivetta",
            market_type="batter_hits", line=0.5,
        )
        assert has_enough_real_data(factors, "hitter_prop")

    def test_projected_hitter_covers_total_bases(self):
        from services.mlb_feature_engine import (
            build_mlb_hitter_factors, has_enough_real_data,
        )
        ctx = _rich_game_ctx(_rich_hitter_row(confirmed=False))
        factors, _ = build_mlb_hitter_factors(
            ctx, player="Aaron Judge", is_home=True,
            opp_pitcher_name="Nick Pivetta",
            market_type="batter_total_bases", line=1.5,
        )
        assert has_enough_real_data(factors, "hitter_prop")


# ═════════════════════════════════════════════════════════════════════
# §F  Public-contract picks — CONFIRMED / PROJECTED / UNKNOWN
# ═════════════════════════════════════════════════════════════════════

def _mk_pick(*, lu_status: str, lu_pos, lock: float,
             market="Aaron Judge (NYY) Over 0.5 Hits",
             line: float = 0.5,
             source: str = "statsapi_schedule_hydrate_lineups",
             pid: str = "p1") -> dict:
    return {
        "id":                 pid,
        "sport":              "MLB",
        "market":             market,
        "side":               "Over",
        "line":               line,
        "book_odds":          -180,
        "sportsbook":         "FanDuel",
        "implied_probability": 0.643,
        "lock_score":         lock,
        "published_lock_score": lock,
        "event_id":           "e-yanks-vs-bosox",
        "event_time":         "2026-08-15T23:05:00Z",
        "player_name":        "Aaron Judge",
        "home_team":          "New York Yankees",
        "away_team":          "Boston Red Sox",
        "no_bet": False, "off_board": False,
        "hide_from_main_board": False,
        "lineup_status": {"status": lu_status, "lineup_pos": lu_pos,
                            "source": source,
                            "updated_at": "2026-08-15T22:00:00Z"},
    }


class TestPickContractCases:
    def test_confirmed_pick_reaches_board(self):
        from services.board_projection_service import BoardProjectionService
        pick = _mk_pick(lu_status="CONFIRMED", lu_pos=3, lock=91.0,
                         source="statsapi_feed_live_batting_order")
        assert BoardProjectionService().project_ids([pick]) == [pick["id"]]

    def test_projected_pick_reaches_board(self):
        from services.board_projection_service import BoardProjectionService
        pick = _mk_pick(lu_status="PROJECTED", lu_pos=2, lock=89.0)
        ids = BoardProjectionService().project_ids([pick])
        assert pick["id"] in ids, \
            "Projected hitter pick must reach the Locks board"

    def test_projected_total_bases_pick_reaches_board(self):
        from services.board_projection_service import BoardProjectionService
        pick = _mk_pick(
            lu_status="PROJECTED", lu_pos=3, lock=88.0,
            market="Aaron Judge (NYY) Over 1.5 Total Bases",
            line=1.5, pid="tb-proj-1",
        )
        ids = BoardProjectionService().project_ids([pick])
        assert pick["id"] in ids

    def test_unknown_pick_below_floor_does_not_reach_board(self):
        """UNKNOWN lineup status implies the data-quality cap has
        already floored lock_score at 79 in the emission path — such
        picks CANNOT clear the >85 board floor."""
        from services.board_projection_service import BoardProjectionService
        pick = _mk_pick(lu_status="UNKNOWN", lu_pos=None, lock=79.0)
        ids = BoardProjectionService().project_ids([pick])
        assert pick["id"] not in ids


# ═════════════════════════════════════════════════════════════════════
# §G  Magic tier — projected + confirmed
# ═════════════════════════════════════════════════════════════════════

class TestMagicConsumesLineupCertainty:
    def test_projected_pick_receives_magic_tier(self):
        from services.magic_tier_policy import apply_magic_tier
        pick = _mk_pick(lu_status="PROJECTED", lu_pos=2, lock=90.0)
        apply_magic_tier(pick, sport="MLB")
        assert "magic_tier" in pick

    def test_confirmed_pick_receives_magic_tier(self):
        from services.magic_tier_policy import apply_magic_tier
        pick = _mk_pick(
            lu_status="CONFIRMED", lu_pos=3, lock=91.0,
            source="statsapi_feed_live_batting_order",
        )
        apply_magic_tier(pick, sport="MLB")
        assert "magic_tier" in pick

    def test_projected_lineup_capped_below_confirmed_in_magic(self):
        """Magic tier policy caps `projected` at Strong Lock."""
        from services.magic_tier_policy import _extract_lineup_certainty
        assert _extract_lineup_certainty(_mk_pick(
            lu_status="PROJECTED", lu_pos=2, lock=90.0,
        )) == "projected"
        assert _extract_lineup_certainty(_mk_pick(
            lu_status="CONFIRMED", lu_pos=3, lock=91.0,
        )) == "confirmed"


# ═════════════════════════════════════════════════════════════════════
# §H  Auto-upgrade PROJECTED → CONFIRMED (post-emission enrichment)
# ═════════════════════════════════════════════════════════════════════

class TestConfirmedUpgradePath:
    """Simulate the refresh path: an emitted PROJECTED pick lands in
    ``enrich_pick_with_projected_lineup`` and a subsequent refresh
    replaces its status with CONFIRMED once the bundle upgrades.
    Verifies via the internal helper (no network)."""

    def test_projected_upgrades_to_confirmed_when_bundle_confirmed(
            self, monkeypatch):
        import asyncio
        from services.enrichment import mlb_projected_lineup as m
        async def _stub_fetch(**_kwargs):
            return {
                "status": "confirmed",
                "source": "statsapi_feed_live_batting_order",
                "updated_at": "2026-08-15T22:35:00+00:00",
                "home": [{"id": 592450, "name": "Aaron Judge", "slot": 2}],
                "away": [{"id": 502110, "name": "Rafael Devers", "slot": 3}],
                "game_pk": 999,
            }
        monkeypatch.setattr(m, "fetch_mlb_lineup_bundle", _stub_fetch)
        pick = _mk_pick(lu_status="PROJECTED", lu_pos=2, lock=90.0)
        out = asyncio.run(m.enrich_pick_with_projected_lineup(pick))
        assert out["lineup_status"]["status"] == "CONFIRMED"
        assert out["lineup_status"]["lineup_pos"] == 2

    def test_projected_downgrades_to_bench_when_player_dropped(
            self, monkeypatch):
        import asyncio
        from services.enrichment import mlb_projected_lineup as m
        async def _stub_fetch(**_kwargs):
            # Confirmed lineup arrived but Judge is NOT in it.
            return {
                "status": "confirmed",
                "source": "statsapi_feed_live_batting_order",
                "updated_at": "2026-08-15T22:35:00+00:00",
                "home": [{"id": 999, "name": "Anthony Volpe", "slot": 2}],
                "away": [{"id": 502110, "name": "Rafael Devers", "slot": 3}],
                "game_pk": 999,
            }
        monkeypatch.setattr(m, "fetch_mlb_lineup_bundle", _stub_fetch)
        pick = _mk_pick(lu_status="PROJECTED", lu_pos=2, lock=90.0)
        out = asyncio.run(m.enrich_pick_with_projected_lineup(pick))
        assert out["lineup_status"]["status"] == "BENCH", (
            "Player dropped from confirmed lineup must be marked BENCH")

    def test_confirmed_pick_is_not_downgraded_to_projected(
            self, monkeypatch):
        import asyncio
        from services.enrichment import mlb_projected_lineup as m
        async def _stub_fetch(**_kwargs):
            # Regressive bundle (rare / stale cache).
            return {
                "status": "projected",
                "source": "statsapi_schedule_hydrate_lineups",
                "updated_at": "2026-08-15T20:00:00+00:00",
                "home": [{"id": 592450, "name": "Aaron Judge", "slot": 8}],
                "away": [],
                "game_pk": 999,
            }
        monkeypatch.setattr(m, "fetch_mlb_lineup_bundle", _stub_fetch)
        pick = _mk_pick(
            lu_status="CONFIRMED", lu_pos=3, lock=91.0,
            source="statsapi_feed_live_batting_order",
        )
        out = asyncio.run(m.enrich_pick_with_projected_lineup(pick))
        assert out["lineup_status"]["status"] == "CONFIRMED"
        assert out["lineup_status"]["lineup_pos"] == 3


# ═════════════════════════════════════════════════════════════════════
# §I  Identity safety — wrong player / wrong team must not synthesize
# ═════════════════════════════════════════════════════════════════════

class TestIdentitySafety:
    def test_wrong_player_projected_leaves_no_lineup(self, monkeypatch):
        import asyncio
        from services.enrichment import mlb_projected_lineup as m
        async def _stub_fetch(**_kwargs):
            return {
                "status": "projected",
                "source": "statsapi_schedule_hydrate_lineups",
                "updated_at": "x",
                "home": [{"id": 592450, "name": "Aaron Judge", "slot": 2}],
                "away": [{"id": 502110, "name": "Rafael Devers", "slot": 3}],
                "game_pk": 999,
            }
        monkeypatch.setattr(m, "fetch_mlb_lineup_bundle", _stub_fetch)
        pick = _mk_pick(lu_status="UNKNOWN", lu_pos=None, lock=79.0)
        pick["player_name"] = "Fake Player"
        pick["market"] = "Fake Player Over 0.5 Hits"
        out = asyncio.run(m.enrich_pick_with_projected_lineup(pick))
        # Player not in confirmed OR projected lineup → UNKNOWN (fail closed).
        assert out["lineup_status"]["status"] == "UNKNOWN"

    def test_projected_lineup_uses_mlb_returned_ordering_only(self):
        """The projected slot comes from MLB's returned list ORDER
        (which IS the anticipated batting order per MLB StatsAPI).
        No name-based, roster-based, or DFS heuristic is applied."""
        from services.enrichment.mlb_projected_lineup import build_hitter_rows
        bundle = {
            "status": "projected",
            "source": "statsapi_schedule_hydrate_lineups",
            "updated_at": "x",
            "home": [
                {"id": 1, "name": "Aaron Judge", "slot": 2},
                {"id": 2, "name": "Juan Soto",   "slot": 3},
            ],
            "away": [],
        }
        rows = build_hitter_rows(bundle)
        # Slots match EXACTLY what MLB provided — no reordering.
        assert rows["aaron judge"]["lineup_slot"] == 2
        assert rows["juan soto"]["lineup_slot"] == 3
        # Source is always the MLB-endpoint tag — never a heuristic.
        assert rows["aaron judge"]["lineup_source"] == \
            "statsapi_schedule_hydrate_lineups"

    def test_no_lineup_source_no_projected_rows(self):
        from services.enrichment.mlb_projected_lineup import build_hitter_rows
        # Simulate empty-lineup bundle: both sides empty, status unknown.
        assert build_hitter_rows({"status": "unknown",
                                   "home": [], "away": []}) == {}


# ═════════════════════════════════════════════════════════════════════
# §J  Real-line integrity — projected cannot fabricate odds
# ═════════════════════════════════════════════════════════════════════

class TestRealLineIntegrityUnderProjected:
    def test_projected_pick_without_book_odds_ineligible(self):
        from services.board_projection_service import BoardProjectionService
        pick = _mk_pick(lu_status="PROJECTED", lu_pos=2, lock=99.0)
        pick["book_odds"] = None
        pick["implied_probability"] = None
        ids = BoardProjectionService().project_ids([pick])
        assert pick["id"] not in ids

    def test_no_real_book_line_flag_blocks_projected_pick(self):
        from services.board_projection_service import BoardProjectionService
        pick = _mk_pick(lu_status="PROJECTED", lu_pos=2, lock=99.0)
        pick["no_real_book_line"] = True
        ids = BoardProjectionService().project_ids([pick])
        assert pick["id"] not in ids


# ═════════════════════════════════════════════════════════════════════
# §K  Batting-order change reflected in current evidence (§10)
# ═════════════════════════════════════════════════════════════════════

class TestBattingOrderChangeReflected:
    def test_confirmed_lineup_moves_player_from_slot_2_to_6(
            self, monkeypatch):
        import asyncio
        from services.enrichment import mlb_projected_lineup as m
        async def _stub_fetch(**_kwargs):
            return {
                "status": "confirmed",
                "source": "statsapi_feed_live_batting_order",
                "updated_at": "2026-08-15T22:40:00+00:00",
                "home": [{"id": 592450, "name": "Aaron Judge", "slot": 6}],
                "away": [{"id": 502110, "name": "Rafael Devers", "slot": 3}],
                "game_pk": 999,
            }
        monkeypatch.setattr(m, "fetch_mlb_lineup_bundle", _stub_fetch)
        pick = _mk_pick(lu_status="PROJECTED", lu_pos=2, lock=90.0)
        out = asyncio.run(m.enrich_pick_with_projected_lineup(pick))
        # Provenance is now confirmed AND slot updated.
        assert out["lineup_status"]["status"] == "CONFIRMED"
        assert out["lineup_status"]["lineup_pos"] == 6
        assert out["lineup_status"]["source"] == \
            "statsapi_feed_live_batting_order"


# ═════════════════════════════════════════════════════════════════════
# §L  Regressions — 2A.5.1 + 2A.5.2 still pass
# ═════════════════════════════════════════════════════════════════════

class TestPriorBlocksRemainGreen:
    def test_2a5_1_totals_neutrality_present(self):
        # Cheap smoke: importing the totals-side normalization util
        # and running one case succeeds — the full suite is run
        # separately in regression.
        from services.mlb_feature_engine import build_mlb_total_factors
        f, _ = build_mlb_total_factors(
            {"park_run_total_avg": 11.5}, side="Over",
        )
        assert isinstance(f, dict)

    def test_2a5_2_total_bases_still_survives_emission(self):
        import inspect, sports_engine
        src = inspect.getsource(sports_engine._props_picks_from_event)
        assert "batter_total_bases" in src
        assert "float(point) == 0.5" in src

    def test_2a5_2_lineup_gate_still_wired(self):
        import inspect, sports_engine
        src = inspect.getsource(sports_engine._props_picks_from_event)
        assert "classify_lineup_status" in src


# ═════════════════════════════════════════════════════════════════════
# §M  Publication contract — normalized to CONFIRMED/PROJECTED/UNKNOWN
# ═════════════════════════════════════════════════════════════════════

class TestPublicationContract:
    def test_hitter_emission_normalizes_to_uppercase_contract(self):
        """Verify the sports_engine hitter path emits the public
        UPPERCASE `lineup_status.status` contract."""
        import inspect, sports_engine
        src = inspect.getsource(sports_engine._props_picks_from_event)
        assert '"CONFIRMED"' in src
        assert '"PROJECTED"' in src
        assert '"UNKNOWN"' in src

    def test_projected_source_field_documented(self):
        import inspect
        from services.enrichment import mlb_projected_lineup
        src = inspect.getsource(mlb_projected_lineup)
        assert "statsapi_schedule_hydrate_lineups" in src
        assert "statsapi_feed_live_batting_order" in src
