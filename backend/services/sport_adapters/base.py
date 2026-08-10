"""Common helpers for sport adapters — pure functions, no I/O."""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Optional


def norm(s: Any) -> str:
    if not s:
        return ""
    nfkd = unicodedata.normalize("NFKD", str(s))
    ascii_only = "".join(ch for ch in nfkd if not unicodedata.combining(ch))
    cleaned = re.sub(r"[.'’\-]", "", ascii_only)
    return re.sub(r"\s+", " ", cleaned).strip().lower()


def any_token(market: str, tokens: tuple[str, ...]) -> bool:
    if not market:
        return False
    m = market.lower()
    return any(t in m for t in tokens)


# ── Common non-player tokens (moneyline / spread / total) ────────
# NOTE: Individual-sport adapters (Tennis/UFC) do NOT apply this
# guard — "Alcaraz Moneyline" is a player-based bet in Tennis.
TEAM_MARKET_TOKENS_TEAM_SPORTS: tuple[str, ...] = (
    "moneyline", "money line", "spread", "handicap",
    "over/under game", "total points", "total goals",
    "team total", "correct score",
    "1st half", "2nd half", "half time",
    "puck line", "run line",
)


def is_team_market_for_team_sport(market: str) -> bool:
    """True iff a team-sport market names the TEAM not the player."""
    return any_token(market, TEAM_MARKET_TOKENS_TEAM_SPORTS)


def dob_matches(a: Optional[str], b: Optional[str]) -> bool:
    """Two DOB strings match iff both non-empty AND equal after
    stripping to YYYY-MM-DD.  Missing DOBs are treated as unknown
    (not a mismatch)."""
    if not a or not b:
        return True
    ak = str(a)[:10]
    bk = str(b)[:10]
    return ak == bk


def provider_ids_conflict(before: dict, after: dict) -> Optional[str]:
    """Return the provider name whose id disagrees between the two
    records, or None if they agree / one side is unknown."""
    bp = before.get("provider_ids") or {}
    ap = after.get("provider_ids") or {}
    for prov, pid in ap.items():
        if prov in bp and str(bp[prov]) != str(pid):
            return prov
    return None


__all__ = [
    "norm", "any_token", "TEAM_MARKET_TOKENS_TEAM_SPORTS",
    "is_team_market_for_team_sport", "dob_matches",
    "provider_ids_conflict",
]
