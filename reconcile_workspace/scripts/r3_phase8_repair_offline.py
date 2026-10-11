"""r3_phase8_repair_offline — direct-Mongo targeted-repair driver.

Why offline?
────────────
The HTTP-based ``r3_phase8_repair_3_collections.py`` driver dispatches
400-800k upsert batches through the Cloudflare-fronted deployed
backend.  Support confirmed one of those batches tripped a
Cloudflare / request-timeout mid-write for ``player_game_actuals``.
This driver bypasses HTTP entirely: it connects to the Production
Mongo canonical database directly via ``PROD_MONGO_URL`` from the
GH-Actions runner and performs the identical logical-key UPSERT
contract already approved by the operator.

Everything else — the merge contract, the fail-closed preflight
assertions, the three-collection allowlist, the NHL shots=null
rule, the audit record — is **imported byte-identical** from
``r3_phase8_repair_3_collections.py`` so the two drivers cannot
drift.  See that module's docstring for the full contract.

Hard safety invariants (identical to the HTTP driver)
─────────────────────────────────────────────────────
    * NEVER truncate / drop / delete any collection.
    * NEVER touch any of the OTHER 18 canonical collections.
    * NEVER recreate indexes, run migration, Phase 7, Phase 5, or
      Phase 6.
    * NEVER mutate the R3 session id.
    * Dry-run mode (default) performs ZERO writes.
    * Apply mode requires the hard-coded confirm phrase AND an
      assertion that the Motor client is pointed at a Mongo URI
      different from ``localhost`` (belt + braces against running
      APPLY against a dev DB by accident).

Required environment (direct-Mongo variant)
───────────────────────────────────────────
    PROD_MONGO_URL             — direct Prod canonical Mongo URI
    PROD_CANONICAL_DB_NAME     — defaults to ``lockscore_canonical``
                                 (same as backend's ``CANONICAL_DB_NAME``)
    CANONICAL_IMPORT_SESSION   = perklocks-cutover-20261003-r3

Optional
────────
    P8R_MODE                   = DRY_RUN (default) | APPLY
    P8R_CONFIRM_PHRASE         = APPLY_PERKLOCKS_R3_PHASE8_REPAIR_V1
    P8R_CHKP_P5 / P8R_CHKP_P6  override pinned tarball paths
    P8R_REPORT_PATH            override report output path
    P8R_LOG_EVERY              progress log cadence (rows, default 5000)
    P8R_START_COLLECTION       resume starting from a specific collection
    RECONCILE_TMP              override tmp extract root
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import pathlib
import shutil
import sys
import tempfile
import time

# Reuse the proven, operator-approved merge logic + constants.
_SELF_DIR = pathlib.Path(__file__).resolve().parent
if str(_SELF_DIR) not in sys.path:
    sys.path.insert(0, str(_SELF_DIR))

from r3_phase8_repair_3_collections import (                            # noqa: E402
    APPLY_CONFIRM_PHRASE,
    EXPECTED_MERGED_COUNTS,
    PINNED_P5_PATH,  PINNED_P5_SHA,
    PINNED_P6_PATH,  PINNED_P6_SHA,
    PINNED_SESSION,
    TARGET_COLLECTIONS,
    _extract_phase_canonicals,
    build_merged,
    nhl_canary_assertions,
    nhl_r12_null_shots_assertion,
)

# bson + motor are already dependencies of the backend.
from motor.motor_asyncio import AsyncIOMotorClient                      # noqa: E402

DEDUPE_AUDIT_COLLECTION: str = "_canonical_dedupe_audit"
TARGETED_REPAIR_AUDIT_EVENT: str = "TARGETED_REPAIR_UPSERT_OFFLINE"


# Exit codes (reserved — distinct from the HTTP driver so operator
# reports can distinguish them).
EXIT_OK                     = 0
EXIT_MISSING_ENV            = 2
EXIT_TAR_MISSING            = 10
EXIT_TAR_SHA_MISMATCH       = 11
EXIT_PRECOUNT_MISMATCH      = 12
EXIT_NHL_SHOTS_MISMATCH     = 13
EXIT_MONGO_CONNECT_FAIL     = 20
EXIT_UNSAFE_MONGO_URL       = 21
EXIT_UNKNOWN_START_COLL     = 22
EXIT_UPSERT_FAILED          = 30
EXIT_POSTCOUNT_MISMATCH     = 41


def _require(k: str) -> str:
    v = os.environ.get(k)
    if not v:
        print(f"ERROR: env var {k} not set", file=sys.stderr)
        sys.exit(EXIT_MISSING_ENV)
    return v


def _sha256_of(path: pathlib.Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _redact_mongo(uri: str) -> str:
    """Return a Mongo URI with the password redacted and the host
    partially masked so logs do not leak credentials."""
    try:
        from urllib.parse import urlparse, urlunparse
        p = urlparse(uri)
        host = (p.hostname or "?").split(".")
        if len(host) >= 3:
            host[0] = "*"
        host_s = ".".join(host) + (f":{p.port}" if p.port else "")
        user = (p.username or "")
        creds = f"{user}:***@" if user else ""
        return f"{p.scheme}://{creds}{host_s}{p.path or ''}"
    except Exception:
        return "***"


def _strip_bookkeeping(doc: dict) -> dict:
    """Mirror of the backend's ``_strip_bookkeeping`` — drops Mongo
    ``_id`` and every underscore-prefixed bookkeeping field."""
    return {k: v for k, v in doc.items()
            if k != "_id" and not k.startswith("_")}


def _lk(d: dict, keys: tuple[str, ...]) -> tuple:
    return tuple(d.get(k) for k in keys)


def _write_report(path: pathlib.Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str))


async def _run() -> int:
    mongo_url = _require("PROD_MONGO_URL")
    db_name   = (os.environ.get("PROD_CANONICAL_DB_NAME")
                  or "lockscore_canonical").strip()
    session   = _require("CANONICAL_IMPORT_SESSION")
    if session != PINNED_SESSION:
        print(f"ERROR: CANONICAL_IMPORT_SESSION must equal "
              f"'{PINNED_SESSION}' (got '{session}')", file=sys.stderr)
        return EXIT_MISSING_ENV

    mode = os.environ.get("P8R_MODE", "DRY_RUN").upper()
    if mode not in ("DRY_RUN", "APPLY"):
        print(f"ERROR: P8R_MODE must be DRY_RUN or APPLY (got {mode!r})",
              file=sys.stderr)
        return EXIT_MISSING_ENV

    if mode == "APPLY":
        if os.environ.get("P8R_CONFIRM_PHRASE") != APPLY_CONFIRM_PHRASE:
            print(f"ERROR: APPLY mode requires P8R_CONFIRM_PHRASE="
                  f"{APPLY_CONFIRM_PHRASE}", file=sys.stderr)
            return EXIT_MISSING_ENV
        # Belt + braces: refuse to run APPLY against a localhost URI.
        if (mongo_url.startswith("mongodb://localhost")
                or mongo_url.startswith("mongodb://127.0.0.1")):
            print(f"ERROR: refusing APPLY against localhost Mongo "
                  f"(url={_redact_mongo(mongo_url)})", file=sys.stderr)
            return EXIT_UNSAFE_MONGO_URL

    log_every  = int(os.environ.get("P8R_LOG_EVERY", "5000"))
    start_from = os.environ.get("P8R_START_COLLECTION", "").strip()
    if start_from and start_from not in TARGET_COLLECTIONS:
        print(f"ERROR: P8R_START_COLLECTION must be one of "
              f"{sorted(TARGET_COLLECTIONS)} (got {start_from!r})",
              file=sys.stderr)
        return EXIT_UNKNOWN_START_COLL

    p5 = pathlib.Path(os.environ.get("P8R_CHKP_P5") or PINNED_P5_PATH)
    p6 = pathlib.Path(os.environ.get("P8R_CHKP_P6") or PINNED_P6_PATH)
    report_path = pathlib.Path(
        os.environ.get("P8R_REPORT_PATH")
        or "/app/reconcile_workspace/logs/phase8_repair_offline_report.json")

    print(f"[r3-phase8-offline] mode={mode}  session={session}")
    print(f"[r3-phase8-offline] mongo={_redact_mongo(mongo_url)}  "
          f"db={db_name}")
    print(f"[r3-phase8-offline] P5={p5}  P6={p6}")
    print(f"[r3-phase8-offline] start_from={start_from or '<all 3>'}")

    # ── FAIL-CLOSED 1 + 2: both tarballs exist and SHAs match ───────
    for tar, expect in ((p5, PINNED_P5_SHA), (p6, PINNED_P6_SHA)):
        if not tar.exists():
            print(f"ERROR: tarball missing: {tar}", file=sys.stderr)
            return EXIT_TAR_MISSING
        got = _sha256_of(tar)
        print(f"[preflight] {tar.name}  sha={got}")
        if got != expect:
            print(f"ERROR: SHA mismatch for {tar.name}: expected={expect}",
                  file=sys.stderr)
            return EXIT_TAR_SHA_MISMATCH

    # ── Extract + merge (reused logic, no drift possible) ───────────
    tmp_root = pathlib.Path(
        os.environ.get("RECONCILE_TMP")
        or tempfile.gettempdir()) / "r3_phase8_repair_offline"
    if tmp_root.exists():
        shutil.rmtree(tmp_root)
    tmp_root.mkdir(parents=True, exist_ok=True)
    _extract_phase_canonicals(p5, "v2_phase5", tmp_root)
    _extract_phase_canonicals(p6, "v2_phase6", tmp_root)

    print("[merge] building P5 ∪ P6 (P6-wins, null-LK filtered) …")
    merged_rows, stats, resolver_by_lk = build_merged(tmp_root)

    # ── FAIL-CLOSED 3: merged counts match hard-coded expectations ──
    print("\n[merge] per-collection summary:")
    print(f"{'collection':<24} {'P5':>8} {'P5uniq':>8} {'P5null':>7} "
          f"{'P6':>7} {'P6ovr':>6} {'P6new':>6} {'merged':>8} "
          f"{'expect':>8}  ok")
    print("-" * 100)
    all_counts_ok = True
    for coll, s in stats.items():
        exp = EXPECTED_MERGED_COUNTS[coll]
        ok  = s["merged_count"] == exp
        if not ok:
            all_counts_ok = False
        print(f"{coll:<24} {s['p5_rows']:>8} {s['p5_unique']:>8} "
              f"{s['p5_null_lk']:>7} {s['p6_rows']:>7} "
              f"{s['p6_overrides']:>6} {s['p6_new']:>6} "
              f"{s['merged_count']:>8} {exp:>8}  "
              f"{'OK' if ok else 'FAIL'}")

    base_payload = {
        "mode":              mode,
        "generated_at_unix": int(time.time()),
        "mongo_redacted":    _redact_mongo(mongo_url),
        "db_name":           db_name,
        "session_id":        session,
        "p5_tar":            {"path": str(p5), "sha": PINNED_P5_SHA},
        "p6_tar":            {"path": str(p6), "sha": PINNED_P6_SHA},
        "target_collections": list(TARGET_COLLECTIONS.keys()),
        "logical_keys":      {k: list(v) for k, v in TARGET_COLLECTIONS.items()},
        "expected_counts":   EXPECTED_MERGED_COUNTS,
        "stats":             stats,
    }

    if not all_counts_ok:
        base_payload["verdict"] = "PRECOUNT_MISMATCH"
        _write_report(report_path, base_payload)
        return EXIT_PRECOUNT_MISMATCH

    # ── FAIL-CLOSED 4: NHL R12 + 2 canaries both null ───────────────
    r12 = nhl_r12_null_shots_assertion(merged_rows, resolver_by_lk)
    print(f"\n[nhl-r12] rows with shots=null AND canonical_from="
          f"R12_pgl_volatile_only_populated_wins = {r12['actual']} "
          f"(expected {r12['expected']})  "
          f"{'OK' if r12['ok'] else 'FAIL'}")
    canaries = nhl_canary_assertions(merged_rows)
    base_payload["nhl_r12_assertion"]     = r12
    base_payload["nhl_canary_assertions"] = canaries
    if not r12["ok"] or not all(c["ok_shots_null"] for c in canaries):
        base_payload["verdict"] = "NHL_ASSERT_FAIL"
        _write_report(report_path, base_payload)
        return EXIT_NHL_SHOTS_MISMATCH

    # ── Connect direct to Production Mongo ──────────────────────────
    print(f"\n[mongo] connecting to {_redact_mongo(mongo_url)}")
    try:
        client = AsyncIOMotorClient(mongo_url,
                                      serverSelectionTimeoutMS=15_000,
                                      socketTimeoutMS=300_000)
        # Force a round-trip so a bad URI fails here, not mid-write.
        await client.admin.command("ping")
    except Exception as e:
        print(f"ERROR: mongo connect failed: {type(e).__name__}: {e}",
              file=sys.stderr)
        return EXIT_MONGO_CONNECT_FAIL
    db = client[db_name]

    # ── Pre-write Production counts (direct — bypasses HTTP/CF) ─────
    print("\n[pre-write] direct Mongo counts for the 3 target "
          "collections (no other collection queried):")
    pre_counts: dict[str, int] = {}
    for coll in TARGET_COLLECTIONS:
        pre_counts[coll] = await db[coll].count_documents({})
        delta = EXPECTED_MERGED_COUNTS[coll] - pre_counts[coll]
        print(f"  {coll:<24} count={pre_counts[coll]:>8}  "
              f"target={EXPECTED_MERGED_COUNTS[coll]:>8}  "
              f"delta={delta:>+8}")
    base_payload["pre_write_counts"] = pre_counts

    if mode == "DRY_RUN":
        base_payload["verdict"] = "DRY_RUN_OK"
        base_payload["note"] = (
            "APPLY mode will perform direct-Mongo update_one("
            "{lk}, {\\$set: doc}, upsert=True) for every merged row "
            "in order: player_game_actuals → player_game_logs → "
            "soccer_matches. Idempotent: existing rows no-op or "
            "update to the merged payload.")
        _write_report(report_path, base_payload)
        print(f"\n[report] {report_path}")
        print("\n[DRY_RUN complete]  No writes performed.")
        return EXIT_OK

    # ── APPLY: direct-Mongo logical-key upserts, in order ───────────
    workflow_run_id = (os.environ.get("GITHUB_RUN_ID")
                        or f"local-{int(time.time())}")
    coll_order = [c for c in TARGET_COLLECTIONS
                   if (not start_from) or (c >= start_from)]
    # ``start_from`` is a specific collection, not a sort key — if
    # the operator asked to resume from one, drop collections
    # ordered BEFORE it in the fixed order below.
    _ORDER = ("player_game_actuals", "player_game_logs", "soccer_matches")
    coll_order = []
    hit = not start_from
    for c in _ORDER:
        if not hit and c == start_from:
            hit = True
        if hit:
            coll_order.append(c)
    print(f"\n[apply] collection order: {coll_order}")

    upsert_results: dict[str, dict] = {}
    for coll in coll_order:
        rows = merged_rows[coll]
        key_fields = TARGET_COLLECTIONS[coll]
        print(f"\n[apply] {coll}: {len(rows)} rows → direct upsert, "
              f"idempotent, concurrency=1")

        inserted = updated = matched_unchanged = failed = 0
        t0 = time.time()
        for i, raw_doc in enumerate(rows):
            lk_vals = {k: raw_doc.get(k) for k in key_fields}
            if any(v is None for v in lk_vals.values()):
                failed += 1
                continue  # Belt + braces — already filtered by build_merged.
            set_doc = _strip_bookkeeping(raw_doc)
            try:
                r = await db[coll].update_one(
                    lk_vals,
                    {"$set": set_doc},
                    upsert=True,
                )
            except Exception as e:
                print(f"ERROR: upsert failed {coll} i={i} "
                      f"lk={lk_vals} err={type(e).__name__}: {e}",
                      file=sys.stderr)
                failed += 1
                base_payload["verdict"] = "UPSERT_FAILED"
                base_payload["upsert_failure"] = {
                    "collection": coll,
                    "row_index": i,
                    "logical_key": lk_vals,
                    "error": f"{type(e).__name__}: {e}",
                }
                base_payload["upsert_results"] = upsert_results
                _write_report(report_path, base_payload)
                return EXIT_UPSERT_FAILED

            if r.upserted_id is not None:
                inserted += 1
            elif r.modified_count == 1:
                updated += 1
            else:
                matched_unchanged += 1

            if (i + 1) % log_every == 0 or (i + 1) == len(rows):
                rate = (i + 1) / max(1e-6, time.time() - t0)
                print(f"  [{coll}] {i+1}/{len(rows)}  "
                      f"inserted={inserted} updated={updated} "
                      f"matched_unchanged={matched_unchanged}  "
                      f"rate={rate:>6.1f} rows/s")

        upsert_results[coll] = {
            "rows":              len(rows),
            "inserted":          inserted,
            "updated":           updated,
            "matched_unchanged": matched_unchanged,
            "failed":            failed,
            "duration_sec":      round(time.time() - t0, 1),
        }

        # Audit record per collection.
        try:
            await db[DEDUPE_AUDIT_COLLECTION].insert_one({
                "event":             TARGETED_REPAIR_AUDIT_EVENT,
                "status":            "OK",
                "collection":        coll,
                "mode":              "OFFLINE_DIRECT_MONGO",
                "rows":              len(rows),
                "inserted":          inserted,
                "updated":           updated,
                "matched_unchanged": matched_unchanged,
                "session_id":        session,
                "workflow_run_id":   workflow_run_id,
                "created_at_unix":   int(time.time()),
            })
        except Exception as e:
            print(f"WARNING: audit insert failed: {type(e).__name__}: {e}",
                  file=sys.stderr)

        # ── FAIL-CLOSED 5: per-collection post-count verification ───
        post = await db[coll].count_documents({})
        want = EXPECTED_MERGED_COUNTS[coll]
        print(f"[verify] {coll}  post_count={post}  "
              f"expected={want}  {'OK' if post == want else 'FAIL'}")
        upsert_results[coll]["post_count"] = post
        if post != want:
            base_payload["verdict"] = "POSTCOUNT_MISMATCH"
            base_payload["upsert_results"] = upsert_results
            _write_report(report_path, base_payload)
            print(f"\nABORT: {coll} did not reach exact target "
                  f"(post={post}, expected={want}).  Will not proceed "
                  f"to next collection.", file=sys.stderr)
            return EXIT_POSTCOUNT_MISMATCH

    # ── Final verdict ───────────────────────────────────────────────
    base_payload["upsert_results"] = upsert_results
    base_payload["verdict"]        = "APPLY_OK"
    _write_report(report_path, base_payload)
    print(f"\n[report] {report_path}")
    print("\nAPPLY complete. 3 collections repaired via direct-Mongo "
          "offline path.  Session preserved.  No other collection "
          "touched.")
    return EXIT_OK


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
