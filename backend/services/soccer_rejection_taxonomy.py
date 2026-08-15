"""Universal Soccer rejection taxonomy — Phase 2A.5 UNIVERSAL.

Every provider market that fails to reach the Perklocks board must
attach a code from this enum.  Generic labels ("filtered", "skipped",
"unavailable") are prohibited by contract — the caller MUST pick the
specific code that describes the exact stage that dropped the market.

Do NOT extend this list from callers.  Add new codes here first, then
call sites, so the taxonomy remains centrally auditable.
"""
from __future__ import annotations
import enum


class SoccerRejection(str, enum.Enum):
    # Provider-side
    NO_PROVIDER_MARKET                = "NO_PROVIDER_MARKET"
    EVENT_IDENTITY_FAILURE            = "EVENT_IDENTITY_FAILURE"
    PLAYER_IDENTITY_FAILURE           = "PLAYER_IDENTITY_FAILURE"
    MARKET_NORMALIZATION_FAILURE      = "MARKET_NORMALIZATION_FAILURE"
    REAL_LINE_NOT_PRESERVED           = "REAL_LINE_NOT_PRESERVED"
    # Candidate side
    CANDIDATE_NOT_CREATED             = "CANDIDATE_NOT_CREATED"
    MISSING_FEATURE_DATA              = "MISSING_FEATURE_DATA"
    MODEL_NOT_INVOKED                 = "MODEL_NOT_INVOKED"
    NO_MODEL_PROBABILITY              = "NO_MODEL_PROBABILITY"
    NO_IMPLIED_PROBABILITY            = "NO_IMPLIED_PROBABILITY"
    NO_EDGE_VALUE                     = "NO_EDGE_VALUE"
    # Publication side
    LOW_LOCK_SCORE                    = "LOW_LOCK_SCORE"
    NO_POSITIVE_EDGE                  = "NO_POSITIVE_EDGE"
    RELATED_MARKET_DOMINATED          = "RELATED_MARKET_DOMINATED"
    TEAMMATE_DOMINATED                = "TEAMMATE_DOMINATED"
    CANONICAL_PUBLICATION_REJECTED    = "CANONICAL_PUBLICATION_REJECTED"
    DUPLICATE_CANONICAL_MARKET        = "DUPLICATE_CANONICAL_MARKET"
    # Contract violations (fail-closed)
    MODEL_ONLY_NO_REAL_BOOK           = "MODEL_ONLY_NO_REAL_BOOK"
    SYNTHETIC_BOOK_ODDS               = "SYNTHETIC_BOOK_ODDS"
    PLAYER_TEAM_INVALID               = "PLAYER_TEAM_INVALID"
    ROSTER_UNVERIFIED                 = "ROSTER_UNVERIFIED"


# Publicly-exported set of ALL valid codes.  Any pick tagged with a
# reason not in this set is a contract violation.
ALL_CODES: frozenset[str] = frozenset(c.value for c in SoccerRejection)


def is_valid_code(code: str) -> bool:
    return code in ALL_CODES


__all__ = ["SoccerRejection", "ALL_CODES", "is_valid_code"]
