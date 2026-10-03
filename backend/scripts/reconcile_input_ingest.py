"""reconcile_input_ingest — incremental Production backup-file ingest.

Lets the operator upload individual Production JSON files from
iPhone/iPad (one at a time, over multiple chat turns) into:

    /tmp/production_reconcile_input/

For every file dropped there, this tool:

 1. Parses it as either a JSON array (preferred) or NDJSON.
 2. Identifies the Mongo collection from the filename.
 3. Counts records.
 4. Computes SHA-256 of the on-disk file (never alters the file).
 5. Detects duplicate ``_id`` across supplied parts of the same
    collection.
 6. Detects duplicate *logical key* across supplied parts (per
    collection; uses the same resolvers as reconcile_offline.py).
 7. Tracks part completeness (``<coll>_part1.json``, …) and compares
    received totals against ``production_count_known`` in the
    transfer manifest to decide collection-level READY status.
 8. NEVER connects to MongoDB.  NEVER writes anything to the DB.
 9. Updates ``/app/memory/reconcile_offline/production_input_inventory.json``
    so the next upload turn resumes from the previous state.

Accepted filename patterns (ORIGINAL Production export filenames —
no slim projection required, no re-export required)::

    <collection>.json
    <collection>.json.gz
    <collection>.ndjson
    <collection>.ndjson.gz
    <collection>_part1.json
    <collection>_part2.json.gz
    <collection>_part3.ndjson
    …
    <collection>_slim.json        (slim projection — OPTIONAL)
    <collection>_slim_part1.json  (slim projection — OPTIONAL)
    …
    <collection>.complete.json    (completion sentinel — ONLY required
                                   when production_count_known is null
                                   in the manifest; contents:
                                   {"total_parts": N})
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import pathlib
import re
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple


INPUT_DIR  = pathlib.Path("/tmp/production_reconcile_input")
INVENTORY  = pathlib.Path("/app/memory/reconcile_offline/production_input_inventory.json")
MANIFEST   = pathlib.Path("/app/memory/reconcile_offline/production_transfer_manifest.json")

INPUT_DIR.mkdir(parents=True, exist_ok=True)
INVENTORY.parent.mkdir(parents=True, exist_ok=True)

# Order matters — `.complete.json` and `.complete` must be matched
# before the generic `<coll>.json` pattern.
_COMPLETE_RE = re.compile(
    r"^(?P<coll>[a-zA-Z0-9_]+?)\.complete(?:\.json)?$"
)
_PART_RE = re.compile(
    r"^(?P<coll>[a-zA-Z0-9_]+?)(?:_slim)?(?:_part(?P<part>\d+))?\.(?P<ext>json(?:\.gz)?|ndjson(?:\.gz)?)$"
)


# ─── Live-input guard ────────────────────────────────────────────────
def _refuse_live(value: str) -> None:
    low = (value or "").lower()
    if low.startswith(("mongodb://", "mongodb+srv://")) \
            or "localhost" in low or "127.0.0.1" in low:
        raise SystemExit(f"REFUSED — live DB input not allowed: {value}")


# ─── Logical-key resolver ────────────────────────────────────────────
# Mirrors scripts/reconcile_offline._logical_key.  Kept inline instead
# of imported so the ingest tool has zero runtime dependency on bson /
# pymongo (reconcile_offline.py imports bson at module load).
def _s(v) -> str:
    return "" if v is None else str(v)


def _logical_key(coll: str, doc: dict) -> Optional[tuple]:
    if coll == "picks":
        v = doc.get("id") or doc.get("pick_id") or doc.get("canonical_pick_id")
        return ("id", _s(v)) if v else None
    if coll == "prediction_snapshots":
        pid, ver = doc.get("prediction_id"), doc.get("snapshot_version")
        if pid is not None and ver is not None:
            return ("prediction_id+snapshot_version", _s(pid), _s(ver))
        return None
    if coll == "pregame_snapshots":
        h = doc.get("snapshot_hash") or doc.get("_id")
        return ("snapshot_hash", _s(h)) if h else None
    if coll == "publication_events":
        pid, ver = doc.get("prediction_id"), doc.get("publication_version")
        ts = doc.get("at") or doc.get("publication_timestamp") or doc.get("ts")
        if pid and ver is not None and ts is not None:
            return ("prediction_id+publication_version+at", _s(pid), _s(ver), _s(ts))
        oid = doc.get("_id")
        if pid and ver is not None and oid is not None:
            return ("prediction_id+publication_version+_id", _s(pid), _s(ver), _s(oid))
        return None
    if coll == "settlement_events":
        sid = doc.get("settlement_id") or doc.get("_id")
        pid = doc.get("prediction_id") or doc.get("pick_id")
        if sid:
            return ("settlement_id", _s(sid))
        if pid:
            return ("prediction_id", _s(pid))
        return None
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
        pid    = doc.get("player_id")
        season = doc.get("season")
        week   = doc.get("week")
        if pid is not None and season is not None and week is not None:
            return ("player+season+week", _s(pid), _s(season), _s(week),
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
        v = doc.get("id") or doc.get("user_bet_id") or doc.get("_id")
        return ("id", _s(v)) if v else None
    if coll == "rollover_slates":
        return ("slate_date+scope", _s(doc.get("slate_date")), _s(doc.get("scope")))
    if coll == "rollover_slate_events":
        return ("slate_date+event+at",
                _s(doc.get("slate_date")),
                _s(doc.get("event") or doc.get("event_id")),
                _s(doc.get("at")))
    if coll == "parlay_history":
        v = doc.get("id") or doc.get("_id")
        return ("id", _s(v)) if v else None
    if coll == "player_identities":
        v = doc.get("canonical_player_id")
        return ("canonical_player_id", _s(v)) if v else None
    if coll in ("historical_ingestion_state", "nfl_ingest_meta"):
        return ("_id", _s(doc.get("_id")))
    return None


# ─── File I/O helpers ────────────────────────────────────────────────
def _sha256(path: pathlib.Path, buf: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            c = f.read(buf)
            if not c:
                break
            h.update(c)
    return h.hexdigest()


def _open_stream(path: pathlib.Path):
    if str(path).endswith(".gz"):
        return gzip.open(path, "rt")
    return open(path, "r")


def _iter_docs(path: pathlib.Path) -> Iterable[dict]:
    """Yield one doc at a time.  Preferred: JSON array.  Fallback:
    NDJSON (one object per line)."""
    # Peek first non-whitespace char.
    f = _open_stream(path)
    try:
        while True:
            c = f.read(1)
            if not c:
                f.close()
                return
            if not c.isspace():
                first = c
                break
    except Exception:
        try:
            f.close()
        except Exception:
            pass
        return
    f.close()
    f = _open_stream(path)
    try:
        if first == "[":
            data = json.load(f)
            if isinstance(data, list):
                for d in data:
                    if isinstance(d, dict):
                        yield d
            return
        if first == "{":
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    yield json.loads(ln)
                except Exception:
                    continue
            return
    finally:
        try:
            f.close()
        except Exception:
            pass


# ─── Filename parsing ────────────────────────────────────────────────
def _parse_filename(name: str) -> Optional[Tuple[str, Optional[int], str]]:
    """Return (collection, part_number or None, kind) where kind is
    'data' or 'sentinel'.  Return None if filename unrecognised."""
    m = _COMPLETE_RE.match(name)
    if m:
        return (m.group("coll"), None, "sentinel")
    m = _PART_RE.match(name)
    if m:
        return (m.group("coll"),
                int(m.group("part")) if m.group("part") else None,
                "data")
    return None


# ─── Manifest loading ────────────────────────────────────────────────
def _load_manifest() -> Dict[str, Any]:
    if not MANIFEST.exists():
        return {"collections": []}
    try:
        with open(MANIFEST) as f:
            return json.load(f)
    except Exception:
        return {"collections": []}


def _manifest_entry(mf: Dict[str, Any], coll: str) -> Optional[Dict[str, Any]]:
    for row in mf.get("collections", []):
        if row.get("collection") == coll:
            return row
    return None


def _required_collections(mf: Dict[str, Any]) -> List[str]:
    out = []
    for row in mf.get("collections", []):
        if row.get("action") == "REQUIRED_FULL_DATA":
            out.append(row["collection"])
    return out


# ─── Inventory ───────────────────────────────────────────────────────
def _load_inventory() -> Dict[str, Any]:
    if INVENTORY.exists():
        try:
            with open(INVENTORY) as f:
                return json.load(f)
        except Exception:
            pass
    return {"files": {}, "collections": {}, "generated_at": None}


def _save_inventory(inv: Dict[str, Any]) -> None:
    inv["generated_at"] = datetime.now(timezone.utc).isoformat()
    with open(INVENTORY, "w") as f:
        json.dump(inv, f, indent=2, sort_keys=True, default=str)


# ─── Core ingest ─────────────────────────────────────────────────────
def _ingest_sentinel(path: pathlib.Path, coll: str,
                     inv: Dict[str, Any]) -> Dict[str, Any]:
    try:
        with open(path) as f:
            payload = json.load(f)
        total_parts = int(payload.get("total_parts"))
    except Exception as e:
        return {"file": path.name, "status": "SENTINEL_PARSE_FAILED",
                "collection": coll, "err": str(e)[:200]}
    sha = _sha256(path)
    inv["files"][path.name] = {
        "file":          path.name,
        "collection":    coll,
        "sentinel":      True,
        "total_parts":   total_parts,
        "sha256":        sha,
        "size_bytes":    path.stat().st_size,
        "ingested_at":   datetime.now(timezone.utc).isoformat(),
    }
    cstate = inv["collections"].setdefault(coll, _empty_coll_state())
    cstate["total_parts_declared"] = total_parts
    return {"file": path.name, "status": "SENTINEL_INGESTED",
            "collection": coll, "total_parts_declared": total_parts}


def _empty_coll_state() -> Dict[str, Any]:
    return {
        "parts":                       [],
        "total_docs":                  0,
        "unique_mongo_ids":            0,
        "duplicate_ids_across_parts":  [],
        "ids_seen":                    [],
        "unique_logical_keys":         0,
        "duplicate_logical_keys_across_parts": [],
        "logical_keys_seen":           [],
        "docs_without_logical_key":    0,
        "total_parts_declared":        None,
    }


def _ingest_data(path: pathlib.Path, coll: str, part: Optional[int],
                 inv: Dict[str, Any]) -> Dict[str, Any]:
    rel = path.name
    sha = _sha256(path)
    size = path.stat().st_size

    prev = inv["files"].get(rel)
    if prev and prev.get("sha256") == sha:
        return {"file": rel, "status": "ALREADY_INGESTED",
                "collection": coll, "part": part, "sha256": sha}

    n = 0
    _id_set: dict[str, int] = {}
    lk_set:  dict[str, int] = {}
    dup_ids_in_file: list[str] = []
    dup_lks_in_file: list[str] = []
    no_lk = 0
    try:
        for d in _iter_docs(path):
            n += 1
            _id = d.get("_id")
            if _id is not None:
                key = str(_id)
                if key in _id_set:
                    dup_ids_in_file.append(key)
                _id_set[key] = _id_set.get(key, 0) + 1
            lk = _logical_key(coll, d)
            if lk is None:
                no_lk += 1
            else:
                lk_key = json.dumps(lk, default=str, sort_keys=True)
                if lk_key in lk_set:
                    dup_lks_in_file.append(lk_key)
                lk_set[lk_key] = lk_set.get(lk_key, 0) + 1
    except Exception as e:
        return {"file": rel, "status": "PARSE_FAILED",
                "collection": coll, "part": part, "err": str(e)[:300],
                "sha256": sha, "size_bytes": size}

    # Cross-part dedup.
    cstate = inv["collections"].setdefault(coll, _empty_coll_state())
    seen_ids  = set(cstate["ids_seen"])
    seen_lks  = set(cstate["logical_keys_seen"])
    dup_ids_across:  list[str] = []
    dup_lks_across:  list[str] = []
    for k in _id_set:
        if k in seen_ids:
            dup_ids_across.append(k)
        else:
            seen_ids.add(k)
    for k in lk_set:
        if k in seen_lks:
            dup_lks_across.append(k)
        else:
            seen_lks.add(k)

    cstate["ids_seen"]              = sorted(seen_ids)
    cstate["unique_mongo_ids"]      = len(seen_ids)
    cstate["duplicate_ids_across_parts"] = sorted(
        set(cstate["duplicate_ids_across_parts"]) | set(dup_ids_across)
    )
    cstate["logical_keys_seen"]     = sorted(seen_lks)
    cstate["unique_logical_keys"]   = len(seen_lks)
    cstate["duplicate_logical_keys_across_parts"] = sorted(
        set(cstate["duplicate_logical_keys_across_parts"]) | set(dup_lks_across)
    )
    cstate["parts"].append({"file": rel, "part": part,
                             "doc_count": n, "sha256": sha,
                             "docs_without_logical_key_in_file": no_lk,
                             "duplicate_logical_keys_within_file": len(dup_lks_in_file),
                             "duplicate_ids_within_file": len(dup_ids_in_file)})
    cstate["total_docs"]            = sum(p["doc_count"] for p in cstate["parts"])
    cstate["docs_without_logical_key"] = (
        cstate.get("docs_without_logical_key", 0) + no_lk
    )

    record = {
        "file":           rel,
        "collection":     coll,
        "part":           part,
        "sha256":         sha,
        "size_bytes":     size,
        "doc_count":      n,
        "duplicate_ids_within_file": len(dup_ids_in_file),
        "duplicate_logical_keys_within_file": len(dup_lks_in_file),
        "docs_without_logical_key_in_file": no_lk,
        "duplicate_ids_across_parts_contrib": len(dup_ids_across),
        "duplicate_logical_keys_across_parts_contrib": len(dup_lks_across),
        "ingested_at":    datetime.now(timezone.utc).isoformat(),
        "status":         "INGESTED",
    }
    inv["files"][rel] = record
    return record


def _ingest_file(path: pathlib.Path, inv: Dict[str, Any]) -> Dict[str, Any]:
    parsed = _parse_filename(path.name)
    if parsed is None:
        return {"file": path.name,
                "status": "SKIPPED_UNKNOWN_FILENAME_PATTERN"}
    coll, part, kind = parsed
    if kind == "sentinel":
        return _ingest_sentinel(path, coll, inv)
    return _ingest_data(path, coll, part, inv)


def _scan_once(inv: Dict[str, Any]) -> List[dict]:
    out: List[dict] = []
    for p in sorted(INPUT_DIR.iterdir()):
        if not p.is_file():
            continue
        if p.name in (".DS_Store", "README.md"):
            continue
        out.append(_ingest_file(p, inv))
    return out


# ─── Readiness evaluation ────────────────────────────────────────────
def _evaluate_collection_ready(coll: str, cstate: Dict[str, Any],
                               mf_row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    parts        = cstate.get("parts", [])
    expected_cnt = (mf_row or {}).get("production_count_known")
    declared     = cstate.get("total_parts_declared")
    numbered     = sorted({p["part"] for p in parts if p.get("part") is not None})
    unnumbered   = [p for p in parts if p.get("part") is None]

    reasons: list[str] = []
    ready = False

    # Rule (a) — exactly one standalone file.
    if len(unnumbered) == 1 and not numbered:
        ready = True
        reasons.append("standalone_file")
    # Rule (b) — multipart contiguous from 1..N with count-match.
    elif numbered:
        contiguous = numbered == list(range(1, len(numbered) + 1))
        if not contiguous:
            missing_parts = [i for i in range(1, (numbered[-1] or 0) + 1)
                             if i not in numbered]
            reasons.append(f"missing_parts={missing_parts}")
        if expected_cnt is not None:
            if cstate.get("total_docs", 0) == expected_cnt and contiguous:
                ready = True
                reasons.append("multipart_count_match")
            elif contiguous:
                reasons.append(
                    f"doc_count_mismatch(received={cstate.get('total_docs', 0)}, expected={expected_cnt})"
                )
        elif declared is not None:
            if contiguous and len(numbered) == declared:
                ready = True
                reasons.append("multipart_sentinel_satisfied")
            elif contiguous:
                reasons.append(f"awaiting_more_parts(received={len(numbered)}, declared={declared})")
        else:
            reasons.append("missing_sentinel_or_production_count_known")
    else:
        reasons.append("no_parts_ingested")

    # Any duplicates across parts BLOCK ready regardless of counts.
    if cstate.get("duplicate_ids_across_parts"):
        ready = False
        reasons.append(f"duplicate_ids_across_parts={len(cstate['duplicate_ids_across_parts'])}")
    if cstate.get("duplicate_logical_keys_across_parts"):
        ready = False
        reasons.append(
            f"duplicate_logical_keys_across_parts={len(cstate['duplicate_logical_keys_across_parts'])}"
        )

    return {"ready": ready, "reasons": reasons,
            "expected_doc_count": expected_cnt,
            "received_doc_count": cstate.get("total_docs", 0),
            "parts_received":     numbered,
            "unnumbered_files":   [p["file"] for p in unnumbered],
            "total_parts_declared": declared}


def _summary(inv: Dict[str, Any], mf: Dict[str, Any]) -> Dict[str, Any]:
    required = _required_collections(mf)
    coll_summaries: Dict[str, Any] = {}
    all_ready = True
    for c in sorted(set(required) | set(inv["collections"].keys())):
        cstate = inv["collections"].get(c) or _empty_coll_state()
        row = _manifest_entry(mf, c)
        rpt = _evaluate_collection_ready(c, cstate, row)
        rpt["required"]   = c in required
        rpt["parts"]      = len(cstate.get("parts", []))
        rpt["total_docs"] = cstate.get("total_docs", 0)
        rpt["unique_mongo_ids"] = cstate.get("unique_mongo_ids", 0)
        rpt["unique_logical_keys"] = cstate.get("unique_logical_keys", 0)
        rpt["duplicate_ids_across_parts"] = len(cstate.get("duplicate_ids_across_parts", []))
        rpt["duplicate_logical_keys_across_parts"] = len(
            cstate.get("duplicate_logical_keys_across_parts", []))
        rpt["docs_without_logical_key"] = cstate.get("docs_without_logical_key", 0)
        coll_summaries[c] = rpt
        if c in required and not rpt["ready"]:
            all_ready = False

    missing_required = [c for c in required
                        if c not in inv["collections"]
                        or not coll_summaries[c]["ready"]]
    return {
        "input_dir":                     str(INPUT_DIR),
        "inventory":                     str(INVENTORY),
        "manifest":                      str(MANIFEST),
        "files_present":                 len(inv["files"]),
        "required_collections":          required,
        "required_collections_ready":    all_ready,
        "collections_still_missing_or_incomplete": missing_required,
        "collections":                   coll_summaries,
    }


# ─── CLI ─────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--path", help="Optional single file to ingest.")
    ap.add_argument("--scan", action="store_true",
                    help="Scan the whole input dir (default when --path is omitted).")
    args = ap.parse_args()

    inv = _load_inventory()
    mf  = _load_manifest()

    results: list[dict] = []
    if args.path:
        _refuse_live(args.path)
        p = pathlib.Path(args.path)
        if not p.exists():
            print(f"REFUSED — path not found: {p}")
            return 2
        results.append(_ingest_file(p, inv))
    else:
        results.extend(_scan_once(inv))

    _save_inventory(inv)
    summary = _summary(inv, mf)
    summary["this_run"] = results
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
