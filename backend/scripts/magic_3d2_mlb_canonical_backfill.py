"""MAGIC 3D.2 — MLB canonical_player_id backfill.

Metadata-only stamp on picks.canonical_player_id where deterministic
name normalization uniquely resolves to a single mlb_statcast_players
or mlb_stuff_plus_players row's player_id.  Session-D safety: ambiguous
matches leave canonical_player_id unset.

Never mutates outcomes, market, selection, book_odds, closing_odds,
status, settled_at, units_profit, or units_risked.  Idempotent.
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Optional

sys.path.insert(0, "/app/backend")
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient
from services.magic.identity_join import normalize_name


async def build_mlb_name_index(db) -> dict:
    """Return {normalized_name: [player_ids]} pulled from both
    statcast + stuff+.  Multiple ids under same name → ambiguous
    (never resolved)."""
    idx: dict[str, set] = {}
    for coll in ("mlb_statcast_players", "mlb_stuff_plus_players"):
        async for r in db[coll].find({},
                {"player_id": 1, "name": 1, "_id": 0}):
            n = normalize_name(r.get("name") or "")
            pid = str(r.get("player_id") or "").strip()
            if not n or not pid:
                continue
            idx.setdefault(n, set()).add(pid)
    return idx


async def run(*, write: bool = False) -> dict:
    db = AsyncIOMotorClient(os.getenv("MONGO_URL"))["lockscore_db"]
    idx = await build_mlb_name_index(db)
    counts = {
        "scanned": 0, "already_stamped": 0,
        "mapped": 0, "ambiguous": 0, "no_candidate": 0,
    }
    ops = []
    async for p in db.picks.find(
        {"sport": "MLB"},
        {"id": 1, "player_name": 1, "selection": 1,
         "canonical_player_id": 1, "_id": 0},
    ):
        counts["scanned"] += 1
        if p.get("canonical_player_id"):
            counts["already_stamped"] += 1
            continue
        n = normalize_name(p.get("player_name") or p.get("selection") or "")
        if not n:
            counts["no_candidate"] += 1
            continue
        cands = idx.get(n) or set()
        if len(cands) == 1:
            counts["mapped"] += 1
            if write:
                pid = next(iter(cands))
                ops.append((p.get("id"), pid))
                if len(ops) >= 500:
                    await _flush(db, ops); ops.clear()
        elif len(cands) > 1:
            counts["ambiguous"] += 1
        else:
            counts["no_candidate"] += 1
    if write and ops:
        await _flush(db, ops)
    return counts


async def _flush(db, ops):
    from pymongo import UpdateOne
    bulk = [UpdateOne({"id": pid},
                       {"$set": {"canonical_player_id": src_id,
                                 "canonical_player_id_source":
                                     "magic_3d2_deterministic_name"}})
            for pid, src_id in ops if pid]
    if bulk:
        await db.picks.bulk_write(bulk, ordered=False)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    c = asyncio.run(run(write=args.write))
    print("=" * 60)
    print(f"MAGIC 3D.2 MLB backfill — {'WRITE' if args.write else 'DRY_RUN'}")
    print("=" * 60)
    for k, v in c.items():
        print(f"  {k:<20}  {v:>6}")


if __name__ == "__main__":
    main()
