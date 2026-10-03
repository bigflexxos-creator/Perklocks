"""phase5_external_authority — Apply R8 + R9 and cross-check the
remaining 187 historical conflicts against orthogonal authoritative
evidence that already lives in the Phase 2/3 reconciled canonical
output.  OFFLINE ONLY.
"""
from __future__ import annotations
import collections, hashlib, json, pathlib, sys

PHASE3 = pathlib.Path("/tmp/perklocks_reconciled_output_phase3")
PHASE2 = pathlib.Path("/tmp/perklocks_reconciled_output")
OUT    = pathlib.Path("/tmp/perklocks_reconciled_output_phase5")
OUT.mkdir(exist_ok=True)
for d in ("canonical","unresolved","external_authority_evidence"):
    (OUT/d).mkdir(exist_ok=True)

R3A = {"historical_teams","historical_national_teams","observed_at",
       "national_team_observed_at","national_team_source","national_team_status",
       "current_team","current_national_team","nationality","aliases",
       "provider_ids","position","name_norm"}
R8_EXTENDED = R3A | {"source","name","nationality","position","roster_status",
                     "canonical_player_name"}
R3_LIST_LIKE  = {"historical_teams","historical_national_teams","aliases","provider_ids"}
R3_TIMESTAMPS = {"observed_at","national_team_observed_at"}

LEDGER = []
def log(entry): LEDGER.append(entry)

def _s(x): return "" if x is None else str(x)

def _pairs(path, types=None):
    bag = collections.defaultdict(dict)
    if not path.exists(): return []
    with open(path) as f:
        for ln in f:
            r = json.loads(ln)
            if types and r.get("type") not in types: continue
            if r.get("side") in ("production","preview"):
                bag[tuple(r["logical_key"])][r["side"]] = r["doc"]
    return list(bag.items())

def _ndjson(path): 
    if not path.exists(): return
    with open(path) as f:
        for ln in f: yield json.loads(ln)

def _diffs(p,v): return sorted({f for f in set(p)|set(v) if f!="_id" and p.get(f)!=v.get(f)})


# ─── Baseline: copy Phase 3 canonical ────────────────────────────────
def copy_phase3_baseline():
    for src in sorted((PHASE3/"canonical").glob("*.ndjson")):
        with open(src) as f_in, open(OUT/"canonical"/src.name,"w") as f_out:
            for ln in f_in: f_out.write(ln)


# ─── R8 — 14 player_identities ───────────────────────────────────────
def _r8_merge(p, v, diffs):
    merged = dict(p)
    for f in diffs:
        pv, vv = p.get(f), v.get(f)
        if f in R3_LIST_LIKE:
            unioned, seen = [], set()
            def _norm(x):
                if x is None: return []
                if isinstance(x, list): return x
                if isinstance(x, dict): return [x]
                return [x]
            for item in _norm(pv)+_norm(vv):
                k = json.dumps(item, sort_keys=True, default=str) if not isinstance(item, dict) else json.dumps(item, sort_keys=True, default=str)
                if k in seen: continue
                seen.add(k); unioned.append(item)
            merged[f] = unioned
        elif f in R3_TIMESTAMPS:
            merged[f] = max([x for x in (pv,vv) if x is not None], default=None)
        else:
            # populated-vs-null preference; else Production
            if pv is None and vv is not None: merged[f] = vv
            elif pv is not None and vv is None: merged[f] = pv
            else: merged[f] = pv
    return merged


def apply_r8():
    pairs = _pairs(PHASE2/"review"/"player_identities.ndjson",
                   types={"HISTORICAL_LOG_CONTENT_DIFF"})
    out_f = open(OUT/"canonical"/"player_identities.ndjson", "a")
    unresolved = open(OUT/"unresolved"/"player_identities.ndjson", "w")
    applied = 0; unresolved_n = 0
    for k, s in pairs:
        p, v = s.get("production") or {}, s.get("preview") or {}
        d = set(_diffs(p,v))
        # Already handled by R3a/R3b (diffs ⊆ R3a or = R3a∪{source}) — skip
        if d.issubset(R3A): continue
        if "source" in d and (d - {"source"}).issubset(R3A): continue
        # R8 scope?
        if d.issubset(R8_EXTENDED):
            merged = _r8_merge(p, v, d)
            # populated preference for extended scalars (name/nationality/position/roster_status/canonical_player_name)
            # — handled by _r8_merge default (prefer Production, with populated-vs-null override)
            # name diacritics: NFC normalization — treat Production as canonical form if both populated
            out_f.write(json.dumps({"canonical_from":"R8_extended_enrichment",
                                     "logical_key":list(k),"doc":merged}, default=str)+"\n")
            log({"rule":"R8","collection":"player_identities","logical_key":list(k),
                 "selected_authority":"merged","differing_fields":sorted(d),
                 "reason":"extended enrichment (R3a + name/nationality/position/roster_status/canonical_player_name/source); populated-vs-null preference + Production for scalars; deterministic union for lists"})
            applied += 1
        else:
            unresolved.write(json.dumps({"side":"production","doc":p,"logical_key":list(k)}, default=str)+"\n")
            unresolved.write(json.dumps({"side":"preview","doc":v,"logical_key":list(k)}, default=str)+"\n")
            unresolved_n += 1
    out_f.close(); unresolved.close()
    return applied, unresolved_n


# ─── R9 — tennis canonical-key migration ─────────────────────────────
def apply_r9():
    """Remove the colliding row from Phase 3 SAFE and re-emit two
    distinct rows under the stronger key (tourney_id, winner_id,
    loser_id, date).  Any Preview-side empty-shell is kept distinct
    from Production's populated real match."""
    pairs = _pairs(PHASE2/"review"/"tennis_matches_history.ndjson",
                   types={"HISTORICAL_LOG_CONTENT_DIFF"})
    # Phase 3 canonical row for the colliding key
    src = OUT/"canonical"/"tennis_matches_history.ndjson"
    rows = list(_ndjson(src))
    migrated = 0; new_rows = []
    for r in rows:
        new_rows.append(r)  # default copy

    # For each colliding pair, strip the colliding Phase 3 row and emit two
    collisions = [k for k,_ in pairs]
    for k, s in pairs:
        p = s.get("production") or {}
        v = s.get("preview") or {}
        old_tuple = tuple(k)
        # Strip the Phase 3 canonical row(s) that match the old tuple.
        new_rows = [r for r in new_rows if tuple(r.get("logical_key",[])) != old_tuple]
        # Emit Production side with stronger key
        prod_key = ("tourney+winner+loser+date", _s(p.get("tourney_id")),
                    _s(p.get("winner_id")), _s(p.get("loser_id")),
                    _s(p.get("date")))
        new_rows.append({"canonical_from":"R9_migrated_production",
                         "logical_key":list(prod_key),"doc":p})
        # Emit Preview side with stronger key (even if empty-shell)
        prev_key = ("tourney+winner+loser+date", _s(v.get("tourney_id")),
                    _s(v.get("winner_id")), _s(v.get("loser_id")),
                    _s(v.get("date")))
        new_rows.append({"canonical_from":"R9_migrated_preview",
                         "logical_key":list(prev_key),"doc":v})
        log({"rule":"R9","collection":"tennis_matches_history",
             "old_logical_key":list(old_tuple),
             "new_production_logical_key":list(prod_key),
             "new_preview_logical_key":list(prev_key),
             "reason":"canonical-key migration (tourney_id+winner_id+loser_id+date); preserves both legitimate matches"})
        migrated += 1
    with open(src, "w") as f:
        for r in new_rows: f.write(json.dumps(r, default=str)+"\n")
    return migrated


# ─── External-authority cross-checks ─────────────────────────────────
def apply_external_pga():
    """player_game_actuals: resolve team affiliation via player_game_logs
    which carries authoritative team context per (sport, event, player)."""
    pairs = _pairs(PHASE2/"review"/"player_game_actuals.ndjson",
                   types={"PROVIDER_ACTUAL_CONFLICT"})
    # Build index from Phase 3 canonical player_game_logs
    pgl_team = {}
    for r in _ndjson(OUT/"canonical"/"player_game_logs.ndjson"):
        d = r.get("doc") or {}
        key = (d.get("sport"),
               d.get("canonical_event_id") or d.get("game_id"),
               d.get("canonical_player_id") or d.get("player_id"))
        if all(key) and d.get("team"):
            pgl_team[key] = d["team"]
    unresolved = open(OUT/"unresolved"/"player_game_actuals.ndjson", "w")
    out_f = open(OUT/"canonical"/"player_game_actuals.ndjson", "a")
    resolved = 0; still = 0; evidence = []
    for k, s in pairs:
        p, v = s.get("production") or {}, s.get("preview") or {}
        key = (p.get("sport") or v.get("sport"),
               p.get("canonical_event_id") or v.get("canonical_event_id"),
               p.get("canonical_player_id") or v.get("canonical_player_id"))
        auth_team = pgl_team.get(key)
        if auth_team and auth_team in (p.get("team"), v.get("team")):
            winner = p if p.get("team") == auth_team else v
            side = "production" if winner is p else "preview"
            out_f.write(json.dumps({"canonical_from":f"R10_pga_pgl_cross_{side}",
                                     "logical_key":list(k),"doc":winner}, default=str)+"\n")
            log({"rule":"R10","collection":"player_game_actuals","logical_key":list(k),
                 "selected_authority":side,"evidence_type":"player_game_logs cross-check",
                 "evidence_key":list(key),"authoritative_team":auth_team,
                 "reason":"player_game_logs team field matches selected side"})
            evidence.append({"logical_key":list(k),"winner_side":side,
                             "authoritative_team":auth_team,"evidence":"player_game_logs"})
            resolved += 1
        else:
            unresolved.write(json.dumps({"side":"production","doc":p,"logical_key":list(k)}, default=str)+"\n")
            unresolved.write(json.dumps({"side":"preview","doc":v,"logical_key":list(k)}, default=str)+"\n")
            still += 1
    out_f.close(); unresolved.close()
    with open(OUT/"external_authority_evidence"/"player_game_actuals.json", "w") as f:
        json.dump({"resolved":resolved,"still_unresolved":still,"records":evidence}, f, indent=2, default=str)
    return resolved, still


def apply_external_pgl():
    """player_game_logs stat corrections: cross-check against
    player_game_actuals.actuals (which carries the authoritative provider
    stat dict and was identical on both sides for 7,353 rows)."""
    pairs = _pairs(PHASE2/"review"/"player_game_logs.ndjson",
                   types={"HISTORICAL_LOG_CONTENT_DIFF"})
    # Index: (sport, canonical_event_id, canonical_player_id) -> actuals dict
    pga_actuals = {}
    for r in _ndjson(OUT/"canonical"/"player_game_actuals.ndjson"):
        d = r.get("doc") or {}
        key = (d.get("sport"),
               d.get("canonical_event_id"),
               d.get("canonical_player_id"))
        if all(key) and d.get("actuals") is not None:
            pga_actuals[key] = d["actuals"]
    unresolved = open(OUT/"unresolved"/"player_game_logs.ndjson", "w")
    out_f = open(OUT/"canonical"/"player_game_logs.ndjson", "a")
    resolved = 0; still = 0; evidence = []
    # Filter R4-already-applied: those where single-field diff with one null — only non-R4 rows show up here
    for k, s in pairs:
        p, v = s.get("production") or {}, s.get("preview") or {}
        d = set(_diffs(p, v))
        # Skip rows R4 already handled (single-field null-vs-populated)
        if len(d) == 1:
            f = next(iter(d))
            if (p.get(f) is None) ^ (v.get(f) is None):
                continue  # was handled in Phase 3
        key = (p.get("sport") or v.get("sport"),
               p.get("canonical_event_id") or v.get("canonical_event_id"),
               p.get("canonical_player_id") or v.get("canonical_player_id"))
        auth = pga_actuals.get(key)
        if auth:
            prod_match = all(auth.get(f) == p.get(f) for f in d if f in auth)
            prev_match = all(auth.get(f) == v.get(f) for f in d if f in auth)
            if prod_match and not prev_match:
                out_f.write(json.dumps({"canonical_from":"R10_pgl_pga_cross_production",
                                         "logical_key":list(k),"doc":p}, default=str)+"\n")
                log({"rule":"R10","collection":"player_game_logs","logical_key":list(k),
                     "selected_authority":"production","evidence_type":"player_game_actuals cross-check",
                     "diffs":sorted(d),"reason":"actuals dict matches Production stats"})
                evidence.append({"logical_key":list(k),"winner_side":"production",
                                 "evidence":"player_game_actuals.actuals","matched_fields":sorted(d)})
                resolved += 1; continue
            if prev_match and not prod_match:
                out_f.write(json.dumps({"canonical_from":"R10_pgl_pga_cross_preview",
                                         "logical_key":list(k),"doc":v}, default=str)+"\n")
                log({"rule":"R10","collection":"player_game_logs","logical_key":list(k),
                     "selected_authority":"preview","evidence_type":"player_game_actuals cross-check",
                     "diffs":sorted(d),"reason":"actuals dict matches Preview stats"})
                evidence.append({"logical_key":list(k),"winner_side":"preview",
                                 "evidence":"player_game_actuals.actuals","matched_fields":sorted(d)})
                resolved += 1; continue
        unresolved.write(json.dumps({"side":"production","doc":p,"logical_key":list(k)}, default=str)+"\n")
        unresolved.write(json.dumps({"side":"preview","doc":v,"logical_key":list(k)}, default=str)+"\n")
        still += 1
    out_f.close(); unresolved.close()
    with open(OUT/"external_authority_evidence"/"player_game_logs.json", "w") as f:
        json.dump({"resolved":resolved,"still_unresolved":still,"records":evidence}, f, indent=2, default=str)
    return resolved, still


def apply_external_soccer():
    """soccer_matches score corrections: cross-check against
    soccer_player_game_logs aggregated goals per team per match."""
    pairs = _pairs(PHASE2/"review"/"soccer_matches.ndjson",
                   types={"HISTORICAL_LOG_CONTENT_DIFF"})
    # Build aggregated goals from Phase 3 soccer_player_game_logs
    agg = collections.defaultdict(lambda: collections.defaultdict(int))
    for r in _ndjson(OUT/"canonical"/"soccer_player_game_logs.ndjson"):
        d = r.get("doc") or {}
        mid = d.get("match_id") or d.get("canonical_event_id")
        team = d.get("team")
        g = d.get("goals") or 0
        if mid and team is not None:
            agg[mid][team] += (g if isinstance(g,(int,float)) else 0)
    unresolved = open(OUT/"unresolved"/"soccer_matches.ndjson", "w")
    out_f = open(OUT/"canonical"/"soccer_matches.ndjson", "a")
    resolved = 0; still = 0; evidence = []
    for k, s in pairs:
        p, v = s.get("production") or {}, s.get("preview") or {}
        mid = p.get("match_id") or p.get("_id") or v.get("match_id") or v.get("_id")
        home = p.get("home_team") or v.get("home_team")
        away = p.get("away_team") or v.get("away_team")
        goals = agg.get(mid) if mid else None
        # Fallback: no cross-match possible → stay unresolved
        if goals and (home in goals or away in goals):
            auth_home = goals.get(home, 0)
            auth_away = goals.get(away, 0)
            def _match(side):
                return side.get("home_score") == auth_home and side.get("away_score") == auth_away
            if _match(p) and not _match(v):
                out_f.write(json.dumps({"canonical_from":"R10_soccer_player_log_cross_production",
                                         "logical_key":list(k),"doc":p}, default=str)+"\n")
                log({"rule":"R10","collection":"soccer_matches","logical_key":list(k),
                     "selected_authority":"production","evidence_type":"soccer_player_game_logs aggregated goals",
                     "authoritative_goals":{home:auth_home,away:auth_away},
                     "reason":"aggregated player goals match Production score"})
                evidence.append({"logical_key":list(k),"winner_side":"production",
                                 "aggregated_goals":{home:auth_home,away:auth_away}})
                resolved += 1; continue
            if _match(v) and not _match(p):
                out_f.write(json.dumps({"canonical_from":"R10_soccer_player_log_cross_preview",
                                         "logical_key":list(k),"doc":v}, default=str)+"\n")
                log({"rule":"R10","collection":"soccer_matches","logical_key":list(k),
                     "selected_authority":"preview","evidence_type":"soccer_player_game_logs aggregated goals",
                     "authoritative_goals":{home:auth_home,away:auth_away},
                     "reason":"aggregated player goals match Preview score"})
                evidence.append({"logical_key":list(k),"winner_side":"preview",
                                 "aggregated_goals":{home:auth_home,away:auth_away}})
                resolved += 1; continue
        unresolved.write(json.dumps({"side":"production","doc":p,"logical_key":list(k)}, default=str)+"\n")
        unresolved.write(json.dumps({"side":"preview","doc":v,"logical_key":list(k)}, default=str)+"\n")
        still += 1
    out_f.close(); unresolved.close()
    with open(OUT/"external_authority_evidence"/"soccer_matches.json", "w") as f:
        json.dump({"resolved":resolved,"still_unresolved":still,"records":evidence}, f, indent=2, default=str)
    return resolved, still


def apply_external_ps():
    """prediction_snapshots: cross-check against publication_events
    payload_hash to see if one side was independently published."""
    pairs = _pairs(PHASE2/"conflicts"/"prediction_snapshots.ndjson",
                   types={"IMMUTABLE_CONFLICT"})
    # Build index: (prediction_id, publication_version|snapshot_version) -> set(payload_hash)
    pub_index = collections.defaultdict(set)
    for r in _ndjson(OUT/"canonical"/"publication_events.ndjson"):
        d = r.get("doc") or {}
        pid = d.get("prediction_id"); pver = d.get("publication_version")
        ph  = d.get("payload_hash")
        if pid and ph:
            pub_index[(pid, pver)].add(ph)
            pub_index[(pid, None)].add(ph)
    unresolved = open(OUT/"unresolved"/"prediction_snapshots.ndjson", "w")
    out_f = open(OUT/"canonical"/"prediction_snapshots.ndjson", "a")
    resolved = 0; quarantined = 0; evidence = []
    for k, s in pairs:
        p, v = s.get("production") or {}, s.get("preview") or {}
        d = set(_diffs(p, v))
        if "payload_hash" not in d or (d & {"model_version","calibration_version",
                                            "feature_snapshot_version","fusion_version",
                                            "scoring_version","simulation_version",
                                            "validator_version","board_version"}):
            # not in the "same-version drift" scope — skip; already R1-applied
            continue
        pid = p.get("prediction_id") or v.get("prediction_id")
        ver = p.get("snapshot_version") or v.get("snapshot_version")
        pub_hashes = pub_index.get((pid, ver)) or pub_index.get((pid, None)) or set()
        prod_h = p.get("payload_hash"); prev_h = v.get("payload_hash")
        decision = None; reason = None
        if prod_h in pub_hashes and prev_h not in pub_hashes:
            decision = "production"; reason = f"payload_hash {prod_h[:12]}… found in publication_events ledger"
        elif prev_h in pub_hashes and prod_h not in pub_hashes:
            decision = "preview"; reason = f"payload_hash {prev_h[:12]}… found in publication_events ledger"
        if decision:
            winner = p if decision == "production" else v
            out_f.write(json.dumps({"canonical_from":f"R11_ps_pub_events_cross_{decision}",
                                     "logical_key":list(k),"doc":winner}, default=str)+"\n")
            log({"rule":"R11","collection":"prediction_snapshots","logical_key":list(k),
                 "selected_authority":decision,"evidence_type":"publication_events payload_hash",
                 "reason":reason})
            evidence.append({"logical_key":list(k),"winner_side":decision,"reason":reason,
                             "candidate_hashes_in_ledger":[h[:12] for h in pub_hashes]})
            resolved += 1
        else:
            # Both or neither in ledger — QUARANTINED
            unresolved.write(json.dumps({"side":"production","doc":p,"logical_key":list(k),"status":"UNRESOLVED_IMMUTABLE_CONFLICT"}, default=str)+"\n")
            unresolved.write(json.dumps({"side":"preview","doc":v,"logical_key":list(k),"status":"UNRESOLVED_IMMUTABLE_CONFLICT"}, default=str)+"\n")
            evidence.append({"logical_key":list(k),"status":"QUARANTINED",
                             "production_hash":prod_h[:12] if prod_h else None,
                             "preview_hash":prev_h[:12] if prev_h else None,
                             "publication_ledger_hashes":[h[:12] for h in pub_hashes]})
            quarantined += 1
    out_f.close(); unresolved.close()
    with open(OUT/"external_authority_evidence"/"prediction_snapshots.json", "w") as f:
        json.dump({"resolved":resolved,"quarantined":quarantined,"records":evidence}, f, indent=2, default=str)
    return resolved, quarantined


# ─── Integrity + manifest ────────────────────────────────────────────
def integrity_and_manifest(summary):
    report = {"production_and_preview_sources_unchanged": True,
              "phase2_and_phase3_canonical_unchanged": True,
              "ledger_entries": len(LEDGER),
              "unresolved_total": summary["unresolved_total"]}
    # Verify sources
    pre = json.load(open(PHASE2/"_pre_run_source_sha256.json"))
    tampered=[]
    for path,expected in pre.items():
        p = pathlib.Path(path) if path.startswith("/") else pathlib.Path("/tmp/production_reconcile_input")/path
        h=hashlib.sha256()
        with open(p,"rb") as f:
            while True:
                c=f.read(1<<20)
                if not c: break
                h.update(c)
        if h.hexdigest() != expected: tampered.append(path)
    report["production_and_preview_sources_unchanged"] = not tampered
    # Verify Phase 2/3 canonical fingerprint unchanged
    for lbl, pdir in [("phase2",PHASE2),("phase3",PHASE3)]:
        fp = json.load(open(pdir/"_canonical_fingerprint.json"))
        now = {}
        for p in sorted((pdir/"canonical").glob("*.ndjson")):
            rows=[]
            with open(p) as f:
                for ln in f:
                    r=json.loads(ln); rows.append((tuple(r.get("logical_key",[])), r.get("canonical_from","")))
            rows.sort()
            now[p.name] = hashlib.sha256(json.dumps(rows,default=str).encode()).hexdigest()
        report[f"{lbl}_canonical_unchanged"] = (fp == now)
    # Phase 5 duplicate check
    dups={}
    for p in sorted((OUT/"canonical").glob("*.ndjson")):
        seen=set(); dup=0
        with open(p) as f:
            for ln in f:
                r=json.loads(ln); k=tuple(r.get("logical_key",[]))
                if k in seen: dup+=1
                else: seen.add(k)
        dups[p.name]=dup
    report["phase5_duplicate_keys_per_collection"] = dups
    report["phase5_no_duplicates"] = all(v==0 for v in dups.values())
    # Deterministic fingerprint
    fp={}
    for p in sorted((OUT/"canonical").glob("*.ndjson")):
        rows=[]
        with open(p) as f:
            for ln in f:
                r=json.loads(ln); rows.append((tuple(r.get("logical_key",[])), r.get("canonical_from","")))
        rows.sort()
        fp[p.name] = hashlib.sha256(json.dumps(rows,default=str).encode()).hexdigest()
    (OUT/"_canonical_fingerprint.json").write_text(json.dumps(fp,indent=2))
    (OUT/"integrity_report.json").write_text(json.dumps(report,indent=2,default=str))
    # Final manifest
    manifest = {"spec_version":"phase5.v1", "rules_applied":["R1","R2","R3a","R3b","R4","R5","R6","R7","R8","R9","R10","R11"],
                "summary":summary, "integrity":report,
                "ready_for_atlas": (report["phase2_canonical_unchanged"]
                                    and report["phase3_canonical_unchanged"]
                                    and report["phase5_no_duplicates"]
                                    and summary["unresolved_total"] == 0),
                "quarantine_notes":"UNRESOLVED_IMMUTABLE_CONFLICT rows live in /unresolved/ and cannot enter canonical runtime truth",}
    (OUT/"reconciliation_manifest_final_candidate.json").write_text(json.dumps(manifest,indent=2,default=str))
    return report


def main():
    copy_phase3_baseline()
    r8_n, r8_un = apply_r8()
    r9_n = apply_r9()
    pga_r, pga_u = apply_external_pga()
    pgl_r, pgl_u = apply_external_pgl()
    sm_r, sm_u   = apply_external_soccer()
    ps_r, ps_q   = apply_external_ps()
    # Ledger
    with open(OUT/"resolution_ledger.ndjson","w") as f:
        for e in LEDGER: f.write(json.dumps(e, default=str)+"\n")
    summary = {
        "R8_player_identities": r8_n,
        "R9_tennis_collisions_migrated": r9_n,
        "R10_player_game_actuals_resolved": pga_r,
        "R10_player_game_actuals_unresolved": pga_u,
        "R10_player_game_logs_resolved": pgl_r,
        "R10_player_game_logs_unresolved": pgl_u,
        "R10_soccer_matches_resolved": sm_r,
        "R10_soccer_matches_unresolved": sm_u,
        "R11_prediction_snapshots_resolved": ps_r,
        "R11_prediction_snapshots_quarantined": ps_q,
        "unresolved_total": pga_u + pgl_u + sm_u + ps_q,
    }
    (OUT/"resolution_ledger_summary.json").write_text(json.dumps(summary,indent=2,default=str))
    report = integrity_and_manifest(summary)
    print(json.dumps({"summary":summary,"integrity":report}, indent=2, default=str))
    return 0

if __name__ == "__main__":
    sys.exit(main())
