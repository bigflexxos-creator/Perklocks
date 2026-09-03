"""STEP 3A · Tennis alt-builder canonical context bug — regression
====================================================================

Before this fix, `sports_engine._build_tennis_alt_picks` referenced an
undefined `game` symbol in two places (spread branch and totals
branch). The parameter is `event_payload`. Every call raised a
silently-swallowed `NameError`, so:

    • `mp` (model probability) stayed None
    • the canonical publication barrier stamped `model_line=True`
    • every Tennis alt spread / total pick was rejected before it
      reached the board

That's why Tennis alts disappeared from Locks — a **programming**
error was being converted into a `no pick` signal by broad exception
handling.

Fix (STEP 3A):
  1. Reference `event_payload` (the actual parameter).
  2. Split the `except` chain: `NameError` (programming) is logged at
     ERROR level with an explicit `ALT_MODEL_PROGRAMMING_ERROR`
     telemetry token; any other Exception (real signal miss) stays at
     DEBUG level with `ALT_MODEL_SIGNAL_UNAVAILABLE`.
"""
from __future__ import annotations
import os, re, pytest


_ENGINE = "/app/backend/sports_engine.py"


def _read() -> str:
    with open(_ENGINE, "r") as f:
        return f.read()


def _extract_fn(src: str, name: str) -> str:
    """Return the source of a top-level def by name."""
    m = re.search(rf"^def\s+{re.escape(name)}\s*\(", src, re.MULTILINE)
    assert m, f"function {name!r} not found in {_ENGINE}"
    start = m.start()
    # Find next top-level `def ` or EOF.
    rest = src[m.end():]
    n = re.search(r"\n(?=def\s+\w+\s*\()", rest)
    end = m.end() + (n.start() if n else len(rest))
    return src[start:end]


def test_step3a_tennis_alt_no_undefined_game_reference():
    """The Tennis alt-picks builder must NOT reference `game.get(` —
    the parameter is `event_payload`.  Regression class: undefined-name
    swallowed by broad except → silent zero-pick output."""
    src = _read()
    body = _extract_fn(src, "_build_tennis_alt_picks")
    offenders = re.findall(r"\bgame\.get\(", body)
    assert not offenders, (
        f"_build_tennis_alt_picks still contains {len(offenders)} "
        f"`game.get(...)` reference(s) — the parameter is "
        f"`event_payload`. This is the audited STEP 3A regression."
    )


def test_step3a_tennis_alt_uses_event_payload_for_surface_ctx():
    src = _read()
    body = _extract_fn(src, "_build_tennis_alt_picks")
    # The fixed code reads surface + _ctx from event_payload.
    assert re.search(r"event_payload\.get\([\"']surface[\"']\)", body), (
        "STEP 3A: surface must be read from event_payload, not `game`."
    )
    assert re.search(r"event_payload\.get\([\"']_ctx[\"']\)", body), (
        "STEP 3A: _ctx must be read from event_payload, not `game`."
    )


def test_step3a_programming_error_is_distinct_from_signal_unavailable():
    """NameError must be handled separately from other Exceptions so
    programming regressions surface at ERROR level rather than being
    silently classified as `signal unavailable`."""
    src = _read()
    body = _extract_fn(src, "_build_tennis_alt_picks")
    assert "ALT_MODEL_PROGRAMMING_ERROR" in body, (
        "STEP 3A: telemetry token `ALT_MODEL_PROGRAMMING_ERROR` missing."
    )
    assert "ALT_MODEL_SIGNAL_UNAVAILABLE" in body, (
        "STEP 3A: telemetry token `ALT_MODEL_SIGNAL_UNAVAILABLE` missing."
    )
    # Both branches use explicit `except NameError` (programming) plus
    # a separate broad `except Exception` (signal). At least two of
    # each — one for spreads, one for totals.
    ne_count = len(re.findall(r"except\s+NameError\s+as\s+\w+", body))
    assert ne_count >= 2, (
        f"STEP 3A: expected ≥2 `except NameError` blocks (spread + "
        f"total), found {ne_count}."
    )


def test_step3a_tennis_alt_builder_still_present():
    """Sanity — a global refactor must not delete the function."""
    src = _read()
    assert "def _build_tennis_alt_picks(" in src
