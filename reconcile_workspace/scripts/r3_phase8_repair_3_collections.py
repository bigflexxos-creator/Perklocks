"""r3_phase8_repair_3_collections — Phase-8 three-collection repair.

Scope (strict)
──────────────
Repair exactly three canonical collections whose Phase-5 baseline was
dropped by the overlay-replace bug at
``push_canonical_to_production.py`` lines 258-269:

    1. player_game_actuals
    2. player_game_logs
    3. soccer_matches

Merge contract (operator-approved 2026-10-10)
─────────────────────────────────────────────
    valid Phase-5 baseline  ∪  valid Phase-6 overlay
    (by logical key; Phase-6 wins on conflict)

Valid = non-null logical key.  No shots=0 canary override — the 7 167
NHL R12 rows return to shots=null globally.  P6-new keys (153 PGA /
1 PGL) are INCLUDED; the ``(soccer, event_id=None, player_id=None)``
P5 degenerate rows are EXCLUDED.

Expected merged logical counts (hard-coded; preflight fails closed
otherwise):

    player_game_actuals = 404 573
    player_game_logs    = 412 747
    soccer_matches      = 25 659

Pinned sources
──────────────
    phase5_20261003_190628Z.tar.gz
      sha256 = 4adc99890885e7b712adfd491124c2ef681715996167d19719ddb04f8eb1e9d7
    phase6_20261003_192150Z.tar.gz
      sha256 = 327a38903daf3bd910ab50b68f69415cfcf181624655b42fb791af43042b6c22

Hard safety invariants
──────────────────────
    * NEVER truncate / drop / delete any collection.
    * NEVER touch any of the OTHER 18 collections.
    * NEVER recreate indexes, run migration, Phase 7, Phase 5, or
      Phase 6.
    * NEVER call ``/api/admin/canonical-import``.
    * NEVER mutate the R3 session id.
    * Dry-run mode (default) performs ZERO writes.
    * Apply mode requires both the workflow confirm phrase AND the
      backend's hard-coded confirm phrase; the endpoint's own
      allowlist is the final belt + braces.

Environment contract (all required)
───────────────────────────────────
    PROD_API_BASE
    PROD_ADMIN_EMAIL
    PROD_ADMIN_PASSWORD
    CANONICAL_IMPORT_TOKEN
    CANONICAL_IMPORT_SESSION = perklocks-cutover-20261003-r3

Optional
────────
    P8R_MODE             = DRY_RUN (default) | APPLY
    P8R_CONFIRM_PHRASE   must equal APPLY_PERKLOCKS_R3_PHASE8_REPAIR_V1 for APPLY
    P8R_CHKP_P5          override pinned Phase-5 tarball path
    P8R_CHKP_P6          override pinned Phase-6 tarball path
    P8R_REPORT_PATH      override report output path
    P8R_BATCH_SIZE       1 ≤ n ≤ 500   (default 250)
    RECONCILE_TMP        override tmp extract root
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
import urllib.error
import urllib.request
from collections import Counter
from typing import Any


# ─── Pinned constants (do NOT change at runtime) ────────────────────
PINNED_SESSION:    str = "perklocks-cutover-20261003-r3"
PINNED_P5_SHA:     str = ("4adc99890885e7b712adfd491124c2ef68"
                           "1715996167d19719ddb04f8eb1e9d7")
PINNED_P6_SHA:     str = ("327a38903daf3bd910ab50b68f69415cfc"
                           "f181624655b42fb791af43042b6c22")
PINNED_P5_PATH:    str = ("/app/reconcile_workspace/checkpoints/"
                           "phase5_20261003_190628Z.tar.gz")
PINNED_P6_PATH:    str = ("/app/reconcile_workspace/checkpoints/"
                           "phase6_20261003_192150Z.tar.gz")

APPLY_CONFIRM_PHRASE: str = "APPLY_PERKLOCKS_R3_PHASE8_REPAIR_V1"

# Hard-coded repair scope.  The backend endpoint carries its own
# identical allowlist — this driver cannot write to any other
# collection even if these constants are edited.
TARGET_COLLECTIONS: dict[str, tuple[str, ...]] = {
    "player_game_actuals": ("sport", "event_id", "player_id"),
    "player_game_logs":    ("sport", "game_id", "player_id"),
    "soccer_matches":      ("league", "season", "home_team",
                             "away_team", "date"),
}

EXPECTED_MERGED_COUNTS: dict[str, int] = {
    "player_game_actuals": 404_573,
    "player_game_logs":    412_747,
    "soccer_matches":      25_659,
}

# NHL canaries that the Phase-7 patch set to shots=0; this repair
# globally returns them to shots=null per the operator-approved
# final contract.  Verified post-merge.
NHL_CANARIES = (
    ("nhl", "nhl_2025020042", "nhl_8480113"),
    ("nhl", "nhl_2025020055", "nhl_8480798"),
)

# Exit codes
EXIT_OK                     = 0
EXIT_MISSING_ENV            = 2
EXIT_TAR_MISSING            = 10
EXIT_TAR_SHA_MISMATCH       = 11
EXIT_PRECOUNT_MISMATCH      = 12
EXIT_NHL_SHOTS_MISMATCH     = 13
EXIT_AUTH_FAILED            = 20
EXIT_PRECOUNT_HTTP_FAIL     = 25
EXIT_UPSERT_FAILED          = 30
EXIT_POSTCOUNT_HTTP_FAIL    = 40
EXIT_POSTCOUNT_MISMATCH     = 41


def _require(k: str) -> str:
    v = os.environ.get(k)
    if not v:
        print(f"ERROR: env var {k} not set", file=sys.stderr)
        sys.exit(EXIT_MISSING_ENV)
    return v


def _redact_host(url: str) -> str:
    from urllib.parse import urlparse
    p = urlparse(url)
    h = (p.hostname or "?").split(".")
    if len(h) >= 3:
        h[0] = "*"
    return ".".join(h) + (f":{p.port}" if p.port else "")


def _http(url: str, headers: dict, body: dict | None = None,
          method: str = "GET", timeout_s: int = 600):
    data = json.dumps(body, default=str).encode() if body else None
    req = urllib.request.Request(
        url, data=data,
        headers={**headers, "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        txt = e.read().decode(errors="replace")[:800]
        try:
            return e.code, json.loads(txt)
        except Exception:
            return e.code, txt
    except urllib.error.URLError as e:
        return 0, {"transport_error": str(e.reason)}


def _sha256_of(path: pathlib.Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _extract_phase_canonicals(tar_path: pathlib.Path, phase_dir: str,
                               tmp_root: pathlib.Path) -> pathlib.Path:
    """Extract ``{phase_dir}/canonical/*.ndjson`` members for the
    three target collections only; returns the extraction root."""
    with tarfile.open(tar_path) as tf:
        wanted = tuple(f"{phase_dir}/canonical/{c}.ndjson"
                       for c in TARGET_COLLECTIONS)
        for m in tf.getmembers():
            if m.isfile() and m.name in wanted:
                tf.extract(m, path=tmp_root)
    return tmp_root


def _rows(fp: pathlib.Path):
    if not fp.exists():
        return
    with open(fp) as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                yield json.loads(ln)
            except Exception:
                continue


def _lk(d: dict, keys: tuple[str, ...]) -> tuple:
    return tuple(d.get(k) for k in keys)


# ─── Merge + verify ─────────────────────────────────────────────────
def build_merged(tmp: pathlib.Path) -> tuple[
        dict[str, list[dict]], dict[str, dict], dict[str, dict[tuple, str]]]:
    """Return (merged_rows_by_coll, stats_by_coll, resolver_by_lk_by_coll).

    ``merged_rows_by_coll[coll]`` is a list of business-doc dicts
    (``doc`` payload only) ready to post to the backend upsert
    endpoint.  Each row has a complete, non-null logical key, no
    duplicate logical keys, and reflects P6-wins-on-conflict.

    ``stats_by_coll[coll]`` records: p5_rows, p5_unique, p5_null_lk,
    p5_excluded, p5_quarantined, p6_rows, p6_overrides, p6_new,
    p6_null_lk, merged_count.

    ``resolver_by_lk_by_coll[coll][lk_tuple]`` carries the last
    ``canonical_from`` label (from the WRAPPER row, not the business
    doc) for post-merge assertions.  Never written to Mongo.
    """
    merged_rows:     dict[str, list[dict]]            = {}
    stats:           dict[str, dict]                  = {}
    resolver_by_lk:  dict[str, dict[tuple, str]]      = {}
    for coll, keys in TARGET_COLLECTIONS.items():
        p5 = tmp / "v2_phase5" / "canonical" / f"{coll}.ndjson"
        p6 = tmp / "v2_phase6" / "canonical" / f"{coll}.ndjson"

        p5_rows = p5_null_lk = p5_excl = p5_quar = 0
        p6_rows = p6_null_lk = 0
        merged: dict[tuple, dict] = {}
        resolver: dict[tuple, str] = {}

        for r in _rows(p5):
            p5_rows += 1
            d = r.get("doc", r)
            if d.get("excluded_from_canonical_runtime") is True:
                p5_excl += 1
                continue
            if d.get("status") == "UNRESOLVED_IMMUTABLE_CONFLICT":
                p5_quar += 1
                continue
            lk = _lk(d, keys)
            if any(v is None for v in lk):
                p5_null_lk += 1
                continue
            merged[lk]   = d
            resolver[lk] = r.get("canonical_from") or ""

        p5_unique = len(merged)
        p6_overrides = 0
        p6_new = 0

        for r in _rows(p6):
            p6_rows += 1
            d = r.get("doc", r)
            if d.get("excluded_from_canonical_runtime") is True:
                continue
            if d.get("status") == "UNRESOLVED_IMMUTABLE_CONFLICT":
                continue
            lk = _lk(d, keys)
            if any(v is None for v in lk):
                p6_null_lk += 1
                continue
            if lk in merged:
                p6_overrides += 1
            else:
                p6_new += 1
            merged[lk]   = d       # P6 wins on conflict
            resolver[lk] = r.get("canonical_from") or ""

        merged_rows[coll]    = list(merged.values())
        resolver_by_lk[coll] = resolver
        stats[coll] = {
            "p5_rows":       p5_rows,
            "p5_unique":     p5_unique,
            "p5_null_lk":    p5_null_lk,
            "p5_excluded":   p5_excl,
            "p5_quarantined": p5_quar,
            "p6_rows":       p6_rows,
            "p6_overrides":  p6_overrides,
            "p6_new":        p6_new,
            "p6_null_lk":    p6_null_lk,
            "merged_count":  len(merged),
        }
    return merged_rows, stats, resolver_by_lk


def nhl_canary_assertions(merged_rows: dict[str, list[dict]]
                          ) -> list[dict]:
    """For every NHL canary, assert the merged row has ``shots is
    None`` (per the global rule).  Returns per-canary diagnostic
    rows; caller enforces fail-closed."""
    pgl = {_lk(d, TARGET_COLLECTIONS["player_game_logs"]): d
           for d in merged_rows["player_game_logs"]}
    out = []
    for key in NHL_CANARIES:
        d = pgl.get(key)
        out.append({
            "key":        list(key),
            "found":      d is not None,
            "shots":      (d.get("shots") if d else None),
            "ok_shots_null": (d is not None and d.get("shots") is None),
        })
    return out


def nhl_r12_null_shots_assertion(
    merged_rows:    dict[str, list[dict]],
    resolver_by_lk: dict[str, dict[tuple, str]],
) -> dict:
    """Count how many NHL R12_pgl_volatile_only_populated_wins rows
    in the merged result have shots == null.  Expected = 7 167.
    """
    expected = 7_167
    seen = 0
    keys = TARGET_COLLECTIONS["player_game_logs"]
    resolver_map = resolver_by_lk["player_game_logs"]
    for d in merged_rows["player_game_logs"]:
        if d.get("sport") != "nhl":
            continue
        if d.get("shots") is not None:
            continue
        lk = _lk(d, keys)
        canon = (resolver_map.get(lk) or "").lower()
        if "r12_pgl_volatile_only_populated_wins" in canon:
            seen += 1
    return {
        "expected":   expected,
        "actual":     seen,
        "ok":         (seen == expected),
    }


# ─── HTTP helpers ───────────────────────────────────────────────────
def _login(api_base: str, email: str, pw: str) -> str:
    code, body = _http(f"{api_base}/api/auth/login", {},
                        body={"email": email, "password": pw},
                        method="POST")
    if code != 200 or not isinstance(body, dict) or not body.get("access_token"):
        print(f"ERROR: login failed status={code}", file=sys.stderr)
        sys.exit(EXIT_AUTH_FAILED)
    return body["access_token"]


def _collection_total_docs(api_base: str, hdr: dict,
                           coll: str) -> int:
    """Returns ``total_docs`` for a collection via the existing
    read-only ``dup-census`` endpoint (``limit=1`` so the heavy
    aggregation collapses to the summary header)."""
    code, body = _http(
        f"{api_base}/api/admin/canonical-cutover/dup-census"
        f"?collection={coll}&offset=0&limit=1&max_docs_per_group=1",
        hdr, method="GET", timeout_s=600,
    )
    if code != 200 or not isinstance(body, dict):
        print(f"ERROR: dup-census {coll} http={code} body={body}",
              file=sys.stderr)
        sys.exit(EXIT_PRECOUNT_HTTP_FAIL)
    return int(body.get("total_docs", -1))


def _write_report(path: pathlib.Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str))


# ────────────────────────────────────────────────────────────────────
# main
# ────────────────────────────────────────────────────────────────────
def main() -> int:
    api_base   = _require("PROD_API_BASE").rstrip("/")
    email      = _require("PROD_ADMIN_EMAIL")
    password   = _require("PROD_ADMIN_PASSWORD")
    import_tok = _require("CANONICAL_IMPORT_TOKEN")
    session    = _require("CANONICAL_IMPORT_SESSION")

    if session != PINNED_SESSION:
        print(f"ERROR: CANONICAL_IMPORT_SESSION must equal "
              f"'{PINNED_SESSION}' (got '{session}')", file=sys.stderr)
        return EXIT_MISSING_ENV

    mode = os.environ.get("P8R_MODE", "DRY_RUN").upper()
    if mode not in ("DRY_RUN", "APPLY"):
        print(f"ERROR: P8R_MODE must be DRY_RUN or APPLY (got {mode!r})",
              file=sys.stderr)
        return EXIT_MISSING_ENV

    p8r_confirm = os.environ.get("P8R_CONFIRM_PHRASE", "")
    if mode == "APPLY" and p8r_confirm != APPLY_CONFIRM_PHRASE:
        print(f"ERROR: APPLY mode requires P8R_CONFIRM_PHRASE="
              f"{APPLY_CONFIRM_PHRASE} — refusing to write",
              file=sys.stderr)
        return EXIT_MISSING_ENV

    batch_size = int(os.environ.get("P8R_BATCH_SIZE", "250"))
    if not (1 <= batch_size <= 500):
        print(f"ERROR: P8R_BATCH_SIZE must be 1..500 (got {batch_size})",
              file=sys.stderr)
        return EXIT_MISSING_ENV

    p5 = pathlib.Path(os.environ.get("P8R_CHKP_P5") or PINNED_P5_PATH)
    p6 = pathlib.Path(os.environ.get("P8R_CHKP_P6") or PINNED_P6_PATH)
    report_path = pathlib.Path(
        os.environ.get("P8R_REPORT_PATH")
        or "/app/reconcile_workspace/logs/phase8_repair_report.json")

    print(f"[r3-phase8-repair] mode={mode}  api={_redact_host(api_base)}")
    print(f"[r3-phase8-repair] session={session}  batch_size={batch_size}")
    print(f"[r3-phase8-repair] P5={p5}  P6={p6}")

    # ── FAIL-CLOSED 1: both tarballs exist and SHAs match ───────────
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

    # ── Extract canonical NDJSONs for the 3 target collections ──────
    tmp_root = pathlib.Path(
        os.environ.get("RECONCILE_TMP")
        or tempfile.gettempdir()) / "r3_phase8_repair"
    if tmp_root.exists():
        shutil.rmtree(tmp_root)
    tmp_root.mkdir(parents=True, exist_ok=True)
    _extract_phase_canonicals(p5, "v2_phase5", tmp_root)
    _extract_phase_canonicals(p6, "v2_phase6", tmp_root)

    # ── Build merge ─────────────────────────────────────────────────
    print("[merge] building P5 ∪ P6 (P6-wins-on-conflict, "
          "null-LK filtered) for the 3 target collections …")
    merged_rows, stats, resolver_by_lk = build_merged(tmp_root)

    # ── FAIL-CLOSED 2: merged counts match hard-coded expectations ──
    print("\n[merge] per-collection summary:")
    print(f"{'collection':<24} {'P5':>8} {'P5uniq':>8} {'P5null':>7} "
          f"{'P6':>7} {'P6ovr':>6} {'P6new':>6} {'P6null':>7} "
          f"{'merged':>8} {'expect':>8}  ok")
    print("-" * 110)
    all_counts_ok = True
    for coll, s in stats.items():
        exp = EXPECTED_MERGED_COUNTS[coll]
        ok  = s["merged_count"] == exp
        if not ok:
            all_counts_ok = False
        print(f"{coll:<24} {s['p5_rows']:>8} {s['p5_unique']:>8} "
              f"{s['p5_null_lk']:>7} {s['p6_rows']:>7} "
              f"{s['p6_overrides']:>6} {s['p6_new']:>6} "
              f"{s['p6_null_lk']:>7} {s['merged_count']:>8} "
              f"{exp:>8}  {'✅' if ok else '❌'}")

    if not all_counts_ok:
        _write_report(report_path, {
            "mode":    mode, "verdict": "PRECOUNT_MISMATCH",
            "stats":   stats, "expected_counts": EXPECTED_MERGED_COUNTS,
            "generated_at_unix": int(time.time()),
        })
        return EXIT_PRECOUNT_MISMATCH

    # ── FAIL-CLOSED 3: NHL R12 shots assertion (7 167 rows null) ────
    r12_assert = nhl_r12_null_shots_assertion(merged_rows, resolver_by_lk)
    print(f"\n[nhl-r12] rows with shots=null AND canonical_from="
          f"R12_pgl_volatile_only_populated_wins = {r12_assert['actual']} "
          f"(expected {r12_assert['expected']})  "
          f"{'✅' if r12_assert['ok'] else '❌'}")
    if not r12_assert["ok"]:
        _write_report(report_path, {
            "mode":    mode, "verdict": "NHL_R12_ASSERT_FAIL",
            "stats":   stats, "nhl_r12": r12_assert,
            "generated_at_unix": int(time.time()),
        })
        return EXIT_NHL_SHOTS_MISMATCH

    # NHL canary spot-check (both canaries end up shots=null).
    canaries = nhl_canary_assertions(merged_rows)
    print("[nhl-canary] (expect shots=null for both):")
    for c in canaries:
        print(f"  {c['key']}  shots={c['shots']!r}  "
              f"ok={c['ok_shots_null']}")
    if not all(c["ok_shots_null"] for c in canaries):
        _write_report(report_path, {
            "mode":    mode, "verdict": "NHL_CANARY_ASSERT_FAIL",
            "stats":   stats, "canaries": canaries,
            "generated_at_unix": int(time.time()),
        })
        return EXIT_NHL_SHOTS_MISMATCH

    # ── Auth + pre-write collection counts ──────────────────────────
    jwt = _login(api_base, email, password)
    hdr   = {"Authorization": f"Bearer {jwt}"}
    hdr_t = {**hdr, "X-Canonical-Import-Token": import_tok}

    print("\n[pre-write] fetching current canonical total_docs via "
          "dup-census (read-only) …")
    pre_counts: dict[str, int] = {}
    for coll in TARGET_COLLECTIONS:
        pre_counts[coll] = _collection_total_docs(api_base, hdr, coll)
        print(f"  {coll:<24} total_docs={pre_counts[coll]:>8}  "
              f"merged_target={EXPECTED_MERGED_COUNTS[coll]:>8}  "
              f"max_inserts≈{max(0, EXPECTED_MERGED_COUNTS[coll] - pre_counts[coll])}")

    base_payload = {
        "mode":              mode,
        "generated_at_unix": int(time.time()),
        "api_target_redacted": _redact_host(api_base),
        "session_id":        session,
        "p5_tar":            {"path": str(p5), "sha": PINNED_P5_SHA},
        "p6_tar":            {"path": str(p6), "sha": PINNED_P6_SHA},
        "target_collections": list(TARGET_COLLECTIONS.keys()),
        "logical_keys":      {k: list(v) for k, v in TARGET_COLLECTIONS.items()},
        "expected_counts":   EXPECTED_MERGED_COUNTS,
        "stats":             stats,
        "nhl_r12_assertion": r12_assert,
        "nhl_canary_assertions": canaries,
        "pre_write_total_docs":  pre_counts,
    }

    if mode == "DRY_RUN":
        base_payload["verdict"] = "DRY_RUN_OK"
        base_payload["note"] = ("APPLY mode will perform logical-key "
                                "UPSERT via /canonical-cutover/"
                                "targeted-logical-upsert in batches of "
                                f"{batch_size}; endpoint carries its own "
                                "allowlist of exactly these 3 collections.")
        _write_report(report_path, base_payload)
        print(f"\n[report] {report_path}")
        print("\n✅ DRY_RUN complete.  No writes performed.")
        return EXIT_OK

    # ── APPLY mode: batched upserts ─────────────────────────────────
    print(f"\n[apply] dispatching UPSERT batches "
          f"(size={batch_size}, concurrency=1) …")
    upsert_results: dict[str, dict] = {}
    for coll, rows in merged_rows.items():
        print(f"[apply] {coll}: {len(rows)} rows → "
              f"{(len(rows) + batch_size - 1) // batch_size} batches")
        inserted = updated = matched = 0
        batch_log: list[dict] = []
        for batch_no, start in enumerate(range(0, len(rows), batch_size)):
            batch = rows[start:start + batch_size]
            code, resp = _http(
                f"{api_base}/api/admin/canonical-cutover/"
                f"targeted-logical-upsert",
                hdr_t,
                body={
                    "session_id":      session,
                    "workflow_run_id": os.environ.get(
                        "GITHUB_RUN_ID",
                        f"local-{int(time.time())}"),
                    "confirm_phrase":  APPLY_CONFIRM_PHRASE,
                    "collection":      coll,
                    "batch_no":        batch_no,
                    "docs":            batch,
                },
                method="POST", timeout_s=900,
            )
            if code != 200 or not isinstance(resp, dict) or not resp.get("ok"):
                print(f"ERROR: upsert batch failed {coll} b={batch_no} "
                      f"status={code} body={resp}", file=sys.stderr)
                base_payload["verdict"] = "UPSERT_FAILED"
                base_payload["upsert_failure"] = {
                    "collection": coll, "batch_no": batch_no,
                    "status": code, "response": resp,
                }
                base_payload["upsert_results"] = upsert_results
                _write_report(report_path, base_payload)
                return EXIT_UPSERT_FAILED
            inserted += resp.get("inserted", 0)
            updated  += resp.get("updated", 0)
            matched  += resp.get("matched_unchanged", 0)
            batch_log.append({
                "batch_no": batch_no,
                "docs":     len(batch),
                "inserted": resp.get("inserted", 0),
                "updated":  resp.get("updated", 0),
                "matched_unchanged": resp.get("matched_unchanged", 0),
            })
            if batch_no % 20 == 0 or batch_no == 0:
                print(f"  …batch {batch_no:>4}  "
                      f"inserted={inserted:>7} updated={updated:>7} "
                      f"matched={matched:>7}")
        upsert_results[coll] = {
            "rows":              len(rows),
            "inserted":          inserted,
            "updated":           updated,
            "matched_unchanged": matched,
            "batch_log":         batch_log,
        }
        print(f"[apply] {coll} done — inserted={inserted} "
              f"updated={updated} matched_unchanged={matched}")

    # ── Post-write verification ─────────────────────────────────────
    print("\n[post-write] verifying canonical counts via dup-census …")
    post_counts: dict[str, int] = {}
    post_mismatch = False
    for coll in TARGET_COLLECTIONS:
        post_counts[coll] = _collection_total_docs(api_base, hdr, coll)
        want = EXPECTED_MERGED_COUNTS[coll]
        ok   = post_counts[coll] == want
        if not ok:
            post_mismatch = True
        print(f"  {coll:<24} post={post_counts[coll]:>8} "
              f"expected={want:>8}  {'✅' if ok else '❌'}")

    base_payload["upsert_results"]    = upsert_results
    base_payload["post_write_total_docs"] = post_counts
    base_payload["verdict"] = ("APPLY_OK" if not post_mismatch
                                else "POSTCOUNT_MISMATCH")
    _write_report(report_path, base_payload)
    print(f"\n[report] {report_path}")

    if post_mismatch:
        print("\n❌ APPLY completed but post-write counts do not match "
              "expectations.", file=sys.stderr)
        return EXIT_POSTCOUNT_MISMATCH

    print("\n✅ APPLY complete.  3 collections repaired.  "
          "No other collection touched.  Session preserved.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
