"""Magic Layer 2.0 — Evidence Convergence.

Read the top-level SKILL: this module SITS ON TOP of the existing
Lock Score / scorer / creator / dual-threat / simulator / calibration
stack.  It does NOT rebuild them.  It reads their outputs, joins them
with authoritative history + real sportsbook consensus, and emits a
transparent evidence bundle a downstream consumer (Locks, Rollover,
Parlay) may CHOOSE to weight — but Magic is NOT wired into any
consumer by this block.

Modules
───────
* ``contract``        — canonical evidence types, availability enum,
                        MagicOutput dataclass.
* ``exact_threshold`` — history helpers (last-N + season + Q10/Q25/…)
                        keyed by exact betting threshold.
* ``model_market``    — model↔market convergence state machine.
* ``contradictions``  — risk-flag detector.
* ``adapters.mlb``    — MLB market adapters (hits / total_bases / HR /
                        RBI / K's / outs).
* ``adapters.nba``    — NBA composite market adapter (points, rebounds,
                        assists, 3PM, PRA/PR/PA/RA).
* ``adapters.nfl``    — NFL passing / rushing / receiving adapter.
* ``adapters.soccer`` — Goalscorer / Creator / Dual-Threat adapter
                        built on soccer_player_form + player_identities.
* ``adapters.tennis`` — Surface-aware ELO + serve/return adapter built
                        on tennis_players + tennis_matches_history.

Rules (from directive)
──────────────────────
* PROVISIONAL identity NEVER consumes authoritative history.
* Missing evidence remains UNAVAILABLE — never 0.
* Every evidence item carries provenance (source, timestamp, sample_size).
* Magic degrades gracefully — one sport's missing evidence cannot
  block another.
* Magic Score is DISTINCT from Lock Score (never overwrites).
"""
from services.magic.contract import (
    EvidenceType, Availability, EvidenceItem, MagicOutput, MagicTier,
    availability_from,
)
from services.magic.model_market import (
    ModelMarketState, evaluate_model_market_convergence,
)
from services.magic.contradictions import (
    RiskFlag, detect_contradictions,
)

__all__ = [
    "EvidenceType", "Availability", "EvidenceItem",
    "MagicOutput", "MagicTier", "availability_from",
    "ModelMarketState", "evaluate_model_market_convergence",
    "RiskFlag", "detect_contradictions",
]
