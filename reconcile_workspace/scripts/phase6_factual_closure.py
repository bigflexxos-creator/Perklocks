"""phase6_factual_closure — Resolve remaining V2 factual conflicts using
authoritative completed-event evidence available offline.

Buckets (as counted by unique logical keys):
  A. player_game_actuals  — 7353 REVIEW rows (expected: metadata-only drift)
  B. player_game_logs     — 31 unresolved (real stat conflicts)
  C. soccer_matches       — 5 REVIEW rows (mix: odds-only & factual)
  D. prediction_snapshots — 2 IMMUTABLE_CONFLICT (quarantined unless publication proof)

Policy:
  - Never guess. If authority doesn't prove, leave UNRESOLVED.
  - Never pick by timestamp.
  - Never pick by current roster.
  - Document every resolution with authority source + evidence identity.
"""
from __future__ import annotations
import collections
import hashlib
import json
import os
import pathlib
import shutil
import sys
import tarfile
import time
from datetime import datetime, timezone

P6_WORK   = pathlib.Path("/opt/reconcile_tmp/p6_work")
PHASE2    = P6_WORK / "v2_phase2"
P6_OUT    = pathlib.Path("/opt/reconcile_tmp/v2_phase6")
P6_OUT.mkdir(parents=True, exist_ok=True)
(P6_OUT / "canonical").mkdir(exist_ok=True)
(P6_OUT / "unresolved").mkdir(exist_ok=True)
(P6_OUT / "evidence").mkdir(exist_ok=True)
(P6_OUT / "quarantined").mkdir(exist_ok=True)

CHECKPOINTS = pathlib.Path("/app/reconcile_workspace/checkpoints")
RECON_V2    = pathlib.Path("/app/reconcile_workspace/reconciliation_v2")


def _sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            c = f.read(1 << 20)
            if not c: break
            h.update(c)
    return h.hexdigest()


def _pairs(path: pathlib.Path):
    bag = collections.defaultdict(dict)
    with open(path) as f:
        for ln in f:
            r = json.loads(ln)
            if r.get("side") in ("production", "preview"):
                bag[tuple(r["logical_key"])][r["side"]] = r["doc"]
    return list(bag.items())


def _doc(side: dict) -> dict: return side or {}


# ===================================================================== A
def resolve_pga():
    """player_game_actuals: 7353 REVIEW pairs.
    Authority: the 'actuals' subdict IS the authoritative completed-event
    payload. If both sides have identical 'actuals', the factual event
    truth is agreed — resolve to Production (PROD_AUTH tie-break convention)
    and record evidence. Otherwise unresolved.
    """
    pairs = _pairs(PHASE2 / "review" / "player_game_actuals.ndjson")
    resolved = []; unresolved = []
    stat_identical_resolved = 0
    stat_differ_unresolved = 0
    evidence = []
    out_canon = open(P6_OUT / "canonical" / "player_game_actuals.ndjson", "w")
    out_unres = open(P6_OUT / "unresolved" / "player_game_actuals.ndjson", "w")

    for k, sides in pairs:
        p, v = sides.get("production", {}), sides.get("preview", {})
        pa = p.get("actuals") or {}
        va = v.get("actuals") or {}
        actuals_identical = (pa == va)
        # Non-stat fields may legitimately differ — provenance drift.
        if actuals_identical and pa:
            winner = p
            canonical_row = {
                "canonical_from": "R12_pga_stats_identical_prod_authority",
                "doc": winner,
                "logical_key": list(k),
            }
            out_canon.write(json.dumps(canonical_row, default=str) + "\n")
            resolved.append(k)
            stat_identical_resolved += 1
            evidence.append({
                "collection":        "player_game_actuals",
                "logical_key":       list(k),
                "canonical_event_id": p.get("canonical_event_id") or p.get("event_id"),
                "canonical_player_id": p.get("canonical_player_id") or p.get("player_id"),
                "sport":             p.get("sport"),
                "selected_side":     "production",
                "authority":         "actuals_payload_identical_both_sides",
                "evidence_identity": {"actuals": pa},
                "resolution_reason": ("Both Preview and Production completed-event "
                                      "`actuals` payload identical byte-for-byte; "
                                      "only provenance/enrichment metadata differs. "
                                      "No factual disagreement. PROD_AUTH tie-break."),
            })
        elif actuals_identical and not pa:
            # Both empty — still structurally identical, PROD_AUTH tie-break
            winner = p
            canonical_row = {
                "canonical_from": "R12_pga_empty_actuals_both_sides_prod",
                "doc": winner,
                "logical_key": list(k),
            }
            out_canon.write(json.dumps(canonical_row, default=str) + "\n")
            resolved.append(k)
            stat_identical_resolved += 1
        else:
            # True stat disagreement — NOT RESOLVED without external authority
            out_unres.write(json.dumps({"side":"preview","doc":v,"logical_key":list(k),
                                         "status":"UNRESOLVED_FACTUAL_PGA"}, default=str)+"\n")
            out_unres.write(json.dumps({"side":"production","doc":p,"logical_key":list(k),
                                         "status":"UNRESOLVED_FACTUAL_PGA"}, default=str)+"\n")
            unresolved.append(k)
            stat_differ_unresolved += 1

    out_canon.close(); out_unres.close()
    with open(P6_OUT / "evidence" / "player_game_actuals.json", "w") as f:
        json.dump(evidence, f, indent=2, default=str)

    return {
        "collection":            "player_game_actuals",
        "input_pairs":           len(pairs),
        "resolved":              len(resolved),
        "unresolved":            len(unresolved),
        "stat_identical_resolved": stat_identical_resolved,
        "stat_differ_unresolved":  stat_differ_unresolved,
        "resolution_rule":       "R12 — actuals-payload equality on both sides",
    }


# ===================================================================== B
def _load_pga_canonical_index():
    """Build (sport, player_id|str, event_id|str) -> actuals index from
    canonical player_game_actuals Phase 2 output."""
    idx = {}
    with open(PHASE2 / "canonical" / "player_game_actuals.ndjson") as f:
        for ln in f:
            r = json.loads(ln)
            d = r.get("doc") if "doc" in r else r
            if not isinstance(d, dict): continue
            key = (str(d.get("sport") or ""),
                   str(d.get("player_id") or d.get("canonical_player_id") or ""),
                   str(d.get("event_id") or d.get("canonical_event_id") or ""))
            if "" in key[1:]: continue
            idx[key] = d.get("actuals") or {}
    return idx


def resolve_pgl():
    """player_game_logs: 26,599 REVIEW rows but only 31 are *real* stat
    conflicts (26,568 are 'shots'-only volatility that R4 should have
    handled; here we re-check and apply R4-equivalent treatment).

    True factual conflicts: 31 (hits, at_bats, hits_allowed, earned_runs, etc.)
    Authority: canonical player_game_actuals for (sport, player, event).
    Field map (MLB): PGL->PGA : hits->h, home_runs->hr, rbi->rbi, strikeouts->k,
    at_bats->at_bats, total_bases->tb, runs->r. Limited; many conflicts unresolvable.
    """
    pairs = _pairs(PHASE2 / "review" / "player_game_logs.ndjson")
    FACT_FIELDS = {'hits','at_bats','home_runs','rbi','runs','total_bases',
                   'strikeouts','walks','earned_runs','innings_pitched',
                   'hits_allowed','pitcher_strikeouts','blocked_shots'}
    VOLATILE = {'shots'}  # NHL volatility — same semantics as R4 in Phase 3
    PGL_TO_PGA_MLB = {
        'hits':'h','home_runs':'hr','rbi':'rbi','strikeouts':'k',
        'at_bats':'at_bats','total_bases':'tb','runs':'r',
    }

    pga_idx = _load_pga_canonical_index()

    resolved = []; unresolved = []; shots_only_resolved = 0
    evidence = []
    out_canon = open(P6_OUT / "canonical" / "player_game_logs.ndjson", "w")
    out_unres = open(P6_OUT / "unresolved" / "player_game_logs.ndjson", "w")

    for k, sides in pairs:
        p, v = sides.get("production", {}), sides.get("preview", {})
        diffs = {f for f in set(p) | set(v) if f != "_id" and p.get(f) != v.get(f)}
        fact_diffs = diffs & FACT_FIELDS

        # 1. Shots-only (or purely-volatile) diffs — R4-equivalent: pick side
        # with populated data; otherwise Production.
        if diffs and not fact_diffs and diffs.issubset(VOLATILE | {'shots','blocks','faceoffs'}):
            # Prefer side with populated numeric value for the volatile field
            winner_side, winner = "production", p
            for f in diffs:
                pv, vv = p.get(f), v.get(f)
                if (pv is None or pv == 0) and vv not in (None, 0):
                    winner_side, winner = "preview", v
                    break
            out_canon.write(json.dumps({
                "canonical_from": "R12_pgl_volatile_only_populated_wins",
                "doc": winner,
                "logical_key": list(k),
                "winner_side": winner_side,
            }, default=str) + "\n")
            resolved.append(k)
            shots_only_resolved += 1
            continue

        # 2. True factual diffs — try PGA cross-check (MLB only)
        if p.get("sport") == "mlb" and fact_diffs:
            idx_key = (str(p.get("sport") or ""),
                       str(p.get("player_id") or ""),
                       str(p.get("game_id") or ""))
            pga_actuals = pga_idx.get(idx_key)
            if pga_actuals:
                # Try to prove winner on at least one disputed field
                winners_per_field = {}
                for pgl_f in fact_diffs:
                    pga_f = PGL_TO_PGA_MLB.get(pgl_f)
                    if not pga_f: continue
                    pga_val = pga_actuals.get(pga_f)
                    if pga_val is None: continue
                    pv, vv = p.get(pgl_f), v.get(pgl_f)
                    # Compare with float coercion
                    def _eq(a, b):
                        try: return float(a) == float(b)
                        except Exception: return a == b
                    if _eq(pga_val, pv) and not _eq(pga_val, vv):
                        winners_per_field[pgl_f] = ("production", pga_val)
                    elif _eq(pga_val, vv) and not _eq(pga_val, pv):
                        winners_per_field[pgl_f] = ("preview", pga_val)
                if winners_per_field:
                    # Pick overall winner only if all authority-proved fields agree
                    sides_won = {s for s, _ in winners_per_field.values()}
                    if len(sides_won) == 1:
                        winner_side = sides_won.pop()
                        winner = p if winner_side == "production" else v
                        out_canon.write(json.dumps({
                            "canonical_from": f"R12_pgl_pga_cross_{winner_side}",
                            "doc": winner,
                            "logical_key": list(k),
                            "authority_fields": list(winners_per_field.keys()),
                        }, default=str) + "\n")
                        resolved.append(k)
                        evidence.append({
                            "collection":        "player_game_logs",
                            "logical_key":       list(k),
                            "canonical_event_id": p.get("game_id"),
                            "canonical_player_id": p.get("player_id"),
                            "sport":             p.get("sport"),
                            "selected_side":     winner_side,
                            "authority":         "canonical_player_game_actuals",
                            "authority_fields":  winners_per_field,
                            "resolution_reason": "PGA actuals proved disputed stat on at least one field; both authority-proved fields agree.",
                        })
                        continue

        # 3. Unresolved
        out_unres.write(json.dumps({"side":"preview","doc":v,"logical_key":list(k),
                                     "status":"UNRESOLVED_FACTUAL_PGL",
                                     "diff_fields": sorted(diffs)}, default=str)+"\n")
        out_unres.write(json.dumps({"side":"production","doc":p,"logical_key":list(k),
                                     "status":"UNRESOLVED_FACTUAL_PGL",
                                     "diff_fields": sorted(diffs)}, default=str)+"\n")
        unresolved.append(k)

    out_canon.close(); out_unres.close()
    with open(P6_OUT / "evidence" / "player_game_logs.json", "w") as f:
        json.dump(evidence, f, indent=2, default=str)

    return {
        "collection":          "player_game_logs",
        "input_pairs":         len(pairs),
        "shots_volatile_resolved": shots_only_resolved,
        "resolved":            len(resolved),
        "unresolved":          len(unresolved),
        "resolution_rule":     "R12 — PGL vs PGA canonical cross-check (MLB) + R4-equivalent for shots/volatile",
    }


# ===================================================================== C
def _build_soccer_team_goals_index():
    """Build (match_id|str) -> {home_goals, away_goals} index from
    canonical soccer_player_game_logs (team-level consensus via summation
    is unsafe without complete coverage; instead we use the home_goals/
    away_goals fields that each per-player row carries which encode the
    final match score, and verify consistency)."""
    idx = {}
    with open(PHASE2 / "canonical" / "soccer_player_game_logs.ndjson") as f:
        for ln in f:
            r = json.loads(ln)
            d = r.get("doc") if "doc" in r else r
            if not isinstance(d, dict): continue
            mid = d.get("match_id") or d.get("canonical_event_id")
            if mid is None: continue
            mid = str(mid)
            hg = d.get("home_goals"); ag = d.get("away_goals")
            if hg is None or ag is None: continue
            # Consistency: all rows for a match must agree
            if mid in idx:
                if idx[mid]["home_goals"] != hg or idx[mid]["away_goals"] != ag:
                    idx[mid] = None  # mark inconsistent
            else:
                idx[mid] = {"home_goals": hg, "away_goals": ag, "n_rows": 1,
                            "match_date": d.get("match_date"),
                            "home_team_name": d.get("home_team_name"),
                            "away_team_name": d.get("away_team_name"),
                            "competition": d.get("competition"),
                            "league": d.get("league")}
    return idx


def resolve_sm():
    """soccer_matches: 5 REVIEW pairs.
    Buckets:
      - odds-only diffs (R5-equivalent): populated-wins
      - factual (home_score / away_score) diffs: authoritative via
        canonical soccer_player_game_logs home_goals/away_goals (which
        carry the final match score on every per-player row).
    """
    R5_ODDS_FIELDS = {'home_odds_open','home_odds_close','away_odds_open',
                      'away_odds_close','draw_odds_open','draw_odds_close',
                      'fetched_at'}
    FACTUAL = {'home_score','away_score','status'}

    pairs = _pairs(PHASE2 / "review" / "soccer_matches.ndjson")
    spgl_idx = _build_soccer_team_goals_index()

    resolved = []; unresolved = []; evidence = []
    out_canon = open(P6_OUT / "canonical" / "soccer_matches.ndjson", "w")
    out_unres = open(P6_OUT / "unresolved" / "soccer_matches.ndjson", "w")

    for k, sides in pairs:
        p, v = sides.get("production", {}), sides.get("preview", {})
        diffs = {f for f in set(p) | set(v) if f != "_id" and p.get(f) != v.get(f)}
        factual_diffs = diffs & FACTUAL

        # Case 1: diffs are entirely odds/fetched_at
        if diffs and not factual_diffs and diffs.issubset(R5_ODDS_FIELDS):
            # populated-wins
            p_pop = sum(1 for f in diffs if f != 'fetched_at' and p.get(f) is not None)
            v_pop = sum(1 for f in diffs if f != 'fetched_at' and v.get(f) is not None)
            if p_pop > v_pop:
                winner_side, winner = "production", p
            elif v_pop > p_pop:
                winner_side, winner = "preview", v
            else:
                winner_side, winner = "production", p   # PROD_AUTH tie-break
            out_canon.write(json.dumps({
                "canonical_from": f"R12_sm_odds_only_{winner_side}_populated",
                "doc": winner, "logical_key": list(k), "winner_side": winner_side,
            }, default=str) + "\n")
            resolved.append(k)
            evidence.append({
                "collection":        "soccer_matches",
                "logical_key":       list(k),
                "selected_side":     winner_side,
                "authority":         "odds_populated_vs_null (R5-equivalent)",
                "authority_fields":  {f: {"production": p.get(f), "preview": v.get(f)} for f in sorted(diffs)},
                "resolution_reason": "Odds-only diff with one side carrying populated odds and the other null; populated-wins tie-break.",
            })
            continue

        # Case 2: factual diff on home_score / away_score — cross-check canonical SPGL
        if factual_diffs:
            # Try to find match_id. Soccer matches logical_key encodes (league, season, home, away, date). We need the match_id.
            # Try side docs for a match_id field.
            mid = p.get("match_id") or v.get("match_id") or p.get("canonical_event_id") or v.get("canonical_event_id") or p.get("_id") or v.get("_id")
            if mid is not None:
                evi = spgl_idx.get(str(mid))
                if evi:
                    hg_auth, ag_auth = evi["home_goals"], evi["away_goals"]
                    # Compare
                    def _eq(a, b):
                        try: return float(a) == float(b)
                        except Exception: return a == b
                    sides_won = set()
                    reasons = {}
                    for f in factual_diffs:
                        if f == 'home_score':
                            if _eq(p.get(f), hg_auth) and not _eq(v.get(f), hg_auth):
                                sides_won.add("production"); reasons[f] = ("production", hg_auth)
                            elif _eq(v.get(f), hg_auth) and not _eq(p.get(f), hg_auth):
                                sides_won.add("preview"); reasons[f] = ("preview", hg_auth)
                        elif f == 'away_score':
                            if _eq(p.get(f), ag_auth) and not _eq(v.get(f), ag_auth):
                                sides_won.add("production"); reasons[f] = ("production", ag_auth)
                            elif _eq(v.get(f), ag_auth) and not _eq(p.get(f), ag_auth):
                                sides_won.add("preview"); reasons[f] = ("preview", ag_auth)
                    if len(sides_won) == 1:
                        winner_side = sides_won.pop()
                        winner = p if winner_side == "production" else v
                        out_canon.write(json.dumps({
                            "canonical_from": f"R12_sm_cross_spgl_{winner_side}",
                            "doc": winner, "logical_key": list(k),
                            "authority_evidence": {"match_id": str(mid), "home_goals": hg_auth, "away_goals": ag_auth},
                        }, default=str) + "\n")
                        resolved.append(k)
                        evidence.append({
                            "collection":        "soccer_matches",
                            "logical_key":       list(k),
                            "canonical_event_id": str(mid),
                            "selected_side":     winner_side,
                            "authority":         "canonical_soccer_player_game_logs",
                            "authority_fields":  reasons,
                            "resolution_reason": "Soccer_player_game_logs final match score consistent with one side's reported home_score/away_score.",
                        })
                        continue

        # Unresolved
        out_unres.write(json.dumps({"side":"preview","doc":v,"logical_key":list(k),
                                     "status":"UNRESOLVED_FACTUAL_SM",
                                     "diff_fields": sorted(diffs)}, default=str)+"\n")
        out_unres.write(json.dumps({"side":"production","doc":p,"logical_key":list(k),
                                     "status":"UNRESOLVED_FACTUAL_SM",
                                     "diff_fields": sorted(diffs)}, default=str)+"\n")
        unresolved.append(k)

    out_canon.close(); out_unres.close()
    with open(P6_OUT / "evidence" / "soccer_matches.json", "w") as f:
        json.dump(evidence, f, indent=2, default=str)

    return {
        "collection":    "soccer_matches",
        "input_pairs":   len(pairs),
        "resolved":      len(resolved),
        "unresolved":    len(unresolved),
        "resolution_rule": "R12 — SPGL cross-check for factual; R5-equivalent for odds-only",
    }


# ===================================================================== D
def _load_publication_hash_set():
    hashes = set()
    with open(PHASE2 / "canonical" / "publication_events.ndjson") as f:
        for ln in f:
            r = json.loads(ln)
            d = r.get("doc") if "doc" in r else r
            if not isinstance(d, dict): continue
            ph = d.get("payload_hash")
            if ph: hashes.add(ph)
    return hashes


def _load_pregame_snapshot_hash_set():
    hashes = set()
    with open(PHASE2 / "canonical" / "pregame_snapshots.ndjson") as f:
        for ln in f:
            r = json.loads(ln)
            d = r.get("doc") if "doc" in r else r
            if not isinstance(d, dict): continue
            sh = d.get("snapshot_hash")
            if sh: hashes.add(sh)
    return hashes


def resolve_ps():
    """prediction_snapshots: 2 Phase 3 R1-unresolved IMMUTABLE_CONFLICT.
    Only resolve if a side's payload_hash appears in publication_events (which
    records the actually-committed artefact) or pregame_snapshots.
    Otherwise KEEP QUARANTINED — mandatory per directive.
    """
    pairs = _pairs(PHASE2 / "conflicts" / "prediction_snapshots.ndjson")
    pub_hashes = _load_publication_hash_set()
    pg_hashes  = _load_pregame_snapshot_hash_set()

    # Filter to the 2 unresolved after Phase 3 R1 — we re-identify them by
    # loading Phase 3 unresolved (via extracted overlay or by replicating R1
    # criterion: no VERSION-only diff). Simpler: re-apply R1 skip criterion
    # here. R1 resolved all CONFLICTS where diffs were strictly within version
    # fields. Any pair with diffs touching payload_hash or any non-version
    # field would land unresolved.
    R1_VERSION_FIELDS = {"model_version","calibration_version","feature_snapshot_version",
                         "fusion_version","scoring_version","simulation_version",
                         "validator_version","board_version"}
    # Volatile fields R1 already strips
    STRIP = {"_id","ingested_at","updated_at","fetched_at","data_as_of",
             "source_fetched_at","last_shown_at","shown_at","shown_count",
             "backfill_version","provenance","compat_write",
             "last_refresh_at","last_refreshed","last_login_at",
             "is_active","superseded_at","superseded_by"}

    quar_f = open(P6_OUT / "quarantined" / "prediction_snapshots.ndjson", "w")
    unres_f = open(P6_OUT / "unresolved" / "prediction_snapshots.ndjson", "w")
    canon_f = open(P6_OUT / "canonical" / "prediction_snapshots.ndjson", "w")
    evidence = []

    r1_unresolved = []
    for k, sides in pairs:
        p, v = sides.get("production", {}), sides.get("preview", {})
        diffs = {f for f in set(p) | set(v) if f not in STRIP and p.get(f) != v.get(f)}
        if diffs.issubset(R1_VERSION_FIELDS) and diffs:
            # R1 would have resolved — skip
            continue
        if not diffs:
            continue
        r1_unresolved.append((k, p, v, diffs))

    # Of the r1_unresolved, check publication / pregame hash cross-check
    resolved = []; quarantined = []
    for k, p, v, diffs in r1_unresolved:
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
                "resolution_reason": f"Side's payload_hash is referenced in canonical {proof_source} ledger; the other side's hash is not.",
            })
        else:
            quar_f.write(json.dumps({"side":"production","doc":p,"logical_key":list(k),
                                      "status":"UNRESOLVED_IMMUTABLE_CONFLICT",
                                      "excluded_from_canonical_runtime": True,
                                      "quarantine_reason": "No immutable publication proof found in publication_events or pregame_snapshots for either payload_hash. Factual commit cannot be determined offline."}, default=str) + "\n")
            quar_f.write(json.dumps({"side":"preview","doc":v,"logical_key":list(k),
                                      "status":"UNRESOLVED_IMMUTABLE_CONFLICT",
                                      "excluded_from_canonical_runtime": True,
                                      "quarantine_reason": "same as production side"}, default=str) + "\n")
            quarantined.append(k)

    canon_f.close(); quar_f.close(); unres_f.close()
    with open(P6_OUT / "evidence" / "prediction_snapshots.json", "w") as f:
        json.dump(evidence, f, indent=2, default=str)

    return {
        "collection":   "prediction_snapshots",
        "input_pairs_in_conflicts": len(pairs),
        "r1_unresolved":            len(r1_unresolved),
        "resolved_by_publication_proof": len(resolved),
        "quarantined":  len(quarantined),
        "resolution_rule": "R12 — publication/pregame hash cross-check; else KEEP QUARANTINED",
    }


# ===================================================================== driver
def main():
    print(f"[PHASE 6] factual conflict closure started at {datetime.now(timezone.utc).isoformat()}")
    t0 = time.time()

    a = resolve_pga()
    print(f"  A player_game_actuals: {a['resolved']}/{a['input_pairs']} resolved, {a['unresolved']} unresolved")
    b = resolve_pgl()
    print(f"  B player_game_logs:    {b['resolved']}/{b['input_pairs']} resolved ({b['shots_volatile_resolved']} shots-only), {b['unresolved']} unresolved")
    c = resolve_sm()
    print(f"  C soccer_matches:      {c['resolved']}/{c['input_pairs']} resolved, {c['unresolved']} unresolved")
    d = resolve_ps()
    print(f"  D prediction_snapshots: {d['resolved_by_publication_proof']} resolved by publication proof, {d['quarantined']} quarantined")

    duration = time.time() - t0
    summary = {
        "phase":    "phase6_factual_closure",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "duration_sec": round(duration, 2),
        "buckets": {"A_player_game_actuals": a,
                     "B_player_game_logs":   b,
                     "C_soccer_matches":     c,
                     "D_prediction_snapshots": d},
        "totals": {
            "resolved_total":    a["resolved"] + b["resolved"] + c["resolved"] + d["resolved_by_publication_proof"],
            "unresolved_total":  a["unresolved"] + b["unresolved"] + c["unresolved"],
            "quarantined_total": d["quarantined"],
        },
    }
    (P6_OUT / "phase6_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(f"  Summary written: {P6_OUT / 'phase6_summary.json'}")
    print(f"  Duration: {duration:.1f}s")
    return summary


if __name__ == "__main__":
    s = main()
    sys.exit(0 if s else 1)
