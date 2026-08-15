"""Board lifecycle disposition — Phase 2A.5D CLOSURE (2026-08).

DELTA — attribute every Soccer board card that disappears from one
refresh to the next with an explicit disposition reason.  Consumers
funnel-log for observability; the store itself is a small MongoDB
collection ``board_lifecycle_events`` so History / Analytics can
consume it later without another rebuild.

Contract
--------
* Never a silent removal — every removed card carries a machine-
  readable reason.
* Odds/line updates on a still-qualifying wager preserve continuity
  (updated card, not delete+recreate).
* Legitimate removals (player OUT, market removed, event started,
  edge collapsed, LS < 85, canonical replacement, teammate dominance)
  still happen.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

logger = logging.getLogger("lockscore.board_lifecycle")

# Canonical dispositions per Phase 2A.5D directive Part 21.
DISPOSITION_REASONS = {
    "PLAYER_CONFIRMED_OUT",
    "PLAYER_UNAVAILABLE",
    "NOT_IN_SQUAD",
    "MARKET_REMOVED_BY_PROVIDER",
    "BOOK_LINE_REMOVED",
    "BOOK_LINE_CHANGED",
    "ODDS_CHANGED",
    "MODEL_PROBABILITY_CHANGED",
    "EDGE_FELL_BELOW_GATE",
    "LOCK_SCORE_BELOW_85",
    "EVENT_STARTED",
    "EVENT_EXPIRED",
    "CANONICAL_REPLACED",
    "RELATED_MARKET_DOMINATED",
    "SCORER_TEAM_RANK",
    "INTEGRITY_REJECTED",
    "PUBLICATION_FAILURE",
    "STILL_ON_BOARD_UPDATED_ODDS",
}


def _card_key(pick: dict) -> str:
    """Stable identity across refreshes.

    Uses (sport, event, market, selection) which survives a canonical
    replacement or odds update.  ``pick_date`` is intentionally NOT
    part of the key so a same-day odds refresh preserves continuity.
    """
    parts = [
        str(pick.get("sport") or ""),
        str(pick.get("event") or ""),
        str(pick.get("market") or ""),
        str(pick.get("selection") or ""),
    ]
    return "|".join(parts).lower()


def _classify_removal(before: dict, after_pool: dict[str, dict]) -> str:
    """Determine the disposition reason for a card no longer on board."""
    # Was the wager replaced by a canonical update with same key?
    key = _card_key(before)
    if key in after_pool:
        # Same identity survives → not a real removal.
        return "STILL_ON_BOARD_UPDATED_ODDS"

    # Event already started or past?
    ev_time = before.get("event_time") or before.get("commence_time")
    if isinstance(ev_time, str):
        try:
            t = datetime.fromisoformat(ev_time.replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            if t <= datetime.now(timezone.utc):
                return "EVENT_STARTED"
        except Exception:
            pass

    # Player availability signal.
    avail = str(before.get("player_availability") or "").lower()
    if avail in ("out", "confirmed_out"):
        return "PLAYER_CONFIRMED_OUT"
    if avail in ("suspended",):
        return "PLAYER_UNAVAILABLE"
    if avail == "not_in_squad":
        return "NOT_IN_SQUAD"

    # Lock Score fell.
    ls_before = float(before.get("published_lock_score")
                       or before.get("lock_score_v2")
                       or before.get("lock_score") or 0)
    if ls_before < 85.0:
        return "LOCK_SCORE_BELOW_85"

    # No book_odds or book_odds vanished on the after-pool sibling for
    # the same event/market ⇒ provider removed the line.
    if not before.get("book_odds"):
        return "BOOK_LINE_REMOVED"

    # Explicit teammate ranking demotion.
    if before.get("teammate_rank_demoted"):
        return "SCORER_TEAM_RANK"
    if before.get("related_market_dominated"):
        return "RELATED_MARKET_DOMINATED"

    # Canonical replacement — if a different sibling card for the same
    # event/market survived but with a different selection, this was a
    # replacement.
    ev = str(before.get("event") or "").lower()
    mk = str(before.get("market") or "").lower()
    for other in after_pool.values():
        if (str(other.get("event") or "").lower() == ev
                and str(other.get("market") or "").lower() == mk):
            return "CANONICAL_REPLACED"

    # Provider removed the whole market for the event.
    if not any(str(o.get("event") or "").lower() == ev
               for o in after_pool.values()):
        return "MARKET_REMOVED_BY_PROVIDER"

    return "EDGE_FELL_BELOW_GATE"


def diff_soccer_boards(
    before_picks: Iterable[dict],
    after_picks: Iterable[dict],
) -> list[dict]:
    """Return a per-card disposition list comparing before vs after."""
    before_list = [p for p in before_picks if (p.get("sport") == "Soccer")]
    after_list  = [p for p in after_picks  if (p.get("sport") == "Soccer")]
    after_map = {_card_key(p): p for p in after_list}

    events: list[dict] = []
    for b in before_list:
        key = _card_key(b)
        if key in after_map:
            # Card survived — possibly odds updated.
            a = after_map[key]
            if (b.get("book_odds") != a.get("book_odds")
                    or b.get("published_lock_score") != a.get("published_lock_score")):
                events.append({
                    "card_key": key,
                    "sport": "Soccer",
                    "disposition": "STILL_ON_BOARD_UPDATED_ODDS",
                    "before": {
                        "book_odds": b.get("book_odds"),
                        "lock_score": b.get("published_lock_score")
                                       or b.get("lock_score"),
                    },
                    "after": {
                        "book_odds": a.get("book_odds"),
                        "lock_score": a.get("published_lock_score")
                                       or a.get("lock_score"),
                    },
                })
        else:
            reason = _classify_removal(b, after_map)
            events.append({
                "card_key": key,
                "sport": "Soccer",
                "disposition": reason,
                "selection": b.get("selection"),
                "market": b.get("market"),
                "event": b.get("event"),
                "removed_at": datetime.now(timezone.utc).isoformat(),
                "prior_lock_score": (b.get("published_lock_score")
                                      or b.get("lock_score")),
            })

    return events


async def persist_lifecycle_events(db, events: list[dict]) -> int:
    """Best-effort persistence of disposition events."""
    if not events:
        return 0
    try:
        result = await db.board_lifecycle_events.insert_many(events)
        return len(result.inserted_ids)
    except Exception as e:
        logger.debug("board_lifecycle_events insert failed: %s", e)
        return 0


__all__ = [
    "DISPOSITION_REASONS",
    "diff_soccer_boards",
    "persist_lifecycle_events",
]
