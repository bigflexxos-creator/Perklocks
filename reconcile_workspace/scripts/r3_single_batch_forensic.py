"""r3_single_batch_forensic — ZERO-WRITE one-batch deep diagnostic.

Context (Canary #3)
───────────────────
Canary #3 proved the batch-size fix: for ``settlement_events`` batch
5 the reconstructed count is now 250 (matches authoritative stored
``doc_count=250``) but the hash STILL mismatches.

This script performs the deep per-batch forensic that isolates the
remaining defect among:

  A. source NDJSON order differs from original import order
  B. filtering/exclusions shift boundaries
  C. original importer sorted before batching
  D. serializer/hash algorithm input differs
  E. checkpoint SHA drift — the ORIGINAL successful import used a
     DIFFERENT phase5 file than the one currently pinned

What it does
────────────
For a single (collection, batch_no) pair:
  1. Fetches authoritative manifest.
  2. Pulls stored ``content_hash``, ``doc_count``, ``source_checkpoints``.
  3. Locally reconstructs the batch using per-batch authoritative
     doc_counts (the Resume #12 contract).
  4. Reports:
       * expected_hash           (from Prod bookkeeping)
       * computed_hash           (locally computed, byte-identical algo)
       * expected_count          (from Prod)
       * reconstructed_count     (locally)
       * local_source_checkpoints (SHAs of the currently-pinned files
         ``push_canonical_accelerated.EXPECTED_SHA``)
       * server_source_checkpoints (SHAs the ORIGINAL successful
         import pinned into the batch record)
       * first 10 inner-doc ``_id`` + logical-key tuples
       * last 10 inner-doc ``_id`` + logical-key tuples
       * exclusion count (should be 0 for settlement_events)
       * sort/hash algorithm used
  5. Classifies the mismatch into A/B/C/D/E so the next surgical fix
     can target the correct root cause.

Zero-write guarantee
────────────────────
The script never POSTs to ``/canonical-import`` and never calls any
mutation endpoint.  All reads use admin-JWT-only GET.

Exit codes
──────────
    0  — hash matches (unexpected during Canary #3-era; indicates the
         specific batch tested is actually clean)
   30  — hash mismatches; forensic report printed + saved to JSON.
         The report classifies the mismatch into A/B/C/D/E.
   10+ — pre-flight / login / HTTP failure.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import pathlib
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request


# ─── Driver module (hash function, LOGICAL_KEYS, _ndjson) ────────────
_driver_path = pathlib.Path("/app/reconcile_workspace/scripts/push_canonical_accelerated.py")
_spec = importlib.util.spec_from_file_location("push_canonical_accelerated", _driver_path)
_mod  = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]


CHKP_DIR = pathlib.Path(os.environ.get("CHKP_DIR") or "/tmp/perklocks-checkpoints")


def _require(k: str) -> str:
    v = os.environ.get(k)
    if not v:
        print(f"ERROR: env var {k} not set", file=sys.stderr); sys.exit(2)
    return v


def _redact(url: str) -> str:
    from urllib.parse import urlparse
    p = urlparse(url); h = (p.hostname or "?").split(".")
    if len(h) >= 3: h[0] = "*"
    return ".".join(h) + (f":{p.port}" if p.port else "")


def _post(url, headers, body, timeout=120):
    data = json.dumps(body, default=str).encode()
    req = urllib.request.Request(url, data=data,
            headers={**headers, "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        txt = e.read().decode(errors="replace")[:800]
        try: return e.code, json.loads(txt)
        except Exception: return e.code, txt


def _get(url, headers, timeout=120):
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        txt = e.read().decode(errors="replace")[:800]
        try: return e.code, json.loads(txt)
        except Exception: return e.code, txt


def _sha256_of_file(p: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for buf in iter(lambda: f.read(1 << 20), b""):
            h.update(buf)
    return h.hexdigest()


def _extract(tarball: pathlib.Path, dest: pathlib.Path) -> pathlib.Path:
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tarball, "r:gz") as t:
        t.extractall(dest)
    for child in dest.iterdir():
        if child.is_dir():
            return child
    return dest


def _source_for(coll: str, canon5: pathlib.Path, canon6: pathlib.Path):
    p5f = canon5 / f"{coll}.ndjson"
    p6f = canon6 / f"{coll}.ndjson"
    if p6f.exists() and p6f.stat().st_size > 0:
        return p6f, "phase6_overlay"
    if p5f.exists():
        return p5f, "phase5"
    return None, None


def main() -> int:
    api_base = _require("PROD_API_BASE").rstrip("/")
    email    = _require("PROD_ADMIN_EMAIL")
    password = _require("PROD_ADMIN_PASSWORD")
    session  = _require("CANONICAL_IMPORT_SESSION")

    coll = os.environ.get("FORENSIC_COLLECTION", "settlement_events")
    bno  = int(os.environ.get("FORENSIC_BATCH_NO", "5"))
    report_path = os.environ.get("FORENSIC_REPORT_PATH") or \
                  "/tmp/perklocks-logs/r3_single_batch_forensic.json"
    pathlib.Path(report_path).parent.mkdir(parents=True, exist_ok=True)

    # ── Auth ─────────────────────────────────────────────────────────
    print(f"[auth] POST {_redact(api_base)}/api/auth/login")
    code, body = _post(f"{api_base}/api/auth/login", {},
                        {"email": email, "password": password})
    if code != 200 or not isinstance(body, dict) or not body.get("access_token"):
        print(f"ERROR login status={code}", file=sys.stderr); return 10
    jwt = body["access_token"]
    hdr_a = {"Authorization": f"Bearer {jwt}"}
    print(f"[auth] OK role={body.get('user',{}).get('role') or body.get('role')}")

    # ── Fetch authoritative manifest ─────────────────────────────────
    print(f"[manifest] GET /batch-manifest  coll={coll}")
    code, bm = _get(
        f"{api_base}/api/admin/canonical-import/batch-manifest"
        f"?session_id={session}&collection={coll}", hdr_a, timeout=60)
    if code != 200 or not isinstance(bm, dict):
        print(f"ERROR manifest status={code}", file=sys.stderr); return 11
    batches_by_bno = {int(b["batch_no"]): b for b in bm.get("batches", [])}
    target = batches_by_bno.get(bno)
    if target is None:
        print(f"ERROR: no manifest entry for {coll} batch_no={bno}", file=sys.stderr)
        return 12
    expected_hash  = target.get("content_hash")
    expected_count = int(target.get("doc_count") or 0)
    server_cps     = target.get("source_checkpoints") or {}
    print(f"[manifest] expected_hash={expected_hash}")
    print(f"[manifest] expected_count={expected_count}")
    print(f"[manifest] server-stored source_checkpoints={json.dumps(server_cps, sort_keys=True)}")

    # ── Load currently-pinned local checkpoints + their SHAs ─────────
    local_cps = {
        "phase5_sha":           _mod.EXPECTED_SHA["phase5_20261003_190628Z.tar.gz"],
        "phase6_sha":           _mod.EXPECTED_SHA["phase6_20261003_192150Z.tar.gz"],
        "preview_baseline_sha": "26cd67d0648ef62f495807c5303fdc1acafb28a3015e249718a7b47a3728d78b",
    }
    print(f"[local]    pinned-driver source_checkpoints={json.dumps(local_cps, sort_keys=True)}")

    # Verify local tarballs' actual SHAs match the pinned expectations
    actual_sha_phase5 = _sha256_of_file(
        CHKP_DIR / "phase5_20261003_190628Z.tar.gz")
    actual_sha_phase6 = _sha256_of_file(
        CHKP_DIR / "phase6_20261003_192150Z.tar.gz")
    print(f"[local]    actual-on-disk phase5_sha={actual_sha_phase5}")
    print(f"[local]    actual-on-disk phase6_sha={actual_sha_phase6}")
    assert actual_sha_phase5 == local_cps["phase5_sha"], \
        "phase5 tarball on disk does not match pinned expected SHA"
    assert actual_sha_phase6 == local_cps["phase6_sha"], \
        "phase6 tarball on disk does not match pinned expected SHA"

    # ── Extract local checkpoints ────────────────────────────────────
    workdir = pathlib.Path(tempfile.mkdtemp(prefix="forensic_"))
    try:
        p5_root = _extract(CHKP_DIR / "phase5_20261003_190628Z.tar.gz", workdir / "p5")
        p6_root = _extract(CHKP_DIR / "phase6_20261003_192150Z.tar.gz", workdir / "p6")
        canon5 = p5_root / "canonical"
        canon6 = p6_root / "canonical"

        src, src_tag = _source_for(coll, canon5, canon6)
        if src is None:
            print(f"ERROR: no NDJSON source for {coll}", file=sys.stderr); return 13
        src_sha = _sha256_of_file(src)
        src_lines = sum(1 for _ in open(src))
        print(f"[source]  file={src.name}  source_tag={src_tag}  "
              f"sha256={src_sha}  total_lines={src_lines}")

        # ── Walk NDJSON using per-batch authoritative doc_counts ─────
        historical_counts = {b: v["doc_count"] for b, v in batches_by_bno.items()
                              if v.get("doc_count", 0) > 0}
        succeeded_sizes = [v["doc_count"] for v in batches_by_bno.values()
                            if v.get("status") == "succeeded"
                            and v.get("doc_count", 0) > 0]
        if succeeded_sizes:
            tail_bsize = max(succeeded_sizes)
        elif historical_counts:
            tail_bsize = max(historical_counts.values())
        else:
            tail_bsize = int(os.environ.get("NEW_COLLECTION_BATCH_SIZE", "1000"))
        max_hb = max(historical_counts.keys(), default=-1)

        def _tgt(b):
            return historical_counts.get(b, tail_bsize) if b <= max_hb else tail_bsize

        print(f"[recon]   max_hist_bno={max_hb} tail_bsize={tail_bsize} "
              f"target[bno={bno}]={_tgt(bno)}")

        # Enumerate NDJSON tracking offsets.
        reconstructed: list[dict] = []
        cur_b = 0
        buf: list[dict] = []
        cur_t = _tgt(cur_b)
        exclusion_count = 0
        file_line_no_for_batch_start: int | None = None
        file_line_no_for_batch_end:   int | None = None
        first_line_no_in_this_batch:  int | None = None

        with open(src) as fh:
            for ln_no, raw_ln in enumerate(fh):
                try:
                    r = json.loads(raw_ln)
                except Exception:
                    continue
                doc = r["doc"] if isinstance(r, dict) and "doc" in r else r
                if _mod._is_excluded(doc):
                    exclusion_count += 1
                    continue
                if first_line_no_in_this_batch is None:
                    first_line_no_in_this_batch = ln_no
                buf.append(doc)
                if len(buf) >= cur_t:
                    if cur_b == bno:
                        file_line_no_for_batch_start = first_line_no_in_this_batch
                        file_line_no_for_batch_end   = ln_no
                        reconstructed = list(buf)
                        break
                    cur_b += 1
                    buf = []
                    first_line_no_in_this_batch = None
                    cur_t = _tgt(cur_b)
            else:
                # EOF before reaching bno — but only valid for tail batch
                if buf and cur_b == bno:
                    file_line_no_for_batch_start = first_line_no_in_this_batch
                    file_line_no_for_batch_end   = ln_no  # noqa: F823
                    reconstructed = list(buf)

        rec_count = len(reconstructed)
        print(f"[recon]   reconstructed_count={rec_count}  "
              f"ndjson_line_range=[{file_line_no_for_batch_start}, "
              f"{file_line_no_for_batch_end}]  "
              f"exclusion_count_so_far={exclusion_count}")

        if rec_count == 0:
            print("ERROR: reconstruction produced zero docs — cannot continue",
                  file=sys.stderr); return 14

        # ── Compute local hash with the SAME algorithm as the server ─
        local_hash = _mod._server_batch_content_hash(coll, reconstructed)
        print(f"[hash]    local_hash   ={local_hash}")
        print(f"[hash]    expected_hash={expected_hash}")

        # ── Logical-key samples (first 10 + last 10) ─────────────────
        lk_fields = tuple(_mod._logical_key_fields(coll))
        print(f"[keys]    logical_key_fields={lk_fields}")

        def _summarize(doc, idx):
            lk = _mod._extract_logical_key(coll, doc)
            return {
                "idx":        idx,
                "logical_key": list(lk),
                "inner__id":  doc.get("_id"),
                "event_id":   doc.get("event_id"),
                "snapshot_version": doc.get("snapshot_version"),
            }
        first10 = [_summarize(d, i) for i, d in enumerate(reconstructed[:10])]
        last10  = [_summarize(d, rec_count - 10 + i)
                    for i, d in enumerate(reconstructed[-10:])]
        for row in first10:
            print(f"  first  idx={row['idx']:>4}  lk={row['logical_key']}  "
                  f"_id={row['inner__id']}  event_id={row['event_id']}")
        for row in last10:
            print(f"  last   idx={row['idx']:>4}  lk={row['logical_key']}  "
                  f"_id={row['inner__id']}  event_id={row['event_id']}")

        # ── Classify the mismatch (A/B/C/D/E) ────────────────────────
        all_logical_keys_null = all(
            all(k is None for k in _mod._extract_logical_key(coll, d))
            for d in reconstructed[:50]
        )
        checkpoint_drift = (
            bool(server_cps)
            and (
                server_cps.get("phase5_sha") not in (None, "", local_cps["phase5_sha"])
                or server_cps.get("phase6_sha") not in (None, "", local_cps["phase6_sha"])
            )
        )
        count_matches  = (rec_count == expected_count)
        hash_matches   = (local_hash == expected_hash)
        classification = []
        if hash_matches:
            classification.append("MATCH — no divergence")
        else:
            if checkpoint_drift:
                classification.append(
                    "E. checkpoint SHA drift — ORIGINAL import used DIFFERENT "
                    "phase5/phase6 file than currently pinned"
                )
            if not count_matches:
                classification.append(
                    "B. filtering/exclusions shift boundaries — "
                    "rec_count != expected_count"
                )
            if all_logical_keys_null:
                classification.append(
                    "C/D-adjacent. all logical keys are None → sort is "
                    "trivial, server-side sort preserves input order → "
                    "input NDJSON order determines membership + hash; "
                    "any upstream resolver step that renumbered docs "
                    "would break parity"
                )
            if not classification:
                classification.append(
                    "A. source NDJSON order differs OR C. original importer "
                    "sorted before batching OR D. serializer differs — "
                    "requires per-doc byte-level comparison with server"
                )

        print(f"[classify] {'; '.join(classification)}")

        report = {
            "schema":             "r3_single_batch_forensic_v1",
            "generated_at_unix":  int(time.time()),
            "api_target_redacted": _redact(api_base),
            "session_id":         session,
            "collection":         coll,
            "batch_no":           bno,
            "expected_hash":      expected_hash,
            "computed_hash":      local_hash,
            "expected_count":     expected_count,
            "reconstructed_count": rec_count,
            "match":              hash_matches,
            "server_source_checkpoints": server_cps,
            "local_source_checkpoints":  local_cps,
            "actual_on_disk_shas": {
                "phase5": actual_sha_phase5,
                "phase6": actual_sha_phase6,
            },
            "checkpoint_drift":          checkpoint_drift,
            "ndjson_source":             {
                "file":              src.name,
                "source_tag":        src_tag,
                "sha256":            src_sha,
                "total_lines":       src_lines,
                "line_range_for_batch": [file_line_no_for_batch_start,
                                           file_line_no_for_batch_end],
            },
            "exclusion_count_up_to_batch": exclusion_count,
            "logical_key_fields":          list(lk_fields),
            "all_logical_keys_null_sample": all_logical_keys_null,
            "first10":                     first10,
            "last10":                      last10,
            "classification":              classification,
        }
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2, default=str)
        print(f"\n[report] written to {report_path}")

        return 0 if hash_matches else 30
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
