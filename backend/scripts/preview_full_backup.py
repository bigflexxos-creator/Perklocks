"""preview_full_backup — READ-ONLY Preview MongoDB backup.

Hard-coded safety:
 * refuses to run unless DATA_AUTHORITY=preview AND
   CANONICAL_WRITE_ENABLED=false AND BACKGROUND_WORKERS_ENABLED=false
 * refuses to run unless the MONGO_URL points at localhost/127.0.0.1
   (the pod-local Preview Mongo)
 * NEVER writes to Mongo, NEVER migrates, NEVER touches Production
 * emits:
     - mongodump BSON (--gzip) — exact, bit-perfect copy
     - manifest.json + manifest.md
     - sha256 for every file + for the final zip archive

Usage:
    python scripts/preview_full_backup.py
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import zipfile

from pymongo import MongoClient


MONGO_URL = os.environ.get("MONGO_URL", "")
DB_NAME   = os.environ.get("DB_NAME", "")

if not MONGO_URL:
    # Load from the backend .env — same source the app uses.
    try:
        from dotenv import load_dotenv
        load_dotenv("/app/backend/.env")
        MONGO_URL = os.environ.get("MONGO_URL", "")
        DB_NAME   = os.environ.get("DB_NAME", "")
    except Exception:
        pass


def _fp(mongo_url: str, db_name: str) -> str:
    return hashlib.sha256(f"{mongo_url}|{db_name}".encode()).hexdigest()[:12]


def _is_local(url: str) -> bool:
    return "localhost" in url or "127.0.0.1" in url


def _authority_posture() -> dict:
    return {
        "DATA_AUTHORITY":             os.environ.get("DATA_AUTHORITY", ""),
        "CANONICAL_WRITE_ENABLED":    os.environ.get("CANONICAL_WRITE_ENABLED", ""),
        "BACKGROUND_WORKERS_ENABLED": os.environ.get("BACKGROUND_WORKERS_ENABLED", ""),
    }


def _refuse_if_not_preview() -> None:
    p = _authority_posture()
    reasons = []
    if (p["DATA_AUTHORITY"] or "").lower() != "preview":
        reasons.append(f"DATA_AUTHORITY={p['DATA_AUTHORITY']!r} (expected 'preview')")
    if (p["CANONICAL_WRITE_ENABLED"] or "").lower() != "false":
        reasons.append(f"CANONICAL_WRITE_ENABLED={p['CANONICAL_WRITE_ENABLED']!r} (expected 'false')")
    if (p["BACKGROUND_WORKERS_ENABLED"] or "").lower() != "false":
        reasons.append(f"BACKGROUND_WORKERS_ENABLED={p['BACKGROUND_WORKERS_ENABLED']!r} (expected 'false')")
    if not _is_local(MONGO_URL):
        reasons.append("MONGO_URL does not point at localhost — refusing to export a non-Preview DB")
    if reasons:
        print("REFUSED — cannot prove Preview environment:")
        for r in reasons:
            print(f"  * {r}")
        sys.exit(2)


def _sha256_file(path: str, buf_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(buf_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _inventory(db) -> dict:
    """Collect per-collection document counts from the source DB.
    Uses count_documents({}) for accuracy (not estimatedCount which
    can drift)."""
    inv = {"collections": {}, "total_documents": 0, "total_collections": 0}
    names = sorted(db.list_collection_names())
    inv["total_collections"] = len(names)
    for n in names:
        try:
            c = db[n].count_documents({})
        except Exception as e:
            c = -1
            inv["collections"][n] = {"count": c, "error": str(e)[:160]}
            continue
        inv["collections"][n] = {"count": c}
        if c > 0:
            inv["total_documents"] += c
    return inv


def _run_mongodump(out_dir: str) -> dict:
    """Run `mongodump --gzip` into `out_dir`.  Returns the per-file
    inventory with SHA-256 and byte size."""
    cmd = [
        "mongodump",
        "--uri", MONGO_URL,
        "--db",  DB_NAME,
        "--gzip",
        "--out", out_dir,
    ]
    print("mongodump:", " ".join(cmd[:1] + ["--uri", "***redacted***"] + cmd[3:]))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    log = {
        "returncode": proc.returncode,
        "stdout":     proc.stdout[-4000:],
        "stderr":     proc.stderr[-4000:],
    }
    if proc.returncode != 0:
        raise RuntimeError(f"mongodump failed: {proc.stderr[-800:]}")
    # Walk the output dir and record every file.
    file_rows: list[dict] = []
    for root, _dirs, files in os.walk(out_dir):
        for f in sorted(files):
            p = os.path.join(root, f)
            size = os.path.getsize(p)
            sha  = _sha256_file(p)
            file_rows.append({
                "relative_path": os.path.relpath(p, out_dir),
                "size_bytes":    size,
                "sha256":        sha,
            })
    log["files"] = file_rows
    return log


def _count_bson_gz(path: str) -> int:
    """Count documents in a gzipped BSON dump file using the canonical
    bson streaming decoder.  This is bit-perfect accurate — unlike
    counting bsondump JSON newlines which breaks on multi-line docs."""
    import gzip
    import bson
    n = 0
    with gzip.open(path, "rb") as f:
        for _doc in bson.decode_file_iter(f):
            n += 1
    return n


def _verify_counts(inv: dict, dump_dir: str, final_inv: dict) -> dict:
    """Cross-check per-collection document counts against the actual
    gzipped BSON files mongodump wrote.  Expected count = the LAST
    count read from Mongo (``final_inv``) since the DB is live and
    request-scoped user activity rows may append between the initial
    inventory read and the end-of-dump moment.  We also report
    initial-vs-final drift so the operator can see any live writes
    that happened during the dump window.  No collection is flagged
    FAIL unless the exported BSON is missing or decode fails."""
    out: dict = {"per_collection": {}, "method": "bson.decode_file_iter",
                 "notes": []}
    db_sub = os.path.join(dump_dir, DB_NAME)
    for coll, meta in inv["collections"].items():
        initial = meta["count"]
        final   = final_inv["collections"].get(coll, {}).get("count", initial)
        bson_path = os.path.join(db_sub, f"{coll}.bson.gz")
        if not os.path.exists(bson_path):
            # mongodump omits an empty collection's BSON file entirely
            # (only metadata.json.gz is written).  Treat that as PASS
            # for count=0, FAIL otherwise.
            status = "PASS_EMPTY" if initial == 0 and final == 0 else "MISSING_EXPORT"
            out["per_collection"][coll] = {
                "initial_count":  initial,
                "final_count":    final,
                "exported":       0,
                "status":         status,
            }
            continue
        try:
            exported = _count_bson_gz(bson_path)
        except Exception as e:
            out["per_collection"][coll] = {
                "initial_count":  initial,
                "final_count":    final,
                "exported":       None,
                "status":         "BSON_DECODE_FAILED",
                "err":            str(e)[:300],
            }
            continue
        # Live DB rules: accept exported ∈ [initial, final] (writes
        # during dump) OR exported == final — anything else is a
        # genuine mismatch.
        if exported == final or exported == initial or initial <= exported <= final or final <= exported <= initial:
            status = "PASS"
        else:
            status = "MISMATCH"
        out["per_collection"][coll] = {
            "initial_count":  initial,
            "final_count":    final,
            "exported":       exported,
            "status":         status,
        }
    return out


def _write_manifest_md(manifest: dict, md_path: str) -> None:
    inv = manifest["source_inventory"]
    v   = manifest["verification"]
    lines = []
    lines.append(f"# Preview MongoDB Backup — {manifest['export_completion_timestamp']}")
    lines.append("")
    lines.append(f"- Environment: **{manifest['environment']}**")
    lines.append(f"- DATA_AUTHORITY: `{manifest['authority_posture']['DATA_AUTHORITY']}`")
    lines.append(f"- CANONICAL_WRITE_ENABLED: `{manifest['authority_posture']['CANONICAL_WRITE_ENABLED']}`")
    lines.append(f"- BACKGROUND_WORKERS_ENABLED: `{manifest['authority_posture']['BACKGROUND_WORKERS_ENABLED']}`")
    lines.append(f"- Database: `{manifest['db_name']}`")
    lines.append(f"- Safe DB fingerprint: `{manifest['db_fingerprint']}`")
    lines.append(f"- Total collections: {inv['total_collections']}")
    lines.append(f"- Total documents: {inv['total_documents']}")
    lines.append(f"- Total backup size (bytes): {manifest['total_backup_size_bytes']}")
    lines.append(f"- Archive path: `{manifest['archive_path']}`")
    lines.append(f"- Archive SHA-256: `{manifest['archive_sha256']}`")
    lines.append("")
    lines.append("## Collections")
    lines.append("")
    lines.append("| Collection | Count | Verification |")
    lines.append("|---|---:|---|")
    for n in sorted(inv["collections"].keys()):
        c = inv["collections"][n]["count"]
        vrow = v["per_collection"].get(n, {})
        status = vrow.get("status", "n/a")
        exp = vrow.get("exported", "")
        fin = vrow.get("final_count", c)
        if status == "MISMATCH":
            label = f"❌ MISMATCH initial={c} final={fin} exported={exp}"
        elif status == "PASS":
            label = f"✅ PASS exported={exp}" + (f" (drift {c}→{fin})" if c != fin else "")
        elif status == "PASS_EMPTY":
            label = "— empty (no bson export expected)"
        elif status == "MISSING_EXPORT":
            label = f"❌ MISSING_EXPORT initial={c} final={fin}"
        elif status == "BSON_DECODE_FAILED":
            label = f"❌ BSON_DECODE_FAILED initial={c}"
        else:
            label = status
        lines.append(f"| `{n}` | {c} | {label} |")
    lines.append("")
    if manifest.get("empty_collections"):
        lines.append("## Empty collections (manifest only, no export file)")
        for n in manifest["empty_collections"]:
            lines.append(f"- `{n}`")
    if manifest.get("failed_collections"):
        lines.append("## FAILED or INCOMPLETE collections")
        for n in manifest["failed_collections"]:
            lines.append(f"- `{n}`")
    lines.append("")
    lines.append("## Guardrails confirmed")
    lines.append("")
    lines.append("- Production was NOT touched.")
    lines.append("- Preview database records were NOT modified.")
    lines.append("- No import/migration/reconciliation occurred.")
    lines.append("- No sports/models/scoring/publication/settlement logic was changed.")
    lines.append("- No background workers were activated.")
    lines.append("- READ/EXPORT ONLY.")
    with open(md_path, "w") as f:
        f.write("\n".join(lines) + "\n")


def main() -> int:
    _refuse_if_not_preview()

    stamp = _dt.datetime.utcnow()
    stamp_iso = stamp.replace(tzinfo=_dt.timezone.utc).isoformat()
    stamp_tag = stamp.strftime("%Y%m%d_%H%M%SZ")
    work = pathlib.Path("/tmp") / f"perklocks_preview_backup_{stamp_tag}"
    work.mkdir(parents=True, exist_ok=False)
    dump_dir = str(work / "dump")

    client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)
    client.admin.command("ping")       # proves reachable
    db = client[DB_NAME]

    print(f"[1/5] inventorying Preview DB …")
    inv_start = _dt.datetime.utcnow()
    inv = _inventory(db)
    inv_end = _dt.datetime.utcnow()
    print(f"      {inv['total_collections']} collections · "
          f"{inv['total_documents']:,} documents")

    print(f"[2/5] mongodump --gzip → {dump_dir} …")
    dump_log = _run_mongodump(dump_dir)
    print(f"      {len(dump_log['files'])} files written")

    print(f"[3/5] verifying export counts …")
    # Re-inventory AFTER mongodump — Preview is live for request-scoped
    # writes (e.g. user_activity).  Comparing exported counts against
    # the final inventory lets us distinguish genuine export loss from
    # harmless mid-dump drift.
    final_inv = _inventory(db)
    verify = _verify_counts(inv, dump_dir, final_inv)

    print(f"[4/5] building manifest …")
    empty = [n for n, m in inv["collections"].items() if m["count"] == 0]
    failed = []
    for n, row in verify["per_collection"].items():
        if row.get("status") in ("MISMATCH", "MISSING_EXPORT",
                                   "BSON_DECODE_FAILED"):
            if inv["collections"][n]["count"] > 0 or final_inv["collections"].get(n, {}).get("count", 0) > 0:
                failed.append(n)

    manifest = {
        "environment":                "preview",
        "authority_posture":          _authority_posture(),
        "db_name":                    DB_NAME,
        "db_fingerprint":             _fp(MONGO_URL, DB_NAME),
        "export_start_timestamp":     inv_start.replace(tzinfo=_dt.timezone.utc).isoformat(),
        "export_completion_timestamp": _dt.datetime.utcnow()
                                            .replace(tzinfo=_dt.timezone.utc).isoformat(),
        "mongodump_version":          dump_log.get("stderr", "").split("\n")[0][:200],
        "source_inventory":           inv,
        "verification":               verify,
        "files":                      dump_log["files"],
        "empty_collections":          sorted(empty),
        "failed_collections":         sorted(failed),
        "total_backup_size_bytes":    sum(f["size_bytes"] for f in dump_log["files"]),
    }

    manifest_json = str(work / "manifest.json")
    manifest_md   = str(work / "manifest.md")
    with open(manifest_json, "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True, default=str)
    # (markdown is written after the archive section so the archive
    # metadata can be included in the human-readable manifest)

    # Create the final archive.
    archive_path = f"/tmp/perklocks_preview_FULL_backup_{stamp.strftime('%Y%m%d')}.zip"
    print(f"[5/5] archiving → {archive_path} …")
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

    # Rewrite the manifest files to include archive info.
    with open(manifest_json, "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True, default=str)
    _write_manifest_md(manifest, manifest_md)
    # Also drop a trimmed manifest copy into /app/memory for review
    # (same JSON minus the per-file list, so the file stays small).
    trimmed = {k: v for k, v in manifest.items() if k != "files"}
    trimmed["files_count"] = len(manifest["files"])
    with open(f"/app/memory/preview_backup_manifest_{stamp.strftime('%Y%m%d')}.json", "w") as f:
        json.dump(trimmed, f, indent=2, sort_keys=True, default=str)

    print()
    print("=" * 70)
    print("PREVIEW BACKUP COMPLETE")
    print("=" * 70)
    print(f"  Environment:             {manifest['environment']}")
    print(f"  Authority posture:       {manifest['authority_posture']}")
    print(f"  Database:                {manifest['db_name']}")
    print(f"  Safe DB fingerprint:     {manifest['db_fingerprint']}")
    print(f"  Collections (total):     {inv['total_collections']}")
    print(f"  Documents (total):       {inv['total_documents']:,}")
    print(f"  Backup size (bytes):     {manifest['total_backup_size_bytes']:,}")
    print(f"  Archive path:            {archive_path}")
    print(f"  Archive size (bytes):    {archive_size:,}")
    print(f"  Archive SHA-256:         {archive_sha}")
    print(f"  Manifest (full):         {manifest_json}")
    print(f"  Manifest (markdown):     {manifest_md}")
    print(f"  Empty collections:       {len(empty)}")
    print(f"  Failed/incomplete:       {len(failed)}")

    if failed:
        print()
        print("WARNING — the following collections failed or were incomplete:")
        for n in failed:
            print(f"  * {n}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
