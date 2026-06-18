"""Test the AI scrubber blocks fabricated stat patterns."""
from ai_engine import _scrub_fabrications


def test_scrubber_strips_record():
    txt = (
        "Why This Pick — Fabian Marozsan to Win\n"
        "• Fabian Marozsan 41-6 record on hard courts\n"
        "• Surface fit: 82/100 — strong\n"
        "Risk: variance."
    )
    out = _scrub_fabrications(txt)
    assert "41-6" not in out
    assert "82/100" in out      # real model score survives
    assert "Risk" in out


def test_scrubber_strips_l12_months():
    txt = "• Player has won 39-5 L12 months on this surface"
    assert _scrub_fabrications(txt) == ""


def test_scrubber_strips_hold_rate():
    txt = (
        "• Hold rate on surface: 83%\n"
        "• Surface fit: 90/100 — elite\n"
    )
    out = _scrub_fabrications(txt)
    assert "Hold rate" not in out
    assert "90/100" in out


def test_scrubber_strips_baseball_stats():
    txt = (
        "• Opposing pitcher allows .275 BAA\n"
        "• OPS 0.840 over last 15 games\n"
        "• Form (model): 78/100 — strong\n"
    )
    out = _scrub_fabrications(txt)
    assert ".275 BAA" not in out
    assert "78/100" in out


def test_scrubber_preserves_real_edge():
    """Honest percentages like '+9.31% edge' must survive."""
    txt = "• Market edge: +9.31% vs book implied"
    assert "+9.31%" in _scrub_fabrications(txt)


def test_scrubber_strips_career_record():
    txt = "• Career 12-0 vs left-handed pitching"
    assert _scrub_fabrications(txt) == ""


def test_scrubber_strips_won_n_of_last():
    txt = "• He won 8 of his last 10 matches at this level"
    assert _scrub_fabrications(txt) == ""


def test_scrubber_empty_input():
    assert _scrub_fabrications("") == ""
    assert _scrub_fabrications(None) is None
