"""Phase 5 (2026-08-11) — Current Player Context Contract.

The "current context" is a small envelope surface that answers a
Magic Layer 2.0 caller's core question: WHAT DO WE KNOW right now
about this player, and how confident are we?

Every field explicitly reports its own freshness — an unknown field
stays ``None`` (never invented) and downstream can react to that.

Fields:

    canonical_player_id : str
    resolved            : bool
    sport               : Optional[str]
    full_name           : Optional[str]
    normalized_name     : Optional[str]
    aliases             : list[str]
    provider_ids        : dict[str, str]
    position            : Optional[str]
    role                : Optional[str]

    current_team        : Optional[str]      # club
    current_team_source : Optional[str]
    current_team_observed_at : Optional[iso_datetime]
    current_team_fresh  : bool               # observed within staleness window
    historical_teams    : list[dict]

    current_national_team          : Optional[str]
    current_national_team_source   : Optional[str]
    current_national_team_observed_at : Optional[iso_datetime]
    current_national_team_fresh    : bool
    historical_national_teams      : list[dict]

    nationality        : Optional[str]
    handedness         : Optional[str]
    stance             : Optional[str]
    division           : Optional[str]

The contract is READ-ONLY.  Callers must NOT extend it silently —
if you need a new field, add it here first so every consumer sees
it consistently.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

_STALENESS_DAYS_DEFAULT = 30


def _parse_iso(v: Optional[str]) -> Optional[datetime]:
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _is_fresh(observed_at: Optional[str],
                staleness_days: int = _STALENESS_DAYS_DEFAULT) -> bool:
    ts = _parse_iso(observed_at)
    if ts is None:
        return False
    return (datetime.now(timezone.utc) - ts) <= timedelta(days=staleness_days)


def build_current_context(
    identity_doc: Optional[dict[str, Any]],
    *,
    staleness_days: int = _STALENESS_DAYS_DEFAULT,
) -> dict[str, Any]:
    """Produce a Current Context envelope from a raw player_identities doc.

    Missing keys stay ``None`` — the contract NEVER invents data.
    """
    if not identity_doc:
        return {
            "canonical_player_id": None,
            "resolved": False,
            "sport": None,
            "full_name": None,
            "normalized_name": None,
            "aliases": [],
            "provider_ids": {},
            "position": None,
            "role": None,
            "current_team": None,
            "current_team_source": None,
            "current_team_observed_at": None,
            "current_team_fresh": False,
            "historical_teams": [],
            "current_national_team": None,
            "current_national_team_source": None,
            "current_national_team_observed_at": None,
            "current_national_team_fresh": False,
            "historical_national_teams": [],
            "nationality": None,
            "handedness": None,
            "stance": None,
            "division": None,
        }
    obs = identity_doc.get("observed_at")
    nat_obs = identity_doc.get("national_team_observed_at")
    return {
        "canonical_player_id": identity_doc.get("canonical_player_id"),
        "resolved": True,
        "sport": identity_doc.get("sport"),
        "full_name": identity_doc.get("name"),
        "normalized_name": identity_doc.get("name_norm"),
        "aliases": list(identity_doc.get("aliases") or []),
        "provider_ids": dict(identity_doc.get("provider_ids") or {}),
        "position": identity_doc.get("position"),
        "role": identity_doc.get("role"),
        # Club affiliation
        "current_team": identity_doc.get("current_team"),
        "current_team_source": identity_doc.get("source"),
        "current_team_observed_at": obs,
        "current_team_fresh": _is_fresh(obs, staleness_days),
        "historical_teams": list(identity_doc.get("historical_teams") or []),
        # National-team affiliation
        "current_national_team": identity_doc.get("current_national_team"),
        "current_national_team_source": identity_doc.get(
            "national_team_source"),
        "current_national_team_observed_at": nat_obs,
        "current_national_team_fresh": _is_fresh(nat_obs, staleness_days),
        "historical_national_teams": list(
            identity_doc.get("historical_national_teams") or []),
        # Attributes
        "nationality": identity_doc.get("nationality"),
        "handedness": identity_doc.get("handedness"),
        "stance": identity_doc.get("stance"),
        "division": identity_doc.get("division"),
    }


__all__ = ["build_current_context"]
