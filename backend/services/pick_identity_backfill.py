"""Backfill canonical identity + model_probability onto UNSETTLED
picks that were published before Pre-Magic Blocker A/B remediation.

§11 CONSTRAINTS:
* Historical settled picks are IMMUTABLE — do NOT rewrite outcomes,
  do NOT rewrite settled_at, do NOT touch anything but the newly-
  added structured identity/model fields.
* Only enrich picks that CAN be resolved deterministically from
  fields that already exist on the pick.
* If a pick cannot be safely enriched, leave it — UNKNOWN stays
  UNKNOWN.
* Idempotent — re-running produces the same enrichment.

Usage:
    python -m services.pick_identity_backfill --dry-run
    python -m services.pick_identity_backfill --commit
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from typing import Optional

from services.pick_identity_enricher import enrich_pick_identity_async
from services.pick_model_evidence import extract_model_evidence


logger = logging.getLogger("lockscore.pick_identity_backfill")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO,
                         format="%(asctime)s %(levelname)s %(name)s %(message)s")


# Statuses considered "unsettled" and thus safe to enrich.  Anything
# else (won/lost/void/push/blocked/etc.) is historical settled truth
# and must NOT be modified.
_UNSETTLED_STATUSES = {None, "", "pending", "unresolved", "?"}


async def enrich_pick(db, pick: dict, *, dry_run: bool) -> Optional[dict]:
    """Return the update_fields for this pick, or None if nothing to
    do.  Only writes when ``dry_run`` is False."""
    ident = {}
    try:
        ident = await enrich_pick_identity_async(db, pick)
    except Exception as e:
        logger.debug("enricher failed on %s: %s", pick.get("id"), e)
    model = {}
    try:
        model = extract_model_evidence(pick)
    except Exception as e:
        logger.debug("model extractor failed on %s: %s", pick.get("id"), e)

    update_fields: dict = {}
    _CANON_ID_KEYS = ("canonical_team_id", "canonical_player_id",
                        "canonical_opponent_id", "canonical_event_id")
    for k, v in {**ident, **model}.items():
        if v is None:
            continue
        cur = pick.get(k)
        if cur in (None, "", []):
            update_fields[k] = v
            continue
        # Upgrade fallback ids to authoritative ones (§3).
        if k in _CANON_ID_KEYS and isinstance(cur, str) and \
                cur.startswith("fallback:") and \
                isinstance(v, str) and not v.startswith("fallback:") \
                and not v.startswith("unresolved:"):
            update_fields[k] = v
            continue
        if k == "identity_quality" and cur == "fallback" and \
                v == "authoritative":
            update_fields[k] = v
    if not update_fields:
        return None
    if not dry_run:
        await db.picks.update_one(
            {"id": pick["id"]}, {"$set": update_fields}
        )
    return update_fields


async def run_backfill(
    db,
    *,
    dry_run: bool = True,
    limit: Optional[int] = None,
    include_settled: bool = True,
) -> dict:
    """Enrich picks with canonical identity + model_probability.

    ``include_settled`` (default True) governs whether we also enrich
    historical / settled picks.  Enrichment adds structured identity
    fields ONLY — it never touches ``status`` / ``settled_at`` /
    ``won`` / ``lost`` / ``clv_value`` / any settlement or outcome
    field (§11 — "Do not rewrite outcomes").  A settled pick's
    immutable outcome truth remains untouched.

    When identity cannot be resolved deterministically the pick is
    left alone (§11 — "if a historical record cannot be safely
    enriched without guessing, leave its identity unresolved").
    """
    if include_settled:
        query = {}
    else:
        query = {"status": {"$in": list(_UNSETTLED_STATUSES) + [None]}}

    # Pre-warm the authoritative id cache in a single Mongo pass.
    try:
        from services.pick_identity_authority import prewarm_cache
        pw = await prewarm_cache(db)
        logger.info("prewarmed id cache: %s", pw)
    except Exception as _e_pw:
        logger.debug("prewarm cache failed (non-fatal): %s", _e_pw)
    total = 0
    enriched = 0
    canonical_added = 0
    model_added = 0
    by_sport: dict = {}
    # Bulk-write buffer for throughput.
    from pymongo import UpdateOne
    pending: list[UpdateOne] = []
    BATCH = 500
    cursor = db.picks.find(query, {"_id": 0})
    if limit:
        cursor = cursor.limit(limit)
    async for pick in cursor:
        total += 1
        # Recompute update_fields — inline to avoid the async
        # single-write overhead of ``enrich_pick``.
        try:
            ident = await enrich_pick_identity_async(db, pick)
        except Exception:
            ident = {}
        try:
            model = extract_model_evidence(pick)
        except Exception:
            model = {}
        update_fields: dict = {}
        _CANON_ID_KEYS = ("canonical_team_id", "canonical_player_id",
                            "canonical_opponent_id", "canonical_event_id")
        for k, v in {**ident, **model}.items():
            if v is None:
                continue
            cur = pick.get(k)
            if cur in (None, "", []):
                update_fields[k] = v
                continue
            if k in _CANON_ID_KEYS and isinstance(cur, str) and \
                    cur.startswith("fallback:") and \
                    isinstance(v, str) and not v.startswith("fallback:") \
                    and not v.startswith("unresolved:"):
                update_fields[k] = v
                continue
            if k == "identity_quality" and cur == "fallback" and \
                    v == "authoritative":
                update_fields[k] = v
        if not update_fields:
            continue
        enriched += 1
        sport = pick.get("sport") or "?"
        by_sport[sport] = by_sport.get(sport, 0) + 1
        if any(k in update_fields for k in _CANON_ID_KEYS):
            canonical_added += 1
        if "model_probability" in update_fields:
            model_added += 1
        if not dry_run:
            pending.append(UpdateOne(
                {"id": pick["id"]}, {"$set": update_fields}))
            if len(pending) >= BATCH:
                await db.picks.bulk_write(pending, ordered=False)
                pending = []
                logger.info("backfill progress: examined=%d updated=%d",
                             total, enriched)
    if pending and not dry_run:
        await db.picks.bulk_write(pending, ordered=False)
    return {
        "total_examined": total,
        "picks_updated": enriched,
        "canonical_id_added": canonical_added,
        "model_probability_added": model_added,
        "by_sport": by_sport,
        "dry_run": dry_run,
    }


def _connect_db():
    from motor.motor_asyncio import AsyncIOMotorClient
    url = os.environ.get("MONGO_URL") or "mongodb://localhost:27017"
    db_name = os.environ.get("MONGO_DB") or "lockscore_db"
    client = AsyncIOMotorClient(url)
    return client[db_name]


async def _amain(args):
    db = _connect_db()
    summary = await run_backfill(
        db, dry_run=args.dry_run, limit=args.limit)
    logger.info("backfill summary: %s", summary)
    return summary


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", default=True)
    p.add_argument("--commit", action="store_true", default=False)
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args()
    if args.commit:
        args.dry_run = False
    return asyncio.run(_amain(args))


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = ["run_backfill", "enrich_pick"]
