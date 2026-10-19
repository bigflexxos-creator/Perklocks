"""
Build / refresh the NFL identity crosswalk.
===========================================

Populates ``nfl_identity_crosswalk`` strictly from data ALREADY in
Mongo — no new provider calls.  Sources (in order of trust):

  1. ``db.players`` — the authoritative registry (carries ``name``,
     ``team``, and in many rows an ``espn_id`` field).  Rows whose
     ``player_id`` is GSIS-shaped (``^\\d{2}-\\d{7}$``) provide the
     GSIS half; the ``espn_id`` field provides the ESPN half.
     Rows whose ``player_id`` is ``espn_<N>`` already carry the ESPN
     half and we only need a GSIS pairing (from other sources).

  2. ``nfl_player_weekly`` — nflverse feed keyed by GSIS.  Carries
     ``player_name`` and ``team``.  Join by normalised name + team to
     the ``db.players`` ESPN half.

  3. ``player_game_actuals`` — observed identity shapes already in
     use.  Captures the raw-numeric ESPN form (``"4361741"``) and the
     ``espn_<N>`` form so the crosswalk expands to BOTH alias shapes
     for lookup.

The script is strictly ADDITIVE (``upsert_identity`` merges aliases
and provenance; never clears a previously-known identifier).  Safe
to re-run as often as you like.

Usage:
    python scripts/maintenance/build_nfl_identity_crosswalk.py --dry-run
    python scripts/maintenance/build_nfl_identity_crosswalk.py --commit
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(dotenv_path=str(ROOT / "backend" / ".env"))
load_dotenv(dotenv_path=str(ROOT / ".env"))

from motor.motor_asyncio import AsyncIOMotorClient

from services.nfl_identity_crosswalk import (
    upsert_identity, ensure_indexes, _norm_name, CROSSWALK_COLL,
)

GSIS_RE = re.compile(r"^\d{2}-\d{7}$")
ESPN_PREFIX_RE = re.compile(r"^espn_(\d+)$")


def _extract_identifiers(doc: dict) -> tuple[str | None, str | None]:
    """From a ``db.players`` row, extract (gsis_id, espn_id)."""
    pid = str(doc.get("player_id") or "")
    gsis = pid if GSIS_RE.match(pid) else (
        doc.get("gsis_id") or doc.get("nfl_gsis_id") or None)
    espn = doc.get("espn_id") or None
    if not espn:
        m = ESPN_PREFIX_RE.match(pid)
        if m: espn = m.group(1)
    # provider_ids fallback — some players rows (per nfl_identity_merge.py)
    # carry a nested provider_ids map.
    pids = doc.get("provider_ids") or {}
    if not gsis:
        gsis = pids.get("gsis") or pids.get("nfl_gsis")
    if not espn:
        espn = pids.get("espn") or pids.get("nfl_espn")
    return (str(gsis) if gsis else None,
            str(espn) if espn else None)


async def main(args):
    uri = os.environ["MONGO_URL"]
    dbn = os.environ.get("DB_NAME", "lockscore_db")
    client = AsyncIOMotorClient(uri)
    db = client[dbn]

    await ensure_indexes(db)

    stats = {
        "db_players_scanned": 0, "db_players_upserted": 0,
        "weekly_scanned": 0, "weekly_merged": 0,
        "actuals_name_rows": 0, "actuals_merged": 0,
    }

    # ── 1. db.players ────────────────────────────────────────────────
    print("Phase 1: scanning db.players for NFL rows …")
    name_to_identifiers: dict[tuple[str, str], tuple[str | None, str | None, str, str]] = {}
    async for p in db.players.find({"sport": "nfl"}, {
        "player_id": 1, "name": 1, "team": 1, "espn_id": 1,
        "gsis_id": 1, "nfl_gsis_id": 1, "provider_ids": 1, "_id": 0,
    }):
        stats["db_players_scanned"] += 1
        name = p.get("name") or ""
        team = p.get("team") or ""
        n = _norm_name(name)
        if not n:
            continue
        gsis, espn = _extract_identifiers(p)
        if not gsis and not espn:
            continue
        key = (n, (team or "").lower())
        existing = name_to_identifiers.get(key)
        if existing:
            # Preserve the richer side if we already saw this
            # (name, team).
            egsis, eespn, ename, eteam = existing
            gsis = gsis or egsis
            espn = espn or eespn
        name_to_identifiers[key] = (gsis, espn, name, team)
        row = await upsert_identity(
            db, gsis_id=gsis, espn_id=espn, name=name, team=team,
            provenance=["db.players"],
        ) if args.commit else None
        if row:
            stats["db_players_upserted"] += 1

    # ── 2. nfl_player_weekly (GSIS keyed, nflverse) — pair GSIS with ESPN
    # via already-cached (name, team) match from phase 1. ────────────
    if "nfl_player_weekly" in await db.list_collection_names():
        print("Phase 2: scanning nfl_player_weekly for GSIS ↔ ESPN pairings …")
        async for w in db.nfl_player_weekly.find({}, {
            "player_id": 1, "player_name": 1, "player_display_name": 1,
            "team": 1, "espn_id": 1, "_id": 0,
        }):
            stats["weekly_scanned"] += 1
            pid = str(w.get("player_id") or "")
            # ``player_display_name`` carries the full name
            # ("Brock Purdy"); ``player_name`` is the ``B.Purdy``
            # abbreviated form.  Prefer the full name so the
            # ``name_norm`` join to ``db.players`` works.
            name = w.get("player_display_name") or w.get("player_name") or ""
            team = w.get("team") or ""
            n = _norm_name(name)
            if not n:
                continue
            gsis = pid if GSIS_RE.match(pid) else None
            espn = str(w.get("espn_id")) if w.get("espn_id") else None
            # If GSIS alone, pair with ESPN from phase-1 cache by
            # (name, team).  nflverse teams are 3-letter codes (``SF``)
            # while ``db.players`` carries full names ("San Francisco
            # 49ers"), so first try a NAME-ONLY lookup against the
            # phase-1 cache (which has normalized full names) and only
            # fall back to (name, team) when a lone name match is
            # ambiguous.
            if gsis and not espn:
                # Any (name_norm == n) row in phase-1 cache?
                name_hits = [v for (nk, _tk), v in name_to_identifiers.items()
                             if nk == n]
                if len(name_hits) == 1:
                    espn = name_hits[0][1]
                elif len(name_hits) > 1 and team:
                    # Multiple same-name players — can we disambiguate
                    # by team?  Try short-code → full-name mapping
                    # via substring match.
                    for g_, e_, _nm, tm_full in name_hits:
                        if team.lower() in (tm_full or "").lower():
                            espn = e_
                            break
            if not gsis and not espn:
                continue
            if args.commit:
                row = await upsert_identity(
                    db, gsis_id=gsis, espn_id=espn, name=name, team=team,
                    provenance=["nfl_player_weekly"],
                )
                if row:
                    stats["weekly_merged"] += 1

    # ── 3. player_game_actuals — observed identity shapes already in use.
    # For every (canonical_player_id, player_name) tuple seen in NFL
    # actuals, make sure the crosswalk has a row for that identity.
    # This captures the raw-numeric ESPN shape ("4361741") and the
    # ``espn_<N>`` prefixed shape AND the GSIS-shaped shape so the
    # resolver can match any of them.
    print("Phase 3: scanning player_game_actuals for observed identity shapes …")
    pipeline = [
        {"$match": {"sport": "nfl"}},
        {"$group": {"_id": {
            "cpid": "$canonical_player_id",
            "pid": "$player_id",
            "name": "$player_name",
            "team": "$team",
        }}},
    ]
    seen: set[tuple[str, str]] = set()
    async for g in db.player_game_actuals.aggregate(pipeline, allowDiskUse=True):
        d = g["_id"] or {}
        name = d.get("name") or ""
        team = d.get("team") or ""
        cpid = str(d.get("cpid") or "")
        pid  = str(d.get("pid")  or "")
        if not name:
            continue
        n = _norm_name(name)
        if not n:
            continue
        # Determine shapes.
        gsis = cpid if GSIS_RE.match(cpid) else (pid if GSIS_RE.match(pid) else None)
        espn_prefixed = ESPN_PREFIX_RE.match(cpid) or ESPN_PREFIX_RE.match(pid)
        espn = None
        if espn_prefixed:
            espn = espn_prefixed.group(1)
        else:
            # Raw numeric shape ("4361741") lives in cpid for new rows.
            for cand in (cpid, pid):
                if cand and cand.isdigit() and 3 <= len(cand) <= 8 and not GSIS_RE.match(cand):
                    espn = cand
                    break
        if not gsis and not espn:
            continue
        key = (gsis or "", espn or "")
        if key in seen:
            continue
        seen.add(key)
        stats["actuals_name_rows"] += 1
        if args.commit:
            row = await upsert_identity(
                db, gsis_id=gsis, espn_id=espn, name=name, team=team,
                provenance=["player_game_actuals"],
            )
            if row:
                stats["actuals_merged"] += 1

    total = await db[CROSSWALK_COLL].count_documents({})
    pair = await db[CROSSWALK_COLL].count_documents(
        {"gsis_player_id": {"$ne": None, "$exists": True},
         "espn_player_id": {"$ne": None, "$exists": True}}
    )
    print("\n── Summary ──")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    print(f"  crosswalk total rows:        {total}")
    print(f"  crosswalk GSIS↔ESPN pairs:   {pair}")
    if not args.commit:
        print("\n(dry-run) Re-run with --commit to persist.")

    client.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--commit", action="store_true", help="actually write to the crosswalk")
    p.add_argument("--dry-run", action="store_true")
    asyncio.run(main(p.parse_args()))
