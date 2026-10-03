"""phase2b_triage — offline conflict root-cause classification.

Reads the Phase 2 reconciliation evidence (conflicts/ + review/) and
groups each conflict into root-cause classes without resolving
anything.  Produces machine-readable triage files under
``/tmp/perklocks_reconciled_output/triage/`` plus a compact text
summary printed to stdout.

NO live DB access, NO modifications to source files, canonical/,
conflicts/, or review/.
"""
from __future__ import annotations

import json
import pathlib
import math
import sys
from collections import Counter, defaultdict

CONFLICTS_DIR = pathlib.Path("/tmp/perklocks_reconciled_output/conflicts")
REVIEW_DIR    = pathlib.Path("/tmp/perklocks_reconciled_output/review")
TRIAGE_DIR    = pathlib.Path("/tmp/perklocks_reconciled_output/triage")
TRIAGE_DIR.mkdir(parents=True, exist_ok=True)

# ─── Field categorisation for prediction_snapshots ───────────────────
PS_VERSION_FIELDS = {
    "model_version", "calibration_version", "feature_snapshot_version",
    "fusion_version", "scoring_version", "simulation_version",
    "validator_version", "board_version",
}
PS_NUMERIC_PUB_FIELDS = {
    "published_lock_score", "published_probability", "published_edge",
    "published_line", "published_odds", "published_confidence",
}
PS_IDENTITY_FIELDS = {
    "prediction_id", "snapshot_version", "pick_id", "idempotency_key",
    "payload_hash", "publication_source",
}
PS_SEMANTIC_FIELDS = {
    "published_grade", "published_reasoning",
}
PS_METADATA_FIELDS = {
    "_id", "is_active", "is_legacy", "published_at",
}
PS_ALL_KNOWN = (PS_VERSION_FIELDS | PS_NUMERIC_PUB_FIELDS |
                PS_IDENTITY_FIELDS | PS_SEMANTIC_FIELDS |
                PS_METADATA_FIELDS)


def _numeric_bucket(a, b) -> str:
    try:
        fa, fb = float(a), float(b)
        if math.isnan(fa) or math.isnan(fb):
            return "nan"
    except (TypeError, ValueError):
        return "nonnumeric"
    if fa == fb:
        return "exact"
    diff = abs(fa - fb)
    if diff <= 1e-6: return "rounding"
    if diff <= 0.001: return "<=0.001"
    if diff <= 0.01:  return "<=0.01"
    return ">0.01"


def _load_conflict_pairs(path: pathlib.Path, conflict_types: set = None):
    """Yield (logical_key_tuple, {"production": doc, "preview": doc})
    pairs reconstructed from the NDJSON conflict/review file."""
    pairs = defaultdict(dict)
    with open(path) as f:
        for ln in f:
            r = json.loads(ln)
            if conflict_types and r.get("type") not in conflict_types:
                continue
            k = tuple(r["logical_key"])
            side = r.get("side")
            if side not in ("production", "preview"):
                continue
            pairs[k][side] = r["doc"]
    for k, sides in pairs.items():
        yield k, sides


# ─── prediction_snapshots triage ─────────────────────────────────────
def triage_prediction_snapshots():
    pairs = list(_load_conflict_pairs(CONFLICTS_DIR / "prediction_snapshots.ndjson",
                                       conflict_types={"IMMUTABLE_CONFLICT"}))
    total = len(pairs)
    classes = Counter()
    class_counts_detailed = defaultdict(lambda: Counter())
    version_shift_counts = Counter()
    numeric_bucket_counts = defaultdict(Counter)   # field -> bucket counter
    payload_hash_same = 0
    payload_hash_differ = 0
    sample_per_class = defaultdict(list)

    for k, sides in pairs:
        p = sides.get("production") or {}
        v = sides.get("preview") or {}
        if not p or not v:
            classes["ONE_SIDE_ONLY"] += 1
            continue
        all_fields = set(p.keys()) | set(v.keys())
        diffs = []
        for f in all_fields:
            if f in {"_id"}: continue  # Mongo _id always differs across clusters
            if p.get(f) != v.get(f):
                diffs.append(f)
        diffs = sorted(diffs)
        diffs_set = set(diffs)

        # payload_hash as semantic truth indicator
        if "payload_hash" not in diffs_set:
            payload_hash_same += 1
        else:
            payload_hash_differ += 1

        # Collect version shifts for the shift distribution
        for vf in PS_VERSION_FIELDS & diffs_set:
            version_shift_counts[(vf, p.get(vf), v.get(vf))] += 1

        # Collect numeric buckets
        for nf in PS_NUMERIC_PUB_FIELDS & diffs_set:
            numeric_bucket_counts[nf][_numeric_bucket(p.get(nf), v.get(nf))] += 1

        # Root-cause classification
        version_only = diffs_set and diffs_set.issubset(PS_VERSION_FIELDS)
        numeric_only = diffs_set and diffs_set.issubset(PS_NUMERIC_PUB_FIELDS)
        metadata_only = diffs_set and diffs_set.issubset(PS_METADATA_FIELDS)
        identity_touched = bool(diffs_set & PS_IDENTITY_FIELDS)
        payload_touched = "payload_hash" in diffs_set

        if version_only:
            cls = "CLASS_A_VERSION_SHIFT_ONLY"
        elif metadata_only:
            cls = "CLASS_B_METADATA_ONLY"
        elif numeric_only:
            # purely numeric diffs on published_*
            size = _dominant_numeric_scale(diffs_set, p, v)
            cls = f"CLASS_C_NUMERIC_ONLY_{size}"
        elif payload_touched and (PS_VERSION_FIELDS & diffs_set):
            cls = "CLASS_D_PAYLOAD_HASH_DIFF_WITH_VERSION_SHIFT"
        elif payload_touched:
            cls = "CLASS_E_PAYLOAD_HASH_DIFF_NO_VERSION_SHIFT"
        elif diffs_set & PS_SEMANTIC_FIELDS:
            cls = "CLASS_F_SEMANTIC_FIELDS_DIFF"
        elif identity_touched:
            cls = "CLASS_G_IDENTITY_FIELD_DIFF"
        else:
            cls = "CLASS_Z_UNCLASSIFIED"
        classes[cls] += 1
        class_counts_detailed[cls][tuple(sorted(diffs_set))] += 1
        if len(sample_per_class[cls]) < 3:
            sample_per_class[cls].append({
                "logical_key": list(k),
                "differing_fields": sorted(diffs_set),
                "production": {f: p.get(f) for f in sorted(diffs_set)[:10]},
                "preview":    {f: v.get(f) for f in sorted(diffs_set)[:10]},
            })

    # Semantic-truth-changed classification
    class_rules = {
        "CLASS_A_VERSION_SHIFT_ONLY":                  ("no", "accept Production (newer pipeline)", "low"),
        "CLASS_B_METADATA_ONLY":                       ("no", "accept Production", "low"),
        "CLASS_C_NUMERIC_ONLY_rounding":               ("no", "accept Production (rounding only)", "low"),
        "CLASS_C_NUMERIC_ONLY_<=0.001":                ("uncertain", "accept Production (sub-0.1% drift)", "low"),
        "CLASS_C_NUMERIC_ONLY_<=0.01":                 ("uncertain", "operator review (sub-1% drift)", "medium"),
        "CLASS_C_NUMERIC_ONLY_>0.01":                  ("yes", "operator review — material numeric drift", "high"),
        "CLASS_D_PAYLOAD_HASH_DIFF_WITH_VERSION_SHIFT":("uncertain", "accept Production (payload moved with model/calibrator)", "medium"),
        "CLASS_E_PAYLOAD_HASH_DIFF_NO_VERSION_SHIFT":  ("yes", "operator review — same version, different payload", "high"),
        "CLASS_F_SEMANTIC_FIELDS_DIFF":                ("yes", "operator review — grade/reasoning changed", "high"),
        "CLASS_G_IDENTITY_FIELD_DIFF":                 ("yes", "operator review — identity field divergence", "high"),
        "CLASS_Z_UNCLASSIFIED":                        ("uncertain", "operator review", "high"),
        "ONE_SIDE_ONLY":                               ("uncertain", "sanity-check Phase 2 pairing", "medium"),
    }

    table = []
    for cls, cnt in sorted(classes.items(), key=lambda x: -x[1]):
        truth, rule, risk = class_rules.get(cls, ("uncertain", "operator review", "high"))
        # Most common differing-fields signature in this class
        sig, sig_cnt = max(class_counts_detailed[cls].items(), key=lambda x: x[1]) if class_counts_detailed[cls] else ((), 0)
        table.append({
            "class": cls,
            "count": cnt,
            "pct":   round(cnt * 100.0 / max(total, 1), 2),
            "semantic_truth_changed": truth,
            "proposed_rule":          rule,
            "risk":                   risk,
            "most_common_diff_signature": list(sig),
            "most_common_diff_signature_count": sig_cnt,
        })

    # Top version shifts
    top_shifts = version_shift_counts.most_common(10)

    out = {
        "collection": "prediction_snapshots",
        "total_conflicts": total,
        "payload_hash_same_count": payload_hash_same,
        "payload_hash_differ_count": payload_hash_differ,
        "classes": table,
        "top_version_shifts": [
            {"field": f, "production": a, "preview": b, "count": c}
            for ((f, a, b), c) in top_shifts
        ],
        "numeric_bucket_distribution": {
            f: dict(c) for f, c in numeric_bucket_counts.items()
        },
        "samples_per_class": {k: v for k, v in sample_per_class.items()},
    }
    (TRIAGE_DIR / "prediction_snapshots_triage.json").write_text(
        json.dumps(out, indent=2, default=str))
    return out


def _dominant_numeric_scale(diffs: set, p: dict, v: dict) -> str:
    scales = Counter()
    for f in diffs & PS_NUMERIC_PUB_FIELDS:
        scales[_numeric_bucket(p.get(f), v.get(f))] += 1
    if not scales: return "unknown"
    return scales.most_common(1)[0][0]


# ─── player_game_actuals triage ──────────────────────────────────────
PGA_ENRICHMENT_FIELDS = {
    "event_time_backfill_source",
    "opponent_enriched_at",
    "opponent_enrichment_source",
}
PGA_PROVENANCE_FIELDS = {
    "backfill_version", "source", "source_player_id", "source_record_id",
    "ingested_at",
}
PGA_IDENTITY_FIELDS = {
    "canonical_event_id", "canonical_opponent_id", "canonical_player_id",
    "canonical_team_id", "event_id", "player_id", "player_name",
    "sport", "team", "opponent", "home_away", "event_time", "season",
    "week", "surface",
}
PGA_STAT_FIELD = "actuals"


def triage_player_game_actuals():
    pairs = list(_load_conflict_pairs(REVIEW_DIR / "player_game_actuals.ndjson",
                                       conflict_types={"PROVIDER_ACTUAL_CONFLICT"}))
    total = len(pairs)
    classes = Counter()
    samples_per_class = defaultdict(list)
    for k, sides in pairs:
        p = sides.get("production") or {}
        v = sides.get("preview") or {}
        if not p or not v:
            classes["ONE_SIDE_ONLY"] += 1; continue
        diffs = {f for f in set(p) | set(v) if f != "_id" and p.get(f) != v.get(f)}
        enrichment_only = diffs and diffs.issubset(PGA_ENRICHMENT_FIELDS)
        provenance_only = diffs and diffs.issubset(PGA_PROVENANCE_FIELDS | PGA_ENRICHMENT_FIELDS)
        actuals_differ  = PGA_STAT_FIELD in diffs
        identity_differ = bool(diffs & PGA_IDENTITY_FIELDS)
        if enrichment_only:
            cls = "A_SAME_ACTUAL_SCHEMA_ENRICHMENT_ONLY"
        elif provenance_only and not actuals_differ:
            cls = "B_PROVIDER_PROVENANCE_ONLY"
        elif actuals_differ and not identity_differ:
            cls = "D_CONTRADICTORY_STAT_VALUES"
        elif identity_differ and not actuals_differ:
            cls = "C_IDENTITY_METADATA_DIFF_SAME_STATS"
        elif actuals_differ and identity_differ:
            cls = "D_CONTRADICTORY_WITH_IDENTITY_DIFF"
        elif not diffs:
            cls = "A_SAME_ACTUAL_NO_DIFF"
        else:
            cls = "E_INCOMPLETE_VS_COMPLETE_OR_OTHER"
        classes[cls] += 1
        if len(samples_per_class[cls]) < 3:
            samples_per_class[cls].append({
                "logical_key": list(k),
                "differing_fields": sorted(diffs),
                "production_actuals": p.get("actuals"),
                "preview_actuals":    v.get("actuals"),
            })

    table = []
    rule_map = {
        "A_SAME_ACTUAL_SCHEMA_ENRICHMENT_ONLY": ("no",  "merge enrichment fields (keep both)", "low"),
        "A_SAME_ACTUAL_NO_DIFF":                ("no",  "keep one", "low"),
        "B_PROVIDER_PROVENANCE_ONLY":           ("no",  "accept Production (newer source metadata)", "low"),
        "C_IDENTITY_METADATA_DIFF_SAME_STATS":  ("uncertain", "operator review — identity reconciliation", "medium"),
        "D_CONTRADICTORY_STAT_VALUES":          ("yes", "operator review — contradictory actual", "high"),
        "D_CONTRADICTORY_WITH_IDENTITY_DIFF":   ("yes", "operator review — both identity and stat divergent", "high"),
        "E_INCOMPLETE_VS_COMPLETE_OR_OTHER":    ("uncertain", "prefer side with populated actuals", "medium"),
        "ONE_SIDE_ONLY":                       ("uncertain", "sanity-check pairing", "medium"),
    }
    for cls, cnt in sorted(classes.items(), key=lambda x: -x[1]):
        truth, rule, risk = rule_map.get(cls, ("uncertain", "operator review", "high"))
        table.append({
            "class": cls, "count": cnt,
            "pct": round(cnt * 100.0 / max(total, 1), 2),
            "semantic_truth_changed": truth,
            "proposed_rule": rule, "risk": risk,
        })

    out = {"collection": "player_game_actuals",
           "total_conflicts": total,
           "classes": table,
           "samples_per_class": samples_per_class}
    (TRIAGE_DIR / "player_game_actuals_triage.json").write_text(
        json.dumps(out, indent=2, default=str))
    return out


# ─── games triage (exact side-by-side) ───────────────────────────────
def triage_games():
    pairs = list(_load_conflict_pairs(REVIEW_DIR / "games.ndjson",
                                       conflict_types={"FINAL_RESULT_CONFLICT"}))
    out = {"collection": "games", "total_conflicts": len(pairs),
           "exact_pairs": []}
    for k, sides in pairs:
        p = sides.get("production") or {}
        v = sides.get("preview") or {}
        diffs = {f for f in set(p) | set(v) if p.get(f) != v.get(f) and f != "_id"}
        out["exact_pairs"].append({
            "logical_key": list(k),
            "home_team_id":   p.get("home_team_id") or v.get("home_team_id"),
            "away_team_id":   p.get("away_team_id") or v.get("away_team_id"),
            "date":           p.get("date") or v.get("date"),
            "production": {f: p.get(f) for f in sorted(diffs)} | {
                "status": p.get("status"), "result": p.get("result"),
                "sport": p.get("sport"), "game_id": p.get("game_id")},
            "preview":    {f: v.get(f) for f in sorted(diffs)} | {
                "status": v.get("status"), "result": v.get("result"),
                "sport": v.get("sport"), "game_id": v.get("game_id")},
            "all_differing_fields": sorted(diffs),
        })
    (TRIAGE_DIR / "games_triage.json").write_text(
        json.dumps(out, indent=2, default=str))
    return out


# ─── Remaining historical review ─────────────────────────────────────
PI_ENRICHMENT_FIELDS = {"provider_ids", "aliases", "name_norm", "profile",
                        "primary_position", "jersey_numbers",
                        "last_seen_at", "last_matched_at", "confidence"}

def _generic_classify(diffs: set, enrichment: set, provenance: set,
                      identity: set, result: set) -> str:
    if not diffs:
        return "A_NO_DIFF"
    if diffs.issubset(enrichment):
        return "B_ENRICHMENT_ONLY"
    if diffs.issubset(provenance):
        return "C_PROVENANCE_ONLY"
    if diffs.issubset({"name", "name_norm", "aliases", "normalized_name"}):
        return "D_ALIAS_OR_NAME_NORMALIZATION"
    if diffs & identity:
        return "E_CANONICAL_IDENTITY_DIFF"
    if diffs & result:
        return "F_CORRECTED_HISTORICAL_RESULT_OR_STAT"
    return "G_OTHER_SCHEMA_EVOLUTION"


def triage_historical(coll_name: str, source_path: pathlib.Path,
                      enrichment=set(), provenance=set(),
                      identity=set(), result=set(),
                      conflict_types=None) -> dict:
    pairs = list(_load_conflict_pairs(source_path, conflict_types=conflict_types))
    classes = Counter()
    samples = defaultdict(list)
    total = len(pairs)
    for k, sides in pairs:
        p = sides.get("production") or {}
        v = sides.get("preview") or {}
        diffs = {f for f in set(p) | set(v) if f != "_id" and p.get(f) != v.get(f)}
        cls = _generic_classify(diffs, enrichment, provenance, identity, result)
        classes[cls] += 1
        if len(samples[cls]) < 3:
            samples[cls].append({"logical_key": list(k),
                                  "differing_fields": sorted(diffs)[:8]})
    rule_map = {
        "A_NO_DIFF":                                   ("no",  "keep one", "low"),
        "B_ENRICHMENT_ONLY":                           ("no",  "union (merge enrichment)", "low"),
        "C_PROVENANCE_ONLY":                           ("no",  "accept Production", "low"),
        "D_ALIAS_OR_NAME_NORMALIZATION":               ("no",  "union aliases", "low"),
        "E_CANONICAL_IDENTITY_DIFF":                   ("yes", "operator review", "high"),
        "F_CORRECTED_HISTORICAL_RESULT_OR_STAT":       ("yes", "operator review", "high"),
        "G_OTHER_SCHEMA_EVOLUTION":                    ("uncertain", "operator review", "medium"),
    }
    out = {"collection": coll_name, "total_conflicts": total,
           "classes": [{"class": c, "count": n,
                        "pct": round(n * 100.0 / max(total, 1), 2),
                        "semantic_truth_changed": rule_map[c][0],
                        "proposed_rule":          rule_map[c][1],
                        "risk":                   rule_map[c][2]}
                       for c, n in sorted(classes.items(), key=lambda x: -x[1])],
           "samples_per_class": samples}
    (TRIAGE_DIR / f"{coll_name}_triage.json").write_text(
        json.dumps(out, indent=2, default=str))
    return out


def main() -> int:
    print("── prediction_snapshots triage ──", flush=True)
    ps = triage_prediction_snapshots()
    print("── player_game_actuals triage ──", flush=True)
    pga = triage_player_game_actuals()
    print("── games triage ──", flush=True)
    gm = triage_games()
    print("── player_identities triage ──", flush=True)
    pi = triage_historical("player_identities",
        REVIEW_DIR / "player_identities.ndjson",
        enrichment={"provider_ids", "aliases", "name_norm", "profile",
                    "primary_position", "jersey_numbers",
                    "last_seen_at", "last_matched_at", "confidence",
                    "canonical_player_name", "historical_teams",
                    "historical_national_teams", "observed_at",
                    "national_team_observed_at", "current_team",
                    "current_national_team", "national_team_source",
                    "national_team_status", "nationality", "position"},
        provenance={"source", "ingested_at", "updated_at"},
        identity={"canonical_player_id", "sport"},
        result=set(),
        conflict_types={"HISTORICAL_LOG_CONTENT_DIFF"})
    print("── player_game_logs triage ──", flush=True)
    pgl = triage_historical("player_game_logs",
        REVIEW_DIR / "player_game_logs.ndjson",
        enrichment={"team", "role", "position", "started", "on_bench"},
        provenance={"source", "ingested_at", "updated_at", "data_as_of"},
        identity={"canonical_player_id", "canonical_event_id", "game_id",
                  "sport", "player_id"},
        result={"stat_block", "hits", "runs", "home_runs", "strikeouts",
                "walks", "rbi", "total_bases", "innings_pitched",
                "earned_runs", "hits_allowed", "at_bats",
                "pitcher_strikeouts", "shots", "shots_on_target",
                "goals", "assists", "points", "blocks", "steals",
                "turnovers", "minutes", "field_goals", "three_pointers",
                "free_throws", "rebounds"},
        conflict_types={"HISTORICAL_LOG_CONTENT_DIFF"})
    print("── soccer_matches triage ──", flush=True)
    sm = triage_historical("soccer_matches",
        REVIEW_DIR / "soccer_matches.ndjson",
        enrichment={"home_xg", "away_xg", "home_odds_open", "home_odds_close",
                    "away_odds_open", "away_odds_close",
                    "draw_odds_open", "draw_odds_close"},
        provenance={"source", "fetched_at", "ingested_at"},
        identity={"league", "season", "home_team", "away_team", "date"},
        result={"home_score", "away_score", "status"},
        conflict_types={"HISTORICAL_LOG_CONTENT_DIFF"})
    print("── tennis_matches_history triage ──", flush=True)
    tm = triage_historical("tennis_matches_history",
        REVIEW_DIR / "tennis_matches_history.ndjson",
        enrichment={"minutes", "walkover", "retirement"},
        provenance={"source", "fetched_at", "ingested_at"},
        identity={"tourney_id", "winner_id", "loser_id"},
        result={"score", "surface", "best_of", "round", "tourney_level"},
        conflict_types={"HISTORICAL_LOG_CONTENT_DIFF"})

    # ─── Blocker matrix ───────────────────────────────────────────────
    def _biggest(trg):
        cls = trg.get("classes") or []
        if not cls: return ("exact-pair-review", trg.get("total_conflicts", 0), 100.0)
        top = cls[0]
        return (top["class"], top["count"], top["pct"])

    def _contradiction_counts(trg):
        if "classes" not in trg:
            # games — every pair needs exact review
            n = trg.get("total_conflicts", 0)
            return n, 0, n
        yes = sum(c["count"] for c in trg["classes"] if c["semantic_truth_changed"] == "yes")
        auto = sum(c["count"] for c in trg["classes"]
                   if c["semantic_truth_changed"] == "no")
        rev = sum(c["count"] for c in trg["classes"]
                  if c["semantic_truth_changed"] in ("yes", "uncertain"))
        return yes, auto, rev

    matrix_rows = []
    for trg in [ps, pga, gm, pi, pgl, sm, tm]:
        top_cls, top_cnt, top_pct = _biggest(trg)
        yes, auto, rev = _contradiction_counts(trg)
        matrix_rows.append({
            "collection":                      trg["collection"],
            "total":                           trg["total_conflicts"],
            "root_cause_classes":              len(trg.get("classes") or []) or 1,
            "largest_class":                   f"{top_cls} ({top_cnt}, {top_pct}%)",
            "true_authoritative_contradictions": yes,
            "potentially_auto_resolvable":     auto,
            "needs_operator_decision":         rev,
        })
    (TRIAGE_DIR / "blocker_matrix.json").write_text(
        json.dumps(matrix_rows, indent=2, default=str))

    # Totals of records that would STILL require operator judgment
    operator_remaining = sum(r["needs_operator_decision"] for r in matrix_rows)
    auto_resolvable    = sum(r["potentially_auto_resolvable"] for r in matrix_rows)
    (TRIAGE_DIR / "summary_totals.json").write_text(
        json.dumps({
            "operator_judgment_still_required": operator_remaining,
            "potentially_auto_resolvable_safely": auto_resolvable,
            "rules_version": "phase2b-triage.rev1",
        }, indent=2, default=str))

    print("\n── matrix ──")
    for row in matrix_rows:
        print(json.dumps(row))
    print("operator_remaining:", operator_remaining,
          "auto_resolvable:", auto_resolvable)
    return 0


if __name__ == "__main__":
    sys.exit(main())
