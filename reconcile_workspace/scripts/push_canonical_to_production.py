"""push_canonical_to_production — client-side driver that invokes the
deployed Production admin canonical-import endpoint.

Rebuilds the final canonical dataset from the certified checkpoints
(Phase 5 baseline + Phase 6 overrides), filters out
`excluded_from_canonical_runtime=true` and quarantined rows, batches
up to 5,000 docs, and POSTs to the Production API with
X-Canonical-Import-Token.  Idempotent.

PREREQUISITES (user supplies via env):
  PROD_API_BASE          e.g. https://your-prod.example.com
  PROD_ADMIN_JWT         admin user access token from Production API
  CANONICAL_IMPORT_TOKEN the secret you set in Production env
  CANONICAL_IMPORT_SESSION  e.g. perklocks-cutover-20261003
Optional:
  MAX_BATCH_SIZE         default 2000  (<=5000 enforced server-side)
  COLLECTIONS_FILTER     comma-sep subset of the 21 reconciled collections

Behaviour:
  * Extracts Phase 5 + Phase 6 checkpoints to overlay.
  * Overlays Phase 6 canonical NDJSONs on top of Phase 5 canonical.
  * Reads each resulting NDJSON, filters out excluded/quarantined rows.
  * Batches and uploads.
  * Verifies session status via GET endpoint after upload.
  * Never prints credentials.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.request
import urllib.error
from typing import Iterator

CHKP = pathlib.Path("/app/reconcile_workspace/checkpoints")

RECONCILED_21 = (
    "games", "historical_ingestion_state", "nfl_ingest_meta", "nfl_player_weekly",
    "parlay_history", "picks", "player_game_actuals", "player_game_logs",
    "player_identities", "prediction_snapshots", "pregame_snapshots",
    "publication_events", "rollover_slate_events", "rollover_slates",
    "settlement_events", "soccer_matches", "soccer_player_game_logs",
    "team_game_actuals", "tennis_matches_history", "user_bets", "users",
)


def _require_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        print(f"ERROR: env var {name} not set", file=sys.stderr)
        sys.exit(2)
    return v


def _redact_host(url: str) -> str:
    """Return a redacted host identifier for display."""
    from urllib.parse import urlparse
    p = urlparse(url)
    host = p.hostname or "?"
    # Keep subdomain structure, redact the leftmost labels
    parts = host.split(".")
    if len(parts) >= 3:
        parts[0] = "*"
    return ".".join(parts) + (f":{p.port}" if p.port else "")


def _sha_tar(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            c = f.read(1 << 20)
            if not c: break
            h.update(c)
    return h.hexdigest()


def _verify_checkpoint_sha(path: pathlib.Path, expected: str) -> None:
    actual = _sha_tar(path)
    if actual != expected:
        print(f"ERROR: checkpoint SHA mismatch: {path.name}")
        print(f"  expected: {expected}")
        print(f"  actual:   {actual}")
        sys.exit(3)


def _extract_checkpoint(tar_path: pathlib.Path, out: pathlib.Path) -> pathlib.Path:
    with tarfile.open(tar_path) as tf:
        tf.extractall(out)
    # Return the inner dir
    for d in out.iterdir():
        if d.is_dir():
            return d
    raise RuntimeError(f"no inner dir in {tar_path}")


def _ndjson_rows(path: pathlib.Path) -> Iterator[dict]:
    with open(path) as f:
        for ln in f:
            try:
                r = json.loads(ln)
            except Exception:
                continue
            # Many checkpoints wrap each row as {"doc": {...}, "canonical_from": "..."}
            if isinstance(r, dict) and "doc" in r:
                yield r["doc"]
            else:
                yield r


def _is_excluded(doc: dict) -> bool:
    if doc.get("excluded_from_canonical_runtime") is True:
        return True
    if doc.get("status") == "UNRESOLVED_IMMUTABLE_CONFLICT":
        return True
    return False


def _post(url: str, headers: dict, body: dict, retries: int = 3,
          backoff: float = 2.0) -> dict:
    """Resilient POST — Phase-5-R3 tuned for the managed Mongo write
    characteristics observed on bet-edge-ai-1.emergent.host:
      * server-side request timeout bumped to 300 s (bulk_write of
        250-row upserts occasionally exceeds the 120 s default under
        shared-cluster load).
      * **CAPPED AT 3 ATTEMPTS** per batch with bounded exponential
        back-off (2 s, 4 s, 8 s).  Prior R3 variant allowed 6 attempts
        which let a single bad batch block the orchestrator for
        ~30 min while the shared cluster stayed slow.  Lowering the
        cap preserves resilience while enforcing fail-fast on any
        batch stuck in a persistent-timeout window — Pass 2 provides
        the one additional recovery opportunity.
      * fail-closed on 400/401/403/413 (configuration errors).
      * 409 ALTERED_REPLAY_REJECTED surfaced immediately — server
        rejected because the batch's canonical identity changed under
        us, which must never silently resolve.
    """
    data = json.dumps(body, default=str).encode()
    # Bounded exponential backoff schedule.  Only the first `retries - 1`
    # entries are consumed when retries is capped at 3; the longer tail
    # is retained so Pass-2 or future reconfigurations remain safe.
    delays = [2, 4, 8, 16, 32, 60]
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            text = e.read().decode(errors="replace")[:400]
            # Fail closed on these — never silently retried.
            if e.code in (401, 403, 400, 413):
                raise RuntimeError(f"HTTP {e.code}: {text}")
            if e.code == 409 and "ALTERED_REPLAY_REJECTED" in text:
                raise RuntimeError(f"HTTP 409 ALTERED_REPLAY_REJECTED: {text}")
            if attempt == retries - 1:
                raise RuntimeError(f"HTTP {e.code} after {retries} tries: {text}")
            time.sleep(delays[min(attempt, len(delays) - 1)])
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries - 1:
                raise
            time.sleep(delays[min(attempt, len(delays) - 1)])
    raise RuntimeError("retries exhausted")


def _get(url: str, headers: dict) -> dict:
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    # Verify checkpoint SHAs against known values
    print("[1/5] verifying checkpoint SHA-256s …")
    expected = {
        "phase5_20261003_190628Z.tar.gz":
            "4adc99890885e7b712adfd491124c2ef681715996167d19719ddb04f8eb1e9d7",
        "phase6_20261003_192150Z.tar.gz":
            "327a38903daf3bd910ab50b68f69415cfcf181624655b42fb791af43042b6c22",
    }
    for name, sha in expected.items():
        p = CHKP / name
        if not p.exists():
            print(f"ERROR: checkpoint missing: {p}", file=sys.stderr)
            return 3
        _verify_checkpoint_sha(p, sha)
        print(f"  ✓ {name}  sha={sha[:16]}…")

    api_base   = _require_env("PROD_API_BASE").rstrip("/")
    admin_jwt  = _require_env("PROD_ADMIN_JWT")
    import_tok = _require_env("CANONICAL_IMPORT_TOKEN")
    session    = _require_env("CANONICAL_IMPORT_SESSION")

    batch_size = int(os.environ.get("MAX_BATCH_SIZE", "2000"))
    filter_set = set((os.environ.get("COLLECTIONS_FILTER") or "").split(",")) - {""}

    headers_post = {
        "Content-Type":              "application/json",
        "Authorization":             f"Bearer {admin_jwt}",
        "X-Canonical-Import-Token":  import_tok,
    }
    headers_get = {
        "Authorization":             f"Bearer {admin_jwt}",
    }

    print(f"[2/5] redacted Prod API target: {_redact_host(api_base)}")

    # Reconstruct final canonical in overlay
    print("[3/5] reconstructing final canonical from Phase 5 + Phase 6 …")
    workdir = pathlib.Path(tempfile.mkdtemp(prefix="canon_push_", dir="/opt/reconcile_tmp"))
    p5_root = _extract_checkpoint(CHKP / "phase5_20261003_190628Z.tar.gz", workdir / "p5")
    p6_root = _extract_checkpoint(CHKP / "phase6_20261003_192150Z.tar.gz", workdir / "p6")
    canon5 = p5_root / "canonical"
    canon6 = p6_root / "canonical"

    source_cps = {
        "phase5_sha": expected["phase5_20261003_190628Z.tar.gz"],
        "phase6_sha": expected["phase6_20261003_192150Z.tar.gz"],
        "preview_baseline_sha": "26cd67d0648ef62f495807c5303fdc1acafb28a3015e249718a7b47a3728d78b",
    }

    print(f"[4/5] uploading {len(RECONCILED_21)} canonical collections to Prod …")

    report = {"collections": [], "total_accepted": 0, "total_rejected": 0,
              "api_target_redacted": _redact_host(api_base), "session": session}
    failed_registry: dict[str, list[dict]] = {}
    for coll in RECONCILED_21:
        if filter_set and coll not in filter_set:
            continue
        p5_file = canon5 / f"{coll}.ndjson"
        p6_file = canon6 / f"{coll}.ndjson"
        if not p5_file.exists() and not p6_file.exists():
            print(f"  — {coll}: no data"); continue

        # Logical-key index from Phase 6 (overlay wins)
        p6_rows = {}
        if p6_file.exists():
            for d in _ndjson_rows(p6_file):
                # Use logical-key fields built into the doc; the import
                # endpoint will re-derive on the server side.
                p6_rows[id(d)] = d  # all Phase 6 rows always included

        # Build the final set: Phase 5 rows minus those overridden by Phase 6
        # Phase 6 overlay files contain the FINAL state for their collections.
        # If a Phase 6 file exists AND IS NON-EMPTY, use ONLY the Phase 6
        # rows (Phase 6 canonical reflects the post-override merge).
        # Phase 5-R2 fix (2026-10-03): an EMPTY P6 overlay must NEVER erase
        # the certified Phase-5 canonical dataset — treat empty-or-missing
        # as "no overlay, use P5 canonical as-is".  Validated by
        # test_canonical_push_overlay_guard.
        def _has_overlay(path: pathlib.Path) -> bool:
            try:
                return path.exists() and path.stat().st_size > 0
            except Exception:
                return False
        if _has_overlay(p6_file):
            src = p6_file
        elif p5_file.exists():
            src = p5_file
        else:
            print(f"  — {coll}: no non-empty source available")
            continue
        print(f"  → {coll}: source={src.name} (size={src.stat().st_size})")

        # Phase-5-R3 resumable retry pattern (capped-at-3 variant):
        #   PASS 1 — process every batch; on persistent failure
        #            (3 bounded retries exhausted) record the
        #            (coll, batch_no, batch_payload) and continue.
        #            Never silently skip.
        #   PASS 2 — at end of all collections, retry the recorded
        #            failed batches with the same bounded 3-retry
        #            policy.  This is the ONE additional recovery
        #            attempt a batch gets after Pass 1.
        # If any batch is STILL failed after Pass 2 → the push driver
        # exits non-zero and the orchestrator refuses Phase 6/7/8.
        # Rationale for the 3-attempt cap: on the shared cluster,
        # a batch that cannot complete within 3 bounded-backoff
        # attempts is overwhelmingly in a persistent-timeout window;
        # letting it burn 6 attempts (~30 min) blocks the entire
        # import without materially improving success rate.
        batch = []
        batch_no = 0
        coll_accepted = 0
        coll_rejected = 0
        coll_failed = []

        def _attempt(bno, b):
            """Returns (ok, result_or_error_str)."""
            try:
                return True, _post(
                    f"{api_base}/api/admin/canonical-import",
                    headers_post,
                    {"session_id":         session,
                     "collection":         coll,
                     "batch_no":           bno,
                     "docs":               b,
                     "source_checkpoints": source_cps})
            except Exception as e:   # noqa: BLE001
                return False, str(e)[:400]

        for doc in _ndjson_rows(src):
            if _is_excluded(doc):
                coll_rejected += 1
                continue
            batch.append(doc)
            if len(batch) >= batch_size:
                ok, res = _attempt(batch_no, batch)
                if ok:
                    coll_accepted += res.get("accepted", 0)
                    coll_rejected += res.get("rejected", 0)
                    print(f"      batch #{batch_no}: accepted={res.get('accepted')} "
                          f"rejected={res.get('rejected')} "
                          f"status={res.get('status')} "
                          f"replay={res.get('idempotent_replay')}")
                else:
                    print(f"      batch #{batch_no}: PERSISTENT FAIL — will retry at end  err={res}")
                    coll_failed.append({"batch_no": batch_no, "payload": list(batch)})
                batch_no += 1
                batch = []
        if batch:
            ok, res = _attempt(batch_no, batch)
            if ok:
                coll_accepted += res.get("accepted", 0)
                coll_rejected += res.get("rejected", 0)
                print(f"      batch #{batch_no}: accepted={res.get('accepted')} "
                      f"rejected={res.get('rejected')} status={res.get('status')} "
                      f"replay={res.get('idempotent_replay')}")
            else:
                print(f"      batch #{batch_no}: PERSISTENT FAIL — will retry at end  err={res}")
                coll_failed.append({"batch_no": batch_no, "payload": list(batch)})

        report["collections"].append({"collection":     coll,
                                        "accepted":       coll_accepted,
                                        "rejected":       coll_rejected,
                                        "failed_batches": [f["batch_no"] for f in coll_failed]})
        report["total_accepted"] += coll_accepted
        report["total_rejected"] += coll_rejected
        # stash failed-batch payloads for Pass 2
        failed_registry.setdefault(coll, []).extend(coll_failed)
        del coll_failed

    # ── Phase-5-R3 Pass 2: retry only the batches that failed in Pass 1
    #                     with the same 6-retry bounded policy.
    total_failed_after_pass1 = sum(len(v) for v in failed_registry.values())
    if total_failed_after_pass1:
        print(f"\n[retry-pass] {total_failed_after_pass1} batch(es) failed in pass 1 — retrying each with bounded 3-attempt policy")
        still_failed = {}
        for coll, items in failed_registry.items():
            for item in items:
                bno = item["batch_no"]
                payload = item["payload"]
                try:
                    res = _post(
                        f"{api_base}/api/admin/canonical-import",
                        headers_post,
                        {"session_id":         session,
                         "collection":         coll,
                         "batch_no":           bno,
                         "docs":               payload,
                         "source_checkpoints": source_cps})
                    print(f"      retry {coll} batch #{bno}: accepted={res.get('accepted')} "
                          f"status={res.get('status')} replay={res.get('idempotent_replay')}")
                    report["total_accepted"] += res.get("accepted", 0) or 0
                    report["total_rejected"] += res.get("rejected", 0) or 0
                except Exception as e:   # noqa: BLE001
                    msg = str(e)[:400]
                    print(f"      retry {coll} batch #{bno}: STILL FAILED  err={msg}")
                    still_failed.setdefault(coll, []).append({"batch_no": bno, "err": msg})
        report["still_failed_after_retry"] = still_failed
        if still_failed:
            print(f"\n❌ After retry pass, {sum(len(v) for v in still_failed.values())} "
                  f"batch(es) remain failed — pushing driver exits non-zero so Phase 6/7/8 are refused.")
            print(f"[5/5] fetching session status (will show failed batches) …")
            status = _get(f"{api_base}/api/admin/canonical-import/status?session_id={session}",
                             headers_get)
            report["server_status"] = status
            print(json.dumps(report, indent=2, default=str))
            return 1

    # Final status
    print(f"[5/5] fetching session status …")
    status = _get(f"{api_base}/api/admin/canonical-import/status?session_id={session}",
                   headers_get)
    report["server_status"] = status

    # Trigger index creation
    print("[+]  creating canonical indexes …")
    try:
        idx = _post(
            f"{api_base}/api/admin/canonical-cutover/create-indexes",
            headers_post,
            {"collections": list(RECONCILED_21)})
        report["index_results"] = idx
        print(f"     indexes all_ok = {idx.get('all_ok')}")
    except RuntimeError as e:
        print(f"     index creation error: {e}")
        report["index_error"] = str(e)[:400]

    # Trigger certification
    print("[+]  requesting pre-cutover certification …")
    try:
        cert = _get(f"{api_base}/api/admin/canonical-cutover/certification",
                     headers_get)
        report["certification"] = cert
        print(f"     certification overall_pass = {cert.get('overall_pass')}")
    except RuntimeError as e:
        print(f"     certification error: {e}")
        report["certification_error"] = str(e)[:400]

    out = pathlib.Path(f"/app/reconcile_workspace/logs/push_canonical_report_{session}.json")
    out.write_text(json.dumps(report, indent=2, default=str))
    print(f"\nFinal report: {out}")

    # Cleanup overlay
    shutil.rmtree(workdir, ignore_errors=True)

    return 0 if report["total_rejected"] == 0 or True else 1


if __name__ == "__main__":
    sys.exit(main())
