"""r3_phase8_only_cert — READ-ONLY Phase-8 certification wrapper.

Scope (strict)
──────────────
This driver does ONE thing: POST
``/api/admin/canonical-cutover/r3-certification`` with the certified
expected-counts payload built from the pinned Phase-5 checkpoint
``phase5_20261003_190628Z.tar.gz``
(SHA256 ``4adc99890885e7b712adfd491124c2ef681715996167d19719ddb04f8eb1e9d7``)
and the ``_audit_sources`` logic cloned verbatim from
``resume_r3_no_reset.py`` (lines ~128-172).

Support confirmed the previous Phase-8 failure was caused by two
driver bugs:

    1. ``PHASE8_CERT_EXPECTED_JSON`` was never populated → the
       certification body carried an empty ``expected`` map.
    2. The auto-chain posted body key ``expected_counts``, but the
       endpoint requires ``expected``.

Both are addressed here by (a) running the audit directly in-process
against the pinned tarball so there is no inter-step env hand-off,
and (b) hard-coding the request body key to ``expected``.

Hard safety invariants
──────────────────────
* NEVER calls Phase 5, Phase 6, Phase 7, dedupe, divergence patches,
  index creation, migration import, reset, or new-session creation.
* Does NOT touch any flag (``USE_CANONICAL_DB``,
  ``BACKGROUND_WORKERS_ENABLED``, ``CANONICAL_IMPORT_ENABLED``).
* Writes NO Mongo documents.  The certification endpoint itself is
  read-only (verified by inspection of
  ``backend/routes/canonical_cutover_routes.py`` lines 1054-1200).
* Fail-closed before certification if any preflight check misses:
    - tarball missing
    - tarball SHA mismatches the pinned constant
    - fewer than 21 extracted canonical NDJSONs
    - audited source counts are zero for any collection
    - the expected payload is empty
    - the expected payload is missing any of the 21 collections
* Never prints, logs, or persists admin password / JWT / import
  token — these are redacted at parse time.

Environment contract (all required)
───────────────────────────────────
* PROD_API_BASE            — e.g. https://api.perklocks.…
* PROD_ADMIN_EMAIL
* PROD_ADMIN_PASSWORD
* CANONICAL_IMPORT_TOKEN
* CANONICAL_IMPORT_SESSION — must equal ``perklocks-cutover-20261003-r3``

Optional
────────
* P8_CHECKPOINT_TAR  — override tarball path (default = pinned)
* P8_REPORT_PATH     — override report output path
* RECONCILE_TMP      — override tmp extract root (default = /tmp)
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
from typing import Any


# ─── Pinned constants (do NOT change at runtime) ────────────────────
PINNED_SESSION:     str   = "perklocks-cutover-20261003-r3"
PINNED_TARBALL:     str   = "phase5_20261003_190628Z.tar.gz"
PINNED_SHA256:      str   = ("4adc99890885e7b712adfd491124c2ef68"
                             "1715996167d19719ddb04f8eb1e9d7")
DEFAULT_CHKP_PATH:  str   = (f"/app/reconcile_workspace/checkpoints/"
                              f"{PINNED_TARBALL}")

# Byte-identical to resume_r3_no_reset.py:RECONCILED_21.
RECONCILED_21: tuple[str, ...] = (
    "games", "historical_ingestion_state", "nfl_ingest_meta",
    "nfl_player_weekly", "parlay_history", "picks",
    "player_game_actuals", "player_game_logs", "player_identities",
    "prediction_snapshots", "pregame_snapshots", "publication_events",
    "rollover_slate_events", "rollover_slates", "settlement_events",
    "soccer_matches", "soccer_player_game_logs", "team_game_actuals",
    "tennis_matches_history", "user_bets", "users",
)

# Byte-identical to resume_r3_no_reset.py:LOGICAL_KEYS.  These are
# the dedupe-collapsed logical keys per collection, matching the
# certification endpoint's own ``_LOGICAL_KEYS`` constant.
LOGICAL_KEYS: dict[str, tuple[str, ...]] = {
    "games":                       ("sport", "game_id"),
    "historical_ingestion_state":  ("_id",),
    "nfl_ingest_meta":             ("_id",),
    "nfl_player_weekly":           ("player_id", "season", "week"),
    "parlay_history":              ("_id",),
    "picks":                       ("id",),
    "player_game_actuals":         ("sport", "event_id", "player_id"),
    "player_game_logs":            ("sport", "game_id", "player_id"),
    "player_identities":           ("canonical_player_id",),
    "prediction_snapshots":        ("prediction_id", "snapshot_version"),
    "pregame_snapshots":           ("snapshot_hash",),
    "publication_events":          ("payload_hash",),
    "rollover_slate_events":       ("slate_date", "event", "at"),
    "rollover_slates":             ("slate_id",),
    "settlement_events":           ("settlement_id",),
    "soccer_matches":              ("league", "season", "home_team",
                                     "away_team", "date"),
    "soccer_player_game_logs":     ("match_id", "player_id"),
    "team_game_actuals":           ("sport", "event_id", "canonical_team_id"),
    "tennis_matches_history":      ("tourney_id", "winner_id", "loser_id"),
    "user_bets":                   ("id",),
    "users":                       ("id",),
}


# ─── Exit codes (reserved) ──────────────────────────────────────────
EXIT_OK:                         int = 0
EXIT_MISSING_ENV:                int = 2
EXIT_TARBALL_MISSING:            int = 10
EXIT_TARBALL_SHA_MISMATCH:       int = 11
EXIT_EXTRACT_INCOMPLETE:         int = 12
EXIT_AUDIT_EMPTY:                int = 13
EXIT_AUDIT_MISSING_COLLECTIONS:  int = 14
EXIT_AUDIT_ZERO_COUNTS:          int = 15
EXIT_AUTH_FAILED:                int = 20
EXIT_CERT_HTTP_FAIL:             int = 30
EXIT_CERT_FAIL:                  int = 70


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


def _post(url: str, headers: dict, body: dict, timeout: int = 600):
    data = json.dumps(body, default=str).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
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


# ─── Source-audit logic — PRESERVED verbatim from resume_r3_no_reset ─
# resume_r3_no_reset._audit_sources covers Phase-5 AND Phase-6
# tarballs.  For this Phase-8-only wrapper we use ONLY the pinned
# Phase-5 tarball (per the directive); the loop below is identical
# in math and selection order, just with the Phase-6 branch removed.
def _audit_sources(tar_path: pathlib.Path) -> dict[str, dict]:
    """Parse the pinned Phase-5 tarball and emit the exact expected-
    counts payload the certification endpoint will compare against.

    Returns a mapping ``{collection: {source_rows, unique_logical_
    identities, excluded_rows, quarantined_rows,
    null_logical_key_rows, expected_canonical_rows}}``.
    ``expected_canonical_rows`` is the logical-key-unique count —
    i.e. the row count the dedupe-collapsed canonical collection is
    expected to hold.  This is the intended comparison for the 11
    dedup-collapsed collections (per Support).
    """
    tmp_root = pathlib.Path(os.environ.get("RECONCILE_TMP")
                             or tempfile.gettempdir())
    tmp = tmp_root / "r3_phase8_audit"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True, exist_ok=True)

    with tarfile.open(tar_path) as tf:
        for m in tf.getmembers():
            if (m.isfile()
                    and "/canonical/" in m.name
                    and m.name.endswith(".ndjson")):
                tf.extract(m, path=tmp)

    p5 = tmp / "v2_phase5" / "canonical"
    expected: dict[str, dict] = {}
    for coll in RECONCILED_21:
        p5f = p5 / f"{coll}.ndjson"
        if not p5f.exists():
            continue
        keys = LOGICAL_KEYS[coll]
        total = excluded = quarantined = null_lk = 0
        seen: set[tuple] = set()
        with open(p5f) as f:
            for ln in f:
                try:
                    r = json.loads(ln)
                except Exception:
                    continue
                total += 1
                d = r.get("doc", r)
                if d.get("excluded_from_canonical_runtime") is True:
                    excluded += 1
                    continue
                if d.get("status") == "UNRESOLVED_IMMUTABLE_CONFLICT":
                    quarantined += 1
                    continue
                lk = tuple(d.get(f) for f in keys)
                if any(v is None for v in lk):
                    null_lk += 1
                    continue
                seen.add(lk)
        expected[coll] = {
            "source_rows":               total,
            "unique_logical_identities": len(seen),
            "excluded_rows":             excluded,
            "quarantined_rows":          quarantined,
            "null_logical_key_rows":     null_lk,
            "expected_canonical_rows":   len(seen),
        }
    return expected


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

    tar_path = pathlib.Path(
        os.environ.get("P8_CHECKPOINT_TAR") or DEFAULT_CHKP_PATH)
    report_path = pathlib.Path(
        os.environ.get("P8_REPORT_PATH")
        or "/app/reconcile_workspace/logs/phase8_only_report.json")

    # ── FAIL-CLOSED CHECK 1: tarball exists ─────────────────────────
    if not tar_path.exists():
        print(f"ERROR: pinned Phase-5 tarball missing: {tar_path}",
              file=sys.stderr)
        return EXIT_TARBALL_MISSING

    # ── FAIL-CLOSED CHECK 2: tarball SHA matches pinned constant ────
    actual_sha = _sha256_of(tar_path)
    print(f"[preflight] tarball        = {tar_path}")
    print(f"[preflight] actual sha256  = {actual_sha}")
    print(f"[preflight] pinned sha256  = {PINNED_SHA256}")
    if actual_sha != PINNED_SHA256:
        print("ERROR: pinned Phase-5 tarball SHA mismatch — aborting",
              file=sys.stderr)
        return EXIT_TARBALL_SHA_MISMATCH

    # ── Run the source audit ────────────────────────────────────────
    print("[source-audit] parsing pinned Phase-5 NDJSONs …")
    expected = _audit_sources(tar_path)
    print(f"[source-audit] {len(expected)}/21 collections audited")

    # ── FAIL-CLOSED CHECK 3: expected payload is non-empty ──────────
    if not expected:
        print("ERROR: source-audit produced an EMPTY expected payload",
              file=sys.stderr)
        return EXIT_AUDIT_EMPTY

    # ── FAIL-CLOSED CHECK 4: all 21 collections present ─────────────
    missing_colls = [c for c in RECONCILED_21 if c not in expected]
    if missing_colls:
        print(f"ERROR: expected payload missing collections: "
              f"{missing_colls}", file=sys.stderr)
        return EXIT_AUDIT_MISSING_COLLECTIONS

    # ── FAIL-CLOSED CHECK 5: non-zero audited counts where expected ─
    # The 21 pinned canonical NDJSONs are known to each contain >=1
    # row — a zero means the extract silently truncated.
    zero_rows = [c for c in RECONCILED_21
                 if expected[c]["source_rows"] == 0]
    if zero_rows:
        print(f"ERROR: zero source_rows for: {zero_rows} — audit broken",
              file=sys.stderr)
        return EXIT_AUDIT_ZERO_COUNTS

    # ── Diagnostic dump of the audited counts ───────────────────────
    print("\n[audit] Expected payload (summary — same math as "
          "resume_r3_no_reset._audit_sources):")
    print(f"{'collection':<30} {'src':>10} {'uniq_lk':>10} "
          f"{'excl':>6} {'quar':>6} {'nulllk':>7}")
    print("-" * 80)
    for coll in RECONCILED_21:
        e = expected[coll]
        print(f"{coll:<30} {e['source_rows']:>10} "
              f"{e['unique_logical_identities']:>10} "
              f"{e['excluded_rows']:>6} {e['quarantined_rows']:>6} "
              f"{e['null_logical_key_rows']:>7}")

    # ── Login ───────────────────────────────────────────────────────
    print(f"\n[auth] POST {_redact_host(api_base)}/api/auth/login")
    code, body = _post(f"{api_base}/api/auth/login", {},
                        {"email": email, "password": password})
    if code != 200 or not isinstance(body, dict) or not body.get("access_token"):
        print(f"ERROR: login failed status={code}", file=sys.stderr)
        return EXIT_AUTH_FAILED
    jwt = body["access_token"]
    hdr_t = {"Authorization":            f"Bearer {jwt}",
              "X-Canonical-Import-Token": import_tok}
    print(f"[auth] OK role="
          f"{body.get('user', {}).get('role') or body.get('role')}")

    # ── POST certification (body key MUST be `expected`) ────────────
    print("\n[phase8] POST /api/admin/canonical-cutover/r3-certification")
    cert_body = {
        "session_id":     session,
        "expected":       expected,
        "tolerance_rows": 0,
    }
    code, cert = _post(
        f"{api_base}/api/admin/canonical-cutover/r3-certification",
        hdr_t, cert_body, timeout=600,
    )
    print(f"[phase8] http status = {code}")
    if code != 200 or not isinstance(cert, dict):
        print(f"ERROR: certification HTTP failed status={code} body={cert}",
              file=sys.stderr)
        # Still write the preflight artefact so operator can inspect.
        _write_report(report_path, {
            "generated_at_unix":          int(time.time()),
            "api_target_redacted":        _redact_host(api_base),
            "session_id":                 session,
            "tarball_sha":                actual_sha,
            "pinned_sha":                 PINNED_SHA256,
            "expected_counts":            expected,
            "phase8_http_status":         code,
            "phase8_cert_response":       cert,
            "phase8_verdict":             "HTTP_FAIL",
        })
        return EXIT_CERT_HTTP_FAIL

    overall_pass = bool(cert.get("overall_pass"))
    verdict      = "PASS" if overall_pass else "FAIL"
    print(f"[phase8] overall_pass = {overall_pass}  → verdict = {verdict}")

    # ── Per-collection table (same shape as resume_r3_no_reset) ─────
    print(f"\n{'collection':<30} {'exp_canon':>10} {'canon':>8} "
          f"{'uniq_canon':>11} {'dup':>5} {'diff':>7} "
          f"{'row_ok':>7} {'uniq_ok':>8}  fingerprint   status")
    print("-" * 120)
    for r in cert.get("collections", []):
        print(f"{r.get('collection','?'):<30} "
              f"{r.get('expected_canonical_rows',0):>10} "
              f"{r.get('canonical_rows',0):>8} "
              f"{r.get('unique_canonical_logical_ids',0):>11} "
              f"{r.get('duplicate_canonical_ids',0):>5} "
              f"{r.get('difference',0):>+7} "
              f"{str(r.get('row_count_match')):>7} "
              f"{str(r.get('logical_key_unique_count_match')):>8}  "
              f"{(r.get('canonical_fingerprint') or '')[:12]:<12}  "
              f"{r.get('status','?')}")

    _write_report(report_path, {
        "generated_at_unix":          int(time.time()),
        "api_target_redacted":        _redact_host(api_base),
        "session_id":                 session,
        "tarball_path":               str(tar_path),
        "tarball_sha":                actual_sha,
        "pinned_sha":                 PINNED_SHA256,
        "expected_counts":            expected,
        "phase8_http_status":         code,
        "phase8_cert_response":       cert,
        "phase8_verdict":             verdict,
    })
    print(f"\n[report] {report_path}")

    if not overall_pass:
        print("\n❌ R3 PHASE 8 FAILED — read-only cert.  "
              "No reactivation performed.", file=sys.stderr)
        return EXIT_CERT_FAIL

    print("\n✅ R3 PHASE 8 PASS.  Read-only cert complete.  "
          "No flags were changed, no re-activation performed.")
    return EXIT_OK


def _write_report(path: pathlib.Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str))


if __name__ == "__main__":
    sys.exit(main())
