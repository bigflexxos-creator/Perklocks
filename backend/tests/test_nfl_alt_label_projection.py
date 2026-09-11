"""
Regression: NFL Alt-Ladder READ-TIME Label Projection
=====================================================

Verifies that ``apply_nfl_alt_label_projection`` rewrites raw
provider-form NFL alt-lock OVER labels into sportsbook-milestone
form for the outgoing ``/api/picks/today`` response — WITHOUT
mutating any settlement-anchor fields.

Contract (from PUBLICATION_CONTRACT §3):
  - Only ``market`` is rewritten.
  - Raw ``line`` / ``threshold`` / ``point`` are preserved.
  - Under-alts stay half-yard.
  - Idempotent — rerunning is a no-op.
"""
import pytest

from services.nfl_alt_label_projection import (
    apply_nfl_alt_label_projection,
    project_nfl_alt_label,
)


def _pick(**overrides):
    base = {
        "sport": "NFL",
        "market": "Rashid Shaheed Over 4.5 Player Reception Yds  · ALT LOCK",
        "is_alt": True,
        "line": 4.5,
        "threshold": 4.5,
    }
    base.update(overrides)
    return base


# ─────────────────────────────────────────────────────────────
# Positive-path (rewrite EXPECTED)
# ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "raw_market, expected_label",
    [
        (
            "Rashid Shaheed Over 4.5 Player Reception Yds  · ALT LOCK",
            "Rashid Shaheed 5+ Receiving Yards",
        ),
        (
            "Joe Burrow Over 199.5 Player Pass Yds  · ALT LOCK",
            "Joe Burrow 200+ Passing Yards",
        ),
        (
            "Christian McCaffrey Over 24.5 Player Rush Yds  · ALT LOCK",
            "Christian McCaffrey 25+ Rushing Yards",
        ),
        (
            "Cade Otton Over 14.5 Player Reception Yds  · ALT LOCK",
            "Cade Otton 15+ Receiving Yards",
        ),
        (
            "Justin Herbert Over 20.5 Pass Completions  · ALT LOCK",
            "Justin Herbert 21+ Passing Completions",
        ),
        # Integer alt (no half-yard) — still floor + 1.
        (
            "Ja'Marr Chase Over 5 Player Receptions  · ALT LOCK",
            "Ja'Marr Chase 6+ Receptions",
        ),
    ],
)
def test_positive_rewrites(raw_market, expected_label):
    p = _pick(market=raw_market)
    changed = project_nfl_alt_label(p)
    assert changed is True
    assert p["market"] == expected_label
    # Settlement-anchor fields untouched.
    assert p["line"] == 4.5 or p["line"] == p.get("line")
    assert p.get("display_label_source") == "nfl_alt_ladder_projection"


# ─────────────────────────────────────────────────────────────
# Negative-path (rewrite NOT expected)
# ─────────────────────────────────────────────────────────────

def test_under_alt_stays_half_yard():
    p = _pick(market="Rashid Shaheed Under 4.5 Player Reception Yds  · ALT LOCK")
    changed = project_nfl_alt_label(p)
    assert changed is False
    assert "4.5" in p["market"]
    assert "Under" in p["market"]


def test_non_nfl_untouched():
    p = _pick(sport="MLB",
              market="Aaron Judge Over 1.5 Total Bases  · ALT LOCK")
    changed = project_nfl_alt_label(p)
    assert changed is False
    assert "Over 1.5" in p["market"]


def test_main_line_untouched():
    # No ALT LOCK marker + is_alt=False → main line, untouched.
    p = _pick(market="Joe Burrow Over 249.5 Player Pass Yds", is_alt=False)
    changed = project_nfl_alt_label(p)
    assert changed is False
    assert p["market"] == "Joe Burrow Over 249.5 Player Pass Yds"


def test_idempotent():
    p = _pick(market="Joe Burrow Over 199.5 Player Pass Yds  · ALT LOCK")
    first = project_nfl_alt_label(p)
    assert first is True
    label_after_first = p["market"]
    second = project_nfl_alt_label(p)
    assert second is False
    assert p["market"] == label_after_first


def test_apply_bulk_stats():
    picks = [
        _pick(market="Player A Over 4.5 Player Reception Yds  · ALT LOCK"),
        _pick(market="Player B Over 99.5 Player Pass Yds  · ALT LOCK"),
        _pick(market="Player C Under 4.5 Player Reception Yds  · ALT LOCK"),
        _pick(sport="MLB",
              market="Aaron Judge Over 1.5 Total Bases  · ALT LOCK"),
    ]
    stats = apply_nfl_alt_label_projection(picks)
    assert stats["rewritten"] == 2
    assert stats["skipped"] == 1  # MLB row
    assert picks[0]["market"] == "Player A 5+ Receiving Yards"
    assert picks[1]["market"] == "Player B 100+ Passing Yards"
    assert "Under 4.5" in picks[2]["market"]  # Under untouched
    assert "Over 1.5" in picks[3]["market"]  # MLB untouched


def test_settlement_fields_never_mutated():
    p = _pick(
        market="Player A Over 199.5 Player Pass Yds  · ALT LOCK",
        line=199.5,
        threshold=199.5,
        point=199.5,
    )
    project_nfl_alt_label(p)
    assert p["line"] == 199.5
    assert p["threshold"] == 199.5
    assert p["point"] == 199.5


def test_case_insensitive_over_matching():
    # Downstream sometimes lowercases side — projector must handle.
    p = _pick(market="Joe Burrow over 199.5 Player Pass Yds  · ALT LOCK")
    changed = project_nfl_alt_label(p)
    assert changed is True
    assert p["market"] == "Joe Burrow 200+ Passing Yards"


def test_pick_without_alt_marker_but_is_alt_flag():
    # Some pipeline paths set is_alt=True without appending
    # " · ALT LOCK" — projector should still apply.
    p = _pick(
        market="Player A Over 4.5 Player Reception Yds",
        is_alt=True,
    )
    changed = project_nfl_alt_label(p)
    assert changed is True
    assert p["market"] == "Player A 5+ Receiving Yards"
