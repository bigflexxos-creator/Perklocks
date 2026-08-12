"""Pre-Magic Certification — orchestrator.

Assembles the complete certification matrix by running every check
in ``checks.py`` against the live pod DB.

**Zero side effects.**  Every DB access is a read; no collections are
mutated.  Magic remains ``NOT_WIRED`` and is never touched.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Optional

from .states import (
    CertificationState as CS,
    EvidenceType as ET,
    CertificationEntry,
    CertificationMatrix,
)
from .market_catalog import (
    PLAYER_SPORTS, TEAM_SPORTS,
    PLAYER_MARKETS, TEAM_MARKETS,
    player_markets_for, team_markets_for,
)
from . import checks


DEFAULT_REPORT_PATH = "/tmp/pre_magic_certification.json"


async def build_certification_matrix(
    db,
    *,
    live_pick_sample: int = 25,
    market_sample: int = 200,
) -> CertificationMatrix:
    """Build the complete certification matrix for Pre-Magic readiness.

    Read-only.  The caller receives a ``CertificationMatrix`` with:

    * Every ``PLAYER_MARKETS`` row → PLAYER_HISTORY entry (+atoms).
    * Every ``TEAM_MARKETS`` row → TEAM_HISTORY entry.
    * One H2H entry per ``TEAM_SPORTS`` sport.
    * The nine cross-cutting invariants (exact-threshold,
      distributions, missing≠0, as-of, identity, market
      normalization, tennis context, market readiness, soccer
      producer integrity, model readiness).
    * ``live_pick_sample`` LIVE_PICK_REACHABILITY entries (one per
      real published pick).

    The matrix's ``magic_consumption`` stays ``NOT_WIRED``.
    ``ready_for_magic`` is computed by ``rollup()``.
    """
    m = CertificationMatrix()

    # ── Player history × market ────────────────────────────────
    for pm in PLAYER_MARKETS:
        e = await checks.certify_player_history_market(db, pm)
        m.add(e)

    # ── Team history × market ─────────────────────────────────
    for tm in TEAM_MARKETS:
        e = await checks.certify_team_history_market(db, tm)
        m.add(e)

    # ── H2H per team sport ────────────────────────────────────
    for sport in TEAM_SPORTS:
        m.add(await checks.certify_h2h(db, sport))

    # ── Cross-cutting engine invariants (in-memory) ───────────
    m.add(checks.certify_exact_threshold_engine())
    m.add(checks.certify_distribution_engine())
    m.add(checks.certify_missing_not_zero())
    m.add(checks.certify_market_normalization(PLAYER_MARKETS + TEAM_MARKETS))

    # ── DB-backed cross-cutting checks ────────────────────────
    m.add(await checks.certify_as_of_safety(db))
    m.add(await checks.certify_identity(db))
    for e in await checks.certify_pick_identity_tagging(db):
        m.add(e)
    m.add(await checks.certify_tennis_context(db))
    m.add(await checks.certify_market_readiness(db, sample_size=market_sample))
    m.add(await checks.certify_soccer_producer_integrity(db))
    m.add(await checks.certify_model_readiness(db, sample_size=market_sample))

    # ── Live pick reachability ────────────────────────────────
    live = await checks.certify_live_pick_reachability(
        db, sample_size=live_pick_sample)
    for e in live:
        m.add(e)

    # ── Consumer visibility guardrails (§15) ──────────────────
    m.magic_consumption = CS.NOT_WIRED.value
    m.lock_score_consumption = "UNCHANGED"

    # ── Findings summary ──────────────────────────────────────
    _emit_findings(m)

    m.rollup()
    return m


def _emit_findings(m: CertificationMatrix) -> None:
    """Aggregate high-signal findings from the matrix into
    ``m.findings`` so the operator gets a short, human-scannable
    summary alongside the JSON."""
    fail_by_evidence: dict[str, int] = {}
    unavailable_by_sport: dict[str, int] = {}
    for e in m.entries:
        if e.certification_status == CS.FAIL.value:
            fail_by_evidence[e.evidence_type] = (
                fail_by_evidence.get(e.evidence_type, 0) + 1)
            m.add_finding("FAIL", f"FAIL_{e.evidence_type}",
                            f"{e.sport}.{e.market} — {e.drop_reason}: {e.detail}")
        elif e.certification_status == CS.UNAVAILABLE.value:
            unavailable_by_sport[e.sport] = (
                unavailable_by_sport.get(e.sport, 0) + 1)
    if unavailable_by_sport:
        m.add_finding("INFO", "UNAVAILABLE_SUMMARY",
                        "sports with unavailable evidence "
                        "(expected per handoff classification)",
                        {"by_sport": unavailable_by_sport})
    # Explicit §15 guardrail — always emit.
    m.add_finding(
        "INFO", "MAGIC_NOT_WIRED",
        "Magic 2.0 remains NOT_WIRED — certification does not "
        "authorise consumer wiring. Downstream promotion requires "
        "an explicit, audited follow-up.")


def write_certification_report(
    m: CertificationMatrix,
    *,
    path: str = DEFAULT_REPORT_PATH,
) -> str:
    """Write the matrix to disk as ``pre_magic_certification.json``.

    Returns the absolute path.  Ignores IO errors — the endpoint
    still returns the matrix in JSON form regardless.
    """
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(m.to_dict(), fh, indent=2, sort_keys=False)
    except Exception:
        pass
    return os.path.abspath(path)


__all__ = [
    "build_certification_matrix",
    "write_certification_report",
    "DEFAULT_REPORT_PATH",
]
