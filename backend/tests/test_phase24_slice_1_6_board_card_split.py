"""SLICE 1.6 — Lock Board Card Render Split (perf invariants)
================================================================

Static contract that guarantees the LockBoardCard list row stays
lightweight over time. Enforced against the actual TypeScript sources
so any regression is caught in CI even if the runtime perf harness is
skipped on cold-cache days.

Invariants:

  1. LOCK_BOARD_CARD_EXPORTED — a formal `LockBoardCard` symbol MUST
     exist in `/app/frontend/src/components/LockBoardCard.tsx` so the
     board list rendering path is decoupled from the deep breakdown
     path (Slice 8's future home).

  2. HOME_BOARD_USES_LOCK_BOARD_CARD — the home tab
     (`app/(tabs)/index.tsx`) MUST render `<LockBoardCard>` and not the
     legacy `<LockPickCard>`. Prevents the deep card from being wired
     back into the board flow.

  3. LOCK_PICK_CARD_MODALS_LAZY_MOUNTED — every `<Modal>` inside
     LockPickCard is wrapped in a `{stateVar && <Modal>}` gate so a
     100-card slate never sits on 100+ idle Modal instances.

  4. MATCHUP_BADGE_SUPPORTS_PRELOAD — `MatchupGradeBadge` accepts a
     `preloaded` prop AND the useEffect fetch bails when it is set;
     the home board consumes it via `pick.matchup_grade` so the badge
     never fires a per-card `GET /api/picks/{id}/matchup`.

  5. HOME_PASSES_PRELOAD — the LockPickCard call site passes the
     `preloaded=` prop so the elimination is actually wired.
"""
from __future__ import annotations
import os, re, pytest

_FRONTEND = "/app/frontend"


def _read(p: str) -> str:
    fp = os.path.join(_FRONTEND, p)
    if not os.path.exists(fp):
        pytest.skip(f"{p} missing")
    with open(fp, "r") as f:
        return f.read()


def test_slice_1_6_lock_board_card_exported():
    src = _read("src/components/LockBoardCard.tsx")
    assert re.search(r"export\s*\{\s*LockPickCard\s+as\s+LockBoardCard\s*\}",
                       src), (
        "LockBoardCard.tsx must re-export LockPickCard under the LockBoardCard "
        "name — Slice 1.6 alias contract."
    )


def test_slice_1_6_home_board_uses_lock_board_card():
    src = _read("app/(tabs)/index.tsx")
    assert "from \"@/src/components/LockBoardCard\"" in src, (
        "Home tab must import from LockBoardCard, not LockPickCard directly."
    )
    assert "<LockBoardCard" in src, (
        "Home tab must render <LockBoardCard> in its FlatList renderItem."
    )
    # And must NOT still reference the old LockPickCard tag in the JSX.
    assert "<LockPickCard" not in src, (
        "Home tab still renders <LockPickCard>; Slice 1.6 requires "
        "switching to <LockBoardCard>."
    )


def test_slice_1_6_lock_pick_card_modals_lazy_mounted():
    src = _read("src/components/LockPickCard.tsx")
    # Every `<Modal` tag inside this file must be preceded by a
    # `{stateVar && (` gate on the same or previous few lines. Strategy:
    # find each `<Modal` occurrence and confirm one of the two lines
    # above starts with `{` and contains ` && (`.
    lines = src.splitlines()
    offenders = []
    for i, line in enumerate(lines):
        if re.search(r"<\s*Modal\b", line):
            window = "\n".join(lines[max(0, i - 3): i])
            if not re.search(r"\{\s*\w+\s*&&\s*\(", window):
                offenders.append((i + 1, line.strip()))
    assert not offenders, (
        f"LockPickCard has {len(offenders)} un-gated <Modal> "
        f"instance(s): {offenders[:3]} — Slice 1.6 requires lazy mount."
    )


def test_slice_1_6_matchup_badge_supports_preload():
    src = _read("src/components/MatchupGradeBadge.tsx")
    assert re.search(r"preloaded\s*[:?]", src), (
        "MatchupGradeBadge must accept a `preloaded` prop (Slice 1.6)."
    )
    # The fetch effect must bail early when preloaded is set. Look for a
    # `if (_seed) return;` (our impl) OR any `if (preloaded)` short-circuit
    # inside the useEffect body.
    ue_match = re.search(r"useEffect\s*\(\s*\(\)\s*=>\s*\{([\s\S]*?)\}\s*,\s*\[",
                          src)
    assert ue_match, "MatchupGradeBadge must retain its fetch useEffect."
    body = ue_match.group(1)
    assert re.search(r"if\s*\(\s*(_seed|preloaded)\s*\)\s*return", body), (
        "MatchupGradeBadge useEffect must short-circuit the fetch when "
        "`preloaded` (or its seed) is provided — Slice 1.6."
    )


def test_slice_1_6_home_passes_preload_to_matchup_badge():
    src = _read("src/components/LockPickCard.tsx")
    # Must construct a preloaded prop from pick.matchup_grade at the
    # MatchupGradeBadge call site.
    call_re = re.compile(r"<MatchupGradeBadge\b([\s\S]*?)/>")
    matches = call_re.findall(src)
    assert matches, "LockPickCard must render <MatchupGradeBadge>."
    passed = any("preloaded" in m for m in matches)
    assert passed, (
        "LockPickCard <MatchupGradeBadge> call must pass a `preloaded=` "
        "prop derived from pick.matchup_grade — Slice 1.6."
    )
    # Guard: at least one call site must derive from pick.matchup_grade.
    joined = "\n".join(matches)
    assert "matchup_grade" in joined, (
        "preloaded prop must be built from pick.matchup_grade (the "
        "Slice 1.2B whitelisted field)."
    )
