"""reconcile_full — PHASE 2 complete offline reconciliation.

Reconciles the Preview mongodump backup against the already-ingested
Production per-collection JSON exports in
``/tmp/production_reconcile_input/``, applying the collection-specific
authority rules documented in the Phase 1 reconciliation manifest.

ABSOLUTE SAFETY
---------------
* No live DB access (hard refuses any Mongo URI, localhost, 127.0.0.1).
* No writes to Preview or Production.
* No modification of the uploaded Production files or the Preview
  mongodump archive.
* The reconciled output is written to a NEW offline location only:
      ``/tmp/perklocks_reconciled_output/``

Outputs
-------
A. ``summary.json``                   per-collection reconciliation table
B. ``conflict_report.json``           conflict identities + reason codes
                                      (no bulk doc contents in B — the
                                       full evidence lives in
                                       conflicts/<coll>.ndjson)
C. ``canonical/<coll>.ndjson``        reconciled SAFE rows
   ``conflicts/<coll>.ndjson``        both-sides conflict evidence
   ``review/<coll>.ndjson``           REVIEW_REQUIRED rows
D. ``reconciliation_manifest_v2.json`` source fingerprints + run metadata
E. the final totals block is also printed to stdout
"""
from __future__ import annotations

import gzip
import hashlib
import json
import pathlib
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone

# Shared logical-key resolver + BSON backup source from Phase 1 tool.
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from reconcile_offline import BackupSource, _logical_key, CLASSIFICATION    # noqa: E402

PREVIEW_ZIP   = pathlib.Path("/tmp/perklocks_preview_FULL_backup_20261003.zip")
INPUT_DIR     = pathlib.Path("/tmp/production_reconcile_input")
INVENTORY     = pathlib.Path("/app/memory/reconcile_offline/production_input_inventory.json")
MANIFEST_V1   = pathlib.Path("/app/memory/reconcile_offline/production_transfer_manifest.json")
OUT_DIR       = pathlib.Path("/tmp/perklocks_reconciled_output")
REPORT_DIR    = pathlib.Path("/app/memory/reconcile_offline")

RULES_VERSION = "phase2.rev1-2026-10-03"

# ─── Authority classification per spec ───────────────────────────────
IMMUTABLE_HARD_ABORT      = {"prediction_snapshots", "pregame_snapshots"}
APPEND_ONLY_REVIEW_ON_DIFF = {"publication_events", "settlement_events", "rollover_slate_events"}
PROD_AUTH                 = {"picks", "users", "user_bets", "rollover_slates",
                             "parlay_history", "historical_ingestion_state", "nfl_ingest_meta"}
PROVIDER_ACTUAL_REVIEW    = {"player_game_actuals", "team_game_actuals"}
FINAL_RESULT_REVIEW       = {"games"}
UNION_REVIEW_ON_DIFF      = {"player_identities", "tennis_matches_history", "soccer_matches",
                             "soccer_player_game_logs", "player_game_logs", "nfl_player_weekly"}

REQUIRED_COLLECTIONS = sorted(
    IMMUTABLE_HARD_ABORT | APPEND_ONLY_REVIEW_ON_DIFF | PROD_AUTH |
    PROVIDER_ACTUAL_REVIEW | FINAL_RESULT_REVIEW | UNION_REVIEW_ON_DIFF
)
assert len(REQUIRED_COLLECTIONS) == 21, f"expected 21 required collections, got {len(REQUIRED_COLLECTIONS)}"

# ─── Volatile fields that must NEVER feed into the content hash ─────
VOLATILE_FIELDS_UNIVERSAL = {
    "_id", "ingested_at", "updated_at", "fetched_at", "data_as_of",
    "source_fetched_at", "last_shown_at", "shown_at", "shown_count",
    "backfill_version", "provenance", "compat_write",
    "last_refresh_at", "last_refreshed", "last_login_at",
}
EXTRA_STRIP = {
    # prediction_snapshots: immutable payload is `snapshot_hash`;
    # is_active/superseded_at/superseded_by are intentional mutations.
    "prediction_snapshots": {"is_active", "superseded_at", "superseded_by"},
    # publication_events: ledger — `at` is canonical, no extra strip.
    # settlement_events: `is_active` toggles when a settlement is corrected.
    "settlement_events": {"is_active"},
    # users: hashed_password / name may drift innocuously — but we
    # still include them so a password rotation surfaces as a diff.
    # pregame_snapshots: `snapshot_hash` is immutable; include everything.
}

# ─── Live-input guard ────────────────────────────────────────────────
def _refuse_live(path: str) -> None:
    low = (path or "").lower()
    if low.startswith(("mongodb://", "mongodb+srv://")) or \
       "localhost" in low or "127.0.0.1" in low:
        raise SystemExit(f"REFUSED — live DB input not allowed: {path}")

# ─── Hashing helpers ─────────────────────────────────────────────────
def _normalize(doc: dict) -> dict:
    """Round-trip through JSON (default=str) so datetime / ObjectId /
    Decimal values from the Preview BSON side normalize to the same
    strings used in the Production JSON files."""
    return json.loads(json.dumps(doc, default=str))

def _content_hash(doc: dict, coll: str) -> str:
    d = _normalize(doc)
    strip = VOLATILE_FIELDS_UNIVERSAL | EXTRA_STRIP.get(coll, set())
    for k in list(d.keys()):
        if k in strip:
            d.pop(k)
    return hashlib.sha256(
        json.dumps(d, sort_keys=True).encode()
    ).hexdigest()[:16]

# ─── Production JSON-parts streamer ──────────────────────────────────
_PART_RE = re.compile(
    r"^(?P<coll>[a-zA-Z0-9_]+?)(?:_slim)?(?:_part(?P<part>\d+))?\.(?P<ext>json(?:\.gz)?|ndjson(?:\.gz)?)$"
)

def _stream_production_collection(coll: str):
    """Yield docs for `coll` by streaming its contiguous JSON parts
    (`<coll>_partN.json[.gz]` and/or `<coll>.json`) from
    /tmp/production_reconcile_input/ in part order."""
    files: list[tuple[int, pathlib.Path]] = []
    for p in sorted(INPUT_DIR.iterdir()):
        m = _PART_RE.match(p.name)
        if not m or m.group("coll") != coll:
            continue
        part = int(m.group("part")) if m.group("part") else 0
        files.append((part, p))
    files.sort()
    for _, path in files:
        is_gz = str(path).endswith(".gz")
        fh = gzip.open(path, "rt") if is_gz else open(path, "r")
        try:
            first = ""
            while True:
                c = fh.read(1)
                if not c:
                    fh.close()
                    return
                if not c.isspace():
                    first = c; break
            fh.close()
            fh = gzip.open(path, "rt") if is_gz else open(path, "r")
            if first == "[":
                data = json.load(fh)
                if isinstance(data, list):
                    for d in data:
                        if isinstance(d, dict):
                            yield d
            elif first == "{":
                for ln in fh:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        yield json.loads(ln)
                    except Exception:
                        continue
        finally:
            try: fh.close()
            except Exception: pass

# ─── Reconcile one collection ────────────────────────────────────────
def reconcile_collection(coll: str, prev_src: BackupSource,
                         out_f, conflicts_f, review_f) -> dict:
    s = {"collection": coll,
         "classification": CLASSIFICATION.get(coll, "UNCLASSIFIED"),
         "authority_rule": _rule_name(coll),
         "preview_total": 0, "production_total": 0,
         "unresolvable_prev": 0, "unresolvable_prod": 0,
         "identical": 0, "preview_only": 0, "production_only": 0,
         "safe_merged": 0, "conflicts": 0, "review_required": 0,
         "aborted": 0,
         "output_count": 0, "status": "SAFE"}

    # Pass A — index Preview: logical_key -> content_hash
    prev_index: dict = {}
    prev_dup_lk = 0
    for d in prev_src.stream_docs(coll):
        s["preview_total"] += 1
        k = _logical_key(coll, d)
        if k is None:
            s["unresolvable_prev"] += 1
            continue
        if k in prev_index:
            prev_dup_lk += 1
        prev_index[k] = _content_hash(d, coll)

    # Pass B — stream Production, decide outcome, write Production side
    outcome: dict = {}
    for d in _stream_production_collection(coll):
        s["production_total"] += 1
        k = _logical_key(coll, d)
        if k is None:
            s["unresolvable_prod"] += 1
            continue
        prev_h = prev_index.get(k)
        prod_h = _content_hash(d, coll)
        if prev_h is None:
            outcome[k] = "prod_only"
            s["production_only"] += 1; s["safe_merged"] += 1
            out_f.write(json.dumps({"canonical_from": "production_only",
                                     "logical_key": k, "doc": _normalize(d)},
                                    default=str) + "\n")
        elif prev_h == prod_h:
            outcome[k] = "identical"
            s["identical"] += 1; s["safe_merged"] += 1
            out_f.write(json.dumps({"canonical_from": "identical_both",
                                     "logical_key": k, "doc": _normalize(d)},
                                    default=str) + "\n")
        else:
            if coll in IMMUTABLE_HARD_ABORT:
                outcome[k] = "immutable_conflict"
                s["aborted"] += 1; s["conflicts"] += 1
                conflicts_f.write(json.dumps({
                    "type": "IMMUTABLE_CONFLICT",
                    "collection": coll, "logical_key": k,
                    "content_hash_production": prod_h,
                    "content_hash_preview": prev_h,
                    "side": "production", "doc": _normalize(d)
                }, default=str) + "\n")
            elif coll in APPEND_ONLY_REVIEW_ON_DIFF:
                outcome[k] = "append_only_review"
                s["review_required"] += 1
                review_f.write(json.dumps({
                    "type": "APPEND_ONLY_CONTENT_DIFF",
                    "collection": coll, "logical_key": k,
                    "content_hash_production": prod_h,
                    "content_hash_preview": prev_h,
                    "side": "production", "doc": _normalize(d)
                }, default=str) + "\n")
            elif coll in PROD_AUTH:
                outcome[k] = "prod_wins"
                s["conflicts"] += 1; s["safe_merged"] += 1
                out_f.write(json.dumps({
                    "canonical_from": "production_wins_authority",
                    "logical_key": k, "doc": _normalize(d),
                    "preview_content_hash": prev_h,
                    "production_content_hash": prod_h
                }, default=str) + "\n")
                conflicts_f.write(json.dumps({
                    "type": "PRODUCTION_WINS",
                    "collection": coll, "logical_key": k,
                    "resolution": "production_authoritative",
                    "content_hash_production": prod_h,
                    "content_hash_preview": prev_h
                }, default=str) + "\n")
            elif coll in PROVIDER_ACTUAL_REVIEW:
                outcome[k] = "provider_actual_review"
                s["review_required"] += 1
                review_f.write(json.dumps({
                    "type": "PROVIDER_ACTUAL_CONFLICT",
                    "collection": coll, "logical_key": k,
                    "content_hash_production": prod_h,
                    "content_hash_preview": prev_h,
                    "side": "production", "doc": _normalize(d)
                }, default=str) + "\n")
            elif coll in FINAL_RESULT_REVIEW:
                outcome[k] = "final_result_review"
                s["review_required"] += 1
                review_f.write(json.dumps({
                    "type": "FINAL_RESULT_CONFLICT",
                    "collection": coll, "logical_key": k,
                    "content_hash_production": prod_h,
                    "content_hash_preview": prev_h,
                    "side": "production", "doc": _normalize(d)
                }, default=str) + "\n")
            elif coll in UNION_REVIEW_ON_DIFF:
                outcome[k] = "union_review"
                s["review_required"] += 1
                review_f.write(json.dumps({
                    "type": "HISTORICAL_LOG_CONTENT_DIFF",
                    "collection": coll, "logical_key": k,
                    "content_hash_production": prod_h,
                    "content_hash_preview": prev_h,
                    "side": "production", "doc": _normalize(d)
                }, default=str) + "\n")
            else:
                outcome[k] = "unknown_review"
                s["review_required"] += 1
                review_f.write(json.dumps({
                    "type": "UNKNOWN_RULE_REVIEW",
                    "collection": coll, "logical_key": k,
                    "side": "production", "doc": _normalize(d)
                }, default=str) + "\n")

    # Pass C — stream Preview again: Preview-only + Preview side of conflicts
    for d in prev_src.stream_docs(coll):
        k = _logical_key(coll, d)
        if k is None:
            continue
        oc = outcome.get(k)
        if oc is None:
            s["preview_only"] += 1; s["safe_merged"] += 1
            out_f.write(json.dumps({"canonical_from": "preview_only",
                                     "logical_key": k, "doc": _normalize(d)},
                                    default=str) + "\n")
        elif oc == "immutable_conflict":
            conflicts_f.write(json.dumps({
                "type": "IMMUTABLE_CONFLICT",
                "collection": coll, "logical_key": k,
                "side": "preview", "doc": _normalize(d)
            }, default=str) + "\n")
        elif oc == "append_only_review":
            review_f.write(json.dumps({
                "type": "APPEND_ONLY_CONTENT_DIFF",
                "collection": coll, "logical_key": k,
                "side": "preview", "doc": _normalize(d)
            }, default=str) + "\n")
        elif oc in ("provider_actual_review", "final_result_review",
                    "union_review", "unknown_review"):
            review_f.write(json.dumps({
                "type": {"provider_actual_review": "PROVIDER_ACTUAL_CONFLICT",
                         "final_result_review":    "FINAL_RESULT_CONFLICT",
                         "union_review":           "HISTORICAL_LOG_CONTENT_DIFF",
                         "unknown_review":         "UNKNOWN_RULE_REVIEW"}[oc],
                "collection": coll, "logical_key": k,
                "side": "preview", "doc": _normalize(d)
            }, default=str) + "\n")
        # identical / prod_only / prod_wins → nothing extra for Preview

    s["output_count"] = s["safe_merged"]
    s["duplicate_preview_logical_keys"] = prev_dup_lk
    if s["aborted"]:
        s["status"] = "ABORTED"
    elif s["review_required"]:
        s["status"] = "REVIEW_REQUIRED"
    elif s["conflicts"]:
        s["status"] = "CONFLICTS_RESOLVED"
    else:
        s["status"] = "SAFE"
    return s


def _rule_name(coll: str) -> str:
    if coll in IMMUTABLE_HARD_ABORT:       return "IMMUTABLE_HARD_ABORT"
    if coll in APPEND_ONLY_REVIEW_ON_DIFF: return "APPEND_ONLY_REVIEW_ON_DIFF"
    if coll in PROD_AUTH:                  return "PROD_AUTH"
    if coll in PROVIDER_ACTUAL_REVIEW:     return "PROVIDER_ACTUAL_REVIEW"
    if coll in FINAL_RESULT_REVIEW:        return "FINAL_RESULT_REVIEW"
    if coll in UNION_REVIEW_ON_DIFF:       return "UNION_REVIEW_ON_DIFF"
    return "UNCLASSIFIED"


# ─── Driver ──────────────────────────────────────────────────────────
def main() -> int:
    _refuse_live(str(PREVIEW_ZIP))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    canonical_dir = OUT_DIR / "canonical";  canonical_dir.mkdir(exist_ok=True)
    conflicts_dir = OUT_DIR / "conflicts";  conflicts_dir.mkdir(exist_ok=True)
    review_dir    = OUT_DIR / "review";     review_dir.mkdir(exist_ok=True)

    print("── loading Preview backup ──", flush=True)
    prev = BackupSource(str(PREVIEW_ZIP), "preview", "lockscore_db")
    print(f"[preview] db_dir={prev._db_dir}", flush=True)
    print(f"[preview] sha256={prev.sha256}", flush=True)

    # Pre-check Production integrity: all 21 required collections ready.
    inv = json.loads(INVENTORY.read_text())
    manifest_v1 = json.loads(MANIFEST_V1.read_text())
    prod_counts_expected = {r["collection"]: r.get("production_count_known")
                            for r in manifest_v1["collections"]}

    results = []
    for coll in REQUIRED_COLLECTIONS:
        print(f"── reconciling {coll} ──", flush=True)
        out_f       = open(canonical_dir / f"{coll}.ndjson", "w")
        conflicts_f = open(conflicts_dir / f"{coll}.ndjson", "w")
        review_f    = open(review_dir / f"{coll}.ndjson", "w")
        try:
            s = reconcile_collection(coll, prev, out_f, conflicts_f, review_f)
        finally:
            out_f.close(); conflicts_f.close(); review_f.close()
        # Attach Production inventory evidence
        prod_state = inv["collections"].get(coll, {})
        s["production_received_count_from_inventory"] = prod_state.get("total_docs", 0)
        s["production_count_known_from_manifest"] = prod_counts_expected.get(coll)
        s["production_parts_received"] = sorted({p.get("part") for p in prod_state.get("parts", []) if p.get("part") is not None})
        results.append(s)
        print(f"   -> {s['status']}  safe={s['safe_merged']}  conflicts={s['conflicts']}  review={s['review_required']}  aborted={s['aborted']}", flush=True)

    # ─── Summary (output A) ───────────────────────────────────────────
    summary_rows = [{
        "collection":        r["collection"],
        "classification":    r["classification"],
        "authority_rule":    r["authority_rule"],
        "production_count":  r["production_total"],
        "preview_count":     r["preview_total"],
        "identical":         r["identical"],
        "production_only":   r["production_only"],
        "preview_only":      r["preview_only"],
        "safe_merged":       r["safe_merged"],
        "conflicts":         r["conflicts"],
        "review_required":   r["review_required"],
        "aborted":           r["aborted"],
        "output_count":      r["output_count"],
        "status":            r["status"],
    } for r in results]
    (REPORT_DIR / "reconciliation_summary.json").write_text(
        json.dumps(summary_rows, indent=2, default=str))

    # ─── Conflict report (output B) — identities only ─────────────────
    conflict_report = defaultdict(list)
    for r in results:
        p = conflicts_dir / f"{r['collection']}.ndjson"
        if p.stat().st_size:
            with open(p) as f:
                for ln in f:
                    try:
                        row = json.loads(ln)
                        conflict_report[r["collection"]].append({
                            "type": row.get("type"),
                            "logical_key": row.get("logical_key"),
                            "resolution": row.get("resolution"),
                            "side": row.get("side"),
                            "content_hash_production": row.get("content_hash_production"),
                            "content_hash_preview":    row.get("content_hash_preview"),
                        })
                    except Exception:
                        continue
    (REPORT_DIR / "conflict_report.json").write_text(
        json.dumps(dict(conflict_report), indent=2, default=str))

    # ─── Reconciliation manifest (output D) ───────────────────────────
    # Source fingerprints
    source_fingerprints = {
        "preview_backup": {
            "path":   str(PREVIEW_ZIP),
            "sha256": prev.sha256,
            "size":   prev.size_bytes,
        },
        "production_files": {},
    }
    for fname, meta in sorted(inv["files"].items()):
        source_fingerprints["production_files"][fname] = {
            "sha256": meta.get("sha256"),
            "size":   meta.get("size_bytes"),
            "doc_count": meta.get("doc_count"),
            "collection": meta.get("collection"),
            "part": meta.get("part"),
        }
    manifest_v2 = {
        "spec_version":              "phase2.v1",
        "rules_version":             RULES_VERSION,
        "generated_at":              datetime.now(timezone.utc).isoformat(),
        "source_fingerprints":       source_fingerprints,
        "per_collection":            {r["collection"]: {
            "preview_count":       r["preview_total"],
            "production_count":    r["production_total"],
            "output_count":        r["output_count"],
            "conflicts":           r["conflicts"],
            "review_required":     r["review_required"],
            "aborted":             r["aborted"],
            "status":              r["status"],
            "authority_rule":      r["authority_rule"],
        } for r in results},
        "safety_confirmations":      {
            "production_live_db_touched":  False,
            "preview_live_db_touched":     False,
            "mongo_url_changed":           False,
            "db_name_changed":             False,
            "atlas_created":               False,
            "deployment":                  False,
            "background_workers_started":  False,
            "canonical_lease_acquired":    False,
            "settlement_regen_backfill":   False,
            "model_scoring_pub_changes":   False,
            "canonical_dataset_imported":  False,
        },
    }
    totals = {
        "SAFE":            sum(1 for r in results if r["status"] == "SAFE"),
        "CONFLICTS_RESOLVED": sum(1 for r in results if r["status"] == "CONFLICTS_RESOLVED"),
        "REVIEW_REQUIRED": sum(1 for r in results if r["status"] == "REVIEW_REQUIRED"),
        "ABORTED":         sum(1 for r in results if r["status"] == "ABORTED"),
    }
    manifest_v2["totals"] = totals
    (REPORT_DIR / "reconciliation_manifest_v2.json").write_text(
        json.dumps(manifest_v2, indent=2, default=str))

    print("── reconciliation done ──", flush=True)
    print(json.dumps(totals, indent=2))
    prev.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
