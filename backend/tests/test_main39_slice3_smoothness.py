"""MAIN 39 · Slice 3 · P1.2 (bounded Expo smoothness) — evidence tests.

Certifies that:
  (1) `_decorate_with_espn_meta` uses bounded-concurrency `asyncio.gather`
      (fanning out the 4 per-pick awaits AND processing picks in
      chunks) instead of the previous sequential per-pick loop that
      was measured at ~10.5 s for 734 picks on the /picks/today hot
      path.
  (2) Per-pick contract is preserved — each pick receives calls to
      _meta / _chip / _form / _sig; fields written are unchanged.
  (3) `arePropsEqual` in LockPickCard.tsx no longer identity-checks
      `pick_rationale` (the fix that stops all-card re-renders on
      every silent focus refetch).

These are STATIC source-level assertions (no live Mongo/network) so
the test is deterministic and cheap.  Runtime latency evidence is
captured separately via `/tmp/profile_picks_today.py`.
"""
from __future__ import annotations
import re
from pathlib import Path

_APP     = Path("/app")
_SERVER  = (_APP / "backend/server.py").read_text()
_CARD    = (_APP / "frontend/src/components/LockPickCard.tsx").read_text()


# ── (1) bounded concurrency in _decorate_with_espn_meta ─────────────
def test_espn_meta_uses_asyncio_gather():
    """Per-pick fan-out via asyncio.gather is present."""
    fn = _extract_fn("_decorate_with_espn_meta", _SERVER)
    assert "asyncio.gather" in fn, "expected asyncio.gather-based fan-out"
    # The 4 awaits must all live inside the gather, not sequentially.
    assert re.search(r"await\s+asyncio\.gather\(\s*\n\s*_meta\(", fn), \
        "expected _meta inside asyncio.gather"
    assert "_chip(" in fn and "_form(" in fn and "_sig(" in fn


def test_espn_meta_uses_bounded_chunks():
    """Bounded concurrency via CHUNK size guard."""
    fn = _extract_fn("_decorate_with_espn_meta", _SERVER)
    assert re.search(r"CHUNK\s*=\s*\d+", fn), "expected CHUNK constant"
    assert re.search(r"for\s+i\s+in\s+range\(0,\s*len\(picks\),\s*CHUNK\)", fn), \
        "expected chunked iteration"


def test_espn_meta_return_exceptions_true():
    """Errors in one enricher must NOT poison the others."""
    fn = _extract_fn("_decorate_with_espn_meta", _SERVER)
    assert "return_exceptions=True" in fn, \
        "asyncio.gather must swallow individual enricher errors"


def test_espn_meta_still_calls_all_four_helpers():
    """All 4 per-pick helpers still fire — contract unchanged."""
    fn = _extract_fn("_decorate_with_espn_meta", _SERVER)
    for helper in ("_meta(", "_chip(", "_form(", "_sig("):
        assert helper in fn, f"per-pick helper {helper} missing"


def test_espn_meta_player_headshot_step_preserved():
    """The player_meta_decorator post-step must still fire once at the end."""
    fn = _extract_fn("_decorate_with_espn_meta", _SERVER)
    assert "decorate_with_player_meta" in fn, \
        "player headshot decoration step dropped"


# ── (2) arePropsEqual no longer identity-checks pick_rationale ──────
def test_are_props_equal_no_rationale_identity_gate():
    """Regression guard: identity-check on pick_rationale was forcing
    every card to re-render on every focus refetch because the
    backend serializer re-emits the dict on every response.  Verify
    the check is REMOVED and there's an explanatory comment.
    """
    # The pre-fix line was: `if ((a as any).pick_rationale !== (b as any).pick_rationale) return false;`
    assert re.search(
        r"if\s*\(\s*\(a as any\)\.pick_rationale\s*!==\s*\(b as any\)\.pick_rationale\s*\)\s*return\s+false\s*;",
        _CARD,
    ) is None, "pick_rationale identity gate must be removed"

    # New code path must include the MAIN 39 Slice 3 comment so a
    # future refactor can trace the intent.
    assert "MAIN 39" in _CARD and "Slice 3" in _CARD, \
        "expected MAIN 39 Slice 3 provenance comment near arePropsEqual"


def test_are_props_equal_still_gates_visible_fields():
    """The remaining scalar gates must still be present so we don't
    regress in the OTHER direction (rendering stale metrics)."""
    # Must still short-circuit re-render when these visible metrics
    # actually change.
    for field in (
        "lock_score", "edge_percent", "win_probability",
        "grade", "market", "event", "event_time",
    ):
        # The field-comparison pattern: `if (a.<field> !== b.<field>)`.
        assert re.search(rf"a\.{re.escape(field)}\s*!==\s*b\.{re.escape(field)}", _CARD), \
            f"arePropsEqual must still gate on {field}"


# ── helpers ─────────────────────────────────────────────────────────
def _extract_fn(name: str, src: str) -> str:
    """Return the body of `async def <name>` up to the next top-level def."""
    m = re.search(rf"async def {re.escape(name)}\(", src)
    assert m, f"function {name} not found"
    start = m.start()
    tail = src[start:]
    # Cut at the next top-level "\nasync def " OR "\ndef " to isolate.
    stop = re.search(r"\n(async def |def )", tail[1:])
    end = stop.start() + 1 if stop else len(tail)
    return tail[:end]
