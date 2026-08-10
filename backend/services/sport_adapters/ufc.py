"""UFC sport adapter — individual sport, no team.

Rules:
  * Weight-class transition (Lightweight → Welterweight → Middleweight)
    is an ATTRIBUTE change, NEVER a new fighter identity.
  * Historical divisions preserved in ``historical_divisions``.
  * Stance (Orthodox / Southpaw / Switch) + reach captured as
    attributes when available.
  * Provider IDs: UFC athlete id + ESPN fighter id.
  * Surname-only merges never allowed — many fighters share
    surnames (e.g. "Silva").
"""
from __future__ import annotations

from typing import Any, Optional

from .base import norm, any_token, dob_matches, provider_ids_conflict

SPORT = "UFC"
SPORT_CLASS = "individual"
ROSTER_SOURCE = "espn_mma_athletes"
PROVIDER_IDS = ("ufc_athlete_id", "espn_mma_id", "odds_api")
IDENTITY_ATTRIBUTES = ("full_name", "dob", "nationality",
                        "stance", "reach_inches")
DISAMBIGUATORS = ("provider_id", "dob")
HISTORY_FIELDS = ("sport", "date", "event_id", "opponent",
                    "division_at_time", "value", "method",
                    "round_finished")

# Fields that change without minting a new identity.
ATTRIBUTES_NOT_IDENTITY = ("division", "weight_class", "ranking",
                            "stance", "camp")

PLAYER_MARKET_TOKENS = (
    "moneyline", "money line",
    "method of victory", "ko/tko", "submission",
    "decision", "fight goes distance",
    "round betting", "round of victory",
    "player_",
    "to win",
)

# Match-level markets (fight goes X rounds — bet on the match).
MATCH_MARKET_TOKENS = (
    "total rounds", "over rounds", "under rounds",
    "fight length",
)


def is_player_market(market: str) -> bool:
    """Individual sport — every market names a fighter UNLESS the
    market is a match-level total (both fighters share the total)."""
    if not market:
        return False
    m = market.lower()
    if any(tok in m for tok in MATCH_MARKET_TOKENS):
        return False
    return True


def canonical_current_team_key(pick: dict) -> Optional[str]:
    """UFC has no team — camp is metadata, not identity."""
    return None


def transfer_preserves_identity(before: dict, after: dict) -> bool:
    """Division change preserves identity."""
    return True


def is_attribute_change_not_identity(field: str) -> bool:
    return field in ATTRIBUTES_NOT_IDENTITY


def validate_identity_change(before: dict, after: dict) -> Optional[str]:
    if not dob_matches(before.get("dob"), after.get("dob")):
        return "dob_mismatch"
    conflict = provider_ids_conflict(before, after)
    if conflict:
        return f"provider_id_conflict:{conflict}"
    return None


def surnames_only_would_merge(a: dict, b: dict) -> bool:
    na = norm(a.get("name"))
    nb = norm(b.get("name"))
    if not na or not nb or na == nb:
        return False
    la = na.split()[-1] if na else ""
    lb = nb.split()[-1] if nb else ""
    return la == lb and la != ""


__all__ = [
    "SPORT", "SPORT_CLASS", "ROSTER_SOURCE", "PROVIDER_IDS",
    "IDENTITY_ATTRIBUTES", "ATTRIBUTES_NOT_IDENTITY",
    "DISAMBIGUATORS", "HISTORY_FIELDS", "PLAYER_MARKET_TOKENS",
    "is_player_market", "canonical_current_team_key",
    "transfer_preserves_identity", "validate_identity_change",
    "is_attribute_change_not_identity", "surnames_only_would_merge",
]
