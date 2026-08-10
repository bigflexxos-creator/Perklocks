"""Tennis sport adapter — delegates identity to ``tennis_identity``.

Rules:
  * Individual sport — ``sport_class="individual"``.
  * Provider IDs: Sackmann ATP/WTA integer ``player_id`` (canonical),
    plus optional Odds API / Tennis Match Log tags.
  * Doubles safety: a doubles market names TWO players (e.g.
    "Alcaraz / Nadal") — the adapter records a synthetic PAIR key so
    the two singles identities remain intact.
  * Nationality / handedness / ranking captured as attributes when
    available — NEVER used as sole identity keys.
  * A ranking change is ALWAYS an attribute update, never a new id.
  * Two players sharing a common surname are NEVER merged from
    surname alone.
"""
from __future__ import annotations

from typing import Any, Optional

from .base import norm, any_token, dob_matches, provider_ids_conflict

SPORT = "Tennis"
SPORT_CLASS = "individual"
ROSTER_SOURCE = "sackmann_player_db_tennis"
PROVIDER_IDS = ("sackmann_id", "atp_id", "wta_id", "odds_api",
                 "tml_player_id")
IDENTITY_ATTRIBUTES = ("full_name", "dob", "tour", "nationality",
                        "handedness")
DISAMBIGUATORS = ("provider_id", "dob", "tour")
HISTORY_FIELDS = ("sport", "date", "event_id", "market",
                    "opponent", "value", "surface", "tour",
                    "round", "ranking_at_time")

PLAYER_MARKET_TOKENS = (
    "moneyline", "money line",
    "set betting", "correct set score",
    "games handicap", "handicap games",
    "first set winner", "any set winner",
    "aces over", "aces under",
    "double faults",
    "player_",
    "to win",
)

# Match-level markets (bet on the match, not a single participant).
# Both players share these; the barrier must not classify them as
# player-based.  Uses substring + regex-lite tokens so the alt-line
# forms ("Over 18.5 Games (Alt)", "Under 21.0 Games") are excluded
# too.
import re as _re
_MATCH_TOTAL_RE = _re.compile(
    r"\b(?:over|under)\s+[\d.]+\s+games\b", _re.IGNORECASE)
MATCH_MARKET_TOKENS = (
    "total games", "over games", "under games",
    "total sets", "over sets", "under sets",
    "match total",
    "games (alt)", "sets (alt)",
)

# Doubles patterns — presence of "/" or " & " or " and " suggests two
# players (e.g. "Alcaraz / Ruud").  Doubles are treated as a PAIR:
# the adapter does not merge two singles identities.
_DOUBLES_SEPARATORS = ("/", " & ", " and ")


def is_player_market(market: str) -> bool:
    """Individual sport — every market names a fighter UNLESS the
    market is a match-level total (both players share the total)."""
    if not market:
        return False
    m = market.lower()
    if any(tok in m for tok in MATCH_MARKET_TOKENS):
        return False
    if _MATCH_TOTAL_RE.search(m):
        return False
    return True


def is_doubles_market(pick: dict) -> bool:
    """True iff the pick names two players (doubles / mixed doubles)."""
    for k in ("player_name", "player", "selection", "market"):
        v = pick.get(k)
        if isinstance(v, str):
            if any(sep in v for sep in _DOUBLES_SEPARATORS):
                return True
    return False


def canonical_current_team_key(pick: dict) -> Optional[str]:
    """Individual sports have no team — return None (never invent)."""
    return None


def transfer_preserves_identity(before: dict, after: dict) -> bool:
    return True


def validate_identity_change(before: dict, after: dict) -> Optional[str]:
    if not dob_matches(before.get("dob"), after.get("dob")):
        return "dob_mismatch"
    # Tour swap (ATP↔WTA) is impossible for the same person — hard mint.
    bt = (before.get("tour") or "").upper()
    at = (after.get("tour") or "").upper()
    if bt and at and bt != at:
        return "tour_mismatch"
    conflict = provider_ids_conflict(before, after)
    if conflict:
        return f"provider_id_conflict:{conflict}"
    return None


def surnames_only_would_merge(a: dict, b: dict) -> bool:
    """True iff two identity records share ONLY a surname (last token
    of the normalised name).  Callers MUST treat this as insufficient
    for merging — always require a provider id or DOB match too."""
    na = norm(a.get("name"))
    nb = norm(b.get("name"))
    if not na or not nb or na == nb:
        return False
    la = na.split()[-1] if na else ""
    lb = nb.split()[-1] if nb else ""
    return la == lb and la != ""


__all__ = [
    "SPORT", "SPORT_CLASS", "ROSTER_SOURCE", "PROVIDER_IDS",
    "IDENTITY_ATTRIBUTES", "DISAMBIGUATORS", "HISTORY_FIELDS",
    "PLAYER_MARKET_TOKENS",
    "is_player_market", "is_doubles_market",
    "canonical_current_team_key",
    "transfer_preserves_identity", "validate_identity_change",
    "surnames_only_would_merge",
]
