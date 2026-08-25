"""NBA canonical-opponent enrichment + team_game_actuals normalizer
(Session F3, 2026-06-XX).

--------------------------------------------------------------
F3a  — player_game_actuals opponent enrichment
--------------------------------------------------------------
Deterministic authoritative join:
    player_game_actuals.(event_id, canonical_player_id)
      ↔ player_game_logs.(game_id, player_id)

NOTE: `player_game_actuals.team` reflects the player's SEASON team,
which can differ from the team he played for in the given game
(trades, mid-season roster moves).  We therefore treat the SEASON team
as unreliable and instead read the GAME-SPECIFIC team from
player_game_logs.team plus is_home / opp_team_id.

Team-ID resolution:
    player_game_logs stores team abbrev in `team` (e.g., "ATL") and
    opp_team_id as an ESPN numeric team id (e.g., "29").  We map
    ESPN numeric team_id → 3-letter abbrev via the `players`
    collection registry (sport=nba, team_id → team).

For each pga row:
    canonical_team_id     = pgl.team.upper()
    canonical_opponent_id = espn_id_to_abbrev[pgl.opp_team_id]
    canonical_event_id    = pgl.game_id (== pga.event_id)
    home_away             = "home" if pgl.is_home == 1 else "away"

Conflict guard: existing non-empty canonical fields that disagree
with pgl are NEVER overwritten — recorded as identity_conflict.

--------------------------------------------------------------
F3b  — team_game_actuals normalization
--------------------------------------------------------------
Authoritative completed-game source: `player_game_logs` (sport=nba).
Each pgl row carries the ESPN game-level fields verbatim:
    game_id, date, home_team_id, away_team_id,
    home_team_score, away_team_score.

We DEDUPE by game_id to get one row per game (2,788 games).
For each game we write TWO team_game_actuals rows (home + away
perspectives), each with:
    sport = "nba"
    event_id / canonical_event_id = game_id
    canonical_team_id      = home_abbrev (or away_abbrev)
    canonical_opponent_id  = away_abbrev (or home_abbrev)
    home_away              = "home" | "away"
    actuals = {
        "team_score": int(home_team_score) | int(away_team_score),
        "opponent_score": ...,
        "result": "W" | "L" | "T",
    }
    event_time             = ISO from date
    source                 = "player_game_logs_dedup_v1"

Idempotent: existing team_game_actuals rows keyed on
(sport, event_id, canonical_team_id) are skipped.
"""
from __future__ import annotations
from typing import Any
from datetime import datetime, timezone


async def _build_espn_team_map(db) -> dict[str, str]:
    """ESPN numeric team_id → 3-letter abbrev (30 NBA teams)."""
    mp: dict[str, str] = {}
    async for p in db.players.find({"sport": "nba"},
                                    {"team_id": 1, "team": 1}):
        tid = str(p.get("team_id") or "")
        tab = (p.get("team") or "").strip().upper()
        if tid and tab and tid not in mp:
            mp[tid] = tab
    return mp


async def enrich_nba_opponent_batch(db, *, batch_size: int = 1000,
                                    limit: int = 0,
                                    dry_run: bool = False) -> dict:
    """F3a: Enrich NBA player_game_actuals canonical opponent identity
    via player_game_logs join.
    """
    stats = {"scanned": 0, "would_update": 0, "updated": 0,
             "already_stamped": 0, "unresolved": 0,
             "identity_conflict": 0, "batches": 0, "unmapped_opp": 0}

    espn_map = await _build_espn_team_map(db)
    if len(espn_map) < 25:
        return {"error": "espn_team_map_incomplete",
                "map_size": len(espn_map)}

    query = {"sport": "nba",
             "$or": [
                 {"canonical_opponent_id": {"$exists": False}},
                 {"canonical_opponent_id": None},
             ]}
    cursor = db.player_game_actuals.find(query, {
        "_id": 1, "event_id": 1, "canonical_player_id": 1,
        "player_id": 1, "canonical_team_id": 1,
        "canonical_opponent_id": 1, "canonical_event_id": 1,
        "home_away": 1,
    })
    if limit:
        cursor = cursor.limit(limit)

    # Cache pgl rows by (game_id, player_id) for THIS run's rows
    pgl_cache: dict[tuple, dict] = {}

    batch_ops = []
    async for p in cursor:
        stats["scanned"] += 1
        eid = str(p.get("event_id") or "")
        cpid = p.get("canonical_player_id") or p.get("player_id")
        if not eid or cpid is None:
            stats["unresolved"] += 1
            continue
        try:
            pid_int = int(cpid)
        except (ValueError, TypeError):
            stats["unresolved"] += 1
            continue
        key = (eid, pid_int)
        if key not in pgl_cache:
            row = await db.player_game_logs.find_one({
                "sport": "nba", "game_id": eid, "player_id": pid_int
            }, {"team": 1, "is_home": 1, "opp_team_id": 1,
                "home_team_id": 1, "away_team_id": 1})
            pgl_cache[key] = row or {}
        pgl = pgl_cache[key]
        if not pgl:
            stats["unresolved"] += 1
            continue

        pteam = (pgl.get("team") or "").strip().upper()
        opp_tid = str(pgl.get("opp_team_id") or "")
        is_home = pgl.get("is_home")
        if not pteam or not opp_tid:
            stats["unresolved"] += 1
            continue
        opp_abbr = espn_map.get(opp_tid)
        if not opp_abbr:
            # Try fallback via home/away pair
            hid = str(pgl.get("home_team_id") or "")
            aid = str(pgl.get("away_team_id") or "")
            if is_home == 1 and aid:
                opp_abbr = espn_map.get(aid)
            elif is_home == 0 and hid:
                opp_abbr = espn_map.get(hid)
        if not opp_abbr:
            stats["unmapped_opp"] += 1
            continue
        new_ha = "home" if is_home == 1 else "away"

        # Conflict guard
        ex_ctid = p.get("canonical_team_id")
        ex_cop  = p.get("canonical_opponent_id")
        if ex_ctid and ex_ctid.upper() != pteam:
            stats["identity_conflict"] += 1
            continue
        if ex_cop and ex_cop.upper() != opp_abbr:
            stats["identity_conflict"] += 1
            continue

        set_doc: dict[str, Any] = {}
        if not ex_ctid: set_doc["canonical_team_id"] = pteam
        if not ex_cop:  set_doc["canonical_opponent_id"] = opp_abbr
        if not p.get("canonical_event_id"): set_doc["canonical_event_id"] = eid
        if not p.get("home_away"): set_doc["home_away"] = new_ha
        if not set_doc:
            stats["already_stamped"] += 1
            continue
        set_doc["opponent_enriched_at"] = datetime.now(timezone.utc) \
            .isoformat().replace("+00:00", "Z")
        set_doc["opponent_enrichment_source"] = "nba_pgl_join_v1"
        stats["would_update"] += 1
        if not dry_run:
            from pymongo import UpdateOne
            batch_ops.append(UpdateOne({"_id": p["_id"]}, {"$set": set_doc}))
            if len(batch_ops) >= batch_size:
                r = await db.player_game_actuals.bulk_write(batch_ops,
                                                             ordered=False)
                stats["updated"] += r.modified_count
                stats["batches"] += 1
                batch_ops = []
    if batch_ops:
        r = await db.player_game_actuals.bulk_write(batch_ops,
                                                     ordered=False)
        stats["updated"] += r.modified_count
        stats["batches"] += 1
    return stats


async def normalize_nba_team_actuals(db, *, batch_size: int = 500,
                                     limit: int = 0,
                                     dry_run: bool = False) -> dict:
    """F3b: Normalize NBA completed games from player_game_logs into
    team_game_actuals (writing BOTH home and away perspectives).
    """
    stats = {"games_scanned": 0, "rows_would_insert": 0,
             "rows_inserted": 0, "rows_already_present": 0,
             "unresolved": 0, "unmapped_team": 0, "batches": 0}

    espn_map = await _build_espn_team_map(db)
    if len(espn_map) < 25:
        return {"error": "espn_team_map_incomplete",
                "map_size": len(espn_map)}

    # Dedup pgl rows by game_id, taking first-seen game meta.
    seen: dict[str, dict] = {}
    async for r in db.player_game_logs.find({"sport": "nba"}, {
            "game_id": 1, "home_team_id": 1, "away_team_id": 1,
            "home_team_score": 1, "away_team_score": 1, "date": 1,
            "season": 1,
    }):
        gid = r.get("game_id")
        if gid and gid not in seen:
            seen[gid] = r

    games = list(seen.items())
    if limit:
        games = games[:limit]

    batch_ops = []
    from pymongo import UpdateOne
    for gid, g in games:
        stats["games_scanned"] += 1
        hid = str(g.get("home_team_id") or "")
        aid = str(g.get("away_team_id") or "")
        try:
            hs = int(g.get("home_team_score"))
            as_ = int(g.get("away_team_score"))
        except (TypeError, ValueError):
            stats["unresolved"] += 1
            continue
        date = g.get("date")
        if not hid or not aid or not date:
            stats["unresolved"] += 1
            continue
        h_ab = espn_map.get(hid)
        a_ab = espn_map.get(aid)
        if not h_ab or not a_ab:
            stats["unmapped_team"] += 1
            continue
        try:
            event_time = datetime.strptime(date, "%Y-%m-%d") \
                .replace(tzinfo=timezone.utc).isoformat() \
                .replace("+00:00", "Z")
        except Exception:
            event_time = None

        def _result(ts, os_):
            if ts > os_: return "W"
            if ts < os_: return "L"
            return "T"

        for tab, opp_ab, ha, ts, os_ in (
                (h_ab, a_ab, "home", hs, as_),
                (a_ab, h_ab, "away", as_, hs)):
            key_q = {"sport": "nba", "event_id": gid,
                     "canonical_team_id": tab}
            # Check if this exact perspective already exists (idempotent)
            existing = await db.team_game_actuals.find_one(key_q,
                                                           {"_id": 1})
            if existing:
                stats["rows_already_present"] += 1
                continue
            doc = {
                "sport": "nba",
                "event_id": gid,
                "canonical_event_id": gid,
                "canonical_team_id": tab,
                "canonical_opponent_id": opp_ab,
                "home_away": ha,
                "team_score": ts,
                "opponent_score": os_,
                "result": _result(ts, os_),
                "actuals": {
                    "team_score": ts,
                    "opponent_score": os_,
                    "result": _result(ts, os_),
                },
                "event_time": event_time,
                "season": g.get("season"),
                "source": "player_game_logs_dedup_v1",
                "source_record_id": gid,
                "ingested_at": datetime.now(timezone.utc).isoformat()
                    .replace("+00:00", "Z"),
            }
            stats["rows_would_insert"] += 1
            if not dry_run:
                batch_ops.append(UpdateOne(key_q, {"$setOnInsert": doc},
                                            upsert=True))
                if len(batch_ops) >= batch_size:
                    r_ = await db.team_game_actuals.bulk_write(batch_ops,
                                                                ordered=False)
                    stats["rows_inserted"] += r_.upserted_count
                    stats["batches"] += 1
                    batch_ops = []
    if batch_ops:
        r_ = await db.team_game_actuals.bulk_write(batch_ops,
                                                    ordered=False)
        stats["rows_inserted"] += r_.upserted_count
        stats["batches"] += 1
    return stats


__all__ = ["enrich_nba_opponent_batch", "normalize_nba_team_actuals"]
