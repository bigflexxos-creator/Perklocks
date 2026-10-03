"""download_and_verify_production — materialize 147 Production files from
attachment store into /app/reconcile_workspace/production_input/ and verify
byte-for-byte SHA-256 against production_input_inventory.json.

Rules:
  - If a file already exists in the destination and its SHA matches, skip.
  - Any mismatch or missing is FAIL — reports exact list.
"""
from __future__ import annotations

import concurrent.futures as _cf
import hashlib
import json
import os
import pathlib
import time
import urllib.request

INVENTORY = pathlib.Path("/app/memory/reconcile_offline/production_input_inventory.json")
URL_MAP   = pathlib.Path("/app/reconcile_workspace/scripts/production_url_map.json")
DEST      = pathlib.Path("/app/reconcile_workspace/production_input")
LOG_DIR   = pathlib.Path("/app/reconcile_workspace/logs")
DEST.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)


def _sha256(path: pathlib.Path, buf: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            c = f.read(buf)
            if not c:
                break
            h.update(c)
    return h.hexdigest()


def _download(url: str, dest: pathlib.Path, retries: int = 3) -> int:
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=120) as resp, open(dest, "wb") as out:
                size = 0
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
                    size += len(chunk)
            return size
        except Exception as e:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
    return -1


def _process_one(filename: str, expected_sha: str, url: str) -> dict:
    dest = DEST / filename
    # If exists and matches, skip
    if dest.exists():
        actual = _sha256(dest)
        if actual == expected_sha:
            return {"filename": filename, "status": "SKIP_ALREADY_PRESENT",
                    "sha256": actual, "size": dest.stat().st_size}
        else:
            dest.unlink()  # bad copy, redownload

    try:
        size = _download(url, dest)
    except Exception as e:
        return {"filename": filename, "status": "DOWNLOAD_FAILED",
                "error": str(e)[:300], "url": url}

    actual = _sha256(dest)
    if actual == expected_sha:
        return {"filename": filename, "status": "PASS",
                "sha256": actual, "size": size}
    else:
        return {"filename": filename, "status": "SHA_MISMATCH",
                "expected": expected_sha, "actual": actual,
                "size": size, "url": url}


def main() -> int:
    with open(INVENTORY) as f:
        inv = json.load(f)
    with open(URL_MAP) as f:
        urlmap = json.load(f)
    base = urlmap["_base"]
    files_map = urlmap["files"]

    expected_files = inv.get("files", {})
    print(f"Inventory lists {len(expected_files)} files. URL map has {len(files_map)} files.")

    missing_urls = [f for f in expected_files if f not in files_map]
    extra_urls = [f for f in files_map if f not in expected_files]
    if missing_urls:
        print(f"ERROR: {len(missing_urls)} files in inventory have no URL mapping:")
        for f in missing_urls:
            print(f"  - {f}")
        return 2
    if extra_urls:
        print(f"NOTE: {len(extra_urls)} files in URL map not in inventory (ignored).")

    tasks = []
    for filename, meta in expected_files.items():
        expected_sha = meta["sha256"]
        asset_key = files_map[filename]
        url = base + asset_key
        tasks.append((filename, expected_sha, url))

    print(f"Downloading {len(tasks)} files to {DEST} …", flush=True)
    results: list[dict] = []
    t0 = time.time()
    # Parallel download — moderate concurrency since files are large
    with _cf.ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(_process_one, fn, sh, u): fn for fn, sh, u in tasks}
        done = 0
        for fut in _cf.as_completed(futs):
            r = fut.result()
            results.append(r)
            done += 1
            if done % 10 == 0 or r["status"] not in ("PASS", "SKIP_ALREADY_PRESENT"):
                print(f"  [{done:>3}/{len(tasks)}] {r['status']:<22} {r['filename']}", flush=True)

    t1 = time.time()
    status_counts: dict[str, int] = {}
    failures: list[dict] = []
    for r in results:
        status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1
        if r["status"] not in ("PASS", "SKIP_ALREADY_PRESENT"):
            failures.append(r)

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "duration_sec": round(t1 - t0, 2),
        "destination":  str(DEST),
        "total_files":  len(tasks),
        "status_counts": status_counts,
        "failures": failures,
        "all_pass": len(failures) == 0,
        "results": sorted(results, key=lambda r: r["filename"]),
    }
    out_report = LOG_DIR / "production_download_verify_report.json"
    with open(out_report, "w") as f:
        json.dump(report, f, indent=2, default=str)

    print("\n" + "=" * 70)
    print("PRODUCTION DOWNLOAD + VERIFY SUMMARY")
    print("=" * 70)
    print(f"  Duration:        {t1 - t0:.1f}s")
    print(f"  Total:           {len(tasks)}")
    for k, v in sorted(status_counts.items()):
        print(f"  {k:<28} {v}")
    print(f"  All-pass:        {report['all_pass']}")
    print(f"  Report:          {out_report}")

    if not report["all_pass"]:
        print("\nFAILURES:")
        for f in failures:
            print(f"  * {f['filename']}: {f['status']} {f.get('error', '')}")
        return 1
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
