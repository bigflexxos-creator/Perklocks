"""Soccer Transfer Registry — Session 10.1 Live-Truth Closure.

Canonical source of truth for a player's CURRENT team.  Historical
player logs (Sirius goal history) describe FORM.  This registry
answers: "as of today, whose roster is this player on?"

The registry is stored in ``db.soccer_transfer_registry`` with the
shape:

    {
        "player_name":       "Robbie Ure",
        "aliases":           ["Robbie Uhre", "R. Ure"],
        "current_team":      "Sevilla",
        "current_league":    "La Liga",
        "previous_team":     "IK Sirius",
        "previous_league":   "Allsvenskan",
        "transfer_date":     "2026-07-15",
        "source":            "manual|api-football|transfermarkt|wikipedia",
        "verified_at":       "2026-09-16T00:00:00Z",
    }

Entries are additive and never overwrite existing history.
"""
from __future__ import annotations

from typing import Optional
from datetime import datetime, timezone


async def get_current_team(db, player_name: str) -> Optional[dict]:
    """Return the canonical current-team registry row, or None."""
    if not player_name:
        return None
    q = {
        "$or": [
            {"player_name": {"$regex": f"^{player_name}$", "$options": "i"}},
            {"aliases":     {"$regex": f"^{player_name}$", "$options": "i"}},
        ]
    }
    return await db.soccer_transfer_registry.find_one(q, {"_id": 0})


async def seed_known_transfers(db) -> int:
    """Idempotent seed of manually-verified transfers.  Add rows here
    when a transfer is observed on the live board.  DO NOT delete
    historical match logs — only the CURRENT-team pointer moves."""
    KNOWN = [
        {
            "player_name":     "Robbie Ure",
            "aliases":         ["Robbie Uhre", "R. Ure", "R. Uhre"],
            "current_team":    "Sevilla",
            "current_league":  "La Liga",
            "previous_team":   "IK Sirius",
            "previous_league": "Allsvenskan",
            "transfer_date":   "2026-07-15",
            "source":          "manual_verification_2026_09_16",
            "verified_at":     datetime.now(timezone.utc).isoformat(),
            "notes":           "Confirmed transfer from IK Sirius (Allsvenskan) to Sevilla (La Liga). Prior-season Wiki top-scorer records still describe FORM but MUST NOT establish current roster.",
        },
        # Additional transfers can be added here.  When a live-slate
        # ingest surfaces a stale attachment via the stale-transfer-scan
        # endpoint, the corrective step is:
        #   1. verify the transfer against a public source
        #   2. add the entry here
        #   3. rerun the scan — the stale row must now flip to
        #      CURRENT_TEAM_MISMATCH terminal reason and be off-boarded
    ]
    n_seeded = 0
    for row in KNOWN:
        res = await db.soccer_transfer_registry.update_one(
            {"player_name": row["player_name"]},
            {"$set": row},
            upsert=True,
        )
        if res.upserted_id is not None or res.modified_count > 0:
            n_seeded += 1
    return n_seeded


async def scan_stale_transfer_attachments(
    db, pick_date: Optional[str] = None,
) -> dict:
    """Scan today's Soccer picks for canonical current-team violations.

    For every player-prop candidate:
        * resolve the pick's player name → registry current_team
        * verify current_team against the event's home/away
        * classify: CURRENT | STALE_PLAYER_TEAM | CURRENT_TEAM_MISMATCH

    Returns per-player and per-terminal-reason counts.  DOES NOT
    mutate any pick — this is a read-only diagnostic.  Quarantining
    is a separate action (add off_board_reason to the offending
    canonical rows).
    """
    from services.soccer_player_authority import (
        verify_current_team, TerminalReason,
    )
    if pick_date is None:
        pick_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    q = {
        "sport": "Soccer",
        "pick_date": pick_date,
        "$or": [
            {"market": {"$regex": "Anytime|Goal Scorer|To Score",
                        "$options": "i"}},
            {"market": {"$regex": "Score or Assist",
                        "$options": "i"}},
        ],
    }
    total = 0
    current  = 0
    stale_team = 0
    mismatch = 0
    unverified = 0
    affected_ids: list[str] = []
    per_player: dict[str, dict] = {}

    async for p in db.picks.find(q, {
        "id": 1, "market": 1, "selection": 1, "event": 1,
        "team": 1, "league": 1, "lock_score": 1, "pick_date": 1,
    }):
        total += 1
        market = p.get("market") or ""
        selection = p.get("selection") or ""
        # Parse player name — try selection first ("R.Ure"), then market prefix.
        player_name = _parse_player_from_market(market, selection)
        event = p.get("event") or ""
        home, away = _parse_event_sides(event)
        registry = await get_current_team(db, player_name)
        canonical_cur = registry.get("current_team") if registry else None
        is_cur, reason, note = verify_current_team(
            player_name=player_name,
            canonical_current_team=canonical_cur,
            event_home_team=home,
            event_away_team=away,
            pick_team_hint=p.get("team"),
        )
        if is_cur:
            if canonical_cur is None:
                unverified += 1
            else:
                current += 1
        else:
            if reason == TerminalReason.STALE_PLAYER_TEAM:
                stale_team += 1
            elif reason == TerminalReason.CURRENT_TEAM_MISMATCH:
                mismatch += 1
            affected_ids.append(p.get("id"))
        per_player.setdefault(player_name or "?", {
            "player": player_name, "n_picks": 0,
            "canonical_current_team": canonical_cur,
            "verdict": "CURRENT" if is_cur else (reason.value if reason else "?"),
            "sample_event": event,
        })
        per_player[player_name or "?"]["n_picks"] += 1

    return {
        "pick_date":                 pick_date,
        "scanned":                   total,
        "current":                   current,
        "unverified_no_registry":    unverified,
        "stale_player_team":         stale_team,
        "current_team_mismatch":     mismatch,
        "sanity_sum":                current + unverified + stale_team + mismatch == total,
        "affected_pick_ids":         affected_ids[:100],
        "affected_pick_count":       len(affected_ids),
        "sample_players":            list(per_player.values())[:50],
    }


async def quarantine_stale_picks(
    db, pick_ids: list[str], reason: str = "STALE_TRANSFER_AUTOCORRECTION",
) -> int:
    """Mark the given canonical picks as off-board with the given
    terminal reason.  Does NOT delete rows — preserves audit trail."""
    if not pick_ids:
        return 0
    res = await db.picks.update_many(
        {"id": {"$in": pick_ids}},
        {"$set": {
            "off_board_reason":            reason,
            "publication_state":           "OFF_BOARD",
            "stale_transfer_flagged_at":   datetime.now(timezone.utc).isoformat(),
        }},
    )
    return int(res.modified_count)


def _parse_player_from_market(market: str, selection: str) -> Optional[str]:
    """Extract the player name from a market string like
    ``Robbie Ure - Anytime Goal Scorer`` or a selection like
    ``Robbie Ure to Score``."""
    if selection and " to Score" in selection:
        return selection.split(" to Score")[0].strip()
    if selection and " to Score or Assist" in selection:
        return selection.split(" to Score or Assist")[0].strip()
    if market:
        # "Robbie Ure - Anytime Goal Scorer"
        for sep in (" - Anytime", " Anytime", " - To Score", " To Score",
                    " Anytime Goal Scorer", " Score or Assist"):
            if sep in market:
                return market.split(sep)[0].strip(" -")
    return None


def _parse_event_sides(event: str) -> tuple[Optional[str], Optional[str]]:
    """Event format: 'Home @ Away' or 'Home vs Away'."""
    if not event:
        return None, None
    for sep in (" @ ", " vs ", " v "):
        if sep in event:
            a, b = event.split(sep, 1)
            return a.strip(), b.strip()
    return None, None
