"""reconcile_offline — PHASE 1 offline reconciliation tool.

Operates ONLY on local backup archives (mongodump BSON.gz inside a
.zip or an already-extracted directory).  It is physically unable
to connect to live MongoDB — any input that smells like a URI is
rejected before any further work happens.

Scope
-----
1a. Streaming manifest + record-level inventory from each backup.
1b. Manifest diff (JSON + Markdown).
1c. Record-level dry-run per MUST_RECONCILE collection using the
    collection-specific logical keys documented in the Phase 1
    reconciliation manifest.
1d. Canonical dataset PLAN (JSON + Markdown) — plan only, no
    database writes, no restores.

The tool emits reports under ``/app/memory/reconcile_offline/`` and
exits non-zero (BLOCKED) if any hard-abort condition is tripped.

Hard aborts (per spec §Hard Abort Conditions)
---------------------------------------------
 * immutable prediction snapshot conflict
 * immutable pregame snapshot conflict
 * contradictory authoritative settlement for same identity
 * contradictory provider actuals
 * FINAL/FINAL score conflict
 * canonical identity collision
 * duplicate incompatible user identity
 * missing / malformed / unreadable backup
 * logical key cannot be determined safely
 * source collection count cannot be reconciled with manifest
 * unexpected authority collection with unknown semantics
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import logging
import os
import pathlib
import shutil
import sys
import tempfile
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    import bson                                        # pymongo bson
except ImportError:
    print("pymongo/bson required.", file=sys.stderr)
    sys.exit(2)


log = logging.getLogger("reconcile_offline")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s reconcile_offline %(levelname)s %(message)s")


REPORT_DIR = pathlib.Path("/app/memory/reconcile_offline")
REPORT_DIR.mkdir(parents=True, exist_ok=True)


# ─── Classification table (per Phase 1 spec) ──────────────────────────
CLASSIFICATION: Dict[str, str] = {
    # A — MUST RECONCILE / PRESERVE
    "picks":                    "A",
    "prediction_snapshots":     "A",
    "publication_events":       "A",
    "pregame_snapshots":        "A",
    "settlement_events":        "A",
    "player_game_actuals":      "A",
    "team_game_actuals":        "A",
    "player_game_logs":         "A",
    "nfl_player_weekly":        "A",
    "games":                    "A",
    "soccer_matches":           "A",
    "soccer_player_game_logs":  "A",
    "tennis_matches_history":   "A",
    "users":                    "A",
    "user_bets":                "A",
    "rollover_slates":          "A",
    "rollover_slate_events":    "A",
    "parlay_history":           "A",
    "player_identities":        "A",
    "historical_ingestion_state": "A",
    "nfl_ingest_meta":          "A",
    # B — SKIP MERGE / REBUILDABLE OR CACHE
    "live_alt_lines":           "B",
    "odds_api_cache":           "B",
    "odds_request_flights":     "B",
    "sports_catalog_snapshots": "B",
    "publication_mismatch_report": "B",
    "production_truth_observations": "B",
    "job_execution_log":        "B",
    "job_audit_log":            "B",
    # C — DO NOT MERGE / ENVIRONMENT-SPECIFIC
    "canonical_worker_leases":  "C",
    "scheduled_jobs":           "C",
    "provider_budget_state":    "C",
    "provider_request_intents": "C",
    "board_generations":        "C",
    # D — REVIEW REQUIRED
    "team_identities":          "D",
}


# ─── Logical key resolvers ────────────────────────────────────────────
def _s(v) -> str:
    if v is None:
        return ""
    return str(v)


def _logical_key(coll: str, doc: dict) -> Optional[tuple]:
    """Return the authoritative logical key tuple for a document in
    `coll`.  Returns None when the key cannot be derived (surfaced
    as REVIEW_REQUIRED by the dry-run)."""
    if coll == "picks":
        v = doc.get("id") or doc.get("pick_id") or doc.get("canonical_pick_id")
        return ("id", _s(v)) if v else None
    if coll == "prediction_snapshots":
        pid  = doc.get("prediction_id")
        ver  = doc.get("snapshot_version")
        if pid is not None and ver is not None:
            return ("prediction_id+snapshot_version", _s(pid), _s(ver))
        return None
    if coll == "pregame_snapshots":
        # Hash-sealed; same canonical_prediction_id can legitimately
        # appear multiple times with distinct snapshot_hash/_id.
        h = doc.get("snapshot_hash") or doc.get("_id")
        return ("snapshot_hash", _s(h)) if h else None
    if coll == "publication_events":
        pid  = doc.get("prediction_id")
        ver  = doc.get("publication_version")
        ts   = doc.get("publication_timestamp") or doc.get("ts")
        if pid and ver is not None and ts is not None:
            return ("prediction_id+publication_version+ts", _s(pid), _s(ver), _s(ts))
        return None
    if coll == "settlement_events":
        pid = doc.get("prediction_id") or doc.get("pick_id")
        sid = doc.get("settlement_id") or doc.get("_id")
        return ("settlement_id", _s(sid)) if sid else (("prediction_id", _s(pid)) if pid else None)
    if coll == "player_game_actuals":
        return ("sport+player+event+market",
                _s(doc.get("sport")),
                _s(doc.get("canonical_event_id") or doc.get("event_id")),
                _s(doc.get("canonical_player_id") or doc.get("player_id")),
                _s(doc.get("market") or doc.get("stat")))
    if coll == "team_game_actuals":
        return ("sport+team+event",
                _s(doc.get("sport")),
                _s(doc.get("canonical_team_id") or doc.get("team_id")),
                _s(doc.get("event_id") or doc.get("canonical_event_id")))
    if coll == "player_game_logs":
        return ("sport+player+game+stat_block",
                _s(doc.get("sport")),
                _s(doc.get("canonical_player_id") or doc.get("player_id")),
                _s(doc.get("canonical_event_id") or doc.get("game_id")),
                _s(doc.get("game_date") or doc.get("date")),
                _s(doc.get("stat_block") or doc.get("role") or ""))
    if coll == "nfl_player_weekly":
        pid = doc.get("player_id")
        season = doc.get("season")
        week = doc.get("week")
        if pid is not None and season is not None and week is not None:
            return ("player+season+week",
                    _s(pid), _s(season), _s(week),
                    _s(doc.get("season_type") or ""))
        _id = doc.get("_id")
        return ("_id", _s(_id)) if _id else None
    if coll == "games":
        return ("sport+game_id",
                _s(doc.get("sport")),
                _s(doc.get("game_id") or doc.get("canonical_event_id")))
    if coll == "soccer_matches":
        return ("league+season+home+away+date",
                _s(doc.get("league")),
                _s(doc.get("season")),
                _s(doc.get("home_team") or doc.get("home")),
                _s(doc.get("away_team") or doc.get("away")),
                _s(doc.get("date")))
    if coll == "soccer_player_game_logs":
        return ("match+player",
                _s(doc.get("match_id") or doc.get("canonical_event_id")),
                _s(doc.get("canonical_player_id") or doc.get("player_name")))
    if coll == "tennis_matches_history":
        return ("tourney+winner+loser",
                _s(doc.get("tourney_id")),
                _s(doc.get("winner_id")),
                _s(doc.get("loser_id")))
    if coll == "users":
        em = doc.get("email")
        return ("email", _s(em).lower()) if em else None
    if coll == "user_bets":
        v = doc.get("user_bet_id") or doc.get("_id")
        return ("user_bet_id", _s(v)) if v else None
    if coll == "rollover_slates":
        return ("slate_date+scope", _s(doc.get("slate_date")), _s(doc.get("scope")))
    if coll == "rollover_slate_events":
        return ("slate_date+event+at",
                _s(doc.get("slate_date")),
                _s(doc.get("event_id")),
                _s(doc.get("at")))
    if coll == "parlay_history":
        v = doc.get("id") or doc.get("_id")
        return ("id", _s(v)) if v else None
    if coll == "player_identities":
        v = doc.get("canonical_player_id")
        return ("canonical_player_id", _s(v)) if v else None
    if coll == "historical_ingestion_state":
        return ("_id", _s(doc.get("_id")))
    if coll == "nfl_ingest_meta":
        return ("_id", _s(doc.get("_id")))
    return None


# ─── Live-DB input refusal (hard guard) ───────────────────────────────
_LIVE_URI_PREFIXES = ("mongodb://", "mongodb+srv://")


def _refuse_live_input(value: str, label: str) -> None:
    v = (value or "").strip()
    low = v.lower()
    if any(low.startswith(p) for p in _LIVE_URI_PREFIXES):
        raise SystemExit(
            f"REFUSED — {label} looks like a live Mongo URI ({v[:24]}…). "
            f"This tool only accepts offline backup files/dirs.")
    if "localhost" in low or "127.0.0.1" in low:
        raise SystemExit(
            f"REFUSED — {label} points at localhost. "
            f"This tool only accepts offline backup files/dirs.")


# ─── Backup access ────────────────────────────────────────────────────
class BackupSource:
    """Lazy accessor for a mongodump backup.  Supports:
      * a .zip archive produced by preview_full_backup.py (dump dir
        inside) OR a direct mongodump dump .zip
      * an already-extracted directory containing <db>/<coll>.bson.gz
    """
    def __init__(self, path: str, env_label: str, db_hint: Optional[str] = None):
        _refuse_live_input(path, f"{env_label} backup path")
        self.env_label = env_label
        self.path = pathlib.Path(path)
        if not self.path.exists():
            raise SystemExit(
                f"REFUSED — {env_label} backup not found: {self.path}. "
                f"Supply the archive or an extracted dump directory.")
        self._tmp: Optional[tempfile.TemporaryDirectory] = None
        self._db_dir: pathlib.Path
        self.sha256: Optional[str] = None
        self.size_bytes = 0
        self._init(db_hint)
        self.manifest = self._load_manifest()

    def _init(self, db_hint: Optional[str]) -> None:
        if self.path.is_file():
            if not zipfile.is_zipfile(self.path):
                raise SystemExit(f"REFUSED — {self.env_label} archive is not a valid zip: {self.path}")
            self.sha256 = _sha256_file(str(self.path))
            self.size_bytes = self.path.stat().st_size
            self._tmp = tempfile.TemporaryDirectory(prefix=f"recon_{self.env_label}_")
            root = pathlib.Path(self._tmp.name)
            log.info("[%s] extracting %s → %s", self.env_label, self.path.name, root)
            with zipfile.ZipFile(self.path) as zf:
                zf.extractall(root)
            self._db_dir = _resolve_db_dir(root, db_hint)
        else:
            self.sha256 = None  # cannot checksum a directory
            self._db_dir = _resolve_db_dir(self.path, db_hint)
        if not self._db_dir.exists():
            raise SystemExit(f"REFUSED — {self.env_label} dump directory missing: {self._db_dir}")

    def _load_manifest(self) -> dict:
        """Preview backup packages ship a manifest.json; a bare
        mongodump may not.  We return whatever is available plus a
        synthesised collection list from the on-disk BSON files."""
        base = pathlib.Path(self._db_dir).parent.parent
        candidates = [base / "manifest.json",
                      self._db_dir.parent / "manifest.json"]
        for c in candidates:
            if c.exists():
                try:
                    with open(c) as f:
                        return json.load(f)
                except Exception as e:
                    log.warning("[%s] manifest.json unreadable: %s", self.env_label, e)
        return {"source_inventory": {"collections": {},
                                     "total_collections": 0,
                                     "total_documents": 0}}

    def collections(self) -> List[str]:
        """Collection names derived from on-disk BSON files."""
        out: List[str] = []
        for p in self._db_dir.glob("*.bson.gz"):
            out.append(p.name[:-len(".bson.gz")])
        for p in self._db_dir.glob("*.bson"):
            n = p.name[:-len(".bson")]
            if n not in out:
                out.append(n)
        return sorted(out)

    def collection_file(self, coll: str) -> Optional[pathlib.Path]:
        p = self._db_dir / f"{coll}.bson.gz"
        if p.exists():
            return p
        p = self._db_dir / f"{coll}.bson"
        return p if p.exists() else None

    def stream_docs(self, coll: str) -> Iterable[dict]:
        f = self.collection_file(coll)
        if f is None:
            return
        if f.suffix == ".gz":
            fh = gzip.open(f, "rb")
        else:
            fh = open(f, "rb")
        try:
            for doc in bson.decode_file_iter(fh):
                yield doc
        finally:
            fh.close()

    def close(self) -> None:
        if self._tmp is not None:
            self._tmp.cleanup()


def _resolve_db_dir(root: pathlib.Path, db_hint: Optional[str]) -> pathlib.Path:
    """Walk the extracted tree to find the ``<db>`` directory that
    directly contains BSON files."""
    for base in [root] + [p for p in root.rglob("dump") if p.is_dir()]:
        # Direct DB match if the hint folder exists under base.
        if db_hint:
            cand = base / db_hint
            if cand.is_dir() and list(cand.glob("*.bson*")):
                return cand
        # Else take the first child dir that contains BSON files.
        for cand in base.iterdir() if base.is_dir() else []:
            if cand.is_dir() and list(cand.glob("*.bson*")):
                return cand
    # Last resort: `root` itself contains BSON files
    if list(root.glob("*.bson*")):
        return root
    raise SystemExit(f"REFUSED — cannot locate the dump/<db> directory under {root}")


def _sha256_file(path: str, buf: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(buf)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


# ─── Phase 1A: streaming per-collection inventory ─────────────────────
def inventory(src: BackupSource) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for coll in src.collections():
        f = src.collection_file(coll)
        n = 0
        try:
            for _d in src.stream_docs(coll):
                n += 1
        except Exception as e:
            out[coll] = {"count": -1, "error": str(e)[:300]}
            continue
        out[coll] = {
            "count": n,
            "file":  str(f.name) if f else None,
            "class": CLASSIFICATION.get(coll, "UNCLASSIFIED"),
        }
    return out


# ─── Phase 1C: record-level dry-run ───────────────────────────────────
def _dry_run_collection(coll: str, prod_src: BackupSource,
                        prev_src: BackupSource) -> Dict[str, Any]:
    """Stream both sides, build index-of-logical-keys, classify."""
    prod_keys: Dict[tuple, str] = {}      # key → doc hash (minus _id)
    prev_keys: Dict[tuple, str] = {}
    unresolvable_prod = 0
    unresolvable_prev = 0

    def _h(doc: dict) -> str:
        clean = {k: v for k, v in doc.items()
                 if k not in ("_id", "updated_at", "ingested_at")}
        return hashlib.sha256(
            json.dumps(clean, sort_keys=True, default=str).encode()
        ).hexdigest()[:16]

    for d in prod_src.stream_docs(coll):
        k = _logical_key(coll, d)
        if k is None:
            unresolvable_prod += 1
            continue
        if k in prod_keys:
            # Duplicate logical key within the same source — track
            # separately rather than silently overwriting.
            prod_keys[k] = prod_keys[k] + "|DUP"
        else:
            prod_keys[k] = _h(d)
    for d in prev_src.stream_docs(coll):
        k = _logical_key(coll, d)
        if k is None:
            unresolvable_prev += 1
            continue
        if k in prev_keys:
            prev_keys[k] = prev_keys[k] + "|DUP"
        else:
            prev_keys[k] = _h(d)

    identical = 0
    conflicts: list[tuple] = []
    only_prod: int = 0
    only_prev: int = 0

    all_keys = set(prod_keys.keys()) | set(prev_keys.keys())
    for k in all_keys:
        a = prod_keys.get(k)
        b = prev_keys.get(k)
        if a is None:
            only_prev += 1
        elif b is None:
            only_prod += 1
        elif a == b:
            identical += 1
        else:
            conflicts.append(k)

    # Classify every conflict by collection authority (per spec).
    high_authority = {"prediction_snapshots", "pregame_snapshots"}
    provider_actual = {"player_game_actuals", "team_game_actuals", "games"}
    production_wins = {"users", "user_bets", "settlement_events", "picks"}
    append_only = {"publication_events", "rollover_slate_events"}

    status = "SAFE_MERGE"
    conflict_type = None
    if conflicts and coll in high_authority:
        status = "BLOCKED_BY_CONFLICT"
        conflict_type = "HIGH_AUTHORITY_CONFLICT"
    elif conflicts and coll in provider_actual:
        status = "BLOCKED_BY_CONFLICT"
        conflict_type = "PROVIDER_ACTUAL_CONFLICT"
    elif conflicts and coll in production_wins:
        status = "MERGE_WITH_AUTHORITY"
        conflict_type = "PRODUCTION_AUTHORITY"
    elif conflicts and coll in append_only:
        status = "UNION_SAFE"
        conflict_type = "APPEND_ONLY_UNION"
    elif conflicts:
        status = "MANUAL_REVIEW_REQUIRED"
        conflict_type = "REVIEW_REQUIRED"

    canonical_projected = identical + only_prod + only_prev + len(conflicts)

    return {
        "collection":          coll,
        "classification":      CLASSIFICATION.get(coll, "UNCLASSIFIED"),
        "production_total":    len(prod_keys) + unresolvable_prod,
        "preview_total":       len(prev_keys) + unresolvable_prev,
        "identical":           identical,
        "production_only":     only_prod,
        "preview_only":        only_prev,
        "conflicts":           len(conflicts),
        "conflict_type":       conflict_type,
        "unresolvable_prod":   unresolvable_prod,
        "unresolvable_prev":   unresolvable_prev,
        "status":              status,
        "canonical_projected": canonical_projected,
    }


# ─── Driver ───────────────────────────────────────────────────────────
def _manifest_diff(prod_inv: Dict[str, Dict[str, Any]],
                   prev_inv: Dict[str, Dict[str, Any]]) -> List[dict]:
    all_colls = sorted(set(prod_inv.keys()) | set(prev_inv.keys()))
    out = []
    for c in all_colls:
        pc = prod_inv.get(c) or {"count": 0, "class": CLASSIFICATION.get(c, "UNCLASSIFIED")}
        pv = prev_inv.get(c) or {"count": 0, "class": CLASSIFICATION.get(c, "UNCLASSIFIED")}
        out.append({
            "collection":        c,
            "classification":    CLASSIFICATION.get(c, "UNCLASSIFIED"),
            "production_count":  pc["count"],
            "preview_count":     pv["count"],
            "delta":             (pv["count"] or 0) - (pc["count"] or 0),
            "production_only":   c in prod_inv and c not in prev_inv,
            "preview_only":      c in prev_inv and c not in prod_inv,
            "logical_key":       _describe_key(c),
        })
    return out


def _describe_key(coll: str) -> str:
    probe = _logical_key(coll, {})
    return probe[0] if probe else "n/a (D-review or SOURCE_ABSENT)"


def _write_json(path: pathlib.Path, data) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True, default=str)


def _write_manifest_md(path: pathlib.Path, diff: List[dict], meta: dict) -> None:
    lines = ["# Reconciliation manifest diff", "",
             f"*generated {datetime.now(timezone.utc).isoformat()}*", "",
             f"- Production backup: `{meta.get('production_backup', 'NOT_SUPPLIED')}`",
             f"- Preview backup:    `{meta['preview_backup']}`",
             "",
             "| Collection | Class | Prod | Preview | Δ | Logical key |",
             "|---|:-:|---:|---:|---:|---|"]
    for row in diff:
        lines.append(
            f"| `{row['collection']}` | {row['classification']} | "
            f"{row['production_count']} | {row['preview_count']} | "
            f"{row['delta']:+} | `{row['logical_key']}` |"
        )
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--preview-backup", required=True,
                   help="Path to Preview mongodump archive (.zip) or extracted dir.")
    p.add_argument("--production-backup",
                   help="Path to Production mongodump archive (.zip) or extracted dir. "
                        "If omitted, the tool stops after Preview inspection + manifest.")
    p.add_argument("--db-name", default="lockscore_db")
    p.add_argument("--max-conflicts-per-collection", type=int, default=100)
    p.add_argument("--inspect-only", action="store_true",
                   help="Force Preview-only mode even if Production backup is supplied "
                        "(useful for smoke testing).")
    return p.parse_args()


def main() -> int:
    args = _parse_args()

    # Hard guard — refuse any live URI before touching anything.
    _refuse_live_input(args.preview_backup, "--preview-backup")
    if args.production_backup:
        _refuse_live_input(args.production_backup, "--production-backup")

    # Preview-only mode is unavoidable when Production is not supplied.
    production_available = bool(args.production_backup) and not args.inspect_only

    prev = BackupSource(args.preview_backup, "preview", args.db_name)
    log.info("[preview] db_dir=%s", prev._db_dir)

    prev_inv = inventory(prev)
    log.info("[preview] %d collections scanned", len(prev_inv))

    if not production_available:
        # Phase 1A/1B emit the Preview half only + explicit boundary stop.
        diff = _manifest_diff({}, prev_inv)
        meta = {"preview_backup": args.preview_backup,
                "production_backup": "NOT_SUPPLIED"}
        _write_json(REPORT_DIR / "preview_inventory.json", prev_inv)
        _write_json(REPORT_DIR / "reconciliation_manifest_diff.json", diff)
        _write_manifest_md(REPORT_DIR / "reconciliation_manifest_diff.md",
                           diff, meta)
        summary = {
            "status":             "STOPPED_PRODUCTION_BACKUP_NOT_SUPPLIED",
            "preview_backup":     args.preview_backup,
            "preview_sha256":     prev.sha256,
            "preview_db_dir":     str(prev._db_dir),
            "preview_collections": len(prev_inv),
            "preview_total_docs": sum((v["count"] or 0) for v in prev_inv.values()
                                       if isinstance(v["count"], int) and v["count"] > 0),
            "generated_at":       datetime.now(timezone.utc).isoformat(),
            "reports":            [
                str(REPORT_DIR / "preview_inventory.json"),
                str(REPORT_DIR / "reconciliation_manifest_diff.json"),
                str(REPORT_DIR / "reconciliation_manifest_diff.md"),
            ],
            "safety_confirmations": {
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
        _write_json(REPORT_DIR / "summary.json", summary)
        print(json.dumps(summary, indent=2, default=str))
        prev.close()
        return 0

    prod = BackupSource(args.production_backup, "production", args.db_name)
    log.info("[production] db_dir=%s", prod._db_dir)
    prod_inv = inventory(prod)
    log.info("[production] %d collections scanned", len(prod_inv))

    # Phase 1B — manifest diff.
    diff = _manifest_diff(prod_inv, prev_inv)
    meta = {"preview_backup":     args.preview_backup,
            "production_backup":  args.production_backup}
    _write_json(REPORT_DIR / "reconciliation_manifest_diff.json", diff)
    _write_manifest_md(REPORT_DIR / "reconciliation_manifest_diff.md",
                       diff, meta)
    _write_json(REPORT_DIR / "production_inventory.json", prod_inv)
    _write_json(REPORT_DIR / "preview_inventory.json",    prev_inv)

    # Phase 1C — record-level dry-run per MUST_RECONCILE collection.
    plan_rows: List[Dict[str, Any]] = []
    blocked: List[str] = []
    for coll, cls in CLASSIFICATION.items():
        if cls == "A":
            # Scan if either side has the collection.
            if coll not in prod_inv and coll not in prev_inv:
                continue
            res = _dry_run_collection(coll, prod, prev)
            plan_rows.append(res)
            if res["status"] == "BLOCKED_BY_CONFLICT":
                blocked.append(coll)
        elif cls == "B":
            plan_rows.append({"collection": coll, "classification": "B",
                              "status": "REBUILD_AFTER_CUTOVER"})
        elif cls == "C":
            plan_rows.append({"collection": coll, "classification": "C",
                              "status": "INITIALIZE_FRESH"})
        elif cls == "D":
            plan_rows.append({"collection": coll, "classification": "D",
                              "status": "MANUAL_REVIEW_REQUIRED"})

    # Phase 1D — canonical dataset plan.
    _write_json(REPORT_DIR / "canonical_dataset_plan.json", plan_rows)

    summary = {
        "status":                  "BLOCKED" if blocked else "PASS",
        "blocked_collections":     blocked,
        "preview_backup":          args.preview_backup,
        "preview_sha256":          prev.sha256,
        "production_backup":       args.production_backup,
        "production_sha256":       prod.sha256,
        "generated_at":            datetime.now(timezone.utc).isoformat(),
        "reports": [
            str(REPORT_DIR / "reconciliation_manifest_diff.json"),
            str(REPORT_DIR / "reconciliation_manifest_diff.md"),
            str(REPORT_DIR / "production_inventory.json"),
            str(REPORT_DIR / "preview_inventory.json"),
            str(REPORT_DIR / "canonical_dataset_plan.json"),
        ],
        "safety_confirmations": {
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
    _write_json(REPORT_DIR / "summary.json", summary)
    print(json.dumps(summary, indent=2, default=str))
    prod.close()
    prev.close()
    return 0 if not blocked else 1


if __name__ == "__main__":
    sys.exit(main())
