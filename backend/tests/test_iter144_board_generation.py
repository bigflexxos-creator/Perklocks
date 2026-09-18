"""Iteration 144 acceptance gate — committed board generations."""
import asyncio

from services import board_generation as bg
from services import board_snapshot_cache as bsc


class _Coll:
    def __init__(self): self.docs = {}
    async def insert_one(self, d): self.docs[d["_id"]] = dict(d)
    async def update_one(self, q, u, upsert=False):
        d = self.docs.get(q["_id"])
        if d is None and upsert:
            d = self.docs[q["_id"]] = {"_id": q["_id"]}
        if d is not None:
            d.update(u.get("$set", {})); d.update(u.get("$setOnInsert", {}))
    async def find_one(self, q, *_a, **_k): return self.docs.get(q["_id"])
    async def find_one_and_update(self, q, u, upsert=False):
        d = self.docs.get(q["_id"])
        if d is None: return None
        ok = any((d.get("generation_id") == c.get("generation_id")) if "generation_id" in c and not isinstance(c["generation_id"], dict)
                 else ("generation_id" not in d) for c in q.get("$or", []))
        if not ok: return None
        d.update(u["$set"]); return d


class _DB:
    def __init__(self): self.c = _Coll()
    def __getitem__(self, name): return self.c


def _reset(monkeypatch):
    db = _DB()
    monkeypatch.setattr(bg, "_db", lambda: db)
    bg._building.clear()
    bg._active.update({"generation_id": "gen_A", "board_version": "bvA", "committed_at": "t0", "revision": 1})
    asyncio.run(db.c.insert_one({"_id": "active", "generation_id": "gen_A", "board_version": "bvA", "revision": 1}))
    return db


def test_failed_build_keeps_A_active_and_snapshots(monkeypatch):
    _reset(monkeypatch)
    bsc.clear_cache(); bsc.put_snapshot({"k": 1}, {"picks": [1, 2, 3]}, "bvA")
    gid = asyncio.run(bg.begin("ALL"))
    assert bg.is_building()
    # pinned: TTL ignored, and new snapshots are refused while building
    monkeypatch.setattr(bsc, "_TTL_SECONDS", -1)
    assert bsc.get_snapshot({"k": 1}).board_version == "bvA"
    bsc.put_snapshot({"k": 2}, {"picks": [1]}, "partial")
    assert bsc.get_snapshot({"k": 2}) is None
    assert asyncio.run(bg.validate(gid, 0)) is False       # empty full-board build cannot commit
    asyncio.run(bg.fail(gid, "provider down"))
    assert not bg.is_building()
    assert bg.active()["generation_id"] == "gen_A" and bg.active()["revision"] == 1
    monkeypatch.setattr(bsc, "_TTL_SECONDS", 15)
    assert bsc.get_snapshot({"k": 1}).board_version == "bvA"   # A snapshot survived (not invalidated)


def test_commit_is_single_atomic_transition_and_cas(monkeypatch):
    db = _reset(monkeypatch)
    bsc.clear_cache(); bsc.put_snapshot({"k": 1}, {"picks": [1]}, "bvA")
    gid = asyncio.run(bg.begin("ALL"))
    assert asyncio.run(bg.validate(gid, 300))
    assert asyncio.run(bg.commit(gid, "bvB", 300, 120)) is True
    a = bg.active()
    assert a["generation_id"] == gid and a["board_version"] == "bvB" and a["revision"] == 2
    assert db.c.docs["active"]["generation_id"] == gid
    assert bsc.get_snapshot({"k": 1}) is None                 # exactly one invalidation on commit
    # concurrent generation that expected A must NOT overwrite B blindly
    bg._active.update({"generation_id": "gen_A", "revision": 1, "board_version": "bvA"})  # stale in-process view
    gid2 = asyncio.run(bg.begin("ALL"))
    assert asyncio.run(bg.commit(gid2, "bvC", 10, 5)) is False
    assert db.c.docs["active"]["generation_id"] == gid
    assert db.c.docs[gid2]["state"] == "SUPERSEDED"


def test_identical_population_is_noop_commit(monkeypatch):
    _reset(monkeypatch)
    bsc.clear_cache(); bsc.put_snapshot({"k": 1}, {"picks": [1]}, "bvA")
    gid = asyncio.run(bg.begin("MLB"))
    assert asyncio.run(bg.commit(gid, "bvA", 300, 120)) is True
    assert bg.active()["generation_id"] == "gen_A" and bg.active()["revision"] == 1
    assert bsc.get_snapshot({"k": 1}) is not None            # no churn


def test_manifest_is_committed_and_secret_free():
    m = bg.manifest("bvX", 396, 181)
    assert m["generation_state"] in ("COMMITTED", "LEGACY_UNTRACKED") and m["schema_version"] == "board.v1"
    assert "mongo" not in str(m).lower() and "password" not in str(m).lower()
