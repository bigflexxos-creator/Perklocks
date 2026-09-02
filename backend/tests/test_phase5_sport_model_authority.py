"""Phase 5 — SPORT MODEL AUTHORITY invariants.

Root-class proofs:

  A1. Every supported (sport, market_family) has ONE canonical
      authority declared (specialists preserved for player props /
      narrower sub-families).
  A2. Two different producers cannot simultaneously claim the same
      canonical authority for the same (sport, market_family).
  A3. UFC + NHL families are registered `MODEL_UNAVAILABLE` and
      fail-closed for publication.
  A4. Wiring proof — the authority tags declared here MUST match
      the `model_source` strings actually stamped by the runtime
      producers in `sports_engine.py` / `services/real_line_scorer_
      ingest.py`.
  A5. Tennis authority: player-name-hash predictive evidence is
      NOT registered as authoritative (only Sackmann engine +
      preserved fair-odds specialist).
  A6. MLB game-total authority = `mlb_shared_run_distribution_v1`
      (proves the Phase-4/§5 Shared Run Distribution is the sole
      canonical authority for MLB run-line / total / team-total).
  A7. NFL Platinum retained for both game markets AND player props
      (specialisation preserved).
"""
from __future__ import annotations
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest

from services.sport_model_authority import (
    AUTHORITY, UNAVAILABLE, get_authority, is_authoritative,
    is_unavailable,
)

REPO_ROOT = pathlib.Path("/app/backend")


# ── A1 — every supported sport declares authorities ─────────────
def test_every_supported_sport_registered():
    for s in ("MLB", "NFL", "CFB", "NBA", "NHL", "Soccer", "Tennis", "UFC"):
        assert s in AUTHORITY, f"missing sport authority: {s}"


def test_every_declared_family_has_canonical_tag():
    for sport, families in AUTHORITY.items():
        for fam, entry in families.items():
            assert "canonical" in entry, f"{sport}.{fam} missing canonical"
            can = entry["canonical"]
            assert isinstance(can, str) and can, \
                f"{sport}.{fam} canonical must be str"
            # preserved_specialists is a tuple (possibly empty).
            assert isinstance(entry.get("preserved_specialists", ()), tuple)


# ── A2 — no cross-family authority collision within one sport ──
def test_no_authority_collision_across_families_same_sport():
    """A canonical authority tag may serve MULTIPLE families in the
    same sport (e.g. MLB shared run distribution serves total +
    run_line + team_total).  What is FORBIDDEN is one tag claiming
    to be authoritative for game-markets while ALSO being a
    preserved specialist for the same family — that would create
    two claim paths.  This test enforces the well-formedness rule."""
    for sport, families in AUTHORITY.items():
        for fam, entry in families.items():
            can = entry.get("canonical")
            spec = entry.get("preserved_specialists") or ()
            assert can not in spec, (
                f"{sport}.{fam}: canonical {can!r} must not also be "
                f"in preserved_specialists")


# ── A3 — UFC + NHL fail-closed ──────────────────────────────────
def test_ufc_families_registered_as_unavailable():
    for fam in ("moneyline", "total"):
        assert is_unavailable("UFC", fam), \
            f"UFC.{fam} must be MODEL_UNAVAILABLE (fail-closed)"


def test_nhl_families_registered_as_unavailable():
    for fam in ("moneyline", "puck_line", "total"):
        assert is_unavailable("NHL", fam), \
            f"NHL.{fam} must be MODEL_UNAVAILABLE (fail-closed)"


def test_unavailable_rejects_any_model_source():
    """Even if a producer stamps a `model_source`, the
    is_authoritative check must return False for UNAVAILABLE
    families — no producer can legitimately claim UFC / NHL
    authority yet."""
    for sport in ("UFC", "NHL"):
        for fam in AUTHORITY.get(sport, {}):
            assert is_authoritative(sport, fam,
                                     "any_producer_tag") is False


# ── A4 — wiring proof: declared authorities match runtime tags ─
def test_mlb_shared_run_distribution_authority_wired_in_sports_engine():
    src = (REPO_ROOT / "sports_engine.py").read_text(encoding="utf-8")
    # The Shared Run Distribution provenance tag must appear in the
    # engine (proves the runtime actually stamps the declared tag).
    assert 'model_source"] = "mlb_shared_run_distribution_v1"' in src


def test_cfb_sp_game_model_authority_wired_in_sports_engine():
    src = (REPO_ROOT / "sports_engine.py").read_text(encoding="utf-8")
    assert 'model_source"] = "cfb_sp_game_model"' in src


def test_platinum_nfl_authority_module_exists():
    """Platinum NFL is claimed as the canonical NFL game-market
    authority — verify the module directory actually exists."""
    platinum_dir = REPO_ROOT / "services" / "platinum_nfl"
    assert platinum_dir.exists() and platinum_dir.is_dir()


# ── A5 — Tennis: player-name-hash NOT authoritative ────────────
def test_tennis_authority_is_real_data_only():
    entry = get_authority("Tennis", "moneyline")
    assert entry is not None
    assert entry["canonical"] == "tennis_sackmann_engine"
    # Player-name-hash / reputation-based tags MUST NOT be listed.
    banned = {"player_name_hash", "tennis_name_hash",
              "tennis_reputation_baseline"}
    all_tags = {entry["canonical"], *entry.get("preserved_specialists", ())}
    for b in banned:
        assert b not in all_tags


def test_tennis_sportsbook_baseline_not_authoritative():
    """The registry must NOT list any 'sportsbook implied' /
    'book_baseline' tag as authoritative — that would let the
    provider price masquerade as independent model evidence."""
    for fam, entry in AUTHORITY["Tennis"].items():
        tags = {entry["canonical"], *entry.get("preserved_specialists", ())}
        for t in tags:
            assert "book" not in t.lower() or "book_odds" not in t.lower()
            assert "implied" not in t.lower()
            assert "baseline" not in t.lower() or t == "tennis_fair_odds_engine"


# ── A6 — MLB coherent scoring distribution ─────────────────────
def test_mlb_totals_run_line_team_total_share_shared_run_distribution():
    """All MLB run-related families must query the ONE shared
    distribution so that ML / Run Line / Total / Team Total are
    mathematically coherent."""
    families = ("run_line", "total", "team_total")
    for fam in families:
        entry = get_authority("MLB", fam)
        assert entry is not None, f"MLB.{fam} unregistered"
        assert entry["canonical"] == "mlb_shared_run_distribution_v1", \
            f"MLB.{fam} not routed through shared run distribution"


def test_mlb_pitcher_and_hitter_props_have_specialised_authorities():
    """Preserved specialisation: MLB pitcher K / outs / hitter
    props run their own specialist engines, NOT the shared run
    distribution."""
    families = (
        "pitcher_strikeouts", "pitcher_outs",
        "hitter_hits", "hitter_home_runs", "hitter_total_bases",
    )
    for fam in families:
        entry = get_authority("MLB", fam)
        assert entry["canonical"] != "mlb_shared_run_distribution_v1"


# ── A7 — NFL Platinum both game markets and player props ──────
def test_nfl_platinum_covers_game_and_player_families():
    game_fams = ("moneyline", "spread", "total")
    prop_fams = ("player_passing_yards", "player_rushing_yards",
                 "player_receiving_yards", "player_receptions")
    for fam in game_fams:
        assert get_authority("NFL", fam)["canonical"] == \
            "platinum_nfl_game_sim"
    for fam in prop_fams:
        assert get_authority("NFL", fam)["canonical"] == \
            "platinum_nfl_prop"


# ── is_authoritative gate ──────────────────────────────────────
def test_is_authoritative_true_for_canonical_tag():
    assert is_authoritative("MLB", "total",
                              "mlb_shared_run_distribution_v1")


def test_is_authoritative_true_for_preserved_specialist():
    assert is_authoritative("MLB", "hitter_home_runs", "mlb_bvp")


def test_is_authoritative_false_for_synthesized_source():
    """`poisson_from_main_total` is exactly the Phase-4 rejected
    synthesized-line source. It must not be authoritative."""
    assert is_authoritative("Soccer", "total",
                              "poisson_from_main_total") is False


def test_is_authoritative_fails_closed_on_unregistered_pair():
    """FAIL-CLOSED: an unregistered (sport, market_family) MUST NOT
    receive production publication authority — a typoed / legacy /
    synthetic / unsupported family cannot bypass the registry."""
    assert is_authoritative("MLB", "brand_new_family_2027",
                              "some_new_producer") is False
    # An unregistered SPORT altogether also fails-closed.
    assert is_authoritative("Cricket", "moneyline",
                              "cricket_authority_v1") is False


def test_unregistered_pair_can_still_be_inspected_by_lab():
    """Research / Lab surfaces may still SEE the pair — the fail-
    closed rule applies only to production authority.  is_registered
    returns False so callers can classify the row as SHADOW."""
    from services.sport_model_authority import is_registered
    assert is_registered("MLB", "brand_new_family_2027") is False
    assert is_registered("MLB", "total") is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
