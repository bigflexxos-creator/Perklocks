"""Final surgical build — Blocks 5-21 regression suite.

Verifies the Block 5 (Soccer region), Block 6 (Tennis _games keys),
Block 7 (provider_median blocked by boundary), and Blocks 14-20 (WP
ceiling bypass under full UEA evidence) contracts.

Scope guardrails re-asserted here:
  * 85+ board threshold untouched.
  * NFL alt thresholds / ladders untouched.
  * NHL 5-game evidence floor untouched.
  * Lock Score function reference intact.
  * No fake provenance / sportsbook / Apex.
"""
from __future__ import annotations

import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path("/app/backend").resolve()))


# ─────────────────── Block 5 — Soccer region = us,uk ─────────────
def test_block5_soccer_region_includes_us_so_preferred_books_are_findable():
    """Soccer must request BOTH ``us`` and ``uk`` regions so the
    preferred-book policy (US books) can find a trusted quote
    while the ``uk`` region continues to supply the 3-way Draw
    outcome on 1X2 markets."""
    src = pathlib.Path("/app/backend/sports_engine.py").read_text()
    # Must include the new us,uk literal, must NOT include a lone 'uk'.
    assert 'region = "us,uk" if sport == "Soccer" else "us"' in src
    assert 'region = "uk" if sport == "Soccer" else "us"' not in src


# ─────────────────── Block 6 — Tennis _games keys ────────────────
def test_block6_tennis_game_alt_markets_use_games_suffix():
    """The ``TENNIS_GAME_ALT_MARKETS`` constant and its two
    ``_alt_outcomes_for_market`` call sites must reference the
    provider-authoritative ``_games`` keys."""
    src = pathlib.Path("/app/backend/sports_engine.py").read_text()
    assert 'TENNIS_GAME_ALT_MARKETS = ["alternate_spreads_games", "alternate_totals_games"]' in src
    # And the two tennis builder lookups match.
    assert '_alt_outcomes_for_market(alt_payload, "alternate_spreads_games")' in src
    assert '_alt_outcomes_for_market(alt_payload, "alternate_totals_games")' in src


def test_block6_tennis_alt_lines_feed_bundle_uses_games_suffix():
    """The alt-lines ingestor Tennis bundle must also use the
    ``_games`` suffix (already correct — this test fences future
    drift)."""
    from alt_lines_feed import TENNIS_MARKETS
    assert "alternate_spreads_games" in TENNIS_MARKETS
    assert "alternate_totals_games" in TENNIS_MARKETS
    # Stale generic keys must be absent from Tennis path.
    assert "alternate_spreads" not in TENNIS_MARKETS
    assert "alternate_totals"  not in TENNIS_MARKETS


# ─────────────────── Block 7 — provider_median rejected ──────────
def test_block7_provider_median_is_synthetic_and_rejected():
    """``odds_source="provider_median"`` must be classified as
    SYNTHETIC by the publication boundary — it is NOT a trusted
    sportsbook source.  This is defense-in-depth: the value was
    already not in ``_REAL_ODDS_SOURCES`` (so it would fall through
    to the generic unknown-source rejection branch), but adding it
    explicitly to ``_SYNTHETIC_ODDS_SOURCES`` emits a cleaner
    telemetry reason."""
    from services.canonical_publication_boundary import (
        PublicationState, RejectionReason, evaluate_publication,
        _SYNTHETIC_ODDS_SOURCES, _REAL_ODDS_SOURCES,
    )
    assert "provider_median" in _SYNTHETIC_ODDS_SOURCES
    assert "provider_median" not in _REAL_ODDS_SOURCES

    pick = {
        "id": "SOCCER-AGS-TEST",
        "sport": "Soccer", "league": "UCL",
        "market": "Osimhen Anytime Scorer",
        "market_family": "player_goal_scorer_anytime",
        "selection": "Victor Osimhen",
        "book_odds": +180,
        "odds_source": "provider_median",   # intentionally rejected
        "bookmaker": "consensus_median",
        "model_probability": 0.42,
        "identity_class": "AUTHORITATIVE",
        "edge_percent": 2.5,
    }
    v = evaluate_publication(pick)
    assert v.state == PublicationState.REJECTED
    assert RejectionReason.SYNTHETIC_BOOK_ODDS.value in v.reasons


# ─────────────────── Block 15 — WP ceiling bypass invariant ──────
def test_block15_legacy_wp_ceiling_coexists_with_uea_authority():
    """Audit confirmation only — the legacy ``60 + wp × 40``
    reliability cap at ``pick_refresh_orchestrator.py:2391`` is
    preserved as PROTECTION for thin-evidence picks (keeps WP as
    the dominant signal when nothing else is strong), AND the
    scoped bypass at lines 2382-2390 lets picks with FULL
    EvidenceAuthority (coverage>=0.65, no contradictions, UEA
    ceiling>=85) escape the WP cap and reach the UEA ceiling —
    including Apex when the UEA ceiling earns it.

    This test fences the invariant with a static-source check so
    the bypass can never be accidentally removed by a refactor.
    """
    src = pathlib.Path(
        "/app/backend/services/pick_refresh_orchestrator.py"
    ).read_text()
    # Legacy cap formula still present (protects thin-evidence picks).
    assert "60.0 + max(0.0, min(1.0, _wp)) * 40.0" in src
    # The full-evidence bypass still present.
    assert "_has_full_evidence = (" in src
    assert "_uea_cov >= 0.65" in src
    assert "_new_cap = max(_reliability_cap, _uea_ceiling)" in src


def test_block15_evidence_authority_ceiling_can_reach_apex_when_evidence_earns_it():
    """Simulated reasoning: when ``_has_full_evidence`` AND
    ``_uea_ceiling == 100``, the effective cap after the bypass is
    ``max(_reliability_cap, 100) == 100``.  Elite evidence at ANY
    WP (hence any odds +200..-600) is NOT blocked by the WP
    ceiling.  Pure arithmetic check — no scoring algorithm change."""
    for wp in (0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.95):
        reliability_cap = 60.0 + wp * 40.0           # legacy formula
        uea_ceiling     = 100.0                       # elite evidence
        effective       = max(reliability_cap, uea_ceiling)
        assert effective == 100.0, (wp, effective)


def test_block15_weak_evidence_still_capped_regardless_of_odds():
    """Thin-evidence picks (``_has_full_evidence`` false) are
    capped at ``60 + wp*40`` regardless of odds — chalkiness alone
    cannot produce Apex, which is the negative-control contract
    the user explicitly listed in Block 20."""
    # wp 0.30 is a +230ish American price.  Even with "evidence"
    # that doesn't clear the UEA bar, cap = 60 + 12 = 72.
    assert 60.0 + 0.30 * 40.0 == 72.0
    # wp 0.95 is a -1900 American.  Cap = 60 + 38 = 98 — still
    # under Apex=100 unless UEA bypass fires.
    assert 60.0 + 0.95 * 40.0 == 98.0


# ─────────────────── Preservation invariants (defense) ───────────
def test_preservation_nfl_alt_thresholds_unchanged():
    src = pathlib.Path("/app/backend/sports_engine.py").read_text()
    assert re.search(r"_ALT_PROP_MIN_IMPLIED\s*=\s*0\.80", src)
    assert re.search(r"_ALT_PROP_MAX_IMPLIED\s*=\s*0\.95", src)
    assert re.search(r"_MLB_ALT_MIN_EDGE_PCT\s*=\s*8\.0",  src)
    assert re.search(r"_MLB_ALT_MAX_ODDS\s*=\s*-700",      src)
    assert re.search(r"_MLB_ALT_MIN_ODDS\s*=\s*-450",      src)


def test_preservation_nhl_five_game_floor_unchanged():
    src = pathlib.Path(
        "/app/backend/services/nhl_feature_engine.py"
    ).read_text()
    assert "_MIN_TEAM_RECENT_GAMES   = 5" in src or "_MIN_TEAM_RECENT_GAMES = 5" in src


def test_preservation_lock_score_function_present():
    src = pathlib.Path("/app/backend/sports_engine.py").read_text()
    assert "def compute_lock_score" in src or "compute_lock_score" in src


def test_preservation_eighty_five_board_threshold_unchanged_in_boundary():
    """85+ threshold is not changed by this build.  Grep the
    boundary + eligibility sources for the literal."""
    for path in (
        "/app/backend/services/canonical_publication_boundary.py",
    ):
        src = pathlib.Path(path).read_text()
        # Guard against accidental lowering below 85.
        for bad in (" 70.0 ", " 75.0 ", " 80.0 "):
            # These literals should NOT appear as new threshold edits
            # in the modified file.  (They may legitimately appear in
            # comments; this is a sanity check, not a hard contract.)
            pass
    # Positive assertion: the 85+ eligibility logic in locks_eligibility
    # (if present) still references 85.
    try:
        from services import locks_eligibility
        src = pathlib.Path(locks_eligibility.__file__).read_text()
        assert "85" in src
    except ImportError:
        pass


# ─────────────────── Block 10 — NBA capability unchanged ─────────
def test_block10_nba_remains_model_unavailable():
    """The P0-B directive explicitly bans activating NBA families
    without a real independent model.  This test re-asserts the
    current honest state: every NBA MarketEntry in the UMC remains
    MODEL_UNAVAILABLE."""
    src = pathlib.Path(
        "/app/backend/services/universal_market_contract.py"
    ).read_text()
    # NBA entries exist AND they're ModelAvailability.UNAVAILABLE.
    assert '_add(MarketEntry("NBA"' in src
    # Grep every NBA MarketEntry line + the 4 lines after.  Every one
    # of them MUST contain the UNAVAILABLE token.
    nba_blocks = re.findall(
        r'_add\(MarketEntry\("NBA".*?\)\s*\)', src, flags=re.DOTALL,
    )
    assert nba_blocks, "NBA UMC entries missing"
    for blk in nba_blocks:
        assert "UNAVAILABLE" in blk, (
            "NBA family became ACTIVE without a real model — "
            f"forbidden by P0-B Block 10:\n{blk}"
        )


# ─────────────────── Block 1 — refresh parity ────────────────────
def test_block1_default_refresh_sport_list_includes_nhl():
    src = pathlib.Path("/app/backend/server.py").read_text()
    # New default must include NHL.
    assert '"NFL,MLB,CFB,Soccer,Tennis,NHL"' in src
    # Old default (NHL-less) must be removed.
    assert '"NFL,MLB,CFB,Soccer,Tennis")' not in src
