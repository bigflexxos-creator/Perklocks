"""Commit B/5 — Remaining runtime-adoption closure tests.

Covers the three sub-commits:

  * B5a — Scheduler / dynamic Soccer/Tennis discovery / provider-request
          bad-market isolation.
  * B5b — Real funnel instrumentation at the universal pick factory
          (``_build_pick``) and HONEST-ZERO rejection pass for sports
          with ``provider_events > 0`` but ``model_candidates == 0``.
  * B5c — Direct H2H + ESPN canonical adoption in the real HI path
          (covered in companion ``test_commit_b5c_hi_adoption.py``).

These tests are static-source + unit-level: they prove the real
production paths call the real shared helpers at the actual runtime
sites — no derivation, no post-hoc inference.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ═══════════════════════════════════════════════════════════════════
# B5a — Scheduler authority / discovery / bad-market isolation
# ═══════════════════════════════════════════════════════════════════

def test_b5a_scheduler_classify_wired_into_generate_all_picks():
    """``classify_sport_activation`` must be invoked at the real
    scheduler entry point (``sports_engine.generate_all_picks``) —
    not merely re-exported from the helper module."""
    src = open("/app/backend/sports_engine.py").read()
    gen_idx = src.index("async def generate_all_picks(")
    body = src[gen_idx: gen_idx + 20_000]
    # The classify + register pair must appear in the executable body
    # of generate_all_picks, BEFORE asyncio.gather(*fetch_jobs).
    assert "classify_sport_activation" in body, (
        "generate_all_picks must call classify_sport_activation at "
        "the real scheduler entry point")
    assert "register_scheduler_entry" in body, (
        "generate_all_picks must call register_scheduler_entry")
    i_classify = body.index("classify_sport_activation")
    i_gather = body.index("asyncio.gather(*fetch_jobs")
    assert i_classify < i_gather, (
        "classify_sport_activation must execute BEFORE the fetch "
        "gather so SPORT_REFRESH_STARVED can be surfaced")


def test_b5a_dynamic_soccer_tennis_discovery_wired():
    """``classify_discovered_provider_key`` must be invoked from
    ``_fetch_picks_for_sport`` (the real Soccer/Tennis discovery path
    inside sports_engine.py)."""
    src = open("/app/backend/sports_engine.py").read()
    fp_idx = src.index("async def _fetch_picks_for_sport")
    body = src[fp_idx: fp_idx + 10_000]
    assert "classify_discovered_provider_key" in body, (
        "_fetch_picks_for_sport must call "
        "classify_discovered_provider_key for Soccer/Tennis discovery")


def test_b5a_bad_market_isolation_in_odds_fetch():
    """``valid_markets`` from ``services.sport_activation`` must
    be wired into ``_fetch_odds_for`` BEFORE the provider request so
    an invalid key never suppresses valid sibling markets."""
    src = open("/app/backend/sports_engine.py").read()
    fo_idx = src.index("async def _fetch_odds_for")
    body = src[fo_idx: fo_idx + 6_000]
    assert "valid_markets" in body, (
        "_fetch_odds_for must call valid_markets before the request")
    assert "BAD_MARKET_ISOLATED" in body, (
        "_fetch_odds_for must emit BAD_MARKET_ISOLATED rejection")
    # Request builder must consume the filtered markets.
    i_valid = body.index("valid_markets")
    i_request = body.index('"regions"')
    assert i_valid < i_request, (
        "valid_markets filter must execute BEFORE the provider "
        "request is dispatched")


def test_b5a_gateway_422_isolation_present():
    """The provider gateway must honor the bad-market registry on
    the request builder (so a single bad key cannot sink valid
    siblings)."""
    src = open("/app/backend/services/odds_api_gateway.py").read()
    assert "valid_markets" in src and "BAD_MARKET_ISOLATED" in src, (
        "odds_api_gateway must consult the bad-market registry via "
        "valid_markets and emit BAD_MARKET_ISOLATED on isolation")


# ═══════════════════════════════════════════════════════════════════
# B5b — Real funnel instrumentation at _build_pick
# ═══════════════════════════════════════════════════════════════════

def test_b5b_build_pick_instrumentation_present():
    """``_build_pick`` must record the universal funnel stages at
    the real pick-factory boundary — not inferred after the fact."""
    src = open("/app/backend/sports_engine.py").read()
    bp_idx = src.index("def _build_pick(")
    # Terminate on the first ``_MLB_TEAM_NAME_TO_ID`` variable
    # (module-level dict declared right after the function body).
    bp_end = src.index("_MLB_TEAM_NAME_TO_ID", bp_idx)
    body = src[bp_idx:bp_end]
    for stage in (
        "STAGE_NORMALIZED_ROWS",
        "STAGE_SOURCE_WAGERS",
        "STAGE_IDENTITY_RESOLVED",
        "STAGE_HISTORY_READY",
        "STAGE_MODEL_ATTEMPTS",
        "STAGE_MODEL_CANDIDATES",
        "STAGE_AT_OR_ABOVE_85",
    ):
        assert stage in body, (
            f"_build_pick must record {stage} at the pick-factory "
            "boundary (B5b real runtime adoption)")
    # The B5b block must execute BEFORE the ``return {...}`` so the
    # emitted row cannot leak without funnel counters.
    i_stage = body.index("STAGE_NORMALIZED_ROWS")
    i_return = body.index("    return {")
    assert i_stage < i_return, (
        "B5b instrumentation must run BEFORE _build_pick returns")


def test_b5b_build_pick_fires_stages_end_to_end():
    """Call ``_build_pick`` with a fully-provenanced input and verify
    every expected stage fires in ``stage_funnel.report()``."""
    from services import stage_funnel as sf
    import sports_engine
    sf.reset()
    pick = sports_engine._build_pick(
        sport="MLB",
        league="MLB",
        event="Yankees vs Red Sox",
        event_time="2026-06-20T23:05:00Z",
        market="batter_hits",
        pick_side="Aaron Judge Over 0.5",
        model_win_prob=0.74,
        book_odds=-150,
        lock=92.0,
        factors={"form": 0.6},
        insights=["model edge"],
        external_id="e1-r1",
        odds_source="the_odds_api",
        bookmaker="fanduel",
        provider="the_odds_api",
        provider_event_id="prov_evt_1",
        provider_market_key="batter_hits",
        provider_outcome_name="Aaron Judge Over",
        canonical_event_id="can_evt_1",
        canonical_player_id="can_plr_1",
        canonical_team_id="can_tm_1",
        source_wager={"id": "sw_1"},
    )
    assert pick is not None, "pick must be built"
    rep = sf.report(sport="MLB")
    mlb = rep["sports"]["MLB"]
    fam = mlb["families"].get("batter_hits", {})
    for stage, expected in (
        (sf.STAGE_NORMALIZED_ROWS,   1),
        (sf.STAGE_SOURCE_WAGERS,     1),
        (sf.STAGE_IDENTITY_RESOLVED, 1),
        (sf.STAGE_HISTORY_READY,     1),
        (sf.STAGE_MODEL_ATTEMPTS,    1),
        (sf.STAGE_MODEL_CANDIDATES,  1),
        (sf.STAGE_AT_OR_ABOVE_85,    1),
    ):
        assert fam.get(stage) == expected, (
            f"stage {stage} should be {expected}; got funnel={fam}")


def test_b5b_build_pick_below_85_does_not_fire_lock_stage():
    """A pick built with lock=70 must NOT fire ``at_or_above_85`` —
    HONEST ZERO at the exact runtime site."""
    from services import stage_funnel as sf
    import sports_engine
    sf.reset()
    sports_engine._build_pick(
        sport="NBA",
        league="NBA",
        event="LAL vs BOS",
        event_time="2026-06-20T23:05:00Z",
        market="h2h",
        pick_side="LAL",
        model_win_prob=0.55,
        book_odds=-115,
        lock=70.0,
        factors={},
        insights=[],
        external_id="e-nba-1",
    )
    rep = sf.report(sport="NBA")
    nba = rep["sports"]["NBA"]
    fam = nba["families"].get("h2h", {})
    assert fam.get(sf.STAGE_NORMALIZED_ROWS, 0) == 1
    assert fam.get(sf.STAGE_MODEL_CANDIDATES, 0) == 1
    assert fam.get(sf.STAGE_AT_OR_ABOVE_85, 0) == 0, (
        "at_or_above_85 must be HONEST ZERO when lock < 85")


def test_b5b_build_pick_without_provider_lineage_honest_zero_source_wagers():
    """When caller does not pass SourceWager lineage, source_wagers
    stage stays at HONEST ZERO (not falsely inflated)."""
    from services import stage_funnel as sf
    import sports_engine
    sf.reset()
    sports_engine._build_pick(
        sport="NHL",
        league="NHL",
        event="TOR vs MTL",
        event_time="2026-06-20T23:05:00Z",
        market="h2h",
        pick_side="TOR",
        model_win_prob=0.60,
        book_odds=-120,
        lock=88.0,
        factors={},
        insights=[],
        external_id="e-nhl-1",
        # No provider / provider_event_id / canonical_* / source_wager.
    )
    rep = sf.report(sport="NHL")
    fam = rep["sports"]["NHL"]["families"].get("h2h", {})
    assert fam.get(sf.STAGE_NORMALIZED_ROWS, 0) == 1
    assert fam.get(sf.STAGE_SOURCE_WAGERS, 0) == 0, (
        "source_wagers must stay HONEST ZERO when lineage absent")
    assert fam.get(sf.STAGE_IDENTITY_RESOLVED, 0) == 0, (
        "identity_resolved must stay HONEST ZERO when canonical "
        "IDs absent")
    # at_or_above_85 fires because lock=88.
    assert fam.get(sf.STAGE_AT_OR_ABOVE_85, 0) == 1


def test_b5b_honest_zero_rejection_pass_wired_in_generate_all_picks():
    """``generate_all_picks`` must emit a MODEL_UNAVAILABLE rejection
    for any sport that saw provider_events > 0 but produced 0
    model_candidates in the real cycle."""
    src = open("/app/backend/sports_engine.py").read()
    gen_idx = src.index("async def generate_all_picks(")
    body = src[gen_idx:]
    assert "REASON_MODEL_UNAVAILABLE" in body, (
        "generate_all_picks must emit REASON_MODEL_UNAVAILABLE in "
        "the HONEST-ZERO rejection pass")
    # The honest-zero pass must live AFTER the asyncio.gather so it
    # inspects the real cycle counters, not stale state.
    i_gather = body.index("asyncio.gather(*fetch_jobs")
    i_zero = body.index("REASON_MODEL_UNAVAILABLE")
    assert i_gather < i_zero, (
        "HONEST-ZERO pass must run AFTER the real refresh completes")


def test_b5b_provider_events_recorded_from_bulk_odds_consumption():
    """``_fetch_odds_for`` must record STAGE_PROVIDER_EVENTS /
    STAGE_PROVIDER_OUTCOMES at the EXACT bulk-odds consumption
    point (not from projector source_rows)."""
    src = open("/app/backend/sports_engine.py").read()
    fo_idx = src.index("async def _fetch_odds_for")
    body = src[fo_idx: fo_idx + 6_000]
    assert "STAGE_PROVIDER_EVENTS" in body
    assert "STAGE_PROVIDER_OUTCOMES" in body


# ═══════════════════════════════════════════════════════════════════
# Preservation: B4.2 contract — provider_events never from projector
# ═══════════════════════════════════════════════════════════════════

def test_b5_preserves_b4_2_projector_zero_contract():
    """B4.2 forbade pumping provider_events from HI projector
    source_rows — B5b must not regress this."""
    src = open(
        "/app/backend/services/pick_refresh_orchestrator.py").read()
    executable = "\n".join(
        ln for ln in src.splitlines()
        if not ln.strip().startswith("#")
    )
    assert "STAGE_PROVIDER_EVENTS" not in executable, (
        "orchestrator must NOT record STAGE_PROVIDER_EVENTS from "
        "projector source_rows (B4.2 contract)")
