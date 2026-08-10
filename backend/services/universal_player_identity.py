"""Phase 5 (2026-08-11) — Universal Cross-Sport Player Identity.

Sport-agnostic façade over :mod:`services.player_identity` (the
P0-A..P0-E race-safe registry) that establishes the CROSS-SPORT
contract Magic Layer 2.0 will consume.

Adapters
════════

Sport-specific ADAPTERS live in ``services.sport_adapters.*`` — each
adapter knows:

  * whether a market string is player-based for that sport
  * which provider-id namespaces are canonical for that sport
  * which fields are IDENTITY attributes vs mere attribute changes
    (e.g. MLB switch-hitter, UFC weight-class transition)
  * whether team / school / division changes preserve the same
    canonical_player_id

Universal module surface
════════════════════════

  * ``ENABLED_SPORTS`` / ``TEAM_SPORTS`` / ``INDIVIDUAL_SPORTS``
  * ``resolve(...)``           — sport-agnostic resolve helper
  * ``upsert(...)``            — sport-agnostic upsert helper
  * ``ensure_universal_indexes`` — indexes for cross-sport queries
  * ``get_player_context``     — unified Current-Context read
  * ``get_history`` / ``link_history_row`` — history linkage
  * ``assert_transfer_preserves_identity`` — cross-sport assertion
  * ``assert_attribute_change_not_identity`` — MLB/UFC guard
"""
from __future__ import annotations

from typing import Any, Optional
from datetime import datetime, timezone

from services import player_identity as _core
from services import sport_adapters as _adapters
from services.player_history_contract import validate_history_row
from services.current_context_contract import build_current_context

ENABLED_SPORTS: tuple[str, ...] = (
    "NFL", "NBA", "MLB", "NHL", "CFB", "Soccer", "Tennis", "UFC",
)

TEAM_SPORTS: frozenset[str] = frozenset(
    ("NFL", "NBA", "MLB", "NHL", "CFB", "Soccer")
)

INDIVIDUAL_SPORTS: frozenset[str] = frozenset(("Tennis", "UFC"))

PLAYER_HISTORY_COLLECTION = "player_history"


async def ensure_universal_indexes(db) -> None:
    """Create cross-sport indexes.  Idempotent."""
    await _core.ensure_identity_indexes(db)
    try:
        await db[PLAYER_HISTORY_COLLECTION].create_index(
            [("canonical_player_id", 1), ("date", -1)],
            name="cpid_date_desc")
    except Exception:
        pass
    try:
        await db[PLAYER_HISTORY_COLLECTION].create_index(
            [("canonical_player_id", 1), ("sport", 1), ("season", 1)],
            name="cpid_sport_season")
    except Exception:
        pass
    try:
        await db[PLAYER_HISTORY_COLLECTION].create_index(
            [("canonical_player_id", 1), ("sport", 1),
             ("event_id", 1), ("market", 1)],
            unique=True, name="cpid_event_market_uniq")
    except Exception:
        pass


def resolve(*, name: Optional[str] = None, sport: str,
             league: Optional[str] = None,
             provider: Optional[str] = None,
             provider_id: Optional[str] = None) -> Optional[_core.PlayerIdentity]:
    """Cross-sport resolve — provider IDs win, name falls back."""
    if sport not in ENABLED_SPORTS:
        return None
    return _core.resolve_player(
        name=name or "", sport=sport, league=league or "",
        provider=provider, provider_id=provider_id,
    )


def upsert(**kwargs) -> _core.PlayerIdentity:
    """Cross-sport upsert — delegates to the P0-A race-safe layer."""
    sport = kwargs.get("sport")
    if sport not in ENABLED_SPORTS:
        raise ValueError(f"sport must be in {ENABLED_SPORTS}, got {sport}")
    return _core.upsert_player(**kwargs)


async def get_player_context(db, canonical_player_id: str) -> dict[str, Any]:
    """Unified read for Magic Layer 2.0 — uses the Current Context
    contract in :mod:`services.current_context_contract`."""
    doc = await db[_core.IDENTITY_COLLECTION].find_one(
        {"canonical_player_id": canonical_player_id}, {"_id": 0})
    return build_current_context(doc)


async def get_history(db, canonical_player_id: str, *,
                       sport: Optional[str] = None,
                       since_date: Optional[str] = None,
                       limit: int = 50) -> list[dict]:
    """Threshold-ready history read.

    Rows are ordered by date descending.  Downstream filters compute
    "N of last M ≥ threshold".
    """
    q: dict[str, Any] = {"canonical_player_id": canonical_player_id}
    if sport:
        q["sport"] = sport
    if since_date:
        q["date"] = {"$gte": since_date}
    return [d async for d in db[PLAYER_HISTORY_COLLECTION].find(
        q, {"_id": 0}).sort("date", -1).limit(limit)]


async def link_history_row(db, canonical_player_id: str,
                            row: dict) -> str:
    """Attach one history row to a canonical player.  Idempotent by
    (canonical_player_id, sport, event_id, market)."""
    if not canonical_player_id:
        raise ValueError("canonical_player_id required")
    doc = dict(row)
    doc["canonical_player_id"] = canonical_player_id
    # Enforce the history-linkage contract.
    validate_history_row(doc)
    key = {
        "canonical_player_id": canonical_player_id,
        "sport": doc.get("sport"),
        "event_id": doc.get("event_id"),
        "market": doc.get("market"),
    }
    res = await db[PLAYER_HISTORY_COLLECTION].update_one(
        key, {"$set": doc, "$setOnInsert": {
            "linked_at": datetime.now(timezone.utc).isoformat()
        }}, upsert=True)
    return "inserted" if res.upserted_id else "updated"


# ── Sport-adapter helpers ─────────────────────────────────────────
def get_adapter(sport: str):
    return _adapters.get_adapter(sport)


def assert_transfer_preserves_identity(
    sport: str, before: dict, after: dict,
) -> Optional[str]:
    """Return None if the (before → after) identity update is
    acceptable per the sport adapter's rules; else return a reason
    string.  Adapters are expected to preserve id across:

        * NFL / NBA / NHL / MLB / Soccer team changes
        * CFB portal transfers
        * MLB switch-hitter / role changes
        * UFC weight-class transitions
        * Tennis ranking changes

    but MUST reject:

        * DOB mismatch
        * provider-id conflict (same provider, different id)
        * Tennis tour swap (ATP ↔ WTA)
    """
    adapter = _adapters.get_adapter(sport)
    if adapter is None:
        return f"unknown_sport:{sport}"
    if not adapter.transfer_preserves_identity(before, after):
        return "transfer_would_split_identity"
    return adapter.validate_identity_change(before, after)


def assert_attribute_change_not_identity(sport: str, field: str) -> bool:
    """True iff `field` is an ATTRIBUTE (not identity) under the
    sport's rules.  Used by tests to assert that MLB switch-hitter
    upgrades don't split identity, that UFC division changes don't,
    etc."""
    adapter = _adapters.get_adapter(sport)
    if adapter is None:
        return False
    fn = getattr(adapter, "is_attribute_change_not_identity", None)
    if fn is None:
        return False
    return bool(fn(field))


def surnames_only_would_merge(sport: str, a: dict, b: dict) -> bool:
    """Tennis/UFC helper: two identity records share only a surname —
    NEVER sufficient for merging.  Returns True to signal "would
    merge on surname alone, please reject"."""
    adapter = _adapters.get_adapter(sport)
    if adapter is None:
        return False
    fn = getattr(adapter, "surnames_only_would_merge", None)
    if fn is None:
        return False
    return bool(fn(a, b))


__all__ = [
    "ENABLED_SPORTS", "TEAM_SPORTS", "INDIVIDUAL_SPORTS",
    "PLAYER_HISTORY_COLLECTION",
    "ensure_universal_indexes",
    "resolve", "upsert",
    "get_player_context", "get_history", "link_history_row",
    "get_adapter",
    "assert_transfer_preserves_identity",
    "assert_attribute_change_not_identity",
    "surnames_only_would_merge",
]
