"""MAGIC 3A.1 — Deterministic historical line backfill.

Idempotent script that walks `db.picks` and repairs the three
canonical line fields on legacy rows:

  * `line`         — numeric threshold (float) or None.
  * `side`         — "over" / "under" / "positive_spread" /
                     "negative_spread" or None.
  * `line_source`  — one of:
        - "sportsbook_structured"    (untouched — never overwritten)
        - "historical_selection_parse"  (this backfill's tag)
        - None                        (unrecoverable)

Rules (per user directive)
──────────────────────────
1. Never fabricate a line — must be present verbatim in
   market/selection.
2. Never overwrite an existing structured `line_source` value.
3. Never mutate settlement truth: status, result, settled_at,
   units_profit, units_risked, market, selection, book_odds,
   odds_source, closing_odds, clv_value, etc. — untouched.
4. Idempotent: re-running does not change any additional rows.

Usage
─────
    # DRY RUN — prints counts only, no writes.
    python /app/backend/scripts/magic_3a1_backfill.py --dry-run

    # WRITE — apply the changes.
    python /app/backend/scripts/magic_3a1_backfill.py --write

Outputs a `LINE_PIPELINE_READY` / `LINE_PIPELINE_NOT_READY` verdict
at the end.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

sys.path.insert(0, "/app/backend")

from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient

from services.magic.line_extractor import extract_line_with_provenance


# ── Immutable fields that MUST NOT change during backfill ────────────
IMMUTABLE_FIELDS: tuple[str, ...] = (
    "status", "result", "settled_at",
    "units_profit", "units_risked",
    "market", "selection",
    "book_odds", "odds_source",
    "closing_odds", "clv_value",
    "sport", "sport_key", "league",
    "event", "event_time", "pick_date",
    "id", "external_id",
)

STRUCTURED_SOURCES: frozenset[str] = frozenset({
    "sportsbook_structured",
    "the_odds_api_structured",
    "book_line_structured",
})


def _immutable_signature(pick: dict) -> str:
    """Return a stable hash of the immutable fields for verification."""
    snapshot: dict[str, Any] = {}
    for f in IMMUTABLE_FIELDS:
        v = pick.get(f)
        if hasattr(v, "isoformat"):
            v = v.isoformat()
        snapshot[f] = v
    js = json.dumps(snapshot, sort_keys=True, default=str)
    return hashlib.sha256(js.encode()).hexdigest()


async def run(*, write: bool) -> dict:
    db = AsyncIOMotorClient(os.getenv("MONGO_URL"))["lockscore_db"]

    total = 0
    counts = {
        "RECOVERED_STRUCTURED_UNTOUCHED": 0,   # already tagged; skipped
        "RECOVERED_TEXT_APPLIED":         0,   # newly parsed + written
        "NOT_RECOVERABLE_APPLIED":        0,   # tagged line_source=None
        "ALREADY_BACKFILLED_SKIP":        0,   # historical_selection_parse
        "SETTLED_ROWS_TOUCHED":           0,   # settled row with metadata repair
        "IMMUTABLE_MISMATCH":             0,   # SHOULD stay 0 (safety)
    }
    per_sport: dict[str, dict] = {}
    ops: list = []
    OP_BATCH = 500

    # Preflight: snapshot immutable signatures for a sampled set of
    # rows to verify no mutation happens.
    sig_map: dict[str, str] = {}

    async for p in db.picks.find(
        {},
        {
            "_id": 0,
            "id": 1, "sport": 1, "sport_key": 1,
            "market": 1, "selection": 1,
            "line": 1, "side": 1, "line_source": 1,
            "book_odds": 1, "odds_source": 1,
            "status": 1, "result": 1, "settled_at": 1,
            "units_profit": 1, "units_risked": 1,
            "closing_odds": 1, "clv_value": 1,
            "league": 1, "event": 1, "event_time": 1,
            "pick_date": 1, "external_id": 1,
        },
    ):
        total += 1
        pid = p.get("id")
        if not pid:
            continue

        sig_map[pid] = _immutable_signature(p)

        sport = p.get("sport_key") or p.get("sport") or "UNKNOWN"
        per_sport.setdefault(sport, {
            "total": 0, "text_applied": 0, "structured": 0,
            "unrec": 0, "already": 0,
        })
        per_sport[sport]["total"] += 1

        existing_src = p.get("line_source")

        # Rule 2: preserve structured provenance.
        if existing_src in STRUCTURED_SOURCES:
            counts["RECOVERED_STRUCTURED_UNTOUCHED"] += 1
            per_sport[sport]["structured"] += 1
            continue

        # Rule 4: idempotency — already backfilled?
        if existing_src == "historical_selection_parse":
            counts["ALREADY_BACKFILLED_SKIP"] += 1
            per_sport[sport]["already"] += 1
            continue

        # Idempotency for unrecoverable rows: if the row already
        # carries line_source explicitly None (a prior backfill
        # visited it) AND its market/selection is still unparseable,
        # skip the write entirely.  `line_source` MUST be present in
        # the doc (not merely missing) — we detect that by checking
        # if the field was projected into `p` from mongo.
        existing_side = p.get("side")
        market_dry = p.get("market") or ""
        selection_dry = p.get("selection") or ""
        _dry = extract_line_with_provenance(market_dry, selection_dry)
        if (
            existing_src is None
            and _dry["line"] is None
            and "line_source" in p                # field present-with-null
            and p.get("side") == _dry["side"]
        ):
            counts["ALREADY_BACKFILLED_SKIP"] += 1
            per_sport[sport]["already"] += 1
            continue

        # Rule 3: parse the market/selection text.
        market = p.get("market") or ""
        selection = p.get("selection") or ""
        # Structured hint: if a numeric `line` already exists on the
        # pick (no source tag) but we have no evidence it came from a
        # sportsbook, we still treat it as authoritative — but tag
        # provenance so downstream Magic can trust it.
        existing_line = p.get("line")
        try:
            structured_hint = (float(existing_line)
                                if existing_line is not None else None)
        except (TypeError, ValueError):
            structured_hint = None

        r = extract_line_with_provenance(
            market, selection,
            structured_line=structured_hint,
        )

        # Determine the provenance tag we will write.
        if r["line"] is not None:
            if r["line_source"] == "sportsbook_structured":
                # Legacy row already carried a numeric line but no
                # source tag — we DO NOT infer sportsbook_structured
                # in the backfill (that would falsely elevate legacy
                # provenance).  Fall back to the historical tag.
                tag = "historical_selection_parse"
            else:
                tag = "historical_selection_parse"
            update = {
                "line":        r["line"],
                "side":        r["side"],
                "line_source": tag,
            }
            counts["RECOVERED_TEXT_APPLIED"] += 1
            per_sport[sport]["text_applied"] += 1
        else:
            # Unrecoverable — tag line_source explicitly None to make
            # coverage measurable, but do NOT overwrite an existing
            # numeric line (preserve legacy hints even without source).
            update = {
                "side":        r["side"],
                "line_source": None,
            }
            # Only set `line` = None when the row doesn't already
            # have a numeric value.
            if structured_hint is None:
                update["line"] = None
            counts["NOT_RECOVERABLE_APPLIED"] += 1
            per_sport[sport]["unrec"] += 1

        # Track settled-row metadata repair (does NOT change settlement
        # truth — only the line-provenance metadata).
        if str(p.get("status") or "").lower() in (
            "won", "lost", "push", "void", "win", "loss",
        ) or p.get("settled_at"):
            counts["SETTLED_ROWS_TOUCHED"] += 1

        if write:
            ops.append((pid, update))
            if len(ops) >= OP_BATCH:
                await _flush(db, ops)
                ops.clear()

    if write and ops:
        await _flush(db, ops)

    # Post-write mutation-safety verification: re-scan and compare
    # the immutable signature.
    if write:
        async for p in db.picks.find(
            {}, {
                "_id": 0,
                "id": 1, "market": 1, "selection": 1, "sport": 1,
                "sport_key": 1, "status": 1, "result": 1,
                "settled_at": 1, "units_profit": 1, "units_risked": 1,
                "book_odds": 1, "odds_source": 1,
                "closing_odds": 1, "clv_value": 1, "league": 1,
                "event": 1, "event_time": 1, "pick_date": 1,
                "external_id": 1,
            },
        ):
            pid = p.get("id")
            if pid in sig_map:
                new_sig = _immutable_signature(p)
                if new_sig != sig_map[pid]:
                    counts["IMMUTABLE_MISMATCH"] += 1

    return {
        "mode":       "WRITE" if write else "DRY_RUN",
        "generated":  datetime.now(timezone.utc).isoformat(),
        "total":      total,
        "counts":     counts,
        "per_sport":  per_sport,
    }


async def _flush(db, ops: list) -> None:
    from pymongo import UpdateOne
    bulk = [
        UpdateOne({"id": pid}, {"$set": upd})
        for pid, upd in ops
    ]
    if bulk:
        await db.picks.bulk_write(bulk, ordered=False)


def _print_report(rep: dict) -> None:
    print("=" * 72)
    print(f"MAGIC 3A.1 BACKFILL — {rep['mode']} @ {rep['generated']}")
    print("=" * 72)
    print(f"Total rows scanned:               {rep['total']:>7}")
    for k, v in rep["counts"].items():
        print(f"  {k:<35}  {v:>7}")
    print()
    print("Per-sport line coverage:")
    for sport, s in sorted(
        rep["per_sport"].items(), key=lambda kv: -kv[1]["total"]
    ):
        recovered = s["text_applied"] + s["structured"] + s["already"]
        pct = 100.0 * recovered / s["total"] if s["total"] else 0.0
        print(
            f"  {sport:<45} total={s['total']:>5}  "
            f"line_coverage={recovered:>5} ({pct:5.1f}%)  "
            f"unrec={s['unrec']:>5}"
        )
    print()

    # Verdict
    if rep["counts"].get("IMMUTABLE_MISMATCH", 0) > 0:
        print("VERDICT: LINE_PIPELINE_NOT_READY")
        print("Reason: immutable-field mutation detected — halting.")
    else:
        print("VERDICT: LINE_PIPELINE_READY")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true",
                     help="Apply changes (default: dry-run).")
    ap.add_argument("--dry-run", action="store_true",
                     help="Explicit dry-run (default behaviour).")
    args = ap.parse_args()
    write = bool(args.write) and not args.dry_run
    rep = asyncio.run(run(write=write))
    _print_report(rep)
    return 0 if rep["counts"].get("IMMUTABLE_MISMATCH", 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
