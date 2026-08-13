"""Rogue NFL runtime guard foundation (Block 2B.1A §34).

This module is a **foundation** — it identifies the approved NFL
runtime paths and provides a scanning helper that verifies no
UNAPPROVED module publishes canonical picks directly.  Full runtime
enforcement lands in Block 2B.1B.

Approved runtime paths
──────────────────────
    * ``sports_engine._props_picks_from_event`` — the ONE authoritative
      NFL candidate generator inside the shared multi-sport pipeline.
    * ``nfl_atd_engine`` — SPECIALIZED_SEPARATE_ENGINE per §3 (ATD
      remains authoritative but MUST use canonical identity +
      publication).
    * ``nfl_safe_engine`` / ``nfl_game_engine`` — LEGACY compat
      routes (``/api/nfl/safe-bets``, ``/api/nfl/games/*``) that must
      NOT publish canonical picks — they are read-only APIs.

Approved publishers
───────────────────
    * ``services.canonical_publication`` (canonical write path).
    * ``services.board_projection_service.BoardProjectionService``.

Anything else that (a) computes a `lock_score` OR (b) inserts into
``picks`` OR ``settlement_events`` OR ``canonical_picks`` for
sport=NFL is a candidate rogue runtime.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


APPROVED_NFL_RUNTIMES: set[str] = {
    "sports_engine._props_picks_from_event",
    "sports_engine.fetch_nfl_picks",
    "nfl_atd_engine.predict_player_atd",
    "nfl_atd_engine.atd_leaderboard",
}

APPROVED_NFL_PUBLISHERS: set[str] = {
    "services.canonical_publication",
    "services.board_projection_service",
    "services.publication_helpers",
    "services.settlement_service",     # settlement writer
}

# Legacy read-only routes that MUST NOT publish canonical picks —
# they only read from ``games`` / ``player_game_actuals`` and return
# HTTP responses.
LEGACY_READ_ONLY_ROUTES: set[str] = {
    "nfl_safe_engine.compute_safe_bets",
    "nfl_game_engine.predict_game",
    "nfl_game_engine.safe_alt_locks",
    "nfl_game_engine.team_strength_leaderboard",
}


@dataclass
class RogueRuntimeFinding:
    """One finding from the guard scan."""
    file: str
    line: int
    category: str          # "direct_board_write" | "sport_nfl_insert" | ...
    snippet: str


# Grep patterns that indicate a NFL board / picks write outside the
# approved publishers.  Kept intentionally conservative — the guard's
# false-positive rate must be low so it's usable in CI.
_ROGUE_PATTERNS = [
    ("direct_picks_insert",
     re.compile(r"\.picks\.insert(_one|_many)?\s*\(", re.IGNORECASE)),
    ("direct_picks_replace",
     re.compile(r"\.picks\.(replace|update)_one\s*\(", re.IGNORECASE)),
    ("direct_board_projection_write",
     re.compile(r"BoardProjectionService\.\w+\s*\(.*write", re.IGNORECASE)),
    ("direct_canonical_picks_write",
     re.compile(r"\.canonical_picks\.(insert|update|replace)", re.IGNORECASE)),
]

# Files/folders that are inherently allowed to write to picks /
# canonical_picks — the publishers themselves + settlement + the
# shared multi-sport pick pipeline (which is not NFL-specific).
_ALLOWED_WRITER_MODULES = {
    "services/canonical_publication.py",
    "services/publication_helpers.py",
    "services/settlement_service.py",
    "services/board_projection_service.py",
    "services/canonical_board_source.py",
    "services/active_registry.py",
    "settlement_service.py",
    "sports_engine.py",         # the shared candidate-emission runtime
    "server.py",                # top-level orchestration
    "routes/",                  # HTTP routes may enrich picks read-only
    # Shared multi-sport pick pipeline (NOT NFL-specific — writes for
    # all sports as part of the ingest/refresh/settlement flow).
    "pick_validator.py",
    "pick_enrichment.py",
    "closing_line_snapshotter.py",
    "services/pick_refresh_orchestrator.py",
    "services/pick_validator.py",
    "services/pick_enrichment.py",
    # Historical / migration scripts (one-shot ops, not runtime).
    "scripts/",
    "migrations/",
}


def _is_writer_allowed(rel_path: str) -> bool:
    for allowed in _ALLOWED_WRITER_MODULES:
        if rel_path == allowed or rel_path.startswith(allowed):
            return True
    return False


def verify_no_rogue_nfl_runtime(
    backend_root: str = "/app/backend",
    *,
    include_tests: bool = False,
) -> list[RogueRuntimeFinding]:
    """Scan the codebase for rogue NFL board writers.

    Returns a list of findings.  Empty list = no rogue runtime
    detected under the current patterns.  This function is
    intentionally CI-friendly (returns findings for the test to
    assert on rather than raising).
    """
    findings: list[RogueRuntimeFinding] = []
    root = Path(backend_root)
    if not root.exists():
        return findings
    for py in root.rglob("*.py"):
        rel = str(py.relative_to(root))
        if "__pycache__" in rel:
            continue
        if not include_tests and (rel.startswith("tests/") or "test_" in rel.rsplit("/", 1)[-1]):
            continue
        if _is_writer_allowed(rel):
            continue
        try:
            src = py.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        # Only inspect NFL-touching files to keep the scan focused.
        if "nfl" not in src.lower():
            continue
        for lineno, line in enumerate(src.splitlines(), start=1):
            for category, pat in _ROGUE_PATTERNS:
                if pat.search(line):
                    findings.append(RogueRuntimeFinding(
                        file=rel, line=lineno,
                        category=category,
                        snippet=line.strip()[:180],
                    ))
    return findings


__all__ = [
    "APPROVED_NFL_RUNTIMES",
    "APPROVED_NFL_PUBLISHERS",
    "LEGACY_READ_ONLY_ROUTES",
    "RogueRuntimeFinding",
    "verify_no_rogue_nfl_runtime",
]
