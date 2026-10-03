"""phase3_apply_rules — Apply approved deterministic resolution rules
R1..R7 OFFLINE and rebuild the canonical output.

INPUT (read-only):
    /tmp/perklocks_reconciled_output/canonical/*.ndjson        (Phase 2 SAFE)
    /tmp/perklocks_reconciled_output/conflicts/*.ndjson        (Phase 2 conflict evidence)
    /tmp/perklocks_reconciled_output/review/*.ndjson           (Phase 2 review evidence)

OUTPUT (new location — Phase 2 is NOT overwritten):
    /tmp/perklocks_reconciled_output_phase3/
        canonical/<coll>.ndjson                — SAFE merged after R1..R7
        operator_review/<coll>.ndjson          — remaining unresolved rows
        resolution_ledger.ndjson               — one entry per rule application
        resolution_ledger_summary.json         — per-rule applied counts
        operator_evidence/<coll>.json          — compact grouped evidence
        integrity_report.json                  — all integrity checks
        _canonical_fingerprint.json            — deterministic hash per coll
"""
from __future__ import annotations

import collections
import hashlib
import json
import pathlib
import sys
from collections import Counter, defaultdict

PHASE2 = pathlib.Path("/tmp/perklocks_reconciled_output")
OUT    = pathlib.Path("/tmp/perklocks_reconciled_output_phase3")
OUT.mkdir(exist_ok=True)
(OUT / "canonical").mkdir(exist_ok=True)
(OUT / "operator_review").mkdir(exist_ok=True)
(OUT / "operator_evidence").mkdir(exist_ok=True)

# ─── Approved rule fields ────────────────────────────────────────────
R1_VERSION_FIELDS = {
    "model_version", "calibration_version", "feature_snapshot_version",
    "fusion_version", "scoring_version", "simulation_version",
    "validator_version", "board_version",
}
R2_PROVENANCE_FIELDS = {
    "backfill_version", "source", "source_player_id", "source_record_id",
    "ingested_at", "event_time_backfill_source",
    "opponent_enriched_at", "opponent_enrichment_source",
}
R3_ENRICHMENT_FIELDS = {
    "historical_teams", "historical_national_teams",
    "observed_at", "national_team_observed_at",
    "national_team_source", "national_team_status",
    "current_team", "current_national_team",
    "nationality", "aliases", "provider_ids",
    "position", "source", "name_norm",
}
R3_LIST_LIKE = {"historical_teams", "historical_national_teams",
                "aliases", "provider_ids"}
R3_TIMESTAMPS = {"observed_at", "national_team_observed_at"}
R5_ODDS_FIELDS = {
    "home_odds_open", "home_odds_close",
    "away_odds_open", "away_odds_close",
    "draw_odds_open", "draw_odds_close",
    "fetched_at",
}

RESOLUTION_LEDGER = []        # append per rule application
R_COUNTS = Counter()


# ─── IO helpers ──────────────────────────────────────────────────────
def load_pairs(path: pathlib.Path, types: set = None):
    pairs = defaultdict(dict)
    if not path.exists():
        return []
    with open(path) as f:
        for ln in f:
            r = json.loads(ln)
            if types and r.get("type") not in types:
                continue
            if r.get("side") in ("production", "preview"):
                pairs[tuple(r["logical_key"])][r["side"]] = r["doc"]
    return list(pairs.items())


def copy_phase2_canonical(coll: str, out_f):
    src = PHASE2 / "canonical" / f"{coll}.ndjson"
    if not src.exists():
        return 0
    n = 0
    with open(src) as f, open(out_f, "a") as o:
        for ln in f:
            o.write(ln)
            n += 1
    return n


def write_ledger_entry(entry: dict):
    RESOLUTION_LEDGER.append(entry)


def _diffs(p: dict, v: dict) -> set:
    return {f for f in set(p) | set(v) if f != "_id" and p.get(f) != v.get(f)}


# ─── R1 prediction_snapshots ─────────────────────────────────────────
def apply_r1():
    pairs = load_pairs(PHASE2 / "conflicts" / "prediction_snapshots.ndjson",
                       types={"IMMUTABLE_CONFLICT"})
    out_canon = open(OUT / "canonical" / "prediction_snapshots.ndjson", "a")
    out_review = open(OUT / "operator_review" / "prediction_snapshots.ndjson", "w")
    applied = 0; unresolved = 0; unresolved_rows = []
    for k, sides in pairs:
        p = sides.get("production") or {}
        v = sides.get("preview") or {}
        diffs = _diffs(p, v)
        if "payload_hash" in diffs and (diffs & R1_VERSION_FIELDS):
            applied += 1
            out_canon.write(json.dumps({"canonical_from": "R1_production_wins",
                                        "logical_key": list(k),
                                        "doc": p}, default=str) + "\n")
            write_ledger_entry({"rule": "R1", "collection": "prediction_snapshots",
                                "logical_key": list(k),
                                "selected_authority": "production",
                                "differing_fields": sorted(diffs),
                                "reason": "payload_hash differs AND >=1 version field advances"})
        else:
            unresolved += 1
            out_review.write(json.dumps({"side":"production","doc":p,"logical_key":list(k)}, default=str) + "\n")
            out_review.write(json.dumps({"side":"preview","doc":v,"logical_key":list(k)}, default=str) + "\n")
            unresolved_rows.append((k, p, v, diffs))
    out_canon.close(); out_review.close()
    R_COUNTS["R1"] = applied
    return applied, unresolved, unresolved_rows


# ─── R2 player_game_actuals ──────────────────────────────────────────
def apply_r2():
    pairs = load_pairs(PHASE2 / "review" / "player_game_actuals.ndjson",
                       types={"PROVIDER_ACTUAL_CONFLICT"})
    out_canon = open(OUT / "canonical" / "player_game_actuals.ndjson", "a")
    out_review = open(OUT / "operator_review" / "player_game_actuals.ndjson", "w")
    applied = 0; unresolved = 0; unresolved_rows = []
    for k, sides in pairs:
        p = sides.get("production") or {}
        v = sides.get("preview") or {}
        diffs = _diffs(p, v)
        actuals_equal = (p.get("actuals") == v.get("actuals"))
        if actuals_equal and diffs.issubset(R2_PROVENANCE_FIELDS):
            applied += 1
            out_canon.write(json.dumps({"canonical_from": "R2_production_provenance_wins",
                                        "logical_key": list(k),
                                        "doc": p}, default=str) + "\n")
            write_ledger_entry({"rule": "R2", "collection": "player_game_actuals",
                                "logical_key": list(k),
                                "selected_authority": "production",
                                "differing_fields": sorted(diffs),
                                "reason": "provenance-only diff; actuals identical"})
        else:
            unresolved += 1
            out_review.write(json.dumps({"side":"production","doc":p,"logical_key":list(k)}, default=str) + "\n")
            out_review.write(json.dumps({"side":"preview","doc":v,"logical_key":list(k)}, default=str) + "\n")
            unresolved_rows.append((k, p, v, diffs))
    out_canon.close(); out_review.close()
    R_COUNTS["R2"] = applied
    return applied, unresolved, unresolved_rows


# ─── R3 player_identities ────────────────────────────────────────────
def _r3_merge(p: dict, v: dict, diffs: set) -> dict:
    """Return a merged document.  Start with Production, overlay
    enrichment per R3 semantics."""
    merged = dict(p)
    for f in diffs:
        pv = p.get(f); vv = v.get(f)
        if f in R3_LIST_LIKE:
            # deterministic deduplicated union
            def _norm(x):
                if x is None: return []
                if isinstance(x, list): return x
                if isinstance(x, dict): return [x]
                return [x]
            unioned = []
            seen_keys = set()
            for item in _norm(pv) + _norm(vv):
                try:
                    k = json.dumps(item, sort_keys=True, default=str)
                except Exception:
                    k = str(item)
                if k in seen_keys: continue
                seen_keys.add(k); unioned.append(item)
            merged[f] = unioned
        elif f in R3_TIMESTAMPS:
            # keep the max (latest) timestamp — comparable as strings for ISO
            pick = max([x for x in (pv, vv) if x is not None], default=None)
            merged[f] = pick
        else:
            # scalar enrichment field: prefer populated over null, else Production
            if pv is None and vv is not None:
                merged[f] = vv
            elif pv is not None and vv is None:
                merged[f] = pv
            else:
                merged[f] = pv  # Production preferred deterministically
    return merged


def apply_r3():
    pairs = load_pairs(PHASE2 / "review" / "player_identities.ndjson",
                       types={"HISTORICAL_LOG_CONTENT_DIFF"})
    out_canon = open(OUT / "canonical" / "player_identities.ndjson", "a")
    out_review = open(OUT / "operator_review" / "player_identities.ndjson", "w")
    applied = 0; unresolved = 0; unresolved_rows = []
    for k, sides in pairs:
        p = sides.get("production") or {}
        v = sides.get("preview") or {}
        diffs = _diffs(p, v)
        if diffs.issubset(R3_ENRICHMENT_FIELDS):
            merged = _r3_merge(p, v, diffs)
            applied += 1
            out_canon.write(json.dumps({"canonical_from": "R3_union_enrichment",
                                        "logical_key": list(k),
                                        "doc": merged}, default=str) + "\n")
            write_ledger_entry({"rule": "R3", "collection": "player_identities",
                                "logical_key": list(k),
                                "selected_authority": "merged",
                                "differing_fields": sorted(diffs),
                                "reason": "enrichment-only diffs; deterministic union/populated-preference merge"})
        else:
            unresolved += 1
            out_review.write(json.dumps({"side":"production","doc":p,"logical_key":list(k)}, default=str) + "\n")
            out_review.write(json.dumps({"side":"preview","doc":v,"logical_key":list(k)}, default=str) + "\n")
            unresolved_rows.append((k, p, v, diffs))
    out_canon.close(); out_review.close()
    R_COUNTS["R3"] = applied
    return applied, unresolved, unresolved_rows


# ─── R4 player_game_logs ─────────────────────────────────────────────
def apply_r4():
    pairs = load_pairs(PHASE2 / "review" / "player_game_logs.ndjson",
                       types={"HISTORICAL_LOG_CONTENT_DIFF"})
    out_canon = open(OUT / "canonical" / "player_game_logs.ndjson", "a")
    out_review = open(OUT / "operator_review" / "player_game_logs.ndjson", "w")
    applied = 0; unresolved = 0; unresolved_rows = []
    for k, sides in pairs:
        p = sides.get("production") or {}
        v = sides.get("preview") or {}
        diffs = _diffs(p, v)
        if len(diffs) == 1:
            f = next(iter(diffs))
            pv, vv = p.get(f), v.get(f)
            if (pv is None) ^ (vv is None):
                # exactly one null, one populated
                base = dict(p)
                base[f] = pv if pv is not None else vv
                applied += 1
                out_canon.write(json.dumps({"canonical_from":"R4_prefer_populated",
                                            "logical_key": list(k),
                                            "doc": base,
                                            "field_populated_from":"production" if pv is not None else "preview"}, default=str) + "\n")
                write_ledger_entry({"rule": "R4", "collection": "player_game_logs",
                                    "logical_key": list(k),
                                    "selected_authority": "production" if pv is not None else "preview",
                                    "differing_fields": [f],
                                    "reason": f"single-field diff, one side null → prefer populated ({f})"})
                continue
        unresolved += 1
        out_review.write(json.dumps({"side":"production","doc":p,"logical_key":list(k)}, default=str) + "\n")
        out_review.write(json.dumps({"side":"preview","doc":v,"logical_key":list(k)}, default=str) + "\n")
        unresolved_rows.append((k, p, v, diffs))
    out_canon.close(); out_review.close()
    R_COUNTS["R4"] = applied
    return applied, unresolved, unresolved_rows


# ─── R5 soccer_matches ───────────────────────────────────────────────
def apply_r5():
    pairs = load_pairs(PHASE2 / "review" / "soccer_matches.ndjson",
                       types={"HISTORICAL_LOG_CONTENT_DIFF"})
    out_canon = open(OUT / "canonical" / "soccer_matches.ndjson", "a")
    out_review = open(OUT / "operator_review" / "soccer_matches.ndjson", "w")
    applied = 0; unresolved = 0; unresolved_rows = []
    for k, sides in pairs:
        p = sides.get("production") or {}
        v = sides.get("preview") or {}
        diffs = _diffs(p, v)
        if diffs and diffs.issubset(R5_ODDS_FIELDS):
            applied += 1
            out_canon.write(json.dumps({"canonical_from":"R5_production_odds_wins",
                                        "logical_key": list(k),
                                        "doc": p}, default=str) + "\n")
            write_ledger_entry({"rule": "R5", "collection": "soccer_matches",
                                "logical_key": list(k),
                                "selected_authority": "production",
                                "differing_fields": sorted(diffs),
                                "reason": "odds-snapshot only; score+status identical"})
        else:
            unresolved += 1
            out_review.write(json.dumps({"side":"production","doc":p,"logical_key":list(k)}, default=str) + "\n")
            out_review.write(json.dumps({"side":"preview","doc":v,"logical_key":list(k)}, default=str) + "\n")
            unresolved_rows.append((k, p, v, diffs))
    out_canon.close(); out_review.close()
    R_COUNTS["R5"] = applied
    return applied, unresolved, unresolved_rows


# ─── R6 + R7 games ───────────────────────────────────────────────────
def apply_r6_r7():
    pairs = load_pairs(PHASE2 / "review" / "games.ndjson",
                       types={"FINAL_RESULT_CONFLICT"})
    out_canon = open(OUT / "canonical" / "games.ndjson", "a")
    out_review = open(OUT / "operator_review" / "games.ndjson", "w")
    applied_r6 = 0; applied_r7 = 0; unresolved = 0; unresolved_rows = []
    for k, sides in pairs:
        p = sides.get("production") or {}
        v = sides.get("preview") or {}
        diffs = _diffs(p, v)

        # R6: date-only diff with identical result
        if diffs == {"date"} and p.get("result") == v.get("result") and p.get("status") == v.get("status"):
            applied_r6 += 1
            out_canon.write(json.dumps({"canonical_from":"R6_production_actual_date",
                                        "logical_key": list(k),
                                        "doc": p}, default=str) + "\n")
            write_ledger_entry({"rule": "R6", "collection": "games",
                                "logical_key": list(k),
                                "selected_authority": "production",
                                "differing_fields": ["date"],
                                "reason": "date-only reschedule; result + status identical"})
            continue

        # R7: both Final, one side null-result, the other populated
        p_final = (p.get("status") == "Final")
        v_final = (v.get("status") == "Final")
        p_res = p.get("result") or {}
        v_res = v.get("result") or {}
        p_null = p_res.get("home") is None and p_res.get("away") is None
        v_null = v_res.get("home") is None and v_res.get("away") is None
        if p_final and v_final and (p_null ^ v_null):
            populated_side = "preview" if p_null else "production"
            doc = v if p_null else p
            applied_r7 += 1
            out_canon.write(json.dumps({"canonical_from":"R7_prefer_populated_final",
                                        "logical_key": list(k),
                                        "doc": doc,
                                        "populated_side": populated_side}, default=str) + "\n")
            write_ledger_entry({"rule": "R7", "collection": "games",
                                "logical_key": list(k),
                                "selected_authority": populated_side,
                                "differing_fields": sorted(diffs),
                                "reason": "both Final; one side null-result"})
            continue

        unresolved += 1
        out_review.write(json.dumps({"side":"production","doc":p,"logical_key":list(k)}, default=str) + "\n")
        out_review.write(json.dumps({"side":"preview","doc":v,"logical_key":list(k)}, default=str) + "\n")
        unresolved_rows.append((k, p, v, diffs))
    out_canon.close(); out_review.close()
    R_COUNTS["R6"] = applied_r6
    R_COUNTS["R7"] = applied_r7
    return applied_r6, applied_r7, unresolved, unresolved_rows


# ─── Tennis — no rule applies, carry over all unresolved from Phase 2 ─
def copy_tennis_review():
    pairs = load_pairs(PHASE2 / "review" / "tennis_matches_history.ndjson",
                       types={"HISTORICAL_LOG_CONTENT_DIFF"})
    out_review = open(OUT / "operator_review" / "tennis_matches_history.ndjson", "w")
    unresolved_rows = []
    for k, sides in pairs:
        p = sides.get("production") or {}
        v = sides.get("preview") or {}
        diffs = _diffs(p, v)
        out_review.write(json.dumps({"side":"production","doc":p,"logical_key":list(k)}, default=str) + "\n")
        out_review.write(json.dumps({"side":"preview","doc":v,"logical_key":list(k)}, default=str) + "\n")
        unresolved_rows.append((k, p, v, diffs))
    out_review.close()
    return unresolved_rows


# ─── Driver ──────────────────────────────────────────────────────────
def main() -> int:
    # 1. Copy ALL Phase 2 canonical NDJSONs as the baseline Phase 3 canonical.
    phase2_copy_counts = {}
    for src in sorted((PHASE2 / "canonical").glob("*.ndjson")):
        n = copy_phase2_canonical(src.stem, OUT / "canonical" / f"{src.stem}.ndjson")
        phase2_copy_counts[src.stem] = n
    # Pre-create empty operator_review for every required collection
    for coll in ["prediction_snapshots","player_game_actuals","player_identities",
                 "player_game_logs","soccer_matches","games","tennis_matches_history"]:
        p = OUT / "operator_review" / f"{coll}.ndjson"
        if not p.exists(): p.touch()

    # 2. Apply rules
    r1_applied, r1_un, r1_rows = apply_r1()
    r2_applied, r2_un, r2_rows = apply_r2()
    r3_applied, r3_un, r3_rows = apply_r3()
    r4_applied, r4_un, r4_rows = apply_r4()
    r5_applied, r5_un, r5_rows = apply_r5()
    r6_applied, r7_applied, g_un, g_rows = apply_r6_r7()
    tennis_rows = copy_tennis_review()

    # 3. Write resolution ledger
    with open(OUT / "resolution_ledger.ndjson", "w") as f:
        for e in RESOLUTION_LEDGER:
            f.write(json.dumps(e, default=str) + "\n")
    summary = {
        "R1_prediction_snapshots_applied": r1_applied,
        "R2_player_game_actuals_applied":  r2_applied,
        "R3_player_identities_applied":    r3_applied,
        "R4_player_game_logs_applied":     r4_applied,
        "R5_soccer_matches_applied":       r5_applied,
        "R6_games_date_only_applied":      r6_applied,
        "R7_games_null_populated_applied": r7_applied,
        "total_rows_resolved_by_rules":    r1_applied + r2_applied + r3_applied
                                            + r4_applied + r5_applied
                                            + r6_applied + r7_applied,
        "unresolved_per_collection": {
            "prediction_snapshots":    r1_un,
            "player_game_actuals":     r2_un,
            "player_identities":       r3_un,
            "player_game_logs":        r4_un,
            "soccer_matches":          r5_un,
            "games":                   g_un,
            "tennis_matches_history":  len(tennis_rows),
        },
        "unresolved_total": r1_un + r2_un + r3_un + r4_un + r5_un + g_un + len(tennis_rows),
    }
    (OUT / "resolution_ledger_summary.json").write_text(
        json.dumps(summary, indent=2))

    # 4. 502-row operator evidence package (compact)
    _build_evidence("prediction_snapshots", r1_rows,
                    classify_fn=_evidence_classifier_ps)
    _build_evidence("player_game_actuals", r2_rows,
                    classify_fn=_evidence_classifier_pga)
    _build_evidence("player_identities", r3_rows,
                    classify_fn=_evidence_classifier_pi)
    _build_evidence("player_game_logs", r4_rows,
                    classify_fn=_evidence_classifier_pgl)
    _build_evidence("soccer_matches", r5_rows,
                    classify_fn=_evidence_classifier_sm)
    _build_evidence("games", g_rows, classify_fn=lambda k,p,v,d: "games_remaining")
    _build_evidence("tennis_matches_history", tennis_rows,
                    classify_fn=_evidence_classifier_tennis)

    # 5. Integrity checks
    ic = _integrity(summary, phase2_copy_counts)
    (OUT / "integrity_report.json").write_text(json.dumps(ic, indent=2, default=str))

    print(json.dumps({"applied": R_COUNTS, "unresolved_total": summary["unresolved_total"],
                      "integrity_pass": ic["all_pass"]}, indent=2))
    return 0


# ─── Evidence classifiers ────────────────────────────────────────────
def _evidence_classifier_ps(k, p, v, diffs):
    if "payload_hash" in diffs and not (diffs & R1_VERSION_FIELDS):
        return "same_version_payload_drift"
    return "r1_not_applicable_other"

def _evidence_classifier_pga(k, p, v, diffs):
    # Group by stat field + sport
    actuals_prod = p.get("actuals") or {}
    actuals_prev = v.get("actuals") or {}
    stat_diffs = sorted({f for f in set(actuals_prod)|set(actuals_prev)
                         if actuals_prod.get(f) != actuals_prev.get(f)})
    sport = p.get("sport") or v.get("sport") or "?"
    return f"{sport}|stats:{','.join(stat_diffs) if stat_diffs else '-'}"

def _evidence_classifier_pi(k, p, v, diffs):
    outside = sorted(diffs - R3_ENRICHMENT_FIELDS)
    if {"canonical_player_id"} & set(outside):
        return "identity_collision"
    aliases_fields = {"name", "name_norm", "aliases", "normalized_name",
                       "canonical_player_name"}
    if set(outside).issubset(aliases_fields):
        return "alias_or_name_issue"
    pid_fields = {"provider_ids"}
    if set(outside).issubset(pid_fields):
        return "provider_id_issue"
    team_pos = {"team", "position", "jersey_number", "primary_position"}
    if set(outside).issubset(team_pos):
        return "team_or_position_issue"
    return "other_semantic_conflict:" + ",".join(outside[:3])

def _evidence_classifier_pgl(k, p, v, diffs):
    if len(diffs) > 1:
        return "multi_field_diff:" + ",".join(sorted(diffs)[:3])
    # single-field, non-null-vs-null
    f = next(iter(diffs))
    pv, vv = p.get(f), v.get(f)
    if pv is not None and vv is not None:
        return f"non_null_vs_non_null:{f}"
    return "identity_or_context_diff"

def _evidence_classifier_sm(k, p, v, diffs):
    if "home_score" in diffs or "away_score" in diffs:
        return "score_correction"
    return "other"

def _evidence_classifier_tennis(k, p, v, diffs):
    return "canonical_key_collision"


# ─── Evidence builder ────────────────────────────────────────────────
def _redact_scalar(v):
    """Keep booleans/numbers/short strings; truncate long strings."""
    if v is None or isinstance(v, (bool, int, float)):
        return v
    if isinstance(v, (list, dict)):
        return v  # keep structure
    s = str(v)
    return s if len(s) <= 200 else s[:197] + "…"


def _build_evidence(coll: str, rows, classify_fn):
    groups = defaultdict(list)
    for k, p, v, diffs in rows:
        cat = classify_fn(k, p, v, diffs)
        groups[cat].append((k, p, v, diffs))
    out = {"collection": coll, "total_unresolved": len(rows),
           "subgroups": []}
    for cat, items in sorted(groups.items(), key=lambda x: -len(x[1])):
        subgroup = {"category": cat, "count": len(items), "examples": []}
        for k, p, v, diffs in items[:3]:
            diff_fields = sorted(diffs)
            subgroup["examples"].append({
                "logical_key": list(k),
                "differing_fields": diff_fields,
                "production_values": {f: _redact_scalar(p.get(f)) for f in diff_fields},
                "preview_values":    {f: _redact_scalar(v.get(f)) for f in diff_fields},
                "production_source":          p.get("source") or p.get("provenance"),
                "preview_source":             v.get("source") or v.get("provenance"),
                "production_timestamps":      {k: p.get(k) for k in ("ingested_at","fetched_at","updated_at","at","published_at") if k in p},
                "preview_timestamps":         {k: v.get(k) for k in ("ingested_at","fetched_at","updated_at","at","published_at") if k in v},
            })
        # add full exhaustive list of logical keys so operator can pick them off
        subgroup["all_logical_keys"] = [list(k) for k, _, _, _ in items]
        out["subgroups"].append(subgroup)
    (OUT / "operator_evidence" / f"{coll}.json").write_text(
        json.dumps(out, indent=2, default=str))


# ─── Integrity ───────────────────────────────────────────────────────
def _file_sha(p: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while True:
            c = f.read(1 << 20)
            if not c: break
            h.update(c)
    return h.hexdigest()


def _integrity(summary: dict, phase2_copy_counts: dict) -> dict:
    report = {}
    # 1 — Production inputs unchanged
    pre = json.loads(pathlib.Path("/tmp/perklocks_reconciled_output/_pre_run_source_sha256.json").read_text())
    tampered = []
    for path, expected in pre.items():
        p = pathlib.Path(path) if path.startswith("/") else pathlib.Path("/tmp/production_reconcile_input")/path
        if _file_sha(p) != expected:
            tampered.append(path)
    report["production_and_preview_sources_unchanged"] = (not tampered, tampered[:5])

    # 2 — Phase 2 outputs unchanged (fingerprint comparison)
    phase2_fp = json.loads(pathlib.Path("/tmp/perklocks_reconciled_output/_canonical_fingerprint.json").read_text())
    phase2_now = {}
    for p in sorted((PHASE2 / "canonical").glob("*.ndjson")):
        rows=[]
        with open(p) as f:
            for ln in f:
                r=json.loads(ln); rows.append((tuple(r["logical_key"]), r.get("canonical_from","")))
        rows.sort()
        phase2_now[p.name] = hashlib.sha256(json.dumps(rows,default=str).encode()).hexdigest()
    phase2_match = (phase2_fp == phase2_now)
    report["phase2_canonical_unchanged"] = phase2_match

    # 3 — every ledger entry exists
    report["ledger_entries"] = len(RESOLUTION_LEDGER)
    report["ledger_matches_applied_total"] = (
        len(RESOLUTION_LEDGER) == summary["total_rows_resolved_by_rules"])

    # 4 — no duplicate logical identities in Phase 3 SAFE
    dup_report = {}
    for p in sorted((OUT / "canonical").glob("*.ndjson")):
        seen = set(); dup = 0
        with open(p) as f:
            for ln in f:
                r = json.loads(ln)
                k = tuple(r["logical_key"])
                if k in seen: dup += 1
                else: seen.add(k)
        dup_report[p.name] = dup
    report["phase3_canonical_duplicates"] = dup_report
    report["phase3_no_canonical_duplicates"] = all(v == 0 for v in dup_report.values())

    # 5 — exactly 502 unresolved
    expected_unresolved = {
        "prediction_snapshots": 2, "player_game_actuals": 153,
        "player_identities": 312, "player_game_logs": 31,
        "soccer_matches": 3, "games": 0, "tennis_matches_history": 1,
    }
    report["expected_unresolved_per_collection"] = expected_unresolved
    report["actual_unresolved_per_collection"] = summary["unresolved_per_collection"]
    report["unresolved_total_matches_expected_502"] = (
        summary["unresolved_total"] == 502 and
        summary["unresolved_per_collection"] == expected_unresolved)

    # 6 — deterministic fingerprint of Phase 3 canonical
    canon_fp = {}
    for p in sorted((OUT / "canonical").glob("*.ndjson")):
        rows=[]
        with open(p) as f:
            for ln in f:
                r=json.loads(ln); rows.append((tuple(r["logical_key"]), r.get("canonical_from","")))
        rows.sort()
        canon_fp[p.name] = hashlib.sha256(json.dumps(rows, default=str).encode()).hexdigest()
    (OUT / "_canonical_fingerprint.json").write_text(json.dumps(canon_fp, indent=2))
    report["phase3_fingerprint_recorded"] = True

    # Overall
    report["all_pass"] = (
        report["production_and_preview_sources_unchanged"][0] and
        report["phase2_canonical_unchanged"] and
        report["ledger_matches_applied_total"] and
        report["phase3_no_canonical_duplicates"] and
        report["unresolved_total_matches_expected_502"]
    )
    return report


if __name__ == "__main__":
    sys.exit(main())
