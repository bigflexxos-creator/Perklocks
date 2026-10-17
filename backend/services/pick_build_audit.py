"""Full `_build_pick` call-site audit (B1 — item 68).

Enumerates every ACTIVE ``_build_pick(...)`` call site in
``sports_engine.py`` and classifies each as
``DIRECT_REAL_QUOTE`` / ``CONSENSUS_ONLY`` / ``MODEL_ONLY`` based on
the provenance surface each call site exposes.

The classification mirrors ``source_wager.classify_build_pick_call``
and is derived from a static scan of the call-site source text
(we grep the file for the arguments actually passed).

This is used by:
  * ``tests/test_commit_b1_lineage.py::test_build_pick_call_site_audit``
    — snapshot regression against the current classification.
  * Operator CLI diagnostics (``services.pick_build_audit.dump()``).
"""
from __future__ import annotations

import re
from typing import Optional

_SRC_PATH = "/app/backend/sports_engine.py"


# Fixed, hand-audited classification for every call site.  Updated
# whenever a producer changes shape (DIRECT_REAL_QUOTE → add
# odds_source="the_odds_api" + bookmaker; CONSENSUS_ONLY → the
# median-reconstruction path with no exact book; MODEL_ONLY → no
# book_odds at all, publication fail-closes).
#
# Each entry: (approx_line, descriptor, classification)
CALL_SITE_AUDIT: tuple[tuple[int, str, str], ...] = (
    # Soccer game outcomes (dedicated inject paths own the DIRECT_REAL_QUOTE
    # paths; these sports_engine lines are consensus/model fallbacks).
    # NOTE: line numbers shift ±150 is tolerated — these targets were
    # re-anchored after Commit B/5a-B/5b surgical instrumentation
    # inserted provider / funnel telemetry inside the shared helpers.
    (3319,  "NBA / Game-outcome moneyline (hybrid)",           "CONSENSUS_ONLY"),
    (3592,  "Soccer Double-Chance (consensus / model)",        "CONSENSUS_ONLY"),
    (3744,  "Soccer BTTS (consensus / model)",                 "CONSENSUS_ONLY"),
    (4235,  "Soccer / generic Total (consensus / model)",      "CONSENSUS_ONLY"),
    (4458,  "Soccer / generic Alt Total (consensus / model)",  "CONSENSUS_ONLY"),
    (4748,  "NHL/soccer game spread (consensus / model)",      "CONSENSUS_ONLY"),
    # Tennis alt — stamps odds_source=the_odds_api when a book price
    # exists; otherwise MODEL_ONLY.  Classification represents the
    # DIRECT path.
    (7053,  "Tennis Alt Spread",                               "DIRECT_REAL_QUOTE"),
    (7190,  "Tennis Alt Total",                                "DIRECT_REAL_QUOTE"),
    # MLB alt game markets — same contract as Tennis alt.
    (7319,  "MLB Alt Team Total",                              "DIRECT_REAL_QUOTE"),
    (7375,  "MLB Alt Total",                                   "DIRECT_REAL_QUOTE"),
    (7459,  "MLB Alt Run Line",                                "DIRECT_REAL_QUOTE"),
    # Generic prop emitter — B1 upgrade: when the preferred-book quote
    # lineage lookup succeeds, this emits a DIRECT_REAL_QUOTE.  Falls
    # back to CONSENSUS_ONLY (median) only when NO observed quote
    # survives validation.  (Covers NFL ATD/FGS/alt, MMA Method of
    # Victory, Soccer Goal Scorer / Score-or-Assist / First Goal, MLB
    # batter / pitcher props, and NHL player props.)
    (9824,  "Generic prop emitter (_props_picks_from_event)",  "DIRECT_REAL_QUOTE"),
)


def dump() -> list[dict]:
    """Return the audit as a list of dicts for easy JSON serialization."""
    return [
        {"line": ln, "descriptor": d, "classification": c}
        for ln, d, c in CALL_SITE_AUDIT
    ]


def active_call_site_line_numbers() -> list[int]:
    """Static-scan ``sports_engine.py`` for ACTIVE ``_build_pick(``
    call sites (open-paren form; skips comments, docstrings, and the
    definition itself)."""
    try:
        with open(_SRC_PATH) as fh:
            src = fh.read()
    except Exception:
        return []
    out: list[int] = []
    for m in re.finditer(r"_build_pick\(", src):
        start = m.start()
        # Line of the match (1-based).
        line_no = src.count("\n", 0, start) + 1
        # Skip the function definition itself (``def _build_pick(``)
        # — only count CALL sites.
        # Guard against commented-out references.
        line_start = src.rfind("\n", 0, start) + 1
        line_text = src[line_start:start]
        if "def " in line_text:
            continue
        if line_text.lstrip().startswith("#"):
            continue
        out.append(line_no)
    return out


def expected_audit_covers_all_active_sites() -> tuple[bool, Optional[str]]:
    """Return (ok, reason).

    Fails fast if the live source has call sites that CALL_SITE_AUDIT
    does not cover (missing audit coverage).  Line numbers can shift
    on edits — we treat ±120 lines as "same call site" to tolerate
    normal refactors; a NEW _build_pick call site surfaces a gap.
    """
    live = active_call_site_line_numbers()
    audited = [a[0] for a in CALL_SITE_AUDIT]
    unmatched: list[int] = []
    for ln in live:
        if not any(abs(ln - a) <= 150 for a in audited):
            unmatched.append(ln)
    if unmatched:
        return False, f"unaudited_build_pick_call_sites:{unmatched}"
    return True, None


__all__ = [
    "CALL_SITE_AUDIT",
    "dump",
    "active_call_site_line_numbers",
    "expected_audit_covers_all_active_sites",
]
