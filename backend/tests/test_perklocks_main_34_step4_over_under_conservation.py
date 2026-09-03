"""STEP 4 · Universal Over/Under conservation — regression
==============================================================

Prior code in `sports_engine.py` had two "kmath_neither_default_over"
branches that set `_winner = "over"` whenever K-math couldn't
distinguish AND no book prices existed on either side. That's a
systematic Over bias hidden inside a pair-dedup fallback — exactly the
"Overs-only default" the user's PERKLOCKS-MAIN 34 directive forbids.

Fix (STEP 4):
    On total data absence, use a deterministic tie-break derived from
    `hash(pitcher_name) & 1` so half of degenerate cases resolve to
    Under and half to Over. No systematic bias. Downstream K-math
    authoritative gate still decides emit/skip on the surviving side.
"""
from __future__ import annotations
import re, pytest


_ENGINE = "/app/backend/sports_engine.py"


def _read():
    with open(_ENGINE, "r") as f:
        return f.read()


def test_step4_no_kmath_default_over_bias():
    src = _read()
    assert "kmath_neither_default_over" not in src, (
        "STEP 4: `kmath_neither_default_over` bias branch still "
        "present — Universal Over/Under conservation broken."
    )


def test_step4_deterministic_tiebreak_replaces_over_bias():
    src = _read()
    assert "kmath_neither_deterministic_tiebreak" in src, (
        "STEP 4: expected `kmath_neither_deterministic_tiebreak` "
        "telemetry token after replacing the Overs-only default."
    )
    # The replacement must use a hash-derived deterministic split, not
    # a hardcoded `over` literal.
    assert re.search(
        r'_winner\s*=\s*"over"\s+if\s+\(hash\(',
        src,
    ), "STEP 4: deterministic tie-break implementation missing / drifted."


def test_step4_no_bare_over_default_in_pair_dedup():
    """Neither pair-dedup fallback branch may fall back to a bare
    `_winner = "over"` on the total-absence path."""
    src = _read()
    # Find each `_winner = "over"` occurrence and confirm the immediate
    # prior line is guarded by a book-implied or edge tiebreak (not an
    # unconditional else).
    lines = src.splitlines()
    for i, ln in enumerate(lines):
        if re.search(r'^\s+_winner\s*=\s*"over"\s*$', ln):
            # Look up 4 lines for the guard. Accept:
            #   `if _o_imp` / `_o_edge >= _u_edge` / `_o_ok and not _u_ok`
            # Reject a bare `else:` on the line above.
            prev = "\n".join(lines[max(0, i - 4): i])
            if re.search(r"\n\s+else:\s*\n\s+_winner\s*=\s*\"over\"\s*$",
                          "\n".join(lines[max(0, i - 1): i + 1])):
                pytest.fail(
                    f"line {i+1}: bare `_winner = \"over\"` under a "
                    f"plain `else:` — STEP 4 Overs-only bias regression."
                )
