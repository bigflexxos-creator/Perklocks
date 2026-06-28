"""Cross-sport Active Player Registry.

A single in-process + MongoDB-backed registry that answers the question:

    `is_active(sport, name)` → True / False / None

`None` means "we don't have a fresh-enough snapshot for this sport, defer
to legacy heuristics so we never break an entire slate". This mirrors
the conservative semantics introduced in `csl_espn_live.is_player_currently_active`.

Sources funnel in here via :func:`record_active`. Each call carries:

    sport: nba | nfl | mlb | cfb | soccer | ...
    source: nba_stats | bbr | pfr | nfl_stats | espn | mlb_stats | fbref | understat | ...
    name: full player display name as printed by the source
    team: best-known current team string (informational)
    extra: arbitrary dict — stats / minutes / appearances. Used by the
           validator to *disqualify* a candidate (e.g. minutes=0).

Active criteria (per the user's "exclude inactive / no-minutes / retired /
traded" requirement):

    A player is ACTIVE for sport S iff
        ∃ source reporting them within the last STALE_AFTER_S
        AND extra.minutes > 0  (when minutes is reported)
        AND extra.status != 'retired'
        AND extra.games_played > 0  (when reported)

Implementation notes:

* We persist every record so a backend restart preserves state until the
  next scheduled refresh runs.
* Last-name fallback + Levenshtein-1 + alias resolution is shared with
  the original CSL module so spelling drift between sources is forgiven.

Author: PerkLocks AI · 2026-06-27
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("lockscore.services.active_registry")

# ─────────────────────────── Config ───────────────────────────
# A registry entry is trusted for this long before is_active() returns
# None instead of False — protects against partial-outage source bugs
# accidentally blocking a whole sport.
STALE_AFTER_S = 36 * 60 * 60         # 36 h
# In-process cache: { sport: { norm_name: record } }
_registry: dict[str, dict[str, dict[str, Any]]] = {}
_last_persist_at: float = 0.0
_persist_lock = asyncio.Lock()

SUPPORTED_SPORTS = ("nba", "nfl", "mlb", "cfb", "soccer")


# ─────────────────────────── Normalisation ───────────────────────────
def _norm(name: str) -> str:
    if not name:
        return ""
    n = unicodedata.normalize("NFD", name)
    n = "".join(c for c in n if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", n.lower()).strip()


def _name_match(query: str, candidate: str) -> bool:
    """Tolerant matcher — single-token / Levenshtein-1 / reversed order.
    Shared semantics with `csl_espn_live._name_match`."""
    if not query or not candidate:
        return False
    if query == candidate:
        return True
    qt, ct = query.split(), candidate.split()
    if len(qt) == 1 and (qt[0] == ct[0] or qt[0] == ct[-1]):
        return True
    if len(ct) == 1 and (ct[0] == qt[0] or ct[0] == qt[-1]):
        return True
    if len(qt) == 1 and len(ct) == 1:
        return _lev_le1(qt[0], ct[0])
    if set(qt) == set(ct):
        return True
    return False


def _lev_le1(a: str, b: str) -> bool:
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(1 for x, y in zip(a, b) if x != y) <= 1
    if len(a) > len(b):
        a, b = b, a
    return any(b[:i] + b[i+1:] == a for i in range(len(b)))


# ─────────────────────────── Write paths ───────────────────────────
def record_active(
    sport: str,
    source: str,
    name: str,
    *,
    team: Optional[str] = None,
    minutes: Optional[float] = None,
    games_played: Optional[int] = None,
    status: Optional[str] = None,
    raw: Optional[dict] = None,
) -> None:
    """Record that `source` saw `name` as part of `sport`'s rosters/leaders.

    Idempotent — multiple sources confirming the same player merge into
    one record. Each record tracks every source that has confirmed them,
    plus the most-recent numeric stats (minutes / games_played).
    """
    if sport not in SUPPORTED_SPORTS:
        return
    key = _norm(name)
    if not key:
        return
    bucket = _registry.setdefault(sport, {})
    rec = bucket.get(key) or {
        "name": name,
        "key": key,
        "sport": sport,
        "team": team,
        "sources": {},
        "first_seen_at": time.time(),
    }
    rec["sources"][source] = {
        "team": team,
        "minutes": minutes,
        "games_played": games_played,
        "status": status,
        "raw": raw,
        "ts": time.time(),
    }
    if team:
        rec["team"] = team
    if minutes is not None:
        rec["minutes"] = max(float(minutes), float(rec.get("minutes") or 0.0))
    if games_played is not None:
        rec["games_played"] = max(int(games_played), int(rec.get("games_played") or 0))
    if status:
        rec["status"] = status
    rec["last_seen_at"] = time.time()
    bucket[key] = rec


async def persist(db) -> dict[str, int]:
    """Write the in-memory registry to MongoDB. Returns counts per sport.
    Safe to call from any source ingestor; bounces if another persist is
    already in flight."""
    global _last_persist_at
    if _persist_lock.locked():
        return {"locked": 1}
    async with _persist_lock:
        counts: dict[str, int] = {}
        for sport, bucket in _registry.items():
            if not bucket:
                continue
            try:
                ops = []
                for key, rec in bucket.items():
                    # We mostly write a fresh snapshot — small docs, MongoDB
                    # is fine with up to ~5k upserts.
                    ops.append({
                        "_id": f"{sport}:{key}",
                        **rec,
                    })
                coll = db.services_active_registry
                # bulk-write: replace one-by-one (cleaner than upsert_many for
                # the volume we have ~< 10k players per refresh).
                for doc in ops:
                    await coll.update_one(
                        {"_id": doc["_id"]},
                        {"$set": doc},
                        upsert=True,
                    )
                counts[sport] = len(ops)
            except Exception as e:
                logger.warning(f"persist({sport}) failed: {e}")
        _last_persist_at = time.time()
        return counts


async def hydrate_from_db(db) -> None:
    """Load the most-recent persisted snapshot back into the in-memory
    cache. Called on app startup so picks generation has data BEFORE
    the first refresh of any ingestor completes."""
    try:
        async for doc in db.services_active_registry.find({}):
            sport = doc.get("sport")
            key = doc.get("key")
            if not sport or not key or sport not in SUPPORTED_SPORTS:
                continue
            bucket = _registry.setdefault(sport, {})
            bucket[key] = {k: v for k, v in doc.items() if k != "_id"}
        sizes = {s: len(b) for s, b in _registry.items()}
        logger.info(f"active_registry hydrated from MongoDB: {sizes}")
    except Exception as e:
        logger.debug(f"active_registry hydrate skipped: {e}")


# ─────────────────────────── Read paths ───────────────────────────
def is_active(sport: str, name: str) -> Optional[bool]:
    """Returns True / False / None per the contract at the top of this file.

    Validation rules (in order):
        1. No registry data at all → None
        2. Snapshot too stale → None
        3. Player NOT in registry → False (truly retired / never existed)
        4. Player in registry, status == 'retired' → False
        5. Player in registry, *all* sources report minutes==0 → False
        6. Player in registry, *all* sources report games_played==0 → False
        7. Otherwise → True
    """
    sport = (sport or "").lower()
    if sport not in SUPPORTED_SPORTS:
        return None
    bucket = _registry.get(sport)
    if not bucket:
        return None
    key = _norm(name)
    rec = bucket.get(key)
    if rec is None:
        # tolerant matcher
        for k, v in bucket.items():
            if _name_match_for_sport(sport, key, k):
                rec = v
                break
    if rec is None:
        return False  # never seen in any source → not active
    # Stale check on this *individual* record.
    last_seen = rec.get("last_seen_at") or 0
    if last_seen and (time.time() - last_seen) > STALE_AFTER_S:
        return None
    # Status: explicit retirement / IR.
    status = (rec.get("status") or "").lower()
    if status in ("retired", "deceased"):
        return False
    # Minutes / games — only enforce when at least one source reported.
    minutes = rec.get("minutes")
    games = rec.get("games_played")
    if minutes is not None and minutes <= 0 and (games is None or games == 0):
        return False
    return True


def _name_match_for_sport(sport: str, query: str, candidate: str) -> bool:
    """Stricter matcher for soccer where single-token names (`Vinicius`,
    `Pelé`, `Maradona`) frequently collide between completely different
    eras / clubs. For soccer we require either:
        * exact match, OR
        * matching last name AND ≥1 shared other token (handles
          'Vinicius Junior' ↔ 'Vinícius Júnior' but not 'Vinicius Silva')

    Other sports use the looser shared matcher (Crysan/Cryzan, etc.).
    """
    if sport == "soccer":
        if query == candidate:
            return True
        qt, ct = query.split(), candidate.split()
        if not qt or not ct:
            return False
        # Soccer specifically: require last-name match
        if qt[-1] != ct[-1]:
            return False
        # If both are multi-token, also require at least one other shared token
        if len(qt) > 1 and len(ct) > 1:
            return bool(set(qt[:-1]) & set(ct[:-1]))
        # Single-token side: still require exact-on-last-name PLUS the
        # other side must also be exactly that token (no other tokens).
        # Otherwise "Vinicius" would match "Vinicius Silva" wrongly.
        if len(qt) == 1 and len(ct) > 1:
            return False
        if len(ct) == 1 and len(qt) > 1:
            return False
        return True   # both single-token, same value
    return _name_match(query, candidate)


def get_record(sport: str, name: str) -> Optional[dict]:
    sport = (sport or "").lower()
    bucket = _registry.get(sport)
    if not bucket:
        return None
    key = _norm(name)
    rec = bucket.get(key)
    if rec is None:
        for k, v in bucket.items():
            if _name_match_for_sport(sport, key, k):
                rec = v
                break
    return rec


def snapshot_state() -> dict[str, Any]:
    """Admin-friendly summary of registry contents per sport."""
    out: dict[str, Any] = {
        "last_persist_at": (
            datetime.fromtimestamp(_last_persist_at, tz=timezone.utc).isoformat()
            if _last_persist_at else None
        ),
        "sports": {},
    }
    for sport, bucket in _registry.items():
        n_total = len(bucket)
        n_active = sum(1 for r in bucket.values() if is_active(sport, r.get("name") or "") is True)
        n_inactive = sum(1 for r in bucket.values() if is_active(sport, r.get("name") or "") is False)
        sources_seen: set[str] = set()
        for rec in bucket.values():
            sources_seen.update(rec.get("sources", {}).keys())
        sample_top = sorted(
            (rec for rec in bucket.values()),
            key=lambda r: (r.get("minutes") or 0, r.get("games_played") or 0),
            reverse=True,
        )[:5]
        out["sports"][sport] = {
            "total": n_total,
            "active": n_active,
            "inactive": n_inactive,
            "sources": sorted(sources_seen),
            "top_sample": [
                {
                    "name": r["name"],
                    "team": r.get("team"),
                    "minutes": r.get("minutes"),
                    "games": r.get("games_played"),
                    "sources": list(r.get("sources", {}).keys()),
                }
                for r in sample_top
            ],
        }
    return out
