"""Magic 3A — line extractor deterministic tests."""
from services.magic.line_extractor import (
    extract_line, extract_side, extract_line_with_provenance,
)


def test_over_pattern_yields_exact_line():
    assert extract_line("Mikal Bridges Over 1.5 Assists", "Mikal Bridges") == 1.5
    assert extract_line("Landry Shamet Over 0.5 Rebounds") == 0.5
    assert extract_line("Karl-Anthony Towns Over 2.5 Assists") == 2.5


def test_spread_pattern_preserves_sign():
    assert extract_line("Miami Marlins +1.5 Spread", "Miami Marlins") == 1.5
    # Negative spreads.
    assert extract_line("LAFC -4.5 Spread") == -4.5


def test_total_games_over():
    assert extract_line("Total Games Over 21.5", "Over") == 21.5
    assert extract_line("Total Goals Over 2.5", "Over") == 2.5


def test_moneyline_has_no_line():
    assert extract_line("Miami Marlins Moneyline", "Miami Marlins") is None
    assert extract_line("Sorana Cirstea Moneyline", "Sorana Cirstea") is None
    assert extract_line("Detroit Tigers Moneyline") is None


def test_never_guesses_generic_threshold():
    # No numeric threshold present → None.  NEVER 0.5.
    assert extract_line("Mikal Bridges Anytime Assist") is None
    assert extract_line("Player To Score", "Player") is None


def test_alt_line_preserved_exactly():
    # 200+ passing yards / 225+ passing yards must produce distinct
    # results — never collapsed to a generic default.
    assert extract_line("Player 225+ Passing Yards") == 225.0
    assert extract_line("Player 200+ Passing Yards") == 200.0
    assert extract_line("Player 300+ Passing Yards") == 300.0


def test_side_detection():
    assert extract_side("Over 1.5") == "over"
    assert extract_side("Under 2.5") == "under"
    assert extract_side("+1.5") == "positive_spread"
    assert extract_side("-4.5") == "negative_spread"
    assert extract_side("Miami Marlins Moneyline") is None


def test_structured_line_wins_over_parse():
    r = extract_line_with_provenance(
        "Over 1.5 Assists", "Over", structured_line=2.5)
    assert r["line"] == 2.5
    assert r["line_source"] == "sportsbook_structured"


def test_parse_fallback_marks_provenance_distinctly():
    r = extract_line_with_provenance("Over 1.5 Assists", "Over")
    assert r["line"] == 1.5
    assert r["line_source"] == "selection_parse_fallback"


def test_missing_line_stays_none_never_zero():
    r = extract_line_with_provenance("Player Anytime Assist", "Player")
    assert r["line"] is None
    assert r["line_source"] is None
    # Explicitly NOT zero.
    assert r["line"] != 0
    assert r["line"] != 0.0
