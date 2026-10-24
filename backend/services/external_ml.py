"""External-ML board externalization — state machine + commit boundary.

Phase 1A (2026-06) — move heavy NBA/NHL/Soccer ML off the Emergent web
server by staging each generation in Mongo and atomically swapping the
live board on a successful commit.  Emergent remains the publication
authority (lock-score, FanDuel-only enforcement, 85 threshold); the
external runner only produces generations and asks us to adopt them.

Design contract (verbatim from the one-pass migration brief):

    EXPORT INPUTS  →  COMPUTE EXTERNALLY  →  STAGE GENERATION  →  ATOMIC COMMIT

Collections introduced here (all read/write isolated from
``db.picks`` — the live board remains the source of truth that
``/api/picks/today`` serves):

* ``board_generations``       – one row per attempted generation.
* ``board_generation_chunks`` – one row per uploaded chunk (idempotent
                                 on ``(generation_id, chunk_no)``).
* ``board_generation_picks``  – staged picks awaiting commit.
                                 Invisible to end-users until commit
                                 flips them live via ``_commit_sport``.
* ``board_generation_heartbeats`` – bounded operational heartbeat log.
* ``board_live_generation``   – authoritative live pointer per sport.

The commit operation is intentionally a two-phase bulk_write:
  1. ``UpdateMany`` old live-board rows for ``sport`` → status
     ``superseded`` (they drop out of ``/api/picks/today`` filter which
     requires ``status in {pending, open, null}``).
  2. ``InsertMany`` new staged picks → they become the live board for
     ``sport`` with the new ``generation_id`` tag.
This is NOT "delete-first" publication — supersession happens inside
the same ordered-False bulk write as the insert, so the window where
the sport has zero visible picks is sub-millisecond at worst.

Hygiene:
* Last-good-board protection: a FAILED / INCOMPLETE generation never
  touches live picks (``_commit_sport`` requires ``status=COMPLETE``
  + full chunk arrival + non-zero pick count).
* Fail-closed on FanDuel-only validation inside ``validate_pick``.
* No scoring math lives here.  The external runner is expected to
  produce picks already scored against the EXACT same `sports_engine`
  math — this module only validates the canonical wager identity and
  the 85 threshold.
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

try:
    import pymongo
    from pymongo import UpdateOne, InsertOne, UpdateMany
except Exception:  # pragma: no cover
    pymongo = None  # type: ignore

logger = logging.getLogger("lockscore.external_ml")

# ── Supported sports for externalization (spec §20 one-at-a-time) ────
SUPPORTED_SPORTS = ("NBA", "NHL", "Soccer")

# ── Publication authority — never relaxed by this module.  Picks are
#    staged if lock_score ≥ 85.  Spec §24 (85 threshold unchanged).
PUBLICATION_LOCK_FLOOR = 85.0

# ── FanDuel-only publication authority (spec §5). ────────────────────
PUBLISHED_BOOKMAKER = "fanduel"

# Allowed generation status values.
GENERATION_STATUSES = (
    "STAGING",      # chunks still arriving
    "COMPLETE",     # every expected chunk uploaded + schema-valid
    "COMMITTED",    # live pointer swapped to this generation
    "FAILED",       # explicit failure recorded; live board untouched
    "EXPIRED",      # TTL exceeded before commit; live board untouched
)

# How long a STAGING generation may linger before being considered
# EXPIRED.  Spec §21 schedule guidance is 10–15 min cadence; we give
# generations a generous 45 min to finish.
STAGING_TTL_SECONDS = 45 * 60


# ═════════════════════════════════════════════════════════════════════
# Pure helpers (no DB)
# ═════════════════════════════════════════════════════════════════════
def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _norm_sport(sport: str) -> str:
    """Case-fold a sport name to our canonical casing.

    External runner can send "nba" / "NBA" / "Nba" — we normalize so
    pointer / collection filters match the live-board sport labels.
    """
    s = (sport or "").strip()
    if not s:
        return s
    sl = s.lower()
    if sl == "nba":
        return "NBA"
    if sl == "nhl":
        return "NHL"
    if sl == "soccer":
        return "Soccer"
    return s


def is_supported_sport(sport: str) -> bool:
    return _norm_sport(sport) in SUPPORTED_SPORTS


def get_execution_mode(sport: str) -> str:
    """Return the configured heavy-refresh execution mode for a sport.

    Reads ``ML_REFRESH_EXECUTION_MODE`` (global) and optional per-sport
    overrides ``ML_REFRESH_EXECUTION_MODE_<SPORT>``.  Default is
    ``internal`` so the migration is strictly opt-in per sport.
    """
    sport_u = _norm_sport(sport).upper() or sport.upper()
    per_sport = os.environ.get(f"ML_REFRESH_EXECUTION_MODE_{sport_u}")
    if per_sport:
        return per_sport.strip().lower()
    return os.environ.get("ML_REFRESH_EXECUTION_MODE", "internal").strip().lower()


def is_external_mode(sport: str) -> bool:
    return get_execution_mode(sport) == "external"


def sports_in_external_mode() -> set[str]:
    """Return the set of SUPPORTED_SPORTS that are currently in
    ``external`` mode.  Used by the pick-refresh orchestrator to
    bypass the heavy in-app ML path for migrated sports.
    """
    return {s for s in SUPPORTED_SPORTS if is_external_mode(s)}


# ─────────────────────────────────────────────────────────────────────
# SINGLE-AUTHORITY RULE (do NOT create a competing board authority)
# ─────────────────────────────────────────────────────────────────────
# ``db.picks`` remains THE live read authority for /api/picks/today and
# every other reader (parlay, home board, feed, analytics, etc.).  The
# ``board_live_generation`` pointer created here is an OPERATIONAL
# OBSERVABILITY RECORD ONLY — it is not read by any end-user route.
#
# The commit path (``commit_generation`` below) writes staged picks
# directly into ``db.picks`` and marks superseded rows via a single
# bulk_write.  This keeps the existing ``board_generation.begin → validate
# → commit`` state machine (services/board_generation.py) as the sole
# board authority; the caller wrapping ``_refresh_picks`` continues to
# own the active pointer A→B swap and the lifecycle event audit log.
#
# LEGACY_ARTIFICIAL_SCORE_MUTATION — the current in-app refresh pipeline
# includes ``elite_players.apply_elite_boost`` (services/pick_refresh_
# orchestrator.py :1034) which multiplies lock_score by ``ELITE_BOOST_PCT``
# based on a curated star-player name list, with no per-pick evidence
# provenance.  See /app/memory/LEGACY_ARTIFICIAL_SCORE_MUTATION.md.  To
# preserve "do NOT change scoring math" THIS pass, external_runner.py
# replays the same mutator so Preview semantics are unchanged; a
# follow-up pass should either attach evidence provenance to the
# boost or remove it.


# ═════════════════════════════════════════════════════════════════════
# Pick validation — enforce FanDuel-only + exact-line + ≥85 lock score
# ═════════════════════════════════════════════════════════════════════
_REQUIRED_PICK_FIELDS = (
    "sport", "event", "event_time", "market", "pick",
    "book_odds", "lock_score",
)


def validate_pick(pick: dict) -> tuple[bool, Optional[str]]:
    """Return ``(ok, error_code_or_None)`` for a staged pick.

    FanDuel-only authority (spec §5): the runner must have resolved the
    exact wager to a FanDuel quote.  Picks sourced from other books
    are rejected with ``FANDUEL_EXACT_WAGER_UNAVAILABLE``.  Picks that
    did not clear the 85 floor are rejected with
    ``LOCK_SCORE_BELOW_FLOOR`` so the client runner can log them for
    debugging without them polluting the staging table.
    """
    if not isinstance(pick, dict):
        return False, "NOT_A_DICT"
    for f in _REQUIRED_PICK_FIELDS:
        if pick.get(f) in (None, ""):
            return False, f"MISSING_FIELD:{f}"
    # Sport eligibility.
    sport = _norm_sport(pick.get("sport") or "")
    if sport not in SUPPORTED_SPORTS:
        return False, f"UNSUPPORTED_SPORT:{sport}"
    # Bookmaker: must be FanDuel.
    book = str(pick.get("bookmaker") or pick.get("bookmaker_key") or "").lower()
    if book != PUBLISHED_BOOKMAKER:
        return False, "FANDUEL_EXACT_WAGER_UNAVAILABLE"
    # Lock score ≥ 85 (publication authority).
    try:
        ls = float(pick.get("published_lock_score") or pick.get("lock_score") or 0)
    except (TypeError, ValueError):
        return False, "INVALID_LOCK_SCORE"
    if ls < PUBLICATION_LOCK_FLOOR:
        return False, "LOCK_SCORE_BELOW_FLOOR"
    # Odds presence (FanDuel quote must be real, not synthetic).
    if pick.get("no_real_book_line"):
        return False, "SYNTHETIC_BOOK_ODDS"
    try:
        float(pick.get("book_odds"))
    except (TypeError, ValueError):
        return False, "INVALID_BOOK_ODDS"
    return True, None


# ═════════════════════════════════════════════════════════════════════
# Index bootstrap (idempotent)
# ═════════════════════════════════════════════════════════════════════
async def ensure_indexes(db) -> None:
    """Create the collections' required indexes.  Idempotent."""
    try:
        await db.board_generations.create_index(
            [("sport", 1), ("status", 1), ("created_at", -1)]
        )
        await db.board_generations.create_index("generation_id", unique=True)
        await db.board_generation_chunks.create_index(
            [("generation_id", 1), ("chunk_no", 1)], unique=True
        )
        await db.board_generation_picks.create_index(
            [("generation_id", 1), ("canonical_pick_id", 1)]
        )
        await db.board_generation_picks.create_index(
            [("generation_id", 1), ("sport", 1)]
        )
        await db.board_generation_heartbeats.create_index(
            [("generation_id", 1), ("ts", -1)]
        )
        await db.board_live_generation.create_index("sport", unique=True)
    except Exception as e:  # pragma: no cover
        logger.debug("external_ml.ensure_indexes partial failure: %s", e)


# ═════════════════════════════════════════════════════════════════════
# Generation lifecycle
# ═════════════════════════════════════════════════════════════════════
async def record_heartbeat(db, *, job_id: str, sport: Optional[str],
                           stage: str, generation_id: Optional[str],
                           message: Optional[str] = None,
                           error: Optional[str] = None) -> dict:
    """Append a bounded heartbeat row.  Never raises on partial db
    errors — observability must not break the runner."""
    doc = {
        "job_id": str(job_id),
        "sport": _norm_sport(sport) if sport else None,
        "stage": str(stage)[:48],
        "generation_id": generation_id,
        "message": (str(message)[:400] if message else None),
        "error": (str(error)[:800] if error else None),
        "ts": _utc_now(),
    }
    try:
        await db.board_generation_heartbeats.insert_one(doc)
    except Exception as e:  # pragma: no cover
        logger.debug("heartbeat insert failed: %s", e)
    # Mirror onto the generation doc for easy ops read.
    if generation_id:
        try:
            await db.board_generations.update_one(
                {"generation_id": generation_id},
                {"$set": {
                    "current_stage":       stage,
                    "last_heartbeat_at":   doc["ts"],
                    "last_heartbeat_msg":  doc["message"],
                    "last_heartbeat_err":  doc["error"],
                    "current_job_id":      doc["job_id"],
                }},
            )
        except Exception as e:  # pragma: no cover
            logger.debug("heartbeat mirror failed: %s", e)
    return doc


async def upsert_generation(db, *, sport: str, generation_id: str,
                            model_version: str,
                            computed_at: Optional[datetime] = None,
                            chunk_count: Optional[int] = None,
                            job_id: Optional[str] = None) -> dict:
    """Create (or touch) a generation row with status STAGING.

    Idempotent on ``generation_id`` — a runner retry does not create a
    second row.  Expired or already-committed generations are refused.
    """
    sp = _norm_sport(sport)
    if sp not in SUPPORTED_SPORTS:
        raise ValueError(f"unsupported sport: {sport}")
    now = _utc_now()
    existing = await db.board_generations.find_one({"generation_id": generation_id}, {"_id": 0})
    if existing:
        if existing.get("status") == "COMMITTED":
            raise ValueError("GENERATION_ALREADY_COMMITTED")
        if existing.get("status") in ("FAILED", "EXPIRED"):
            raise ValueError(f"GENERATION_{existing.get('status')}")
        update: dict[str, Any] = {}
        if chunk_count is not None and existing.get("chunk_count") != chunk_count:
            update["chunk_count"] = int(chunk_count)
        if job_id and existing.get("current_job_id") != job_id:
            update["current_job_id"] = job_id
        if update:
            await db.board_generations.update_one(
                {"generation_id": generation_id}, {"$set": update}
            )
        return {**existing, **update}
    doc = {
        "generation_id":      generation_id,
        "sport":              sp,
        "model_version":      str(model_version or "unknown"),
        "computed_at":        computed_at or now,
        "status":             "STAGING",
        "chunk_count":        int(chunk_count) if chunk_count is not None else None,
        "received_chunks":    0,
        "received_chunk_set": [],
        "pick_count":         0,
        "created_at":         now,
        "committed_at":       None,
        "current_job_id":     job_id,
        "current_stage":      "STAGING",
        "errors":             [],
    }
    try:
        await db.board_generations.insert_one(dict(doc))
    except Exception as e:
        # Duplicate-key race — another worker beat us; return the existing row.
        existing = await db.board_generations.find_one({"generation_id": generation_id}, {"_id": 0})
        if existing:
            return existing
        raise
    return doc


async def append_chunk(db, *, sport: str, generation_id: str, chunk_no: int,
                       model_version: str, computed_at: Optional[datetime],
                       picks: list[dict]) -> dict:
    """Idempotent chunk append + per-pick validation.

    Returns a result dict with counts: ``{accepted, rejected,
    duplicates, errors}``.  A chunk re-upload (same ``generation_id`` +
    ``chunk_no``) must NOT duplicate picks (spec §7).
    """
    sp = _norm_sport(sport)
    if sp not in SUPPORTED_SPORTS:
        raise ValueError(f"unsupported sport: {sport}")

    # Upsert the generation record.
    gen = await upsert_generation(
        db, sport=sp, generation_id=generation_id,
        model_version=model_version, computed_at=computed_at,
    )

    # Duplicate-chunk guard — fast path.
    existing_chunk = await db.board_generation_chunks.find_one(
        {"generation_id": generation_id, "chunk_no": int(chunk_no)}, {"_id": 0}
    )
    if existing_chunk:
        return {
            "accepted": 0,
            "rejected": 0,
            "duplicates": int(existing_chunk.get("pick_count") or 0),
            "errors": [],
            "chunk_already_received": True,
            "generation_status": gen.get("status"),
        }

    accepted: list[dict] = []
    rejected: list[dict] = []
    errors: list[str] = []
    for raw in picks or []:
        ok, err = validate_pick(raw)
        if not ok:
            rejected.append({"canonical_pick_id": raw.get("canonical_pick_id") if isinstance(raw, dict) else None,
                             "reason": err})
            errors.append(err or "UNKNOWN")
            continue
        enriched = dict(raw)
        enriched["sport"] = _norm_sport(enriched.get("sport"))
        enriched["generation_id"] = generation_id
        enriched["model_version"] = str(model_version or "unknown")
        enriched["staged_at"] = _utc_now()
        # canonical_pick_id is REQUIRED for commit-time replacement
        # semantics; fall back to a deterministic uuid5 if missing.
        if not enriched.get("canonical_pick_id"):
            seed = "|".join([
                enriched.get("sport") or "",
                str(enriched.get("event") or ""),
                str(enriched.get("market") or ""),
                str(enriched.get("pick") or ""),
                str(enriched.get("line") or ""),
            ])
            enriched["canonical_pick_id"] = str(uuid.uuid5(
                uuid.UUID("00000000-0000-0000-0000-000000000055"), seed))
        accepted.append(enriched)

    # Persist the chunk receipt FIRST so a retry can idempotently
    # short-circuit.  Then insert the picks (ordered=False so one
    # duplicate inside the chunk doesn't abort the batch).
    chunk_doc = {
        "generation_id": generation_id,
        "chunk_no":      int(chunk_no),
        "received_at":   _utc_now(),
        "pick_count":    len(accepted),
        "rejected":      rejected[:200],  # cap size on disk
    }
    try:
        await db.board_generation_chunks.insert_one(dict(chunk_doc))
    except Exception as e:
        msg = str(e).lower()
        if "duplicate" in msg or "e11000" in msg:
            # Another worker beat us; treat as duplicate.
            return {
                "accepted": 0, "rejected": 0, "duplicates": len(accepted),
                "errors": [], "chunk_already_received": True,
                "generation_status": gen.get("status"),
            }
        raise
    if accepted:
        try:
            await db.board_generation_picks.insert_many(accepted, ordered=False)
        except Exception as e:
            # ordered=False absorbs duplicate key errors on retried
            # (generation_id, canonical_pick_id) pairs.
            logger.debug("staged pick insert partial failure: %s", e)

    # Mirror chunk receipt + running count onto the generation doc.
    await db.board_generations.update_one(
        {"generation_id": generation_id},
        {
            "$addToSet": {"received_chunk_set": int(chunk_no)},
            "$inc":      {"received_chunks": 1, "pick_count": len(accepted)},
            "$set":      {"last_chunk_at": _utc_now()},
        },
    )
    return {
        "accepted":    len(accepted),
        "rejected":    len(rejected),
        "duplicates":  0,
        "errors":      errors[:50],
        "generation_status": gen.get("status"),
    }


async def mark_complete(db, *, generation_id: str,
                        expected_chunks: Optional[int] = None) -> dict:
    """Flip STAGING → COMPLETE when every chunk is in.  Returns the
    generation doc."""
    gen = await db.board_generations.find_one({"generation_id": generation_id}, {"_id": 0})
    if not gen:
        raise ValueError("GENERATION_NOT_FOUND")
    if gen.get("status") == "COMMITTED":
        return gen
    if gen.get("status") in ("FAILED", "EXPIRED"):
        raise ValueError(f"GENERATION_{gen.get('status')}")
    expected = int(expected_chunks if expected_chunks is not None
                   else (gen.get("chunk_count") or 0))
    received = int(gen.get("received_chunks") or 0)
    if expected and received < expected:
        return gen  # still STAGING
    await db.board_generations.update_one(
        {"generation_id": generation_id, "status": "STAGING"},
        {"$set": {"status": "COMPLETE", "completed_at": _utc_now()}},
    )
    return await db.board_generations.find_one({"generation_id": generation_id}, {"_id": 0})


async def mark_failed(db, *, generation_id: str, reason: str) -> dict:
    """Mark a generation FAILED.  Does NOT touch live board (spec §10)."""
    await db.board_generations.update_one(
        {"generation_id": generation_id,
         "status": {"$nin": ["COMMITTED"]}},
        {"$set": {"status": "FAILED", "failed_at": _utc_now()},
         "$push": {"errors": {"reason": str(reason)[:400], "at": _utc_now()}}},
    )
    return await db.board_generations.find_one({"generation_id": generation_id}, {"_id": 0})


# ═════════════════════════════════════════════════════════════════════
# Commit — the ONE live-board switch
# ═════════════════════════════════════════════════════════════════════
async def commit_generation(db, *, sport: str, generation_id: str,
                            today_str_fn=None) -> dict:
    """Atomic last-good-board switch.

    Preconditions (spec §9):
      * generation exists
      * generation status == COMPLETE
      * all expected chunks received
      * at least one staged pick (healthy generation only)

    Mechanism:
      1. Load staged picks.
      2. Mark previous live-board picks for ``sport`` with pre-game
         status (``pending``/``open``/null) → status ``"replaced"``.
      3. Insert staged picks into ``db.picks`` with the new
         ``generation_id`` tag and status ``"pending"``.
      4. Flip pointer in ``board_live_generation`` and
         ``board_generations`` → ``COMMITTED``.

    All four steps run in a single ``bulk_write(ordered=False)`` on
    ``db.picks`` plus a two-document pointer update.  A Mongo single-
    collection bulk_write is atomic per-document; the window during
    which both old and new rows are visible is sub-millisecond.

    Replacement semantics (spec §11): events that already started are
    NEVER replaced (status != pending/open/null → the Update does not
    match).  Legitimate pre-game line moves on FanDuel are allowed
    because the fresh ``canonical_pick_id`` is recomputed from
    exact-line identity.

    Returns the pointer doc.
    """
    sp = _norm_sport(sport)
    if sp not in SUPPORTED_SPORTS:
        raise ValueError(f"unsupported sport: {sport}")
    gen = await db.board_generations.find_one({"generation_id": generation_id}, {"_id": 0})
    if not gen:
        raise ValueError("GENERATION_NOT_FOUND")
    if gen.get("sport") != sp:
        raise ValueError("GENERATION_SPORT_MISMATCH")
    if gen.get("status") == "COMMITTED":
        return await _read_live_pointer(db, sp)
    if gen.get("status") != "COMPLETE":
        raise ValueError(f"GENERATION_NOT_COMPLETE:{gen.get('status')}")
    # Expected chunk arrival guard.
    expected = int(gen.get("chunk_count") or 0)
    received = int(gen.get("received_chunks") or 0)
    if expected and received < expected:
        raise ValueError("GENERATION_INCOMPLETE_CHUNKS")

    staged = await db.board_generation_picks.find(
        {"generation_id": generation_id, "sport": sp}, {"_id": 0}
    ).to_list(length=None)
    if not staged:
        # Spec §11 — healthy later generation with zero ≥85 picks for
        # the sport is a LEGITIMATE replacement.  BUT the empty case
        # requires an explicit guard: the external runner must have
        # explicitly declared the slate empty.  Here we treat zero
        # staged picks as INCOMPLETE unless the generation metadata
        # claimed pick_count == 0 at completion time.
        if int(gen.get("pick_count") or 0) != 0:
            raise ValueError("GENERATION_HAS_NO_STAGED_PICKS")

    # Build the live-board docs — these go into ``db.picks`` which is
    # exactly what ``/api/picks/today`` reads today.  We give each
    # staged pick an id, pick_date, and the status/visibility fields
    # the existing picks router expects.
    _today = today_str_fn() if today_str_fn else _utc_now().strftime("%Y-%m-%d")
    now = _utc_now()
    live_docs: list[dict] = []
    for sp_doc in staged:
        d = dict(sp_doc)
        # ``id`` must be a stable identifier for the pick slip / detail
        # route lookups.  Re-use canonical_pick_id so the staged row
        # and the live row share identity.
        d["id"] = d.get("id") or d["canonical_pick_id"]
        d["pick_date"] = _today
        d.setdefault("status", "pending")
        d.setdefault("off_board", False)
        d.setdefault("no_bet", False)
        d["committed_at"] = now
        d["generation_id"] = generation_id
        d["published_lock_score"] = float(
            d.get("published_lock_score") or d.get("lock_score") or 0
        )
        d["source"] = d.get("source") or "external_ml"
        live_docs.append(d)

    # Build the atomic bulk_write.
    ops: list = []
    # 1) Mark previous live rows for the same sport + still-pre-game
    #    as "replaced" (they drop out of /picks/today filter which
    #    requires status in {pending, open, null}).  Scope to the
    #    SAME pick_date so historical picks remain immutable.
    ops.append(UpdateMany(
        {
            "sport":    sp,
            "pick_date": _today,
            "status":   {"$in": ["pending", "open", None]},
            # Never touch picks already tagged with THIS generation
            # (idempotent commit retry).
            "generation_id": {"$ne": generation_id},
        },
        {"$set": {
            "status":       "replaced",
            "replaced_at":  now,
            "replaced_by_generation_id": generation_id,
        }},
    ))
    # 2) Insert new live rows.  Use upsert via replace_one so a retry
    #    of commit on the same generation does not double-insert.
    for d in live_docs:
        ops.append(UpdateOne(
            {"id": d["id"], "pick_date": _today},
            {"$set": d},
            upsert=True,
        ))
    if ops:
        try:
            await db.picks.bulk_write(ops, ordered=False)
        except Exception as e:  # pragma: no cover
            logger.warning("commit_generation bulk_write partial failure: %s", e)
            # Partial failure here means the atomic swap did NOT finish
            # cleanly.  We refuse to flip the pointer so the
            # last-good-board stays live (spec §10).
            await mark_failed(db, generation_id=generation_id,
                              reason=f"commit_bulk_write_error: {e}")
            raise

    # 3) Flip the pointer.  Pointer document is the authoritative
    #    "which generation is live" per sport — read by
    #    /api/ops/refresh-health and (optionally) by /api/picks/today
    #    once we add generation-scoped filtering.
    pointer_doc = {
        "sport":              sp,
        "live_generation_id": generation_id,
        "committed_at":       now,
        "model_version":      gen.get("model_version"),
        "pick_count":         len(live_docs),
        "computed_at":        gen.get("computed_at"),
    }
    await db.board_live_generation.update_one(
        {"sport": sp}, {"$set": pointer_doc}, upsert=True
    )
    # 4) Mark the generation COMMITTED.
    await db.board_generations.update_one(
        {"generation_id": generation_id},
        {"$set": {
            "status":        "COMMITTED",
            "committed_at":  now,
            "committed_pick_count": len(live_docs),
        }},
    )
    logger.info(
        "external_ml commit OK sport=%s generation=%s picks=%d",
        sp, generation_id, len(live_docs),
    )
    return pointer_doc


async def _read_live_pointer(db, sport: str) -> Optional[dict]:
    sp = _norm_sport(sport)
    return await db.board_live_generation.find_one({"sport": sp}, {"_id": 0})


async def get_live_pointer(db, sport: str) -> Optional[dict]:
    return await _read_live_pointer(db, sport)


# ═════════════════════════════════════════════════════════════════════
# Last-good-board observability for /api/ops/refresh-health
# ═════════════════════════════════════════════════════════════════════
async def ops_summary(db) -> dict:
    """Build the per-sport externalization summary used by
    ``/api/ops/refresh-health``.  Read-only."""
    out: dict[str, dict] = {}
    for sport in SUPPORTED_SPORTS:
        execution_mode = get_execution_mode(sport)
        pointer = await _read_live_pointer(db, sport) or {}
        # Latest generation row for this sport (any status).
        latest = await db.board_generations.find_one(
            {"sport": sport},
            {"_id": 0},
            sort=[("created_at", -1)],
        ) or {}
        current_running = await db.board_generations.find_one(
            {"sport": sport, "status": {"$in": ["STAGING", "COMPLETE"]}},
            {"_id": 0},
            sort=[("created_at", -1)],
        ) or {}
        out[sport] = {
            "execution_mode":           "EXTERNAL_ML" if execution_mode == "external" else "IN_APP",
            "last_committed_at":        pointer.get("committed_at"),
            "live_generation_id":       pointer.get("live_generation_id"),
            "last_model_version":       pointer.get("model_version") or latest.get("model_version"),
            "last_generation_status":   latest.get("status"),
            "last_generation_pick_count": latest.get("pick_count")
                                          if latest.get("status") == "COMMITTED"
                                          else latest.get("committed_pick_count"),
            "current_job_id":           current_running.get("current_job_id"),
            "current_stage":            current_running.get("current_stage"),
            "last_heartbeat_at":        current_running.get("last_heartbeat_at")
                                        or latest.get("last_heartbeat_at"),
            "last_error":               (latest.get("errors") or [{}])[-1].get("reason")
                                        if latest.get("errors") else None,
        }
    return out


# ═════════════════════════════════════════════════════════════════════
# Housekeeping — expire old STAGING generations so dead runners can't
# pin a pointer or starve a sport.
# ═════════════════════════════════════════════════════════════════════
async def expire_stale_staging(db) -> int:
    cutoff = _utc_now() - timedelta(seconds=STAGING_TTL_SECONDS)
    r = await db.board_generations.update_many(
        {"status": "STAGING", "created_at": {"$lt": cutoff}},
        {"$set": {"status": "EXPIRED", "expired_at": _utc_now()}},
    )
    return int(getattr(r, "modified_count", 0) or 0)
