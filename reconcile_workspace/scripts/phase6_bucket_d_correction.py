"""phase6_bucket_d_correction — Re-scope prediction_snapshots to just the
2 Phase 3 R1-unresolved rows and check for independent publication proof."""
import json, pathlib, collections

P6_WORK  = pathlib.Path("/opt/reconcile_tmp/p6_work")
PHASE2   = P6_WORK / "v2_phase2"
PHASE3   = P6_WORK / "v2_phase3"
P6_OUT   = pathlib.Path("/opt/reconcile_tmp/v2_phase6")

# Build the two authority hash sets from canonical Phase 2 output.
pub_hashes = set(); pg_hashes = set()
with open(PHASE2 / "canonical" / "publication_events.ndjson") as f:
    for ln in f:
        r = json.loads(ln); d = r.get("doc") if "doc" in r else r
        if isinstance(d, dict) and d.get("payload_hash"):
            pub_hashes.add(d["payload_hash"])
with open(PHASE2 / "canonical" / "pregame_snapshots.ndjson") as f:
    for ln in f:
        r = json.loads(ln); d = r.get("doc") if "doc" in r else r
        if isinstance(d, dict) and d.get("snapshot_hash"):
            pg_hashes.add(d["snapshot_hash"])

print(f"authority sets: publication_events={len(pub_hashes)} payload_hashes, "
      f"pregame_snapshots={len(pg_hashes)} snapshot_hashes")

# Load the 2 Phase 3 R1-unresolved pairs
bag = collections.defaultdict(dict)
with open(PHASE3 / "operator_review" / "prediction_snapshots.ndjson") as f:
    for ln in f:
        r = json.loads(ln)
        k = tuple(r["logical_key"])
        bag[k][r["side"]] = r["doc"]

print(f"\nPhase 3 R1-unresolved prediction_snapshots: {len(bag)} unique pairs")

canon_f = open(P6_OUT / "canonical" / "prediction_snapshots.ndjson", "w")
quar_f  = open(P6_OUT / "quarantined" / "prediction_snapshots.ndjson", "w")
unres_f = open(P6_OUT / "unresolved" / "prediction_snapshots.ndjson", "w")
evidence = []

resolved = []; quarantined = []
for k, sides in bag.items():
    p, v = sides.get("production", {}), sides.get("preview", {})
    ph_prod = p.get("payload_hash"); ph_prev = v.get("payload_hash")
    prod_in_pub = ph_prod in pub_hashes if ph_prod else False
    prev_in_pub = ph_prev in pub_hashes if ph_prev else False
    prod_in_pg  = ph_prod in pg_hashes  if ph_prod else False
    prev_in_pg  = ph_prev in pg_hashes  if ph_prev else False

    winner_side = None; proof_source = None
    if prod_in_pub and not prev_in_pub:
        winner_side, proof_source = "production", "publication_events"
    elif prev_in_pub and not prod_in_pub:
        winner_side, proof_source = "preview", "publication_events"
    elif prod_in_pg and not prev_in_pg:
        winner_side, proof_source = "production", "pregame_snapshots"
    elif prev_in_pg and not prod_in_pg:
        winner_side, proof_source = "preview", "pregame_snapshots"

    if winner_side:
        winner = p if winner_side == "production" else v
        canon_f.write(json.dumps({
            "canonical_from": f"R12_ps_publication_proof_{winner_side}",
            "doc": winner, "logical_key": list(k),
            "proof_source": proof_source,
        }, default=str) + "\n")
        resolved.append(k)
        evidence.append({
            "collection": "prediction_snapshots",
            "logical_key": list(k),
            "selected_side": winner_side,
            "authority": proof_source,
            "evidence_identity": {"payload_hash": p.get("payload_hash") if winner_side == "production" else v.get("payload_hash")},
            "resolution_reason": f"Side's payload_hash referenced in canonical {proof_source}; other side's not.",
        })
        print(f"  RESOLVED {list(k)} -> {winner_side} via {proof_source}")
    else:
        quar_f.write(json.dumps({"side":"production","doc":p,"logical_key":list(k),
                                  "status":"UNRESOLVED_IMMUTABLE_CONFLICT",
                                  "excluded_from_canonical_runtime": True,
                                  "quarantine_reason": "No immutable publication proof: neither payload_hash present in publication_events; no matching snapshot_hash in pregame_snapshots."}, default=str) + "\n")
        quar_f.write(json.dumps({"side":"preview","doc":v,"logical_key":list(k),
                                  "status":"UNRESOLVED_IMMUTABLE_CONFLICT",
                                  "excluded_from_canonical_runtime": True,
                                  "quarantine_reason": "same as production side"}, default=str) + "\n")
        quarantined.append(k)
        print(f"  QUARANTINED {list(k)}  prod_hash={ph_prod[:16] if ph_prod else None}... prev_hash={ph_prev[:16] if ph_prev else None}...")

canon_f.close(); quar_f.close(); unres_f.close()
with open(P6_OUT / "evidence" / "prediction_snapshots.json", "w") as f:
    json.dump(evidence, f, indent=2, default=str)

# Update summary
summary = json.load(open(P6_OUT / "phase6_summary.json"))
summary["buckets"]["D_prediction_snapshots"] = {
    "collection": "prediction_snapshots",
    "scope": "Phase 3 R1-unresolved (correct 2-pair scope)",
    "input_pairs": len(bag),
    "resolved_by_publication_proof": len(resolved),
    "quarantined": len(quarantined),
    "resolution_rule": "R12 — publication_events/pregame_snapshots hash cross-check; else KEEP QUARANTINED",
}
summary["totals"]["resolved_total"] = (
    summary["buckets"]["A_player_game_actuals"]["resolved"]
  + summary["buckets"]["B_player_game_logs"]["resolved"]
  + summary["buckets"]["C_soccer_matches"]["resolved"]
  + len(resolved))
summary["totals"]["quarantined_total"] = len(quarantined)
with open(P6_OUT / "phase6_summary.json", "w") as f:
    json.dump(summary, f, indent=2, default=str)

print(f"\n=== bucket D corrected ===")
print(f"resolved: {len(resolved)}  quarantined: {len(quarantined)}")
print(json.dumps(summary["totals"], indent=2))
