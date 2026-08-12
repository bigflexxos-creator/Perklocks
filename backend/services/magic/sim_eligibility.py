"""MAGIC 3B.1 — Simulator eligibility inventory + observability.

Explicit sport → market → simulator routing.  This lets the coverage
report separate:

  * ELIGIBLE  — sport+market pair the simulator supports.
  * UNSUPPORTED — sport+market pair the simulator does NOT support
                   (never a bug — honest UNAVAILABLE).
  * NOT_INVOKED — pick was published by a direct-inject producer
                   that structurally bypasses ``apply_simulations``
                   (mls_direct_inject, soccer_prop_inject,
                   uefa_espn_v1, soccer_hot_scorers_v1, soccer_v1,
                   soccer_v1_synth).  Per Magic 3B.1 directive we
                   never activate the simulator on these paths
                   because doing so would mutate Lock Score.

Aggregate counter helper — used by the refresh orchestrator to log a
single summary line per refresh cycle instead of per-row spam.
"""
from __future__ import annotations

import re
from typing import Optional


# Direct-inject producers that STRUCTURALLY bypass apply_simulations.
# Their picks are NEVER simulator-eligible in the current architecture
# — the simulator would need to be added to their code paths, which
# would change Lock Score behavior (blocked by directive).
DIRECT_INJECT_SOURCES: frozenset[str] = frozenset({
    "mls_direct_inject", "soccer_prop_inject",
    "uefa_espn_v1", "soccer_hot_scorers_v1",
    "soccer_v1", "soccer_v1_synth",
    # ESPN-produced picks that use their own publish path.
    "espn_soccer_fixtures", "espn_signal",
})

# Publication sources that route through apply_simulations().
SIM_CAPABLE_SOURCES: frozenset[str] = frozenset({
    "canonical_pipeline",
    "legacy_backfill",       # legacy path — some historical rows.
})


def is_sim_capable_source(publication_source: Optional[str]) -> bool:
    if not publication_source:
        return False
    return publication_source in SIM_CAPABLE_SOURCES


# ── Per-sport market eligibility ─────────────────────────────────────
#
# Each entry lists the market patterns the sport's Monte Carlo
# simulator ACTUALLY routes.  See:
#   brain/sim_mlb.py         (MLB hitter/pitcher props)
#   brain/sim_nba.py         (NBA player props)
#   brain/sim_soccer.py      (moneyline/totals/BTTS/draw/DC/scorer)
#   brain/sim_soccer_scorer.py (scorer sub-routing)
#   brain/sim_tennis.py      (event-simulation for tennis)

_MLB_SUPPORTED = (
    re.compile(r"\b(over|under)\s+\d+\.?\d*\s+(hits?|home runs?|"
               r"total bases|strikeouts?|runs?|rbi)\b", re.I),
)
_MLB_UNSUPPORTED = (
    re.compile(r"moneyline|spread|nrfi|yrfi|f5|total runs", re.I),
)


_NBA_SUPPORTED = (
    re.compile(r"\b(over|under)\s+\d+\.?\d*\s+(points?|rebounds?|"
               r"assists?|threes|steals|blocks|"
               r"points\s*(and|\+)?\s*rebounds\s*(and|\+)?\s*assists|"
               r"pra)\b", re.I),
)


_SOCCER_SUPPORTED = (
    re.compile(r"anytime.*goal.*scorer|anytime.*scorer|first goal scorer|"
                r"last goal scorer|to score or assist|to score|"
                r"anytime assist", re.I),
    re.compile(r"total goals\b|(over|under)\s+\d+\.?\d*\s+goals?", re.I),
    re.compile(r"both teams to score|btts", re.I),
    re.compile(r"win or draw|draw or win|double chance", re.I),
    re.compile(r"moneyline|to win|match winner", re.I),
    re.compile(r"\bthe\s+draw\b|\bdraw\b$", re.I),
)


_TENNIS_SUPPORTED = (
    re.compile(r"moneyline|to win|match winner", re.I),
    re.compile(r"total games|(over|under)\s+\d+\.?\d*\s+games?", re.I),
    re.compile(r"set\s+spread|game\s+spread|handicap", re.I),
)


def classify_sim_eligibility(sport: str, market: str) -> str:
    """Return one of:
        "SUPPORTED"    — simulator routes this market.
        "UNSUPPORTED"  — simulator does NOT route this market.
        "UNKNOWN_SPORT"— sport has no dedicated simulator.
    """
    s = (sport or "").strip()
    m = market or ""
    if s == "MLB":
        for p in _MLB_UNSUPPORTED:
            if p.search(m):
                return "UNSUPPORTED"
        for p in _MLB_SUPPORTED:
            if p.search(m):
                return "SUPPORTED"
        return "UNSUPPORTED"
    if s == "NBA":
        for p in _NBA_SUPPORTED:
            if p.search(m):
                return "SUPPORTED"
        return "UNSUPPORTED"
    if s == "Soccer":
        for p in _SOCCER_SUPPORTED:
            if p.search(m):
                return "SUPPORTED"
        return "UNSUPPORTED"
    if s == "Tennis":
        for p in _TENNIS_SUPPORTED:
            if p.search(m):
                return "SUPPORTED"
        return "UNSUPPORTED"
    return "UNKNOWN_SPORT"


# ── Observability counter helper ─────────────────────────────────────

class SimPersistenceCounters:
    """Aggregate counters for a single refresh cycle.  Never per-row
    spam — call ``log_summary(logger)`` once at the end of the
    refresh."""
    __slots__ = (
        "attempted", "persisted", "skipped_no_sim_result",
        "skipped_unsupported_market", "skipped_not_invoked_source",
        "rejected_no_provenance", "rejected_low_runs",
        "failed_persistence",
    )

    def __init__(self) -> None:
        self.attempted = 0
        self.persisted = 0
        self.skipped_no_sim_result = 0
        self.skipped_unsupported_market = 0
        self.skipped_not_invoked_source = 0
        self.rejected_no_provenance = 0
        self.rejected_low_runs = 0
        self.failed_persistence = 0

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__slots__}

    def log_summary(self, logger, sport_filter: Optional[str] = None) -> None:
        tag = f"[sport={sport_filter}]" if sport_filter else ""
        logger.info(
            "MAGIC 3B.1 sim persistence %s attempted=%d persisted=%d "
            "skipped_no_sim=%d skipped_unsupported=%d "
            "skipped_not_invoked=%d rejected_no_provenance=%d "
            "rejected_low_runs=%d failed=%d",
            tag, self.attempted, self.persisted,
            self.skipped_no_sim_result, self.skipped_unsupported_market,
            self.skipped_not_invoked_source, self.rejected_no_provenance,
            self.rejected_low_runs, self.failed_persistence,
        )


__all__ = [
    "DIRECT_INJECT_SOURCES",
    "SIM_CAPABLE_SOURCES",
    "is_sim_capable_source",
    "classify_sim_eligibility",
    "SimPersistenceCounters",
]
