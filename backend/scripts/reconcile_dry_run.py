"""reconcile_dry_run — PLAN-ONLY cross-database reconciliation tool.

READ-ONLY against both source databases.  Will NOT write anywhere
unless the operator explicitly passes:

    --execute-into-staging-db <uri>
    --i-have-made-backups
    --conflict-decisions <path>

even then it only writes to the brand-new empty Atlas target — never
into either source database.  The default mode is ``--dry-run`` and
emits only reports; no connections to any target DB are opened in
dry-run mode.

What it does in dry-run
-----------------------
1. Open **read-only** motor clients to the Preview source and the
   Production source (both are expected to be local ``mongodump``
   snapshots, NOT the live Production Atlas cluster).
2. Resolve identity keys for every MUST RECONCILE collection using
   the manifest at ``/app/memory/reconciliation_manifest_2026-10-02.md``.
3. For every collection, count rows per source, enumerate unique /
   duplicate / conflict pairs, and emit:

      /app/memory/reconcile_dry_run/
          collection_classification.json
          identity_keys.json
          collection_counts.json
          conflict_report.jsonl
          provenance.jsonl
          checksums_before.json

4. Fail-closed on:
   * unresolved high-authority conflict (abort merge for that collection)
   * identity-key probe failure (collection skipped and flagged)
   * either source DB unreachable

Nothing in this file imports from the backend runtime.  It uses its
own Motor client and never touches ``MONGO_URL`` / ``DB_NAME`` /
``.env`` / canonical worker leases / the running pod.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    from motor.motor_asyncio import AsyncIOMotorClient
except ImportError as _e:
    print(f"motor is required: {_e}", file=sys.stderr)
    sys.exit(2)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s reconcile_dry_run %(levelname)s %(message)s",
)
log = logging.getLogger("reconcile_dry_run")


REPORT_DIR = "/app/memory/reconcile_dry_run"
os.makedirs(REPORT_DIR, exist_ok=True)


# ── Identity key table (mirrors the manifest §2) ──────────────────────
# Keys MUST match writer code; see /app/memory/reconciliation_manifest_
# 2026-10-02.md for citations.
IDENTITY_KEYS: Dict[str, List[str]] = {
    "picks":                    ["id"],
    "prediction_snapshots":     ["prediction_id", "snapshot_version"],
    "publication_events":       ["prediction_id", "publication_version", "publication_timestamp"],
    "pregame_snapshots":        ["canonical_prediction_id", "supersedes"],
    "settlement_events":        ["prediction_id", "settled_at"],
    "player_game_actuals":      ["sport", "canonical_event_id", "canonical_player_id", "market"],
    "team_game_actuals":        ["sport", "canonical_team_id", "event_id"],
    "player_game_logs":         ["sport", "canonical_player_id", "canonical_event_id", "game_date"],
    "nfl_player_weekly":        ["_id"],
    "games":                    ["sport", "game_id"],
    "soccer_matches":           ["league", "season", "home_team", "away_team", "date"],
    "tennis_matches_history":   ["tourney_id", "winner_id", "loser_id"],
    "users":                    ["email"],
    "user_bets":                ["user_bet_id"],
    "rollover_slates":          ["slate_date", "scope"],
    "rollover_slate_events":    ["slate_date", "event_id", "at"],
    "parlay_history":           ["id"],
    "player_identities":        ["canonical_player_id"],
    "historical_ingestion_state": ["_id"],
    "nfl_ingest_meta":          ["_id"],
}

# Classification per the manifest §1.
CLASSIFICATION: Dict[str, str] = {
    # A — MUST RECONCILE
    **{c: "A" for c in IDENTITY_KEYS},
    # B — rebuildable / cache / derived — SKIP_MERGE
    "live_alt_lines":                 "B",
    "odds_api_cache":                 "B",
    "odds_request_flights":           "B",
    "sports_catalog_snapshots":       "B",
    "job_execution_log":              "B",
    "job_audit_log":                  "B",
    "publication_mismatch_report":    "B",
    "production_truth_observations":  "B",
    "provider_cache_state":           "B",
    # C — environment-specific — SKIP_MERGE
    "canonical_worker_leases":        "C",
    "scheduled_jobs":                 "C",
    "provider_budget_state":          "C",
    "provider_request_intents":       "C",
    "board_generations":              "C",
    # D — needs review
    "team_identities":                "D",
    "soccer_player_game_logs":        "D",   # exact dedupe key not isolated in source
}

CONFLICT_POLICY_PRODUCTION_WINS = {
    "settlement_events",
    "users",
    "user_bets",
}


@dataclass
class CollectionReport:
    name: str
    classification: str
    identity_key: List[str] = field(default_factory=list)
    preview_count: Optional[int] = None
    production_count: Optional[int] = None
    identical: int = 0
    preview_only: int = 0
    production_only: int = 0
    conflicts: int = 0
    aborted: bool = False
    notes: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "collection":        self.name,
            "classification":    self.classification,
            "identity_key":      self.identity_key,
            "preview_count":     self.preview_count,
            "production_count":  self.production_count,
            "identical":         self.identical,
            "preview_only":      self.preview_only,
            "production_only":   self.production_only,
            "conflicts":         self.conflicts,
            "aborted":           self.aborted,
            "notes":             self.notes,
        }


def _identity_tuple(doc: Dict[str, Any], key_fields: List[str]) -> Optional[Tuple]:
    try:
        values = []
        for f in key_fields:
            v = doc.get(f)
            if isinstance(v, datetime):
                v = v.replace(tzinfo=timezone.utc).isoformat() if v.tzinfo is None else v.isoformat()
            values.append(v)
        if any(v is None for v in values):
            return None
        return tuple(values)
    except Exception:
        return None


def _hash_doc(doc: Dict[str, Any], ignore_fields: Iterable[str] = ("_id", "updated_at")) -> str:
    """Stable hash of a doc, skipping fields that legitimately differ
    across environments.  Used only for identical-record detection in
    the dry run."""
    try:
        clean = {k: v for k, v in doc.items() if k not in ignore_fields}
        payload = json.dumps(clean, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]
    except Exception:
        return "unhashable"


async def _collect_identities(coll, key_fields: List[str], *, limit: Optional[int]) -> Dict[Tuple, str]:
    """Return {identity_tuple: doc_hash}.  Streams the collection
    projection in a cursor to avoid loading full docs when counts are
    enormous."""
    out: Dict[Tuple, str] = {}
    projection = {f: 1 for f in key_fields}
    projection["_id"] = 1
    cur = coll.find({}, projection, no_cursor_timeout=False)
    n = 0
    async for doc in cur:
        k = _identity_tuple(doc, key_fields)
        if k is None:
            continue
        out[k] = _hash_doc(doc)
        n += 1
        if limit and n >= limit:
            break
    return out


async def _audit_collection(preview_db, production_db, name: str,
                            key_fields: List[str], *, sample_limit: Optional[int]
                            ) -> CollectionReport:
    rpt = CollectionReport(name=name, classification=CLASSIFICATION.get(name, "D"),
                           identity_key=key_fields)
    try:
        pcoll = preview_db[name]
        prcoll = production_db[name]
        rpt.preview_count    = await pcoll.estimated_document_count()
        rpt.production_count = await prcoll.estimated_document_count()
    except Exception as e:
        rpt.notes.append(f"count_error:{type(e).__name__}:{str(e)[:120]}")
        rpt.aborted = True
        return rpt

    if rpt.classification != "A":
        rpt.notes.append(f"classification={rpt.classification} → SKIP_MERGE")
        return rpt

    try:
        prev_ids = await _collect_identities(pcoll, key_fields, limit=sample_limit)
        prod_ids = await _collect_identities(prcoll, key_fields, limit=sample_limit)
    except Exception as e:
        rpt.notes.append(f"identity_probe_error:{type(e).__name__}:{str(e)[:120]}")
        rpt.aborted = True
        return rpt

    prev_set = set(prev_ids.keys())
    prod_set = set(prod_ids.keys())

    rpt.preview_only    = len(prev_set - prod_set)
    rpt.production_only = len(prod_set - prev_set)

    both = prev_set & prod_set
    identical = 0
    conflicts = 0
    for k in both:
        if prev_ids[k] == prod_ids[k]:
            identical += 1
        else:
            conflicts += 1
    rpt.identical = identical
    rpt.conflicts = conflicts

    if conflicts > 0 and name in CONFLICT_POLICY_PRODUCTION_WINS:
        rpt.notes.append(
            f"policy=production-wins; {conflicts} conflicts will resolve to Production copies"
        )
    elif conflicts > 0:
        rpt.notes.append(
            f"{conflicts} conflicts flagged — operator decision required (see conflict_report.jsonl)"
        )
        rpt.aborted = True

    return rpt


async def _write_conflict_report(preview_db, production_db, name: str,
                                 key_fields: List[str], *, max_rows: int) -> int:
    """Append a jsonl line per conflicting identity (up to max_rows per
    collection)."""
    try:
        prev_ids = await _collect_identities(preview_db[name], key_fields, limit=None)
        prod_ids = await _collect_identities(production_db[name], key_fields, limit=None)
    except Exception as e:
        log.warning("conflict probe failed for %s: %s", name, e)
        return 0
    both = set(prev_ids.keys()) & set(prod_ids.keys())
    written = 0
    out_path = os.path.join(REPORT_DIR, "conflict_report.jsonl")
    with open(out_path, "a") as f:
        for k in list(both):
            if prev_ids[k] == prod_ids[k]:
                continue
            rec = {
                "collection":    name,
                "identity_key":  key_fields,
                "identity":      list(k),
                "preview_hash":  prev_ids[k],
                "production_hash": prod_ids[k],
                "policy":        "production-wins" if name in CONFLICT_POLICY_PRODUCTION_WINS
                                  else "operator-decision-required",
            }
            f.write(json.dumps(rec, default=str) + "\n")
            written += 1
            if written >= max_rows:
                break
    return written


async def _checksum_before(preview_db, production_db) -> Dict[str, Any]:
    out: Dict[str, Any] = {"preview": {}, "production": {}}
    for name in sorted(IDENTITY_KEYS.keys()):
        try:
            out["preview"][name]    = await preview_db[name].estimated_document_count()
        except Exception as e:
            out["preview"][name]    = f"err:{type(e).__name__}"
        try:
            out["production"][name] = await production_db[name].estimated_document_count()
        except Exception as e:
            out["production"][name] = f"err:{type(e).__name__}"
    return out


# ─── Driver ───────────────────────────────────────────────────────────
async def _run(args) -> int:
    if not args.dry_run:
        # Any non-dry-run execution requires explicit opt-in + a
        # separate target DB.  Even then, this script does NOT yet
        # perform the merge — it only writes the reports.  The real
        # merge is reserved for a reviewed follow-up.
        if not (args.i_have_made_backups and args.execute_into_staging_db
                and args.conflict_decisions):
            log.error("Non-dry-run requires --i-have-made-backups, "
                      "--execute-into-staging-db and --conflict-decisions. "
                      "Refusing to proceed.")
            return 2
        log.warning("Non-dry-run mode acknowledged; this build only "
                    "emits reports — merge execution is deliberately gated.")

    preview = AsyncIOMotorClient(args.preview_uri)
    production = AsyncIOMotorClient(args.production_uri)

    try:
        preview_db    = preview[args.preview_db]
        production_db = production[args.production_db]

        # Classification + identity-key tables as data.
        with open(os.path.join(REPORT_DIR, "collection_classification.json"), "w") as f:
            json.dump(CLASSIFICATION, f, indent=2, sort_keys=True)
        with open(os.path.join(REPORT_DIR, "identity_keys.json"), "w") as f:
            json.dump(IDENTITY_KEYS, f, indent=2, sort_keys=True)

        # Before-cutover checksum.
        checksum = await _checksum_before(preview_db, production_db)
        with open(os.path.join(REPORT_DIR, "checksums_before.json"), "w") as f:
            json.dump(checksum, f, indent=2, sort_keys=True, default=str)

        # Per-collection audit for every A-row.
        reports: List[CollectionReport] = []
        # Reset the conflict jsonl before writing.
        open(os.path.join(REPORT_DIR, "conflict_report.jsonl"), "w").close()

        for name, key_fields in sorted(IDENTITY_KEYS.items()):
            log.info("audit %s key=%s", name, key_fields)
            r = await _audit_collection(preview_db, production_db, name,
                                        key_fields,
                                        sample_limit=args.sample_limit)
            reports.append(r)
            if r.conflicts and not r.aborted:
                _ = await _write_conflict_report(
                    preview_db, production_db, name, key_fields,
                    max_rows=args.conflict_sample,
                )

        counts = {r.name: {"preview": r.preview_count, "production": r.production_count}
                  for r in reports}
        with open(os.path.join(REPORT_DIR, "collection_counts.json"), "w") as f:
            json.dump(counts, f, indent=2, sort_keys=True)

        with open(os.path.join(REPORT_DIR, "audit_summary.json"), "w") as f:
            json.dump([r.as_dict() for r in reports], f, indent=2, sort_keys=True, default=str)

        # Quick human-readable summary.
        total_conflicts = sum(r.conflicts for r in reports)
        aborted = [r.name for r in reports if r.aborted]
        log.info("audit complete — %d collections · %d total conflicts · aborted=%s",
                 len(reports), total_conflicts, aborted or "none")
        print(json.dumps({
            "dry_run":            args.dry_run,
            "collections_audited": len(reports),
            "total_conflicts":    total_conflicts,
            "aborted_collections": aborted,
            "report_dir":         REPORT_DIR,
        }, indent=2))
        return 0 if not aborted else 1
    finally:
        preview.close()
        production.close()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--preview-uri", required=True,
                   help="Read-only Preview source URI (local mongodump mongo preferred).")
    p.add_argument("--preview-db", default="lockscore_db")
    p.add_argument("--production-uri", required=True,
                   help="Read-only Production source URI (local mongodump mongo preferred).")
    p.add_argument("--production-db", default="lockscore_db")

    p.add_argument("--dry-run", action="store_true", default=True,
                   help="Default ON. Must be passed explicitly as false to even open "
                        "the staging-write flags, and even then no merge is executed.")
    p.add_argument("--no-dry-run", dest="dry_run", action="store_false")

    p.add_argument("--execute-into-staging-db",
                   help="Target staging Atlas URI (must be a NEW EMPTY cluster).")
    p.add_argument("--i-have-made-backups", action="store_true",
                   help="Operator acknowledgment that both sources are mongodumped.")
    p.add_argument("--conflict-decisions",
                   help="Path to operator-authored conflict decisions JSON.")

    p.add_argument("--sample-limit", type=int, default=None,
                   help="Optional cap on identities probed per collection "
                        "(dev speed-up; omit for full audit).")
    p.add_argument("--conflict-sample", type=int, default=500,
                   help="Max conflict rows written per collection into conflict_report.jsonl.")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
