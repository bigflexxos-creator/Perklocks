"""preview_baseline_v2_backup — Fresh persistent Preview baseline for V2 reconciliation.

Writes to /app/reconcile_workspace/preview_baseline_v2/ (NOT /tmp).
Guardrails identical to preview_full_backup.py.  New baseline tag: v2.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import zipfile

# Reuse all helpers from the proven preview_full_backup.py.
sys.path.insert(0, "/app/backend/scripts")
from preview_full_backup import (  # type: ignore
    _authority_posture, _fp, _inventory, _is_local, _refuse_if_not_preview,
    _run_mongodump, _sha256_file, _verify_counts, _write_manifest_md,
)
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv("/app/backend/.env")

MONGO_URL = os.environ.get("MONGO_URL", "")
DB_NAME   = os.environ.get("DB_NAME", "")

# Hard override module-level constants that preview_full_backup helpers rely on
import preview_full_backup as _pfb
_pfb.MONGO_URL = MONGO_URL
_pfb.DB_NAME   = DB_NAME

WS = pathlib.Path("/app/reconcile_workspace/preview_baseline_v2")
WS.mkdir(parents=True, exist_ok=True)


def main() -> int:
    _refuse_if_not_preview()

    stamp = _dt.datetime.utcnow()
    stamp_iso = stamp.replace(tzinfo=_dt.timezone.utc).isoformat()
    stamp_tag = stamp.strftime("%Y%m%d_%H%M%SZ")
    work = WS / f"baseline_v2_{stamp_tag}"
    work.mkdir(parents=True, exist_ok=False)
    dump_dir = str(work / "dump")

    client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)
    client.admin.command("ping")
    db = client[DB_NAME]

    print(f"[1/5] inventorying Preview DB …", flush=True)
    inv_start = _dt.datetime.utcnow()
    inv = _inventory(db)
    inv_end = _dt.datetime.utcnow()
    print(f"      {inv['total_collections']} collections · {inv['total_documents']:,} documents", flush=True)

    print(f"[2/5] mongodump --gzip → {dump_dir} …", flush=True)
    dump_log = _run_mongodump(dump_dir)
    print(f"      {len(dump_log['files'])} files written", flush=True)

    print(f"[3/5] verifying export counts …", flush=True)
    final_inv = _inventory(db)
    verify = _verify_counts(inv, dump_dir, final_inv)

    print(f"[4/5] building manifest …", flush=True)
    empty = [n for n, m in inv["collections"].items() if m["count"] == 0]
    failed = []
    for n, row in verify["per_collection"].items():
        if row.get("status") in ("MISMATCH", "MISSING_EXPORT", "BSON_DECODE_FAILED"):
            if inv["collections"][n]["count"] > 0 or final_inv["collections"].get(n, {}).get("count", 0) > 0:
                failed.append(n)

    manifest = {
        "baseline_tag":                "preview_baseline_v2",
        "generated_by":                "preview_baseline_v2_backup.py",
        "environment":                 "preview",
        "authority_posture":           _authority_posture(),
        "db_name":                     DB_NAME,
        "db_fingerprint":              _fp(MONGO_URL, DB_NAME),
        "export_start_timestamp":      inv_start.replace(tzinfo=_dt.timezone.utc).isoformat(),
        "export_completion_timestamp": _dt.datetime.utcnow().replace(tzinfo=_dt.timezone.utc).isoformat(),
        "mongodump_version":           dump_log.get("stderr", "").split("\n")[0][:200],
        "source_inventory":            inv,
        "verification":                verify,
        "files":                       dump_log["files"],
        "empty_collections":           sorted(empty),
        "failed_collections":          sorted(failed),
        "total_backup_size_bytes":     sum(f["size_bytes"] for f in dump_log["files"]),
    }

    manifest_json = str(work / "manifest.json")
    manifest_md   = str(work / "manifest.md")

    archive_path = str(WS / f"perklocks_preview_baseline_v2_{stamp.strftime('%Y%m%d_%H%M%SZ')}.zip")
    print(f"[5/5] archiving → {archive_path} …", flush=True)
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_STORED) as zf:
        for root, _dirs, files in os.walk(str(work)):
            for f in files:
                p = os.path.join(root, f)
                zf.write(p, os.path.relpath(p, str(work)))
    archive_sha = _sha256_file(archive_path)
    archive_size = os.path.getsize(archive_path)

    manifest["archive_path"]        = archive_path
    manifest["archive_sha256"]      = archive_sha
    manifest["archive_size_bytes"]  = archive_size

    with open(manifest_json, "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True, default=str)
    _write_manifest_md(manifest, manifest_md)

    # Also drop trimmed manifest alongside workspace root for easy access
    trimmed = {k: v for k, v in manifest.items() if k != "files"}
    trimmed["files_count"] = len(manifest["files"])
    with open(WS / f"baseline_v2_manifest_{stamp.strftime('%Y%m%d_%H%M%SZ')}.json", "w") as f:
        json.dump(trimmed, f, indent=2, sort_keys=True, default=str)
    # Stable alias (overwritten each run)
    with open(WS / "LATEST_manifest.json", "w") as f:
        json.dump(trimmed, f, indent=2, sort_keys=True, default=str)
    with open(WS / "LATEST_archive_path.txt", "w") as f:
        f.write(archive_path + "\n")

    print()
    print("=" * 70, flush=True)
    print("PREVIEW BASELINE V2 BACKUP COMPLETE", flush=True)
    print("=" * 70, flush=True)
    print(f"  Baseline tag:            preview_baseline_v2", flush=True)
    print(f"  Database:                {manifest['db_name']}", flush=True)
    print(f"  DB fingerprint:          {manifest['db_fingerprint']}", flush=True)
    print(f"  Collections (total):     {inv['total_collections']}", flush=True)
    print(f"  Documents (total):       {inv['total_documents']:,}", flush=True)
    print(f"  Backup size (bytes):     {manifest['total_backup_size_bytes']:,}", flush=True)
    print(f"  Archive path:            {archive_path}", flush=True)
    print(f"  Archive size (bytes):    {archive_size:,}", flush=True)
    print(f"  Archive SHA-256:         {archive_sha}", flush=True)
    print(f"  Manifest (full):         {manifest_json}", flush=True)
    print(f"  Failed/incomplete:       {len(failed)}", flush=True)

    if failed:
        print("\nWARNING — these collections failed or were incomplete:", flush=True)
        for n in failed:
            print(f"  * {n}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
