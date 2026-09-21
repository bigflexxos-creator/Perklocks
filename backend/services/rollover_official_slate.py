"""P6 — Rollover official slate: immutable Top 3 with an append-only audit.

Collections
  rollover_slates        one doc per (slate_date, scope) — official membership
  rollover_slate_events  append-only: FROZEN / LEG_REPLACED / LEG_INVALIDATED

Rules
  * Once frozen, game start does NOT remove a leg, settlement does NOT
    replace it, refresh does NOT rerank it.
  * Only legitimate PREGAME invalidation (pick pulled off board / no_bet /
    voided / line gone BEFORE kickoff) may replace a leg; the replacement
    appends an audit event {old, new, time, reason}.  No silent change.
  * PICK_MISSING alone (the mutable ``db.picks`` row disappeared because
    of board regen / cache movement / retirement) is NOT sufficient
    evidence to replace an official leg — the wager truth lives in the
    frozen leg regardless of whether the picks row currently exists.
  * Filtered rollover requests never touch the official slate.
  * Historical replay (rollover_history_tagger) is RESEARCH_REPLAY only.

Root Closure (2026-06-21) — TRUE IMMUTABLE WAGER
  * `_leg()` now freezes the FULL wager snapshot: sportsbook, published
    odds, published line, published probability, published lock score,
    grade, publication_version, board_version, plus canonical event
    identity.  ``frozen_wager_version = 2`` records the freeze contract
    version so the reader can distinguish v1 legacy legs (identity-only)
    from v2 legs (full wager freeze).
  * Readers MUST use ``services.rollover_frozen_view.build_frozen_view``
    to render wager truth — never rehydrate wager fields from mutable
    ``db.picks``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

SLATES = "rollover_slates"
EVENTS = "rollover_slate_events"
SCOPE_OFFICIAL = "official"

# Freeze contract version. v1 = identity-only (canonical_pick_id + a few
# ranking fields).  v2 = FULL wager snapshot (line/odds/book/probability/
# lock_score all frozen at selection time).  Bump this whenever the leg
# schema in _leg() changes materially so the frozen-view reader can
# distinguish legacy legs from current-contract legs.
FROZEN_WAGER_VERSION = 2


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _first_present(pick: dict, *keys: str) -> Any:
    """Return the first non-None value across ``keys`` (does NOT filter falsy 0)."""
    for k in keys:
        v = pick.get(k)
        if v is not None:
            return v
    return None


def _leg(p: dict, rank: int) -> dict:
    """Freeze one wager as an immutable leg snapshot.

    Prefers canonical ``published_*`` fields for wager truth so a later
    mutation of the underlying ``db.picks`` row cannot alter what the
    Rollover promised the user at selection time.  Legacy aliases are
    only consulted when the canonical field is genuinely absent.
    """
    return {
        "rank": rank,
        # Identity — canonical wager ID + event ID.
        "canonical_pick_id": p.get("id"),
        "canonical_event_id": (
            p.get("canonical_event_id") or p.get("event_id") or p.get("event")
        ),
        # Provenance / freshness snapshot.
        "publication_version": _first_present(
            p, "publication_revision", "snapshot_version", "publication_version",
        ),
        "board_version": p.get("board_version"),
        # Sport / event display.
        "sport": p.get("sport"),
        "league": p.get("league"),
        "event": p.get("event"),
        "event_time": p.get("event_time"),
        "home_team": p.get("home_team"),
        "away_team": p.get("away_team"),
        # Wager terms — canonical first, legacy only when canonical absent.
        "market": _first_present(
            p, "canonical_market_family", "market", "provider_market_key",
        ),
        "market_display": p.get("market"),
        "selection": _first_present(
            p, "canonical_selection", "provider_selection", "selection",
        ),
        "line": _first_present(p, "published_line", "provider_line", "line"),
        # Book + price — this is the wager truth we promise never mutates.
        "sportsbook": _first_present(p, "sportsbook", "book", "bookmaker"),
        "odds": _first_present(
            p, "published_odds", "book_odds", "american_odds", "odds",
        ),
        # Model surface — frozen at selection time.
        "win_probability": _first_present(
            p, "published_probability", "model_win_prob", "win_probability",
        ),
        "lock_score": _first_present(p, "published_lock_score", "lock_score"),
        "grade": _first_present(p, "published_grade", "grade"),
        # Player identity for prop rendering.
        "player_name": _first_present(
            p, "canonical_player_id", "elite_player_name", "player_name",
        ),
        # Ranking metadata.
        "ev_score": p.get("rollover_ev_score"),
        # Freeze contract version + timestamp.
        "frozen_wager_version": FROZEN_WAGER_VERSION,
        "frozen_at": _now_iso(),
    }


async def get_official_slate(db, slate_date: str) -> Optional[dict]:
    return await db[SLATES].find_one({"slate_date": slate_date, "scope": SCOPE_OFFICIAL}, {"_id": 0})


async def ensure_slate_indexes(db) -> None:
    """Create the unique (slate_date, scope) index so two workers cannot
    ever freeze two different official slates for the same product day.
    Idempotent.  Called once at process startup by ``server.py``.
    """
    try:
        await db[SLATES].create_index(
            [("slate_date", 1), ("scope", 1)],
            unique=True,
            name="rollover_slates_date_scope_unique",
        )
    except Exception:
        # Concurrent bootstrap or already-exists — safe to ignore.
        pass
    try:
        await db[EVENTS].create_index(
            [("slate_date", 1), ("at", 1)],
            name="rollover_slate_events_date_at",
        )
    except Exception:
        pass


async def freeze_official_slate(db, slate_date: str, picks: list[dict], *,
                                selector_version: str, board_version: Optional[str]) -> dict:
    """Freeze Top-N as the official slate for the date.  Idempotent: if a
    slate already exists it is returned untouched (never reranked).

    Concurrency: the collection-level unique index on ``(slate_date, scope)``
    (see ``ensure_slate_indexes``) makes this operation safe under
    concurrent workers — the second insert raises DuplicateKeyError and
    we fetch/return the winner.
    """
    existing = await get_official_slate(db, slate_date)
    if existing:
        return existing
    legs = [_leg(p, i + 1) for i, p in enumerate(picks)]
    doc = {
        "slate_id": f"rollover:{slate_date}:{SCOPE_OFFICIAL}",
        "slate_date": slate_date,
        "scope": SCOPE_OFFICIAL,
        "legs": legs,
        "leg_count": len(legs),
        "selector_version": selector_version,
        "board_version": board_version,
        "frozen_wager_version": FROZEN_WAGER_VERSION,
        "frozen_at": _now_iso(),
        "version": 1,
    }
    try:
        await db[SLATES].insert_one(dict(doc))
    except Exception:
        # Concurrent freeze — the first writer wins (unique index enforces).
        return (await get_official_slate(db, slate_date)) or doc
    await db[EVENTS].insert_one({
        "slate_id": doc["slate_id"], "slate_date": slate_date, "event": "FROZEN",
        "at": doc["frozen_at"], "legs": legs, "selector_version": selector_version,
        "board_version": board_version,
        "frozen_wager_version": FROZEN_WAGER_VERSION,
        "version": 1,
    })
    return doc


def _has_explicit_invalidation_evidence(pick: dict) -> Optional[str]:
    """Return an invalidation reason ONLY when the mutable ``db.picks``
    row carries explicit real-world evidence the wager is dead — off-board,
    no-bet, void, cancelled, or a line-removed marker with provenance.
    """
    if pick.get("off_board"):
        return "OFF_BOARD_PREGAME"
    if pick.get("no_bet"):
        return "NO_BET_PREGAME"
    status = (pick.get("status") or "pending").lower()
    if status in ("void", "cancelled", "canceled", "postponed"):
        return f"{status.upper()}_PREGAME"
    # A line explicitly pulled requires the ingestion provenance flag,
    # not merely an absent price — a frozen wager's price is authoritative.
    if pick.get("no_real_book_line") and pick.get("line_removed_provenance"):
        return "LINE_REMOVED_PREGAME"
    return None


def _pregame_invalid_reason(pick: Optional[dict], now: datetime) -> Optional[str]:
    """Return an invalidation reason ONLY for legitimate pregame reasons
    with explicit real-world evidence.

    Root Closure (2026-06-21): PICK_MISSING alone is NOT sufficient
    evidence — the wager truth lives in the frozen leg regardless of
    whether the mutable ``db.picks`` row currently exists.  We only
    return a reason when the underlying pick doc is present AND carries
    an explicit invalidation flag (off_board / no_bet / voided /
    cancelled / line-removed-with-provenance).
    """
    if pick is None:
        # The frozen leg's wager truth stands on its own — a missing
        # mutable row is not evidence the wager became invalid.
        return None
    et = pick.get("event_time")
    try:
        started = bool(et) and datetime.fromisoformat(str(et).replace("Z", "+00:00")) <= now
    except Exception:
        started = False
    if started:
        return None  # post-kickoff: nothing may replace the leg
    return _has_explicit_invalidation_evidence(pick)


async def reconcile_official_slate(db, slate: dict, candidates: list[dict], *,
                                   selector_version: str) -> dict:
    """Replace ONLY legitimately pregame-invalidated legs, appending an audit
    event per replacement.  Returns the (possibly updated) slate.

    A leg is invalidated (LEG_INVALIDATED) or replaced (LEG_REPLACED)
    ONLY when the mutable pick doc carries explicit real-world evidence
    the wager is dead — never merely because the mutable row is absent.
    """
    now = datetime.now(timezone.utc)
    legs = list(slate.get("legs") or [])
    used_events = {l.get("canonical_event_id") for l in legs}
    changed = False
    for i, leg in enumerate(legs):
        pick = await db.picks.find_one({"id": leg.get("canonical_pick_id")}, {"_id": 0})
        reason = _pregame_invalid_reason(pick, now)
        if not reason:
            continue
        repl = next((c for c in candidates
                     if c.get("id") not in {l.get("canonical_pick_id") for l in legs}
                     and (c.get("canonical_event_id") or c.get("event_id") or c.get("event")) not in used_events), None)
        event_doc = {
            "slate_id": slate["slate_id"], "slate_date": slate["slate_date"],
            "event": "LEG_REPLACED" if repl else "LEG_INVALIDATED",
            "at": _now_iso(), "reason": reason, "rank": leg.get("rank"),
            "old_pick": leg, "new_pick": _leg(repl, leg.get("rank") or i + 1) if repl else None,
            "selector_version": selector_version,
            # Provenance so post-hoc audit can reconstruct why replacement
            # happened without relying on today's mutable rows.
            "evidence": {
                "off_board": bool(pick and pick.get("off_board")),
                "no_bet":    bool(pick and pick.get("no_bet")),
                "status":    pick.get("status") if pick else None,
                "no_real_book_line": bool(pick and pick.get("no_real_book_line")),
            },
        }
        await db[EVENTS].insert_one(event_doc)
        if repl:
            legs[i] = event_doc["new_pick"]
            used_events.add(legs[i].get("canonical_event_id"))
        else:
            legs[i] = {**leg, "invalidated": True, "invalidated_reason": reason,
                       "invalidated_at": event_doc["at"]}
        changed = True
    if changed:
        new_version = int(slate.get("version") or 1) + 1
        await db[SLATES].update_one(
            {"slate_id": slate["slate_id"]},
            {"$set": {"legs": legs, "version": new_version, "updated_at": _now_iso()}},
        )
        slate = {**slate, "legs": legs, "version": new_version}
    return slate


async def slate_events(db, slate_date: str) -> list[dict]:
    return [e async for e in db[EVENTS].find({"slate_date": slate_date}, {"_id": 0}).sort("at", 1)]


__all__ = [
    "get_official_slate", "freeze_official_slate", "reconcile_official_slate",
    "slate_events", "ensure_slate_indexes",
    "SLATES", "EVENTS", "FROZEN_WAGER_VERSION",
]
