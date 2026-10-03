"""preview_baseline_v2_scoped — Scoped Preview backup for V2 reconciliation.

Exports EXACTLY the 21 reconciliation-contract collections.
Writes to overlay FS (/opt/reconcile_tmp/), zips, SHA-verifies,
then persists the ZIP + manifest under /app/reconcile_workspace/preview_baseline_v2/
and deletes the intermediate dump tree.

Explicit label on the manifest: PREVIEW_BASELINE_V2_SCOPED_21_COLLECTIONS
Explicitly NOT a full Preview DB backup.
"""
from __future__ import annotations

import datetime as _dt
import gzip
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import zipfile

sys.path.insert(0, "/app/backend/scripts")
from preview_full_backup import (  # type: ignore
    _authority_posture, _fp, _is_local, _sha256_file,
)
import preview_full_backup as _pfb
import bson
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv("/app/backend/.env")

MONGO_URL = os.environ.get("MONGO_URL", "")
DB_NAME   = os.environ.get("DB_NAME", "")
_pfb.MONGO_URL = MONGO_URL
_pfb.DB_NAME   = DB_NAME

PERSISTENT = pathlib.Path("/app/reconcile_workspace/preview_baseline_v2")
PERSISTENT.mkdir(parents=True, exist_ok=True)
TRANSIENT_ROOT = pathlib.Path("/opt/reconcile_tmp")
TRANSIENT_ROOT.mkdir(parents=True, exist_ok=True)

RECONCILIATION_COLLECTIONS = [
    "games",
    "historical_ingestion_state",
    "nfl_ingest_meta",
    "nfl_player_weekly",
    "parlay_history",
    "picks",
    "player_game_actuals",
    "player_game_logs",
    "player_identities",
    "prediction_snapshots",
    "pregame_snapshots",
    "publication_events",
    "rollover_slate_events",
    "rollover_slates",
    "settlement_events",
    "soccer_matches",
    "soccer_player_game_logs",
    "team_game_actuals",
    "tennis_matches_history",
    "user_bets",
    "users",
]
assert len(RECONCILIATION_COLLECTIONS) == 21


def _refuse_if_not_preview() -> None:
    p = _authority_posture()
    reasons = []
    if (p["DATA_AUTHORITY"] or "").lower() != "preview":
        reasons.append(f"DATA_AUTHORITY={p['DATA_AUTHORITY']!r} (expected 'preview')")
    if (p["CANONICAL_WRITE_ENABLED"] or "").lower() != "false":
        reasons.append(f"CANONICAL_WRITE_ENABLED={p['CANONICAL_WRITE_ENABLED']!r}")
    if (p["BACKGROUND_WORKERS_ENABLED"] or "").lower() != "false":
        reasons.append(f"BACKGROUND_WORKERS_ENABLED={p['BACKGROUND_WORKERS_ENABLED']!r}")
    if not _is_local(MONGO_URL):
        reasons.append("MONGO_URL does not point at localhost")
    if reasons:
        print("REFUSED — not Preview environment:")
        for r in reasons:
            print(f"  * {r}")
        sys.exit(2)


def _dump_one(coll: str, out_dir: str) -> dict:
    cmd = [
        "mongodump", "--uri", MONGO_URL, "--db", DB_NAME,
        "--collection", coll, "--gzip", "--out", out_dir,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(f"mongodump({coll}) failed: {proc.stderr[-800:]}")
    return {"returncode": proc.returncode, "stderr_tail": proc.stderr[-400:]}


def _count_bson_gz(path: str) -> int:
    n = 0
    with gzip.open(path, "rb") as f:
        for _ in bson.decode_file_iter(f):
            n += 1
    return n


def main() -> int:
    _refuse_if_not_preview()

    stamp = _dt.datetime.utcnow()
    stamp_iso = stamp.replace(tzinfo=_dt.timezone.utc).isoformat()
    stamp_tag = stamp.strftime("%Y%m%d_%H%M%SZ")

    work = TRANSIENT_ROOT / f"baseline_v2_scoped_{stamp_tag}"
    work.mkdir(parents=True, exist_ok=False)
    dump_dir = work / "dump"
    dump_dir.mkdir()

    client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)
    client.admin.command("ping")
    db = client[DB_NAME]
    print(f"[1/5] inventorying 21 reconciliation collections …", flush=True)
    initial_counts: dict[str, int] = {}
    for c in RECONCILIATION_COLLECTIONS:
        try:
            initial_counts[c] = db[c].count_documents({})
        except Exception as e:
            initial_counts[c] = -1
            print(f"    WARN {c}: count failed: {e}")
    initial_total = sum(v for v in initial_counts.values() if v > 0)
    print(f"      initial total = {initial_total:,} docs across 21 collections", flush=True)

    print(f"[2/5] mongodump 21 collections → {dump_dir} …", flush=True)
    dump_logs: dict[str, dict] = {}
    for i, c in enumerate(RECONCILIATION_COLLECTIONS, 1):
        print(f"      ({i:>2}/21) {c}", flush=True)
        dump_logs[c] = _dump_one(c, str(dump_dir))

    print(f"[3/5] re-inventorying + verifying BSON counts …", flush=True)
    final_counts: dict[str, int] = {}
    verify: dict[str, dict] = {}
    db_sub = dump_dir / DB_NAME
    for c in RECONCILIATION_COLLECTIONS:
        try:
            final_counts[c] = db[c].count_documents({})
        except Exception:
            final_counts[c] = -1
        bson_path = db_sub / f"{c}.bson.gz"
        if not bson_path.exists():
            status = "PASS_EMPTY" if initial_counts[c] == 0 and final_counts[c] == 0 else "MISSING_EXPORT"
            verify[c] = {"initial": initial_counts[c], "final": final_counts[c],
                         "exported": 0, "status": status}
            continue
        try:
            exported = _count_bson_gz(str(bson_path))
        except Exception as e:
            verify[c] = {"initial": initial_counts[c], "final": final_counts[c],
                         "exported": None, "status": "BSON_DECODE_FAILED",
                         "err": str(e)[:200]}
            continue
        initial, final = initial_counts[c], final_counts[c]
        if exported in (initial, final) or (initial <= exported <= final) or (final <= exported <= initial):
            status = "PASS"
        else:
            status = "MISMATCH"
        verify[c] = {"initial": initial, "final": final,
                     "exported": exported, "status": status}

    failed = [c for c, v in verify.items() if v["status"] in ("MISMATCH", "MISSING_EXPORT", "BSON_DECODE_FAILED")]

    print(f"[4/5] enumerating output files + SHA ...", flush=True)
    files_rows = []
    for root, _dirs, files in os.walk(str(dump_dir)):
        for f in sorted(files):
            p = os.path.join(root, f)
            size = os.path.getsize(p)
            sha = _sha256_file(p)
            files_rows.append({
                "relative_path": os.path.relpath(p, str(dump_dir)),
                "size_bytes": size,
                "sha256": sha,
            })
    total_backup_size = sum(f["size_bytes"] for f in files_rows)

    archive_path = str(PERSISTENT / f"perklocks_preview_baseline_v2_scoped_{stamp_tag}.zip")
    print(f"[5/5] zipping → {archive_path}", flush=True)
    # Pre-write manifest into the dump work tree so it's inside the zip
    pre_manifest = {
        "baseline_tag":                "PREVIEW_BASELINE_V2_SCOPED_21_COLLECTIONS",
        "generated_by":                "preview_baseline_v2_scoped.py",
        "scope":                       "SCOPED_21_RECONCILIATION_COLLECTIONS",
        "scoped_collections":          RECONCILIATION_COLLECTIONS,
        "environment":                 "preview",
        "authority_posture":           _authority_posture(),
        "db_name":                     DB_NAME,
        "db_fingerprint":              _fp(MONGO_URL, DB_NAME),
        "export_start_timestamp":      stamp.replace(tzinfo=_dt.timezone.utc).isoformat(),
        "export_completion_timestamp": _dt.datetime.utcnow().replace(tzinfo=_dt.timezone.utc).isoformat(),
        "source_inventory": {
            "scoped_collections":   RECONCILIATION_COLLECTIONS,
            "initial_counts":       initial_counts,
            "final_counts":         final_counts,
            "total_documents":      sum(v for v in final_counts.values() if v > 0),
            "total_collections":    len(RECONCILIATION_COLLECTIONS),
        },
        "verification":                verify,
        "files":                       files_rows,
        "failed_collections":          sorted(failed),
        "empty_collections":           [c for c, v in verify.items() if v["status"] == "PASS_EMPTY"],
        "total_backup_size_bytes":     total_backup_size,
        "warning_this_is_scoped_not_full": True,
    }
    with open(work / "manifest.json", "w") as f:
        json.dump(pre_manifest, f, indent=2, sort_keys=True, default=str)

    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_STORED) as zf:
        for root, _dirs, files in os.walk(str(work)):
            for f in files:
                p = os.path.join(root, f)
                zf.write(p, os.path.relpath(p, str(work)))

    archive_sha = _sha256_file(archive_path)
    archive_size = os.path.getsize(archive_path)

    final_manifest = dict(pre_manifest)
    final_manifest["archive_path"] = archive_path
    final_manifest["archive_sha256"] = archive_sha
    final_manifest["archive_size_bytes"] = archive_size

    with open(PERSISTENT / f"baseline_v2_scoped_manifest_{stamp_tag}.json", "w") as f:
        # trimmed (no per-file list to keep size small)
        trim = {k: v for k, v in final_manifest.items() if k != "files"}
        trim["files_count"] = len(files_rows)
        json.dump(trim, f, indent=2, sort_keys=True, default=str)

    with open(PERSISTENT / "LATEST_scoped_manifest.json", "w") as f:
        trim = {k: v for k, v in final_manifest.items() if k != "files"}
        trim["files_count"] = len(files_rows)
        json.dump(trim, f, indent=2, sort_keys=True, default=str)
    with open(PERSISTENT / "LATEST_scoped_archive_path.txt", "w") as f:
        f.write(archive_path + "\n")

    # Clean up transient dump tree
    shutil.rmtree(work, ignore_errors=True)

    print()
    print("=" * 70, flush=True)
    print("PREVIEW BASELINE V2 (SCOPED 21 COLLECTIONS) COMPLETE", flush=True)
    print("=" * 70, flush=True)
    print(f"  Label:               PREVIEW_BASELINE_V2_SCOPED_21_COLLECTIONS", flush=True)
    print(f"  DB fingerprint:      {final_manifest['db_fingerprint']}", flush=True)
    print(f"  Collections:         21", flush=True)
    print(f"  Total docs:          {sum(v for v in final_counts.values() if v > 0):,}", flush=True)
    print(f"  Backup raw size:     {total_backup_size:,} bytes", flush=True)
    print(f"  Archive path:        {archive_path}", flush=True)
    print(f"  Archive size:        {archive_size:,} bytes", flush=True)
    print(f"  Archive SHA-256:     {archive_sha}", flush=True)
    print(f"  Failed collections:  {len(failed)} {failed}", flush=True)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
