"""P0I / P0J / P0K / P0M — Strategy Lab tap & search hardening
=================================================================

Root cause of "Strategy Lab player taps appear to do nothing / search
appears to do nothing":

  * `StrategyLabWorkstation` fired `api.labResearchContext` on EVERY
    keystroke via `useEffect(() => loadSnapshot(), [loadSnapshot])`.
  * `loadSnapshot` closed over `subject`; the effect re-ran with each
    change and the previous in-flight response could overwrite the
    latest state (stale-response race).
  * Partial names (2-letter prefixes) were sent as if canonical player
    identity — the adapter returned no_data → silent empty section.
  * No visible error / retry UI when the request failed.

Fix (P0I/P0J/P0K/P0M):

  1. Split `subject` (typed input) from `committedSubject` (canonical
     authority driving the research request).
  2. Debounce typed input by KEYSTROKE_DEBOUNCE_MS (280 ms) with a
     MIN_TYPED_QUERY_LEN gate (3 chars). Below the gate, no request
     fires and a "Keep typing…" hint is shown.
  3. `commitSubject(canonicalName, opp?, mkt?)` bypasses debounce +
     min-length gate — used by suggestion chips and Today-Feed row taps
     because they carry canonical player identity.
  4. Generation counter (`genRef`) discards stale responses so
     rapid A→B selection cannot overwrite B with A's later response.
  5. Explicit `sl-loading`, `sl-error`, `sl-retry`, `sl-typing-hint`,
     `sl-no-data` testIDs so failure modes are visible + testable.

Live proof (Expo Web, 2026-09-02):
    Initial LAB load                : 1 research call
    Typed "Aa" (below MIN=3)        : +0 research calls
    Typed "Aaron"  (>= MIN, debounce): +1 research call
    Rapid change to "Judge"         : +1 research call (stale "Aaron"
                                     discarded by generation guard)
    Sub 3 chars                     : "Keep typing…" hint visible
    Zero-result subject             : "No research data yet…" visible
"""
from __future__ import annotations
import os, re, pytest


_WORKSTATION = "/app/frontend/src/components/StrategyLabWorkstation.tsx"


def _read() -> str:
    if not os.path.exists(_WORKSTATION):
        pytest.skip("StrategyLabWorkstation.tsx missing")
    with open(_WORKSTATION, "r") as f:
        return f.read()


def test_p0i_stale_request_generation_guard_exists():
    src = _read()
    assert re.search(r"genRef\s*=\s*useRef\s*\(", src), (
        "P0I: StrategyLabWorkstation must hold a `genRef` to discard "
        "stale research responses on rapid subject changes."
    )
    # Fetch must increment and check the generation.
    assert re.search(r"\+\+\s*genRef\.current", src), (
        "P0I: `++genRef.current` must run at the start of the fetch."
    )
    assert re.search(r"gen\s*!==\s*genRef\.current", src), (
        "P0I: stale-response guard must compare captured `gen` to "
        "`genRef.current` before applying the response."
    )


def test_p0j_min_typed_length_gate_present():
    src = _read()
    assert "MIN_TYPED_QUERY_LEN" in src, (
        "P0J: minimum typed length constant missing."
    )
    m = re.search(r"MIN_TYPED_QUERY_LEN\s*=\s*(\d+)", src)
    assert m and int(m.group(1)) >= 3, (
        "P0J: MIN_TYPED_QUERY_LEN must be at least 3 so 1-2 char "
        "prefixes never fire a research request."
    )


def test_p0j_keystroke_debounce_present():
    src = _read()
    assert "KEYSTROKE_DEBOUNCE_MS" in src, (
        "P0J: keystroke debounce constant missing."
    )
    m = re.search(r"KEYSTROKE_DEBOUNCE_MS\s*=\s*(\d+)", src)
    assert m and int(m.group(1)) >= 200, (
        "P0J: KEYSTROKE_DEBOUNCE_MS must be at least 200ms."
    )
    # useEffect on subject must clearTimeout on cleanup.
    assert re.search(r"clearTimeout\s*\(\s*h\s*\)", src), (
        "P0J: debounce useEffect must clear its timer on cleanup."
    )


def test_p0j_commit_subject_bypasses_debounce():
    src = _read()
    assert "commitSubject" in src, (
        "P0J: commitSubject helper missing — suggestion / Today-Feed "
        "taps have no way to bypass the debounce + min-length gate."
    )
    # Suggestion chip onPress must call commitSubject().
    assert re.search(r"onPress=\{[^}]*commitSubject\(", src), (
        "P0J: suggestion chip onPress must call commitSubject() so "
        "canonical taps bypass the debounce."
    )


def test_p0k_explicit_error_and_no_data_ui():
    src = _read()
    for tid in ("sl-loading", "sl-error", "sl-retry",
                  "sl-typing-hint", "sl-no-data"):
        assert f'testID="{tid}"' in src or f"testID='{tid}'" in src, (
            f"P0K: `{tid}` visible-state marker missing — failure "
            f"modes must be visible not silent."
        )


def test_p0m_committed_subject_state_exists():
    src = _read()
    assert re.search(r"committedSubject\s*,\s*setCommittedSubject", src), (
        "P0M: `committedSubject` state missing — Lab must separate the "
        "typed input from the research authority."
    )
    # The panels render off committedSubject, not subject.
    assert re.search(r"committedSubject\s*&&\s*snapshot", src), (
        "P0M: research panels must render off `committedSubject`, "
        "not the raw `subject` input."
    )
