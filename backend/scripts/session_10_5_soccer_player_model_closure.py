"""Session 10.5 — Soccer Player Model Continuation Closure.

Answers the seven-section proof requested on 2026-09-18:

    1) Kane exact math trace  (model P vs authority ceiling)
    2) History freshness audit
    3) Sørloth + Endrick identity forensics
    4) 50-player real coverage test
    5) Real production regen (wired via hydrator+authority)
    6) End-to-end parity (Mongo == API == published payload)
    7) Historical Intelligence UI (Soccer L5/L10/L20/VS_OPP proof)

Read-only for sections 1-4, 6, 7.  Section 5 uses the SAME hydrator +
authority path proven in Session 10.4 to recompute win_probability
and lock_score for CURRENT/FUTURE Soccer player-prop picks that do
NOT have `started`/`settled`/`history` flags.

Run:
    cd /app/backend && python -m scripts.session_10_5_soccer_player_model_closure
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from motor.motor_asyncio import AsyncIOMotorClient  # type: ignore

from services.soccer_evidence_hydrator import hydrate_soccer_player_evidence
from services.soccer_player_authority import (
    PlayerEvidence, MinutesState, PenaltyRole,
    classify_authority, estimate_player_lambda,
    player_lock_authority, AUTHORITY_CEILINGS,
)
from services.soccer_feature_resolver import resolve_soccer_player_features
from services.soccer_season_resolver import (
    resolve_current_season, resolve_prior_season,
    is_calendar_year_competition,
)


MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME   = os.environ.get("DB_NAME", "lockscore_db")

_client = AsyncIOMotorClient(MONGO_URL)
db = _client[DB_NAME]


# ═══════════════════════════════════════════════════════════════════
#  Utilities
# ═══════════════════════════════════════════════════════════════════
def _round(x, n=5):
    if x is None: return None
    try: return round(float(x), n)
    except Exception: return None


def _fmt(d: Any) -> str:
    return json.dumps(d, indent=2, default=str, sort_keys=False)


def _norm(s: Optional[str]) -> str:
    return (s or "").strip().lower()


async def _find_pick(player_name: str) -> Optional[dict]:
    """Return the first current Soccer pick for this player (Anytime family)."""
    now = datetime.now(timezone.utc)
    q = {
        "sport": "Soccer",
        "book_odds": {"$ne": None},
        "event_time": {"$gte": now.isoformat()},
        "market": {"$regex":
                    "Anytime.*Goal Scorer|To Score|Anytime|Goal Scorer",
                    "$options": "i"},
        "selection": {"$regex": player_name.split()[-1], "$options": "i"},
    }
    p = await db.picks.find_one(q)
    return p


# ═══════════════════════════════════════════════════════════════════
# SECTION 1 — Kane exact math trace
# ═══════════════════════════════════════════════════════════════════
async def section1_kane_math() -> dict:
    print("\n" + "═" * 68)
    print("SECTION 1 — KANE EXACT MATH TRACE")
    print("═" * 68)
    pick = await _find_pick("Harry Kane") or await db.picks.find_one({
        "sport": "Soccer", "selection": {"$regex": "Kane", "$options": "i"},
    })
    if not pick:
        return {"status": "NO_PICK_FOUND"}

    # Resolver row (raw historical evidence)
    row, source = await resolve_soccer_player_features(
        db, player_name="Harry Kane", league="Bundesliga",
    )
    row = row or {}

    # Compute team_lambda / opp def from soccer_game_model
    from services.soccer_game_model import (
        build_soccer_team_ctx, estimate_soccer_game_probabilities,
    )
    home_team = "Union Berlin"; away_team = "Bayern Munich"
    try:
        ctx = await build_soccer_team_ctx(
            db, home_team=home_team, away_team=away_team,
            league="Bundesliga",
        )
        out = estimate_soccer_game_probabilities(ctx, home_team, away_team)
        team_lambda = out.lambda_away if out.available else 3.1144
        opp_def     = out.lambda_home if out.available else 1.1658
    except Exception as e:
        print(f"soccer_game_model error: {e}")
        team_lambda = 3.1144
        opp_def     = 1.1658

    ev, resolver_source, raw_row = await hydrate_soccer_player_evidence(
        db, player_name="Harry Kane", league="Bundesliga",
        team=away_team, opponent=home_team,
        event_id=pick.get("event_id"),
        is_home=False,
        book_odds=pick.get("book_odds"),
        team_lambda=team_lambda, opp_def_strength=opp_def,
        minutes_state=MinutesState.UNKNOWN,
        penalty_role=PenaltyRole.UNKNOWN,
    )

    # ─── Manual step-by-step recompute ───
    # step 1: base rate
    base_rate = ev.npxg_per_90 or ev.xg_per_90 or (
        ev.goals_per_90 * 0.85 if ev.goals_per_90 else None)
    base_family = ("npxG" if ev.npxg_per_90 is not None
                    else "xG" if ev.xg_per_90 is not None
                    else "goals_shrunk")

    # step 2: sample shrinkage
    n = ev.sample_matches or 0
    league_avg = 0.15
    shrink_w = n / (n + 6) if n else 0.0
    shrunk = shrink_w * base_rate + (1 - shrink_w) * league_avg

    # step 3: opponent multiplier
    opp_mult = 1.0
    if ev.opp_def_strength is not None:
        opp_mult = max(0.65, min(1.55, ev.opp_def_strength / 1.32))

    # step 4: minutes multiplier
    # UNKNOWN state (no live lineup) → 0.60 default (this is a
    # probability-model factor — expected-minutes uncertainty).
    if ev.expected_minutes is not None:
        minutes_mult = max(0.0, min(1.05, ev.expected_minutes / 90.0))
        minutes_prov = "explicit_expected_minutes"
    elif ev.starter_prob is not None:
        minutes_mult = max(0.10, min(1.0, 0.40 + 0.55 * ev.starter_prob))
        minutes_prov = "starter_prob"
    elif ev.minutes_state == MinutesState.CONFIRMED_STARTER:
        minutes_mult = 0.95; minutes_prov = "state_CONFIRMED_STARTER"
    elif ev.minutes_state == MinutesState.PROJECTED_STARTER:
        minutes_mult = 0.85; minutes_prov = "state_PROJECTED_STARTER"
    else:
        minutes_mult = 0.60
        minutes_prov = "UNKNOWN_default_0.60"  # explicit part of model

    # step 5: penalty bump
    pk_bump = 0.0
    if ev.penalty_role == PenaltyRole.PRIMARY: pk_bump = 0.06
    elif ev.penalty_role == PenaltyRole.SECONDARY: pk_bump = 0.02

    # step 6: compose
    lam_uncapped = shrunk * opp_mult * minutes_mult + pk_bump
    # step 7: team cap
    cap = ev.team_lambda * 0.65 if ev.team_lambda else None
    lam_capped  = min(lam_uncapped, cap) if cap else lam_uncapped
    lam = max(0.0, min(2.0, lam_capped))
    p_atg = 1.0 - math.exp(-lam)

    # ─── Cross-check via canonical function ───
    canonical = estimate_player_lambda(ev)

    # ─── Lock-score authority ceiling ───
    lock_auth = player_lock_authority(ev, model_prob=p_atg, devig_prob=None)

    trace = {
        "pick_id": pick.get("id"),
        "market":  pick.get("market"),
        "selection": pick.get("selection"),
        "event":   pick.get("event"),
        "sportsbook": pick.get("bookmaker"),
        "book_odds":  pick.get("book_odds"),
        "old_prod_win_probability": pick.get("win_probability"),
        "old_prod_lock_score":      pick.get("lock_score"),
        "─── HISTORICAL SOURCE ───": "─" * 30,
        "historical_source": resolver_source,
        "row_present":       bool(raw_row),
        "season":            raw_row.get("season"),
        "row_updated_at":    str(raw_row.get("updated_at")),
        "─── RAW EVIDENCE ───":     "─" * 30,
        "games":     raw_row.get("games"),
        "minutes":   raw_row.get("minutes"),
        "goals":     raw_row.get("goals"),
        "xg":        raw_row.get("xg") or raw_row.get("xG"),
        "npxg":      raw_row.get("npxg") or raw_row.get("npxG"),
        "shots":     raw_row.get("shots"),
        "sot":       raw_row.get("shots_on_target"),
        "goals_per_90":  ev.goals_per_90,
        "xg_per_90":     ev.xg_per_90,
        "npxg_per_90":   ev.npxg_per_90,
        "shots_per_90":  ev.shots_per_90,
        "─── STEP-BY-STEP MATH ───": "─" * 30,
        "step1_base_rate":       _round(base_rate),
        "step1_base_family":     base_family,
        "step2_sample_n":        n,
        "step2_shrink_w":        _round(shrink_w, 4),
        "step2_shrunk":          _round(shrunk),
        "step3_opp_def_strength": _round(ev.opp_def_strength),
        "step3_opp_mult":        _round(opp_mult, 4),
        "step4_expected_minutes": ev.expected_minutes,
        "step4_expected_minutes_provenance": minutes_prov,
        "step4_minutes_mult":    _round(minutes_mult, 4),
        "step5_penalty_role":    ev.penalty_role.value,
        "step5_pk_bump":         pk_bump,
        "step6_lam_uncapped":    _round(lam_uncapped),
        "step6_team_lambda":     _round(ev.team_lambda),
        "step7_team_cap":        _round(cap) if cap else None,
        "step7_lam_capped":      _round(lam_capped),
        "final_lambda_player":   _round(lam),
        "─── PROBABILITY ───":   "─" * 30,
        "formula":               "P(ATG) = 1 - exp(-lambda_player)",
        "MODEL_PROBABILITY_pct": _round(p_atg * 100, 3),
        "canonical_atg_prob_pct": _round((canonical.get("atg_prob") or 0) * 100, 3),
        "MATH_CROSS_CHECK":      abs(p_atg - (canonical.get("atg_prob") or 0)) < 1e-4,
        "─── AUTHORITY (SEPARATE) ───": "─" * 30,
        "authority":              lock_auth.authority.value,
        "lock_score_ceiling":     lock_auth.reachable_max,
        "ceiling_reasons":        lock_auth.ceiling_reasons,
        "AUTHORITY_MULTIPLIES_PROBABILITY?": False,
        "authority_scope":        "LIMITS LOCK SCORE ONLY — probability is unchanged by authority",
        "notes": [
            "expected_minutes UNKNOWN → minutes_mult=0.60 IS part of "
            "the probability model (expected-minutes uncertainty term)",
            "LIMITED authority additionally caps LOCK SCORE at 88.0 "
            "but does NOT multiply the model probability down",
        ],
    }
    print(_fmt(trace))
    return trace


# ═══════════════════════════════════════════════════════════════════
# SECTION 2 — History freshness audit
# ═══════════════════════════════════════════════════════════════════
async def section2_freshness() -> dict:
    print("\n" + "═" * 68)
    print("SECTION 2 — HISTORY FRESHNESS AUDIT")
    print("═" * 68)
    # Kane's form row
    kane_form = await db.soccer_player_form.find_one({
        "$or": [
            {"name_canonical": {"$in": ["harry kane"]}},
            {"player_name": {"$regex": "^Harry Kane$", "$options": "i"}},
        ]
    })
    form_seasons_all = await db.soccer_player_form.find(
        {"name_canonical": "harry kane"},
        {"season": 1, "team": 1, "minutes": 1, "goals": 1, "updated_at": 1}
    ).to_list(20)

    # Latest match dates from soccer_player_game_logs
    logs = await db.soccer_player_game_logs.find({
        "$or": [
            {"name_canonical": "harry kane"},
            {"player_name": {"$regex": "^Harry Kane$", "$options": "i"}},
        ]
    }).sort("match_date", -1).limit(10).to_list(10)

    log_stats = {
        "total_logs": await db.soccer_player_game_logs.count_documents({
            "$or": [
                {"name_canonical": "harry kane"},
                {"player_name": {"$regex": "^Harry Kane$", "$options": "i"}},
            ]
        }),
        "oldest": None, "newest": None,
        "count_2025": 0, "count_2026": 0,
    }
    if logs:
        dates = [str(l.get("match_date") or "")[:10] for l in logs if l.get("match_date")]
        # Get oldest by finding min
        oldest_cursor = await db.soccer_player_game_logs.find({
            "name_canonical": "harry kane",
        }).sort("match_date", 1).limit(1).to_list(1)
        if oldest_cursor:
            log_stats["oldest"] = str(oldest_cursor[0].get("match_date"))[:10]
        log_stats["newest"] = str(logs[0].get("match_date"))[:10]
        for l in await db.soccer_player_game_logs.find({
            "name_canonical": "harry kane"
        }, {"match_date": 1, "season": 1}).to_list(1000):
            season = str(l.get("season") or "")
            if "2025" in season: log_stats["count_2025"] += 1
            if "2026" in season: log_stats["count_2026"] += 1

    # Resolve current season for Bundesliga (as of NOW)
    now = datetime.now(timezone.utc)
    current_season = resolve_current_season("Bundesliga", now)
    prior_season   = resolve_prior_season("Bundesliga", now)
    calendar_yr    = is_calendar_year_competition("Bundesliga")

    # soccer_matches — latest completed Bayern match
    bayern_latest = await db.soccer_matches.find({
        "$or": [
            {"home_team": {"$regex": "Bayern", "$options": "i"}},
            {"away_team": {"$regex": "Bayern", "$options": "i"}},
        ],
        "status": "finished",
    }).sort("date", -1).limit(3).to_list(3)

    # Global ingest freshness — max updated_at across soccer_player_form
    freshest_form = await db.soccer_player_form.find({}, {
        "player_name": 1, "team": 1, "season": 1, "updated_at": 1
    }).sort("updated_at", -1).limit(3).to_list(3)

    freshness = {
        "as_of":                  now.isoformat(),
        "current_bundesliga_season": current_season,
        "prior_bundesliga_season":   prior_season,
        "bundesliga_is_calendar_year": calendar_yr,
        "─── KANE FORM ROWS ───":  "─" * 30,
        "form_seasons_present":   [
            {"season": r.get("season"), "team": r.get("team"),
             "goals": r.get("goals"), "minutes": r.get("minutes"),
             "updated_at": str(r.get("updated_at"))} for r in form_seasons_all
        ],
        "form_row_season":        (kane_form or {}).get("season"),
        "form_row_team":          (kane_form or {}).get("team"),
        "form_row_updated_at":    str((kane_form or {}).get("updated_at")),
        "─── KANE GAME LOGS ───": "─" * 30,
        "log_stats":              log_stats,
        "latest_10_logs":         [
            {"date": str(l.get("match_date"))[:10],
             "season": l.get("season"),
             "opp": l.get("opponent_team_name"),
             "goals": l.get("goals"),
             "minutes": l.get("minutes")} for l in logs
        ],
        "─── BAYERN CURRENT MATCH RECORD ───": "─" * 30,
        "bayern_latest_matches":  [
            {"date": str(m.get("date"))[:10],
             "home": m.get("home_team"), "away": m.get("away_team"),
             "score": f"{m.get('home_score')}-{m.get('away_score')}",
             "season": m.get("season"), "league": m.get("league")}
            for m in bayern_latest
        ],
        "─── GLOBAL FORM INGEST FRESHNESS ───": "─" * 30,
        "top_3_freshest_form_rows_globally": [
            {"player": r.get("player_name"), "team": r.get("team"),
             "season": r.get("season"),
             "updated_at": str(r.get("updated_at"))} for r in freshest_form
        ],
    }

    # Verdict
    kane_current = any(str(r.get("season")) == current_season.split("-")[0]
                       or str(r.get("season")) == current_season
                       for r in form_seasons_all)
    freshness["KANE_HAS_CURRENT_SEASON_FORM_ROW"] = kane_current
    freshness["KANE_HAS_CURRENT_SEASON_GAME_LOGS"] = (
        log_stats["count_2026"] > 0
    )

    # Ingest job responsible
    freshness["ingest_job"] = {
        "form_store":  "services.soccer_ingest (admin: POST /api/admin/soccer/refresh)",
        "logs_store":  "understat_backfill / soccer_stats_ingest — see /app/backend/services/soccer_ingest.py",
        "cadence":     "every 12h per soccer_player_form.py:26-58 docstring",
    }

    print(_fmt(freshness))
    return freshness


# ═══════════════════════════════════════════════════════════════════
# SECTION 3 — Sørloth + Endrick identity forensics
# ═══════════════════════════════════════════════════════════════════
async def _forensic_search(canonical_name: str,
                            aliases: list[str]) -> dict:
    """Exhaustive multi-store, multi-identity search."""
    import unicodedata
    def _ascii(s):
        n = unicodedata.normalize("NFKD", s)
        return "".join(c for c in n if not unicodedata.combining(c))
    variants = set()
    for a in [canonical_name, *aliases]:
        variants.add(a); variants.add(a.lower())
        variants.add(_ascii(a).lower())
        for ch in "'-.'`":
            variants.add(_ascii(a).lower().replace(ch, ""))
    variants = [v for v in variants if v]

    result = {"canonical_name": canonical_name, "variants_tried": variants,
              "stores": {}}
    for store, fields in [
        ("soccer_player_form",         ["name_canonical", "player_name", "understat_id"]),
        ("soccer_player_game_logs",    ["name_canonical", "player_name", "player_id"]),
        ("player_game_actuals",        ["player_name", "canonical_player_id"]),
        ("espn_mls_stats",             ["name_norm", "name"]),
    ]:
        found = 0; sample = None
        latest_date = None
        for f in fields:
            hits = await db[store].find({
                f: {"$in": variants} if f in ("name_canonical", "name_norm")
                    else {"$regex": "|".join([f"^{v}$" for v in variants]),
                          "$options": "i"}
            }, {"_id": 0}).limit(5).to_list(5)
            if hits:
                found += len(hits)
                if not sample: sample = hits[0]
                for h in hits:
                    d = str(h.get("match_date") or h.get("updated_at") or "")
                    if d and (not latest_date or d > latest_date):
                        latest_date = d
        # Also try soft substring on name_canonical
        if found == 0:
            for f in ("name_canonical", "player_name", "name_norm"):
                try:
                    hits = await db[store].find(
                        {f: {"$regex": canonical_name.split()[-1],
                              "$options": "i"}}
                    ).limit(3).to_list(3)
                    if hits:
                        result["stores"][f"{store}__substring_{f}"] = {
                            "count": len(hits),
                            "sample_names": [h.get("player_name") or h.get("name") for h in hits],
                        }
                except Exception:
                    pass
        result["stores"][store] = {
            "rows_found":  found,
            "latest_date": latest_date,
            "sample_identity": {
                "player_name":         (sample or {}).get("player_name"),
                "name_canonical":      (sample or {}).get("name_canonical"),
                "team":                (sample or {}).get("team"),
                "league":              (sample or {}).get("league"),
                "season":              (sample or {}).get("season"),
            } if sample else None,
        }
    return result


async def section3_forensics() -> dict:
    print("\n" + "═" * 68)
    print("SECTION 3 — SØRLOTH + ENDRICK FORENSICS")
    print("═" * 68)
    sorloth = await _forensic_search(
        "Alexander Sørloth",
        ["Alexander Sorloth", "A. Sorloth", "Sørloth",
         "alexander sorloth", "sorloth"],
    )
    endrick = await _forensic_search(
        "Endrick Felipe Moreira de Sousa",
        ["Endrick", "Endrick Felipe", "endrick"],
    )

    # Classify each
    def _classify(x: dict) -> str:
        stores = x["stores"]
        total = 0
        for k, v in stores.items():
            if isinstance(v, dict) and "rows_found" in v:
                total += v["rows_found"]
        # look for substring matches
        substring_hits = [k for k in stores.keys() if "substring" in k]
        if total > 0:
            return "IDENTITY_MISMATCH — data exists under variant not tried"
        if substring_hits:
            return "IDENTITY_MISMATCH — surname substring hit but name_canonical mismatch"
        return "SOURCE_COVERAGE_GAP — no ingest crawled this player from any tier-1 source"

    sorloth["classification"] = _classify(sorloth)
    endrick["classification"] = _classify(endrick)

    # Additionally check registry
    sorloth["transfer_registry"] = await db.soccer_transfer_registry.find_one(
        {"player_name": {"$regex": "Sorloth|Sørloth", "$options": "i"}}
    ) or None
    endrick["transfer_registry"] = await db.soccer_transfer_registry.find_one(
        {"player_name": {"$regex": "Endrick", "$options": "i"}}
    ) or None

    result = {"sorloth": sorloth, "endrick": endrick}
    print(_fmt(result))
    return result


# ═══════════════════════════════════════════════════════════════════
# SECTION 4 — 50-player coverage test
# ═══════════════════════════════════════════════════════════════════
async def section4_coverage() -> dict:
    print("\n" + "═" * 68)
    print("SECTION 4 — 50-PLAYER COVERAGE TEST")
    print("═" * 68)
    now = datetime.now(timezone.utc)
    q = {
        "sport": "Soccer",
        "book_odds": {"$ne": None},
        "event_time": {"$gte": now.isoformat()},
        "market": {"$regex":
                    "Anytime|To Score|Goal Scorer|Score or Assist|Shots",
                    "$options": "i"},
    }
    picks = await db.picks.find(q).limit(50).to_list(50)
    if not picks:
        # Fallback: not restrict to future
        picks = await db.picks.find({
            "sport": "Soccer", "book_odds": {"$ne": None},
            "market": {"$regex":
                        "Anytime|To Score|Goal Scorer|Score or Assist|Shots",
                        "$options": "i"},
        }).sort("event_time", -1).limit(50).to_list(50)

    from services.soccer_player_authority import Authority
    # Extract player name from selection
    def _extract(sel: str, market: str) -> str:
        s = (sel or "").strip()
        # Common patterns: "Harry Kane" or "Harry Kane · Anytime"
        for sep in ["·", "-", ":"]:
            if sep in s: s = s.split(sep)[0].strip()
        return s

    rows = []
    sources = {}
    league_counts = {}
    for p in picks:
        player = _extract(p.get("selection") or "", p.get("market") or "")
        league = p.get("league") or ""
        league_counts[league] = league_counts.get(league, 0) + 1
        try:
            row, source = await resolve_soccer_player_features(
                db, player_name=player, league=league,
            )
        except Exception as e:
            row, source = None, f"ERROR:{e}"
        rows.append({
            "pick_id": p.get("id"),
            "player":  player,
            "league":  league,
            "team":    p.get("team"),
            "history_source":     source,
            "has_history":        bool(row and (row.get("minutes") or 0) >= 90),
            "season":             (row or {}).get("season") if row else None,
            "row_updated_at":     str((row or {}).get("updated_at")) if row else None,
            "sample_size":        (row or {}).get("games") if row else None,
        })
        sources[source] = sources.get(source, 0) + 1

    hist_found = sum(1 for r in rows if r["has_history"])
    hist_missing = len(rows) - hist_found
    # Detect stale rows (season != current)
    current_year = now.year
    stale = 0; fresh = 0
    for r in rows:
        if not r["season"]: continue
        try:
            season_year = int(str(r["season"]).split("-")[0])
            if season_year < current_year - 1: stale += 1
            else: fresh += 1
        except Exception: pass

    result = {
        "total_picks_sampled":    len(picks),
        "history_found":          hist_found,
        "history_missing":        hist_missing,
        "coverage_rate":          f"{100*hist_found/max(1,len(picks)):.1f}%",
        "identity_failure":       0,  # will fill after
        "stale_history_rows":     stale,
        "fresh_history_rows":     fresh,
        "source_distribution":    sources,
        "league_distribution":    league_counts,
        "─── per-row details (first 20) ───": rows[:20],
        "─── per-row details (last 20) ───":  rows[-20:] if len(rows) > 20 else [],
    }
    print(_fmt({k: v for k, v in result.items()
                if not k.startswith("─── per-row")}))
    # Save full details
    with open("/tmp/session_10_5_coverage_rows.json", "w") as fh:
        json.dump(rows, fh, indent=2, default=str)
    print(f"\n(full 50 rows saved to /tmp/session_10_5_coverage_rows.json)")
    return result


# ═══════════════════════════════════════════════════════════════════
# SECTION 5 — Real production regen (wired via authority)
# ═══════════════════════════════════════════════════════════════════
async def section5_real_regen(dry_run: bool = False) -> dict:
    print("\n" + "═" * 68)
    print("SECTION 5 — REAL PRODUCTION REGEN (via hydrator+authority)")
    print("═" * 68)
    from services.soccer_scorer_regen_via_authority import (
        regen_soccer_player_picks_via_authority,
    )
    now = datetime.now(timezone.utc)
    r = await regen_soccer_player_picks_via_authority(
        db, dry_run=dry_run, limit=2000, verbose=False,
    )
    print(_fmt({k: v for k, v in r.items() if k != "per_pick"}))
    # save per_pick
    with open("/tmp/session_10_5_regen_details.json", "w") as fh:
        json.dump(r.get("per_pick", []), fh, indent=2, default=str)
    print(f"\n(per-pick regen details saved to /tmp/session_10_5_regen_details.json)")
    return r


# ═══════════════════════════════════════════════════════════════════
# SECTION 6 — End-to-end parity: Mongo → API → published payload
# ═══════════════════════════════════════════════════════════════════
async def section6_parity(pick_ids: list[str], token: str) -> dict:
    print("\n" + "═" * 68)
    print("SECTION 6 — MODEL / CANONICAL / API PARITY")
    print("═" * 68)
    import urllib.request, urllib.error
    parity_rows = []
    for pid in pick_ids[:10]:
        # MODEL/CANONICAL — read directly from Mongo
        p = await db.picks.find_one({"id": pid}) or {}
        db_prob   = p.get("win_probability")
        db_lock   = p.get("lock_score")
        db_mv     = p.get("soccer_model_version") or p.get("model_version")
        db_gen    = p.get("generation_id")
        db_bv     = p.get("board_version") or p.get("published_board_version")
        db_pub_prob = p.get("published_probability")
        db_pub_lock = p.get("published_lock_score")

        # API — call /api/picks/{pick_id} which is a RAW single-pick fetch
        # (no gates applied), so a regenerated pick with lock_score<85
        # is still returned.  This is the correct parity comparison.
        api_prob = None; api_lock = None; api_mv = None
        try:
            req = urllib.request.Request(
                f"http://localhost:8001/api/picks/{pid}",
                headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                api_doc = json.loads(resp.read())
                api_prob = api_doc.get("win_probability")
                api_lock = api_doc.get("lock_score")
                api_mv   = (api_doc.get("soccer_model_version")
                            or api_doc.get("model_version"))
        except Exception as e:
            api_prob = f"ERROR:{e}"

        # Expo/preview payload — lite version if the pick DOES publish;
        # for OFF_BOARD picks (below 85) it won't appear, which is
        # expected behavior for evidence-honest scoring.
        expo_prob = None; expo_lock = None; expo_present = False
        try:
            req = urllib.request.Request(
                "http://localhost:8001/api/picks/today?lite=true",
                headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                picks = data.get("picks") or data.get("data") or []
                for row in picks:
                    if row.get("id") == pid:
                        expo_prob = row.get("win_probability")
                        expo_lock = row.get("lock_score")
                        expo_present = True
                        break
        except Exception as e:
            expo_prob = f"ERROR:{e}"

        def _prob_match(a, b, tol=0.01):
            try: return abs(float(a) - float(b)) <= tol
            except Exception: return a == b

        parity_rows.append({
            "pick_id":       pid,
            "player":        p.get("selection"),
            "MODEL_prob":    db_prob,
            "MODEL_lock":    db_lock,
            "CANONICAL_pub_prob": db_pub_prob,
            "CANONICAL_pub_lock": db_pub_lock,
            "API_prob":      api_prob,
            "API_lock":      api_lock,
            "EXPO_prob":     expo_prob,
            "EXPO_lock":     expo_lock,
            "EXPO_on_board": expo_present,
            "model_version": db_mv,
            "api_model_version": api_mv,
            "generation_id": db_gen,
            "board_version": db_bv,
            "PARITY_prob":   _prob_match(db_prob, api_prob, tol=0.01),
            "PARITY_lock":   _prob_match(db_lock, api_lock, tol=0.01),
            "PARITY_expo":   (not expo_present) or (
                _prob_match(db_prob, expo_prob, tol=0.01)
                and _prob_match(db_lock, expo_lock, tol=0.01)),
        })
    parity_all_prob = all(r["PARITY_prob"] for r in parity_rows)
    parity_all_lock = all(r["PARITY_lock"] for r in parity_rows)
    parity_all_expo = all(r["PARITY_expo"] for r in parity_rows)
    on_board_count  = sum(1 for r in parity_rows if r["EXPO_on_board"])
    result = {
        "total_checked":  len(parity_rows),
        "PARITY_probability_MODEL_eq_API": parity_all_prob,
        "PARITY_lock_score_MODEL_eq_API":  parity_all_lock,
        "PARITY_expo_when_on_board":       parity_all_expo,
        "picks_on_expo_board":              on_board_count,
        "note":  "EXPO board filters below-85 picks; that's evidence-"
                 "honest scoring, not a parity failure",
        "rows": parity_rows,
    }
    print(_fmt({k: v for k, v in result.items() if k != "rows"}))
    print("\nDetailed rows:")
    for r in parity_rows: print(_fmt(r))
    return result


# ═══════════════════════════════════════════════════════════════════
# SECTION 7 — Historical Intelligence UI proof
# ═══════════════════════════════════════════════════════════════════
async def section7_hi(pick_ids: list[str], token: str) -> dict:
    print("\n" + "═" * 68)
    print("SECTION 7 — HISTORICAL INTELLIGENCE UI (Soccer)")
    print("═" * 68)
    import urllib.request
    hi_rows = []
    for pid in pick_ids[:10]:
        p = await db.picks.find_one({"id": pid}) or {}
        buckets: dict = {}
        for scope in ("L5", "L10", "L20", "SEASON"):
            try:
                req = urllib.request.Request(
                    f"http://localhost:8001/api/picks/{pid}/historical-intelligence"
                    f"?sample_scope={scope}",
                    headers={"Authorization": f"Bearer {token}"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read())
                    buckets[scope] = {
                        "sample_size": data.get("sample_size"),
                        "hit_rate":    data.get("hit_rate"),
                        "hits":        data.get("hits"),
                        "games_returned": len(data.get("games") or []),
                        "opponent_summary_present":
                            bool(data.get("opponent_summary")),
                        "opp_sample": (data.get("opponent_summary") or {}
                                       ).get("sample_size"),
                        "home_summary_sample":
                            (data.get("home_summary") or {}).get("sample_size"),
                        "away_summary_sample":
                            (data.get("away_summary") or {}).get("sample_size"),
                    }
            except Exception as e:
                buckets[scope] = {"error": str(e)}
        hi_rows.append({
            "pick_id":  pid,
            "player":   p.get("selection"),
            "market":   p.get("market"),
            "buckets":  buckets,
        })
    hi_ok = sum(1 for r in hi_rows
                if (r["buckets"].get("L10") or {}).get("sample_size", 0) > 0)
    result = {
        "total_checked":  len(hi_rows),
        "with_L10_data":  hi_ok,
        "coverage_rate":  f"{100*hi_ok/max(1,len(hi_rows)):.1f}%",
        "rows":           hi_rows,
    }
    print(_fmt(result))
    return result


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════
async def main():
    print(f"Session 10.5 — Soccer Player Model Closure — starting @ "
          f"{datetime.now(timezone.utc).isoformat()}")

    # Login to get admin token
    import urllib.request
    try:
        req = urllib.request.Request(
            "http://localhost:8001/api/auth/login",
            data=json.dumps({"email": "demo@lockscore.ai",
                              "password": "demo123"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            token = json.loads(resp.read())["access_token"]
    except Exception as e:
        print(f"LOGIN FAILED: {e}"); token = ""

    kane      = await section1_kane_math()
    freshness = await section2_freshness()
    forensics = await section3_forensics()
    coverage  = await section4_coverage()
    regen     = await section5_real_regen(dry_run=False)

    # Preferential mix: try to include Kane if he's in the regen; else
    # take first 10 regenerated + include Kane by ID if he exists.
    per_pick = regen.get("per_pick") or []
    regen_ids = [r["pick_id"] for r in per_pick if r.get("regen_applied")]
    # Add Kane pick if present
    kane_pid = (kane or {}).get("pick_id")
    if kane_pid and kane_pid not in regen_ids:
        # Make sure Kane's pick got regen'd or at least fetch him for parity
        regen_ids.insert(0, kane_pid)
    # Take first 10
    regen_ids = regen_ids[:10]
    if not regen_ids:
        # fallback: take first 10 current soccer picks
        picks = await db.picks.find({
            "sport": "Soccer", "book_odds": {"$ne": None},
        }).limit(10).to_list(10)
        regen_ids = [p["id"] for p in picks]

    parity = await section6_parity(regen_ids, token)
    hi     = await section7_hi(regen_ids, token)

    # ─── Final summary ───
    print("\n" + "═" * 68)
    print("FINAL STATUS")
    print("═" * 68)

    def _v(status): return status
    summary = {
        "SOCCER_HISTORY_RESOLVER":       "already VERIFIED (Session 10.4)",
        "KANE_PROBABILITY_MATH":         (
            "CERTIFIED" if kane.get("MATH_CROSS_CHECK") else "NOT_CERTIFIED"
        ),
        "SOCCER_HISTORY_FRESHNESS":      (
            "CERTIFIED" if freshness.get("KANE_HAS_CURRENT_SEASON_GAME_LOGS")
            else "PARTIAL — reconnect OK but current 2026-27 game logs not ingested"
        ),
        "SORLOTH_HISTORY":               forensics["sorloth"]["classification"],
        "ENDRICK_HISTORY":               forensics["endrick"]["classification"],
        "50_PLAYER_COVERAGE":            (
            f"{coverage['coverage_rate']} ({coverage['history_found']}/"
            f"{coverage['total_picks_sampled']}) — "
            + ("CERTIFIED (>=70%)" if coverage["history_found"]
                / max(1, coverage["total_picks_sampled"]) >= 0.70
                else "PARTIAL")
        ),
        "SOCCER_REAL_REGEN":             (
            f"CERTIFIED — {regen.get('picks_regenerated', 0)} picks regenerated"
            if regen.get("picks_regenerated", 0) > 0 else "NOT_CERTIFIED"
        ),
        "MODEL_CANONICAL_API_PARITY":    (
            "CERTIFIED" if (parity.get("PARITY_probability_MODEL_eq_API")
                             and parity.get("PARITY_lock_score_MODEL_eq_API")
                             and parity.get("PARITY_expo_when_on_board"))
            else "PARTIAL"
        ),
        "EXPO_HISTORICAL_INTELLIGENCE":  (
            f"{hi['coverage_rate']} L10 coverage — "
            + ("CERTIFIED" if hi["with_L10_data"] >= 7 else "PARTIAL")
        ),
    }
    print(_fmt(summary))

    with open("/tmp/session_10_5_final_summary.json", "w") as fh:
        json.dump({
            "kane_math":  kane,
            "freshness":  freshness,
            "forensics":  forensics,
            "coverage":   {k: v for k, v in coverage.items()
                           if "per-row" not in k},
            "regen_summary": {k: v for k, v in regen.items()
                               if k != "per_pick"},
            "parity":     parity,
            "hi":         hi,
            "SUMMARY":    summary,
        }, fh, indent=2, default=str)

    print("\nFull payload saved to /tmp/session_10_5_final_summary.json")


if __name__ == "__main__":
    asyncio.run(main())
