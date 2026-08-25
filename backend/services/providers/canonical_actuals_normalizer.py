"""Canonical Soccer actuals normalizer (Session C, 2026-08-25).

Upserts provider-verified completed-fixture actuals into the EXISTING
canonical stores:
  • db.player_game_actuals   (sport="soccer", by canonical_player_id)
  • db.team_game_actuals     (sport="soccer", by canonical_team_id)

Never creates new history collections.  Never overwrites a higher-
authority row with lower-authority fallback data unless the field is
missing.  Idempotent on the composite key.

Design rules:
  • Completed fixtures are IMMUTABLE — one upsert per (provider,
    canonical_event_id, canonical_player_id_or_team_id) key.
  • Every write records provenance: source provider, provider IDs,
    market family, retrieved_at.
  • Callers hand this normalized data ONLY after the cascade returns
    OK — we never persist DATA_UNAVAILABLE / MARKET_UNSUPPORTED.
  • This module DOES NOT feed research-only fields (xG, xA,
    chances_created, lineup) into any model — those go into the
    optional `research` sub-doc for consumer display use.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional


PROVIDER_AUTHORITY = {"pitchapi": 90, "bigballs": 60, "espn": 50}


async def upsert_player_actual(
    db, *, canonical_event_id: str,
    canonical_player_id: Optional[str],
    provider_player_name: str,
    provider: str, provider_event_id: str,
    canonical_team_id: Optional[str] = None,
    opponent: Optional[str] = None,
    event_date: str = "",
    home_away: Optional[str] = None,
    stats: Optional[dict] = None,
    research: Optional[dict] = None,
) -> dict:
    """Idempotent upsert into player_game_actuals for a Soccer player.

    Returns a summary dict:
      {inserted|updated|skipped, doc_id, existing_authority,
       new_authority}
    """
    if not canonical_event_id or (not canonical_player_id and not
                                    provider_player_name):
        return {"status": "skipped", "reason": "missing_identity"}
    key: dict[str, Any] = {"sport": "soccer",
                            "canonical_event_id": canonical_event_id}
    if canonical_player_id:
        key["canonical_player_id"] = canonical_player_id
    else:
        key["player_name"] = provider_player_name
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    new_authority = PROVIDER_AUTHORITY.get(provider, 50)

    existing = await db.player_game_actuals.find_one(key, {"_id": 0})
    if existing:
        # Only overwrite if new authority strictly higher OR field is
        # missing on the existing row.
        ex_auth = PROVIDER_AUTHORITY.get(
            (existing.get("provenance") or {}).get("provider", ""), 0
        )
        merged_actuals = dict(existing.get("actuals") or {})
        for k, v in (stats or {}).items():
            if v is None:
                continue
            if new_authority >= ex_auth or merged_actuals.get(k) is None:
                merged_actuals[k] = v
        set_doc = {
            "actuals": merged_actuals,
            "provider_last_seen": provider,
            "updated_at": now_iso,
        }
        if research:
            merged_research = dict(existing.get("research") or {})
            for k, v in research.items():
                if v is not None:
                    merged_research[k] = v
            set_doc["research"] = merged_research
        await db.player_game_actuals.update_one(
            {**key}, {"$set": set_doc}, upsert=False)
        return {"status": "updated", "existing_authority": ex_auth,
                "new_authority": new_authority}

    doc = {
        **key,
        "player_name": provider_player_name,
        "canonical_team_id": canonical_team_id,
        "opponent": opponent,
        "home_away": home_away,
        "event_time": None,
        "season": None,
        "actuals": {k: v for k, v in (stats or {}).items() if v is not None},
        "research": {k: v for k, v in (research or {}).items() if v is not None},
        "provider_event_id": provider_event_id,
        "provenance": {
            "provider": provider,
            "authority": new_authority,
            "market_family": "soccer_composite",
            "retrieved_at": now_iso,
        },
        "source": provider,
        "ingested_at": now_iso,
    }
    if event_date:
        doc["event_date"] = event_date
    await db.player_game_actuals.update_one(
        {**key}, {"$setOnInsert": doc}, upsert=True)
    return {"status": "inserted", "new_authority": new_authority}


async def upsert_team_actual(
    db, *, canonical_event_id: str,
    canonical_team_id: Optional[str],
    provider_team_name: str, provider: str, provider_event_id: str,
    canonical_opponent_id: Optional[str] = None,
    opponent_name: Optional[str] = None,
    event_date: str = "",
    home_away: Optional[str] = None,
    stats: Optional[dict] = None,
) -> dict:
    """Idempotent upsert into team_game_actuals for a Soccer team."""
    if not canonical_event_id or not provider_team_name:
        return {"status": "skipped", "reason": "missing_identity"}
    key: dict[str, Any] = {"sport": "soccer",
                            "canonical_event_id": canonical_event_id,
                            "team_name": provider_team_name}
    if canonical_team_id:
        key["canonical_team_id"] = canonical_team_id
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    new_authority = PROVIDER_AUTHORITY.get(provider, 50)

    existing = await db.team_game_actuals.find_one(key, {"_id": 0})
    if existing:
        ex_auth = PROVIDER_AUTHORITY.get(
            (existing.get("provenance") or {}).get("provider", ""), 0)
        merged = dict(existing.get("actuals") or {})
        for k, v in (stats or {}).items():
            if v is None:
                continue
            if new_authority >= ex_auth or merged.get(k) is None:
                merged[k] = v
        await db.team_game_actuals.update_one(
            {**key},
            {"$set": {"actuals": merged, "provider_last_seen": provider,
                      "updated_at": now_iso}},
            upsert=False,
        )
        return {"status": "updated", "existing_authority": ex_auth,
                "new_authority": new_authority}

    doc = {
        **key,
        "opponent": opponent_name,
        "canonical_opponent_id": canonical_opponent_id,
        "home_away": home_away,
        "actuals": {k: v for k, v in (stats or {}).items() if v is not None},
        "provider_event_id": provider_event_id,
        "provenance": {
            "provider": provider, "authority": new_authority,
            "retrieved_at": now_iso,
        },
        "source": provider,
        "ingested_at": now_iso,
    }
    if event_date:
        doc["event_date"] = event_date
    await db.team_game_actuals.update_one(
        {**key}, {"$setOnInsert": doc}, upsert=True)
    return {"status": "inserted", "new_authority": new_authority}


__all__ = [
    "upsert_player_actual", "upsert_team_actual",
    "PROVIDER_AUTHORITY",
]
