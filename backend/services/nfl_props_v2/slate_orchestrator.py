"""Slate-scan orchestrator — universal NFL prop discovery on the CURRENT slate.

Design:
  1. Enumerate current-slate NFL games (from db.picks pick_date=today).
  2. Enumerate distinct canonical players who ALREADY have real
     sportsbook props on the board (never manufacture candidates).
  3. Run ``engine.evaluate_player_across_markets`` per player — the
     engine reuses cached game context per game and computes each
     distribution once.
  4. Return the full trace for admin/acceptance inspection.

NOTHING here modifies the main /picks/today pipeline or reduces
existing NFL prop coverage.  This orchestrator READS the same board
and produces an enhanced discovery layer that the frontend may render
alongside the existing publications.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from .engine import evaluate_player_across_markets


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def discover_slate_best_bets(db, *, sample_by_position: bool = True,
                                    max_players_per_position: int = 3,
                                    pick_date: Optional[str] = None) -> dict:
    """Return the compact acceptance-test payload described in the spec."""
    pick_date = pick_date or _today_str()

    # ── 1. Games ──────────────────────────────────────────────────
    games_cursor = db.picks.aggregate([
        {"$match": {"sport": "NFL", "pick_date": pick_date}},
        {"$group": {
            "_id": {"event": "$event", "canonical_event_id": "$canonical_event_id"},
            "home_team": {"$first": "$home_team"},
            "away_team": {"$first": "$away_team"},
            "event_time": {"$first": "$event_time"},
            "event_id":   {"$first": "$event_id"},
            "player_count": {"$sum": 1},
        }},
        {"$limit": 8},
    ])
    games = [g async for g in games_cursor]

    # ── 2. Enumerate distinct candidate players per game ──────────
    per_position_bucket = {"QB": [], "RB": [], "WR": [], "TE": []}
    picks_seen = 0
    async for p in db.picks.find(
        {"sport": "NFL", "pick_date": pick_date, "elite_player_name": {"$exists": True, "$ne": None}},
        {"_id": 0, "elite_player_name": 1, "player_name": 1,
         "canonical_player_id": 1, "position": 1, "team": 1,
         "home_team": 1, "away_team": 1, "event": 1,
         "canonical_event_id": 1, "event_id": 1, "event_time": 1,
         "market": 1},
    ):
        picks_seen += 1
        pos = (p.get("position") or "").upper()
        if pos not in per_position_bucket:
            # Guess position from market string when position missing
            m = (p.get("market") or "").lower()
            if "pass" in m and "att" not in m and "comp" not in m: pos = "QB"
            elif "pass" in m: pos = "QB"
            elif "rush" in m: pos = "RB"
            elif "recept" in m or "rec yd" in m or "reception" in m: pos = "WR"
            else: pos = "WR"
        if pos not in per_position_bucket:
            continue
        # Dedupe by canonical_player_id or name
        pid = p.get("canonical_player_id") or p.get("elite_player_name") or p.get("player_name")
        if any(x.get("_key") == pid for x in per_position_bucket[pos]):
            continue
        if len(per_position_bucket[pos]) >= max_players_per_position:
            continue
        per_position_bucket[pos].append({
            "_key": pid,
            "player_name": p.get("elite_player_name") or p.get("player_name"),
            "canonical_player_id": p.get("canonical_player_id"),
            "position": pos,
            "team": p.get("team") or p.get("home_team"),
            "home_team": p.get("home_team"), "away_team": p.get("away_team"),
            "event": p.get("event"),
            "canonical_event_id": p.get("canonical_event_id"),
            "event_id": p.get("event_id"), "event_time": p.get("event_time"),
        })

    # ── 3. Evaluate ───────────────────────────────────────────────
    evaluations = []
    for pos, players in per_position_bucket.items():
        for p in players:
            opp = None
            if p.get("home_team") and p.get("away_team"):
                opp = p["away_team"] if p.get("team") == p.get("home_team") else p.get("home_team")
            game_doc = {
                "home_team": p.get("home_team"),
                "away_team": p.get("away_team"),
                "event": p.get("event"),
                "canonical_event_id": p.get("canonical_event_id"),
                "event_id": p.get("event_id"),
                "event_time": p.get("event_time"),
            }
            try:
                ev = await evaluate_player_across_markets(
                    db,
                    player_name=p.get("player_name") or "",
                    canonical_player_id=p.get("canonical_player_id"),
                    position=pos, team=p.get("team"), opponent=opp,
                    game=game_doc,
                )
                evaluations.append(ev.as_dict())
            except Exception as e:
                evaluations.append({
                    "player_name": p.get("player_name"),
                    "position": pos,
                    "error": f"{type(e).__name__}: {e}",
                })

    return {
        "pick_date": pick_date,
        "picks_scanned": picks_seen,
        "games_found": len(games),
        "evaluations": evaluations,
        "coverage_snapshot": {
            "by_position": {k: len(v) for k, v in per_position_bucket.items()},
        },
        "engine_version": "nfl_props_v2:2026-06-21",
    }


__all__ = ["discover_slate_best_bets"]
