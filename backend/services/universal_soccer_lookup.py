"""Phase 5.2 (2026-08-11) — Soccer Universal Lookup Builder.

Reads the P0-A→E persisted Soccer identity registry
(``db.player_identities`` where ``sport=="Soccer"``) and returns the
FIVE lookup dicts the existing Soccer validator
(``services.player_team_fixture_validator.validate_player_fixture_pick``)
expects:

    roster_lookup             : name_norm → current club
    fresh_roster_names        : set of name_norm observed within window
    national_team_lookup      : name_norm → current national team
    fresh_national_team_names : set of name_norm with fresh NT obs
    nationality_lookup        : name_norm → nationality (country of eligibility)

Every name_norm alias present on the identity doc is also folded into
each lookup so alias-based resolution works even when the pick spells
the player differently.  Aliases are already anti-collision-safe
because they were seeded via the P0-A→E ingest pipeline against
provider ids.

READ-ONLY — never writes to Mongo.  Async I/O because it reads from
the persisted identity collection.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from services.player_identity import (
    IDENTITY_COLLECTION, _norm,
)


STALENESS_DAYS_DEFAULT = 30


async def build_soccer_lookups(
    db, *, staleness_days: int = STALENESS_DAYS_DEFAULT,
) -> dict[str, Any]:
    """Return the 5 lookup dicts the Soccer validator expects.

    Only the ``current_team`` / ``current_national_team`` values are
    used for matching; the freshness sets carry the staleness gate
    the validator applies when deciding between ``verified`` and
    ``roster_unverified``.
    """
    roster_lookup: dict[str, str] = {}
    fresh_roster: set[str] = set()
    nt_lookup: dict[str, str] = {}
    fresh_nt: set[str] = set()
    nat_lookup: dict[str, str] = {}
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=staleness_days)).isoformat()
    async for d in db[IDENTITY_COLLECTION].find(
        {"sport": "Soccer"},
        {"_id": 0, "name": 1, "name_norm": 1, "aliases": 1,
         "current_team": 1, "observed_at": 1,
         "current_national_team": 1,
         "national_team_observed_at": 1,
         "nationality": 1}):
        # Normalise every key used to match a pick: canonical
        # name_norm plus every alias.  Alias entries stored on the
        # identity doc were already vetted against provider ids by
        # the P0-A→E ingest — folding them here does NOT loosen name
        # matching.
        keys: list[str] = []
        nn = d.get("name_norm") or _norm(d.get("name") or "")
        if nn:
            keys.append(nn)
        for al in (d.get("aliases") or []):
            a = _norm(al)
            if a and a != nn:
                keys.append(a)
        if not keys:
            continue

        club = d.get("current_team")
        club_obs = d.get("observed_at") or ""
        if club:
            for k in keys:
                # Do NOT clobber an earlier fresher observation.
                if k not in roster_lookup:
                    roster_lookup[k] = club
                if club_obs >= cutoff:
                    fresh_roster.add(k)

        nt = d.get("current_national_team")
        nt_obs = d.get("national_team_observed_at") or ""
        if nt:
            for k in keys:
                if k not in nt_lookup:
                    nt_lookup[k] = nt
                if nt_obs >= cutoff:
                    fresh_nt.add(k)

        nat = d.get("nationality")
        if nat:
            for k in keys:
                if k not in nat_lookup:
                    nat_lookup[k] = nat

    return {
        "roster_lookup": roster_lookup,
        "fresh_roster_names": fresh_roster,
        "national_team_lookup": nt_lookup,
        "fresh_national_team_names": fresh_nt,
        "nationality_lookup": nat_lookup,
    }


__all__ = ["build_soccer_lookups"]
