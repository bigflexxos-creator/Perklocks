"""P0 Canary Funnel Trace — NHL + Soccer Player Prop Intelligence Recovery.

PREVIEW ONLY.  DO NOT DEPLOY.  85 THRESHOLD PRESERVED.

Pipeline traced per canary:
   EVENT DISCOVERY
      → EVENT-LEVEL MARKET REQUEST (/events/{id}/odds)
      → MARKET RETURNED
      → NORMALIZED
      → IDENTITY RESOLVED (canonical_player_id)
      → HISTORY ROWS (L5 / L10 / Season / H2H)
      → OPPONENT CONTEXT
      → ROLE / MINUTES / TOI
      → RAW MODEL PROBABILITY (Poisson)
      → CALIBRATED PROBABILITY (Poisson + Empirical + H2H blend)
      → EVIDENCE COUNT
      → FACTOR SOURCES
      → LOCK SCORE (via compute_lock_score)
      → PUBLICATION DECISION  (>=85 or exact deduction cap)

Stop reasons (never silent):
   PROVIDER_ZERO | APP_DROPPED_MARKET | IDENTITY_FAILED | HISTORY_ZERO
   FEATURE_INCOMPLETE | MODEL_REJECTED | LOCK_SCORE_BELOW_85
"""
from __future__ import annotations

import os
import sys
import json
import math
import time
import asyncio
import unicodedata
from typing import Optional

sys.path.insert(0, "/app/backend")
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
try:
    from dotenv import load_dotenv
    load_dotenv("/app/backend/.env")
except Exception:
    pass

from pymongo import MongoClient
import httpx

ODDS_KEY = os.environ.get("THE_ODDS_API_KEY", "")
BASE     = "https://api.the-odds-api.com/v4"

NHL_SPORT_KEY = "icehockey_nhl"

SOCCER_LEAGUES = [
    ("EPL",        "soccer_epl"),
    ("Bundesliga", "soccer_germany_bundesliga"),
    ("La Liga",    "soccer_spain_la_liga"),
    ("Serie A",    "soccer_italy_serie_a"),
    ("Ligue 1",    "soccer_france_ligue_one"),
    ("MLS",        "soccer_usa_mls"),
]

NHL_PLAYER_MARKETS = (
    "player_goals,player_goals_alternate,"
    "player_shots_on_goal,player_shots_on_goal_alternate,"
    "player_assists,player_assists_alternate,"
    "player_points,player_points_alternate"
)
SOCCER_PLAYER_MARKETS = (
    "player_goal_scorer_anytime,player_to_score_or_assist,"
    "player_assists,player_shots,player_shots_on_target"
)

NHL_CANARIES = [
    "Connor McDavid",
    "Kirill Kaprizov",
    "Leon Draisaitl",
    "Jack Eichel",
    "Kyle Connor",
    "Quinn Hughes",
    "Evan Bouchard",
]
# Soccer required canaries + leagues (Ligue 1 / Serie A are auto-discovered).
SOCCER_LEAGUE_CANARY = {
    "EPL":        ["Erling Haaland"],
    "Bundesliga": ["Harry Kane"],
    "La Liga":    ["Kylian Mbappe", "Kylian Mbappé", "Kylian Mbappe-Lottin"],
    "MLS":        ["Lionel Messi"],
    # Ligue 1 + Serie A: fill with first available high-volume scorer live.
    "Ligue 1":    [],
    "Serie A":    [],
}

MONGO = MongoClient(os.environ["MONGO_URL"])
DB = MONGO.lockscore_db


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────
def _strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", s or "")
        if not unicodedata.combining(c)
    ).strip()


def _init_form(name: str) -> str:
    parts = (name or "").strip().split()
    if len(parts) < 2:
        return name
    return f"{parts[0][:1].upper()}. {' '.join(parts[1:])}"


def _poisson_p_at_least(mu: float, k: int) -> float:
    if mu <= 0 or k <= 0:
        return 1.0 if k <= 0 else 0.0
    _cum = math.exp(-mu); term = _cum
    for i in range(1, k):
        term *= mu / i; _cum += term
    return max(0.0, min(1.0, 1.0 - _cum))


async def _get(client: httpx.AsyncClient, url: str, params: dict) -> dict | list | None:
    params = {**params, "apiKey": ODDS_KEY}
    try:
        r = await client.get(url, params=params, timeout=25.0)
        if r.status_code == 200:
            return r.json()
        return {"__error__": r.status_code, "body": r.text[:400]}
    except Exception as e:
        return {"__error__": "exception", "msg": str(e)}


# ─────────────────────────────────────────────────────────────────────
# NHL canary trace
# ─────────────────────────────────────────────────────────────────────
async def trace_nhl_canary(client: httpx.AsyncClient,
                           player_full: str,
                           events: list[dict]) -> dict:
    """Walk full NHL funnel for one canary across tonight's events."""
    trace: dict = {
        "player": player_full,
        "sport": "NHL",
        "stage":  "START",
        "events_scanned": 0,
        "found_in_event": None,
        "markets_offered": [],
        "identity": None,
        "history_rows": 0,
        "features": None,
        "model_prob": None,
        "model_prob_h2h": None,
        "lock_score": None,
        "lock_breakdown": None,
        "publication_decision": None,
        "stop_reason": None,
    }

    init = _init_form(player_full)
    # Identity: canonical_players collection
    cp = DB.canonical_players.find_one(
        {"sport": "nhl", "$or": [{"canonical_name": player_full},
                                   {"name": player_full}]},
        {"canonical_player_id": 1, "name": 1, "team": 1,
         "team_abbrev": 1},
    )
    trace["identity"] = {
        "canonical_player_id": (cp or {}).get("canonical_player_id"),
        "name": (cp or {}).get("name"),
        "team": (cp or {}).get("team"),
    }
    cpid = (cp or {}).get("canonical_player_id")

    # Scan every event for this player's name in player_ markets
    hit_event = None
    hit_markets: list[tuple[str, float, str, int]] = []  # (mk, line, side, price)
    for ev in events:
        trace["events_scanned"] += 1
        pdata = await _get(
            client,
            f"{BASE}/sports/{NHL_SPORT_KEY}/events/{ev['id']}/odds",
            {"regions": "us",
             "markets": NHL_PLAYER_MARKETS,
             "oddsFormat": "american"},
        )
        if not isinstance(pdata, dict) or pdata.get("__error__"):
            continue
        found_any_player_mk = False
        for bm in pdata.get("bookmakers") or []:
            for m in bm.get("markets") or []:
                if not (m.get("key") or "").startswith("player_"):
                    continue
                found_any_player_mk = True
                for o in m.get("outcomes") or []:
                    nm = (o.get("description") or o.get("name") or "").strip()
                    if (nm.lower() == player_full.lower()
                            or _strip_accents(nm).lower()
                            == _strip_accents(player_full).lower()):
                        try:
                            ln = float(o.get("point"))
                        except Exception:
                            continue
                        side = ("over"
                                if "over" in str(o.get("name") or "").lower()
                                else "under"
                                if "under" in str(o.get("name") or "").lower()
                                else "over")
                        try:
                            prc = int(o.get("price"))
                        except Exception:
                            prc = 0
                        hit_markets.append((m["key"], ln, side, prc))
        if hit_markets and hit_event is None:
            hit_event = {
                "id": ev["id"],
                "home": ev.get("home_team"),
                "away": ev.get("away_team"),
                "commence": ev.get("commence_time"),
                "any_player_market": found_any_player_mk,
            }
            break

    if not hit_markets:
        trace["stop_reason"] = ("PROVIDER_ZERO"
                                if trace["events_scanned"] > 0
                                else "NO_EVENTS")
        return trace

    trace["found_in_event"] = hit_event
    # Dedupe + keep top market priorities for the trace display
    seen = set(); dedup = []
    for mk_, ln_, sd_, pr_ in hit_markets:
        k = (mk_, ln_, sd_)
        if k in seen: continue
        seen.add(k); dedup.append({"market": mk_, "line": ln_,
                                   "side": sd_, "price_american": pr_})
    trace["markets_offered"] = dedup[:12]

    # ── History fetch (mirror precompute logic) ──────────────────────
    hist_rows: list[dict] = []
    if cpid:
        hist_rows = list(DB.player_game_actuals.find(
            {"sport": "nhl", "canonical_player_id": cpid},
            {"actuals": 1, "event_time": 1, "opponent": 1,
             "opponent_abbrev": 1, "home_away": 1, "season": 1,
             "canonical_player_id": 1},
        ).sort("event_time", -1).limit(40))
    if not hist_rows:
        hist_rows = list(DB.player_game_actuals.find(
            {"sport": "nhl",
             "$or": [{"player_name": init},
                     {"player_name": player_full},
                     {"name": init}]},
            {"actuals": 1, "event_time": 1, "opponent": 1,
             "opponent_abbrev": 1, "home_away": 1, "season": 1,
             "canonical_player_id": 1},
        ).sort("event_time", -1).limit(40))
    trace["history_rows"] = len(hist_rows)
    if not hist_rows:
        trace["stop_reason"] = "HISTORY_ZERO"
        return trace

    l5, l10, season = hist_rows[:5], hist_rows[:10], hist_rows
    def mean(logs, key):
        v = []
        for r in logs:
            a = r.get("actuals") or {}
            if a.get(key) is not None:
                try: v.append(float(a[key]))
                except: pass
        return sum(v) / len(v) if v else 0.0

    # Opponent for THIS event
    opp_name = (hit_event["away"] if cp and (cp.get("team") or "").lower() in (hit_event["home"] or "").lower() else hit_event["home"])
    opp_l = (opp_name or "").lower()
    h2h = [r for r in hist_rows
           if (opp_l and (
               (r.get("opponent") or "").lower() == opp_l
               or ((r.get("opponent") or "").lower().split() and
                   opp_l.split() and
                   (r.get("opponent") or "").lower().split()[0]
                   == opp_l.split()[0])
               or (opp_l in (r.get("opponent") or "").lower())
               or ((r.get("opponent") or "").lower() in opp_l)
           ))]

    toi_sec = [((r.get("actuals") or {}).get("toi_seconds") or 0) for r in l10]
    toi_l10 = sum(t for t in toi_sec if t) / max(1, len([t for t in toi_sec if t])) / 60.0

    features = {
        "L5 Goals Avg":    round(mean(l5, "goals"), 3),
        "L10 Goals Avg":   round(mean(l10, "goals"), 3),
        "Season Goals Avg":round(mean(season, "goals"), 3),
        "L10 Shots Avg":   round(mean(l10, "shots_on_goal"), 3),
        "L10 Assists Avg": round(mean(l10, "assists"), 3),
        "L10 Points Avg":  round(mean(l10, "points"), 3),
        "H2H Games":       len(h2h),
        "H2H Goals Avg":   round(mean(h2h, "goals"), 3) if h2h else None,
        "H2H Points Avg":  round(mean(h2h, "points"), 3) if h2h else None,
        "TOI L10 (min)":   round(toi_l10, 2),
        "PP Points L10":   round(mean(l10, "power_play_points"), 3),
    }
    trace["features"] = features

    # Pick the strongest market for the trace (lowest line Points / Assists)
    def _rank(entry):
        pri = 0
        mk = entry["market"]
        if "points" in mk: pri = 1
        elif "shots_on_goal" in mk: pri = 2
        elif "assists" in mk: pri = 3
        elif "goals" in mk: pri = 4
        return (pri, entry["line"], 0 if entry["side"] == "over" else 1)

    best = sorted(
        [e for e in dedup if e["side"] == "over"],
        key=_rank,
    )
    if not best:
        best = dedup
    chosen = best[0]
    mk = chosen["market"]; ln = chosen["line"]
    stat = ("goals" if "player_goals" in mk else
            "shots_on_goal" if "shots_on_goal" in mk else
            "assists" if "player_assists" in mk else
            "points" if "player_points" in mk else None)
    k = (math.floor(ln + 0.5) + 1 if (ln % 1 != 0) else int(ln) + 1)
    mu = mean(l10, stat)
    mu_h2h = mean(h2h, stat) if h2h else 0.0
    p_poisson = _poisson_p_at_least(mu, k)
    # L10 empirical hit rate
    hits = sum(1 for r in l10
               if ((r.get("actuals") or {}).get(stat) or 0) >= k)
    p_emp = (hits / len(l10)) if l10 else 0.0
    w_emp = len(l10) / max(10.0, float(len(l10)))
    p_blend = w_emp * p_emp + (1 - w_emp) * p_poisson
    # H2H blend
    if h2h:
        h2h_hits = sum(1 for r in h2h
                       if ((r.get("actuals") or {}).get(stat) or 0) >= k)
        p_h2h = h2h_hits / len(h2h)
        w_h2h = min(0.25, len(h2h) / 20.0)
        p_blend = (1.0 - w_h2h) * p_blend + w_h2h * p_h2h

    trace["model_prob"]     = round(p_poisson, 4)
    trace["model_prob_h2h"] = round(p_blend, 4)
    trace["chosen_market"]  = {"market": mk, "line": ln,
                               "stat": stat, "threshold_k": k}

    # ── Build factors for compute_lock_score ─────────────────────────
    from sports_engine import compute_lock_score

    factors = {
        "L10 Hit Rate":   round(p_emp, 3),
        "Poisson μ":      round(mu, 3),
        "L5 Goals Avg":   features["L5 Goals Avg"],
        "L10 Goals Avg":  features["L10 Goals Avg"],
        "Season Goals Avg": features["Season Goals Avg"],
        "L10 Shots Avg":  features["L10 Shots Avg"],
        "L10 Assists Avg":features["L10 Assists Avg"],
        "L10 Points Avg": features["L10 Points Avg"],
        "H2H Hit Rate":   None if not h2h else round(p_h2h, 3),
        "TOI L10":        features["TOI L10 (min)"],
    }
    # Drop None factors (compute_lock_score ignores them anyway).
    factors = {k: v for k, v in factors.items() if v is not None}
    ev_count = 0
    if len(l10) >= 5: ev_count += 1
    if len(l5) >= 3: ev_count += 1
    if len(season) >= 15: ev_count += 1
    if len(h2h) >= 2: ev_count += 1
    if toi_l10 > 0: ev_count += 1
    pick_shim = {
        "sport": "NHL",
        "player": player_full,
        "player_name": player_full,
        "canonical_player_id": cpid,
        "player_id": cpid,
        "player_identity_source": "canonical_player_id" if cpid else "name_only",
        "player_identity_stable": bool(cpid),
        "simulator_provenance": "EMPIRICAL_INDEPENDENT",
        "factor_sources": [
            "player_game_actuals:nhl",
            "nhl_player_prop_model:poisson_plus_empirical",
            f"nhl_l10:{len(l10)}", f"nhl_l5:{len(l5)}",
            f"nhl_season:{len(season)}",
        ] + ([f"nhl_h2h:{len(h2h)}"] if h2h else []) + (
            ["nhl_toi_role"] if toi_l10 > 0 else []
        ),
        "evidence_count": ev_count,
        "opponent": opp_name,
        "is_home": (hit_event["home"] and cp
                    and (cp.get("team") or "").lower()
                    in (hit_event["home"] or "").lower()),
        "h2h_games": len(h2h),
        "toi_l10": round(toi_l10, 2),
    }
    try:
        lock, breakdown = compute_lock_score(
            factors, win_prob=p_blend * 100, pick=pick_shim,
        )
    except Exception as e:
        trace["stop_reason"] = f"MODEL_REJECTED:{type(e).__name__}:{e}"
        return trace
    trace["lock_score"] = round(float(lock), 2)
    trace["lock_breakdown"] = {k: round(float(v), 2)
                                for k, v in (breakdown or {}).items()
                                if isinstance(v, (int, float))}
    trace["publication_decision"] = (
        "PUBLISH" if lock >= 85 else "BELOW_85_HOLD"
    )
    if lock < 85:
        # Diagnose exact deductions
        deductions = []
        for comp_name, comp_val in (breakdown or {}).items():
            if not isinstance(comp_val, (int, float)):
                continue
            if comp_val < 70:
                deductions.append({"component": comp_name,
                                   "score": round(comp_val, 2)})
        trace["exact_deductions"] = deductions
        trace["stop_reason"] = "LOCK_SCORE_BELOW_85"
    return trace


# ─────────────────────────────────────────────────────────────────────
# Soccer canary trace
# ─────────────────────────────────────────────────────────────────────
async def trace_soccer_canary(client: httpx.AsyncClient,
                              league_label: str,
                              sport_key: str,
                              player_full: str,
                              events: list[dict]) -> dict:
    trace: dict = {
        "player": player_full,
        "sport": "Soccer",
        "league": league_label,
        "sport_key": sport_key,
        "events_scanned": 0,
        "found_in_event": None,
        "markets_offered": [],
        "identity": None,
        "form_row_present": False,
        "features": None,
        "lock_score": None,
        "stop_reason": None,
    }

    form_row = DB.soccer_player_form.find_one({
        "name_canonical": _strip_accents(player_full).lower()
    }) or DB.soccer_player_form.find_one({
        "name_canonical": player_full.lower()
    })
    if not form_row:
        # Fallback regex search by surname
        import re
        surname = (player_full.split()[-1] if player_full else "").lower()
        if surname:
            form_row = DB.soccer_player_form.find_one(
                {"player_name": re.compile(surname, re.I)})
    trace["form_row_present"] = bool(form_row)
    if form_row:
        trace["identity"] = {
            "player_name": form_row.get("player_name"),
            "name_canonical": form_row.get("name_canonical"),
            "team": form_row.get("team"),
            "league": form_row.get("league"),
            "games": form_row.get("games"),
            "goals": form_row.get("goals"),
            "canonical_player_id":
                form_row.get("canonical_player_id")
                or form_row.get("player_id"),
        }

    hit_event = None
    hit_markets = []  # (mk, player_name_as_offered, price)
    PROVIDER_ZERO_LOCAL = True
    for ev in events:
        trace["events_scanned"] += 1
        pdata = await _get(
            client,
            f"{BASE}/sports/{sport_key}/events/{ev['id']}/odds",
            {"regions": "uk,eu",
             "markets": SOCCER_PLAYER_MARKETS,
             "oddsFormat": "american"},
        )
        if not isinstance(pdata, dict) or pdata.get("__error__"):
            continue
        any_player_mk = False
        for bm in pdata.get("bookmakers") or []:
            for m in bm.get("markets") or []:
                if not (m.get("key") or "").startswith("player_"):
                    continue
                any_player_mk = True
                PROVIDER_ZERO_LOCAL = False
                for o in m.get("outcomes") or []:
                    nm = (o.get("description") or o.get("name") or "").strip()
                    nm_norm = _strip_accents(nm).lower()
                    pf_norm = _strip_accents(player_full).lower()
                    if (nm_norm == pf_norm
                            or (pf_norm.split()[-1] in nm_norm
                                and len(pf_norm.split()[-1]) >= 4)):
                        try:
                            prc = int(o.get("price"))
                        except Exception:
                            prc = 0
                        hit_markets.append({
                            "market": m["key"],
                            "offered_name": nm,
                            "price_american": prc,
                        })
        if any_player_mk and hit_event is None and hit_markets:
            hit_event = {
                "id": ev["id"],
                "home": ev.get("home_team"),
                "away": ev.get("away_team"),
                "commence": ev.get("commence_time"),
            }
            break
        elif any_player_mk and hit_event is None:
            # provider returned markets but our name wasn't in outcomes
            # (still mark event as "coverage_present")
            hit_event = {
                "id": ev["id"],
                "home": ev.get("home_team"),
                "away": ev.get("away_team"),
                "commence": ev.get("commence_time"),
                "note": "markets_present_but_player_not_listed",
            }

    if not hit_markets and not hit_event:
        trace["stop_reason"] = ("PROVIDER_ZERO"
                                if PROVIDER_ZERO_LOCAL else "APP_DROPPED_MARKET")
        return trace
    trace["found_in_event"] = hit_event
    trace["markets_offered"] = hit_markets[:10]
    if not hit_markets:
        trace["stop_reason"] = "PLAYER_NOT_IN_OFFERED_OUTCOMES"
        return trace

    # ── Call the scorer bridge ───────────────────────────────────────
    if not form_row:
        trace["stop_reason"] = "HISTORY_ZERO"
        return trace

    from services.soccer_scorer_bridge import compute_soccer_scorer_factors_sync
    from sports_engine import compute_lock_score

    team_ctx = {
        "team": form_row.get("team"),
        "opponent": (hit_event or {}).get("away"),
        "is_home": True if (hit_event
                             and str(form_row.get("team") or "").lower()
                             in str(hit_event.get("home") or "").lower()) else False,
        "h2h": None,
    }
    bp = compute_soccer_scorer_factors_sync(
        player=player_full,
        market_key="player_goal_scorer_anytime",
        book_implied=0.30,
        form_row=form_row,
        league=league_label,
        team_ctx=team_ctx,
    )
    if not bp:
        trace["stop_reason"] = "MODEL_REJECTED"
        return trace
    trace["features"] = bp["factors"]
    trace["model_prob"] = bp["model_prob"]
    trace["evidence_count"] = bp.get("evidence_count")
    trace["factor_sources"] = bp.get("factor_sources")
    trace["identity_source"] = bp.get("player_identity_source")
    trace["identity_stable"] = bp.get("player_identity_stable")
    pick_shim = {
        "sport": "Soccer",
        "player": player_full,
        "player_name": player_full,
        "canonical_player_id": bp.get("canonical_player_id"),
        "player_id": bp.get("canonical_player_id"),
        "player_identity_source": bp.get("player_identity_source"),
        "player_identity_stable": bool(bp.get("player_identity_stable")),
        "simulator_provenance": bp.get("simulator_provenance"),
        "factor_sources": bp.get("factor_sources"),
        "evidence_count": bp.get("evidence_count"),
        "is_home": bp.get("is_home"),
        "h2h_games": bp.get("h2h_games"),
    }
    try:
        lock, breakdown = compute_lock_score(
            bp["factors"], win_prob=bp["model_prob"] * 100, pick=pick_shim,
        )
    except Exception as e:
        trace["stop_reason"] = f"MODEL_REJECTED:{type(e).__name__}:{e}"
        return trace
    trace["lock_score"] = round(float(lock), 2)
    trace["lock_breakdown"] = {
        k: round(float(v), 2) for k, v in (breakdown or {}).items()
        if isinstance(v, (int, float))
    }
    trace["publication_decision"] = (
        "PUBLISH" if lock >= 85 else "BELOW_85_HOLD"
    )
    if lock < 85:
        deductions = []
        for comp_name, comp_val in (breakdown or {}).items():
            if isinstance(comp_val, (int, float)) and comp_val < 70:
                deductions.append({"component": comp_name,
                                   "score": round(comp_val, 2)})
        trace["exact_deductions"] = deductions
        trace["stop_reason"] = "LOCK_SCORE_BELOW_85"
    return trace


# ─────────────────────────────────────────────────────────────────────
# Soccer board-wide scan (count markets per league)
# ─────────────────────────────────────────────────────────────────────
async def scan_soccer_league(client: httpx.AsyncClient,
                             league_label: str,
                             sport_key: str) -> dict:
    """Count real event-level player-market coverage for one league.
    Returns PROVIDER_ZERO + APP_DROPPED_MARKET tallies."""
    events = await _get(
        client, f"{BASE}/sports/{sport_key}/events",
        {"dateFormat": "iso"},
    )
    if not isinstance(events, list):
        return {"league": league_label, "events": 0,
                "provider_zero": 0, "with_markets": 0,
                "scorer_outcomes": 0,
                "error": "NO_EVENTS" if not events else "ERROR"}
    out = {
        "league": league_label,
        "sport_key": sport_key,
        "events": len(events),
        "with_player_markets": 0,
        "with_anytime_scorer": 0,
        "with_sot_market": 0,
        "with_first_scorer_market": 0,
        "with_last_scorer_market": 0,
        "scorer_outcome_count": 0,
        "sot_outcome_count": 0,
        "provider_zero": 0,
        "sample_offered_players": [],
        "events_detail": [],
    }
    for ev in events[:12]:  # cap requests for budget
        pdata = await _get(
            client,
            f"{BASE}/sports/{sport_key}/events/{ev['id']}/odds",
            {"regions": "uk,eu",
             "markets": SOCCER_PLAYER_MARKETS,
             "oddsFormat": "american"},
        )
        detail = {"event": f"{ev.get('away_team')} @ {ev.get('home_team')}",
                  "player_markets": {}}
        has_any_player = False
        for bm in (pdata or {}).get("bookmakers") or []:
            for m in bm.get("markets") or []:
                mk = m.get("key") or ""
                if not mk.startswith("player_"):
                    continue
                has_any_player = True
                outs = m.get("outcomes") or []
                detail["player_markets"].setdefault(mk, 0)
                detail["player_markets"][mk] += len(outs)
                if mk == "player_goal_scorer_anytime":
                    out["scorer_outcome_count"] += len(outs)
                    for o in outs:
                        nm = o.get("description") or o.get("name") or ""
                        if nm and nm not in out["sample_offered_players"]:
                            out["sample_offered_players"].append(nm)
                            if len(out["sample_offered_players"]) >= 15:
                                break
                elif mk == "player_shots_on_target":
                    out["sot_outcome_count"] += len(outs)
        if has_any_player:
            out["with_player_markets"] += 1
            if "player_goal_scorer_anytime" in detail["player_markets"]:
                out["with_anytime_scorer"] += 1
            if "player_shots_on_target" in detail["player_markets"]:
                out["with_sot_market"] += 1
            if "player_first_goal_scorer" in detail["player_markets"]:
                out["with_first_scorer_market"] += 1
            if "player_last_goal_scorer" in detail["player_markets"]:
                out["with_last_scorer_market"] += 1
        else:
            out["provider_zero"] += 1
        out["events_detail"].append(detail)
    return out


# ─────────────────────────────────────────────────────────────────────
# High-lock / Apex reachability (synthetic — structural proof only)
# ─────────────────────────────────────────────────────────────────────
def apex_reachability():
    """Prove through the REAL compute_lock_score that NHL + Soccer
    scoring paths can STRUCTURALLY reach every tier.  Synthetic
    inputs = idealized evidence, used only to prove the ceiling is
    not capped below Apex.  Never published."""
    from sports_engine import compute_lock_score
    results = []
    scenarios = [
        ("NHL_MEDIAN", {
            "L10 Hit Rate": 0.6, "L5 Goals Avg": 0.6, "L10 Goals Avg": 0.5,
            "Season Goals Avg": 0.45, "L10 Shots Avg": 2.8, "L10 Assists Avg": 1.0,
            "L10 Points Avg": 1.4, "H2H Hit Rate": 0.5, "TOI L10": 19.0,
        }, 0.55, "NHL"),
        ("NHL_STRONG", {
            "L10 Hit Rate": 0.75, "L5 Goals Avg": 1.0, "L10 Goals Avg": 0.85,
            "Season Goals Avg": 0.78, "L10 Shots Avg": 3.6, "L10 Assists Avg": 1.4,
            "L10 Points Avg": 2.4, "H2H Hit Rate": 0.7, "TOI L10": 22.0,
        }, 0.72, "NHL"),
        ("NHL_ELITE", {
            "L10 Hit Rate": 0.85, "L5 Goals Avg": 1.2, "L10 Goals Avg": 1.0,
            "Season Goals Avg": 0.92, "L10 Shots Avg": 4.2, "L10 Assists Avg": 2.0,
            "L10 Points Avg": 3.0, "H2H Hit Rate": 0.80, "TOI L10": 24.0,
        }, 0.80, "NHL"),
        ("NHL_APEX_STACKED", {
            "L10 Hit Rate": 0.95, "L5 Goals Avg": 1.5, "L10 Goals Avg": 1.3,
            "Season Goals Avg": 1.1, "L10 Shots Avg": 5.0, "L10 Assists Avg": 2.5,
            "L10 Points Avg": 3.8, "H2H Hit Rate": 0.95, "TOI L10": 25.0,
        }, 0.95, "NHL"),
        ("SOC_MEDIAN", {
            "Scorer Model Probability": 0.42, "Expected Minutes": 0.85,
            "xG per 90 (shrunk)": 0.42, "Shots on Target per 90": 0.45,
            "Finishing Quality": 0.52, "Team Attack Environment": 0.60,
            "Opponent Defensive Weakness": 0.52, "Recent Form": 0.55,
            "Home Advantage": 0.55, "L10 Goals": 0.5,
        }, 0.45, "Soccer"),
        ("SOC_STRONG", {
            "Scorer Model Probability": 0.58, "Expected Minutes": 1.0,
            "xG per 90 (shrunk)": 0.72, "Shots on Target per 90": 0.65,
            "Finishing Quality": 0.65, "Team Attack Environment": 0.72,
            "Opponent Defensive Weakness": 0.58, "Recent Form": 0.70,
            "H2H vs Opponent": 0.55, "Home Advantage": 0.55, "L10 Goals": 0.8,
            "Penalty Taker Role": 0.70,
        }, 0.60, "Soccer"),
        ("SOC_ELITE", {
            "Scorer Model Probability": 0.72, "Expected Minutes": 1.0,
            "xG per 90 (shrunk)": 0.88, "Shots on Target per 90": 0.80,
            "Finishing Quality": 0.75, "Team Attack Environment": 0.82,
            "Opponent Defensive Weakness": 0.72, "Recent Form": 0.85,
            "H2H vs Opponent": 0.70, "Home Advantage": 0.55, "L10 Goals": 1.0,
            "Penalty Taker Role": 0.70,
        }, 0.72, "Soccer"),
        ("SOC_APEX_STACKED", {
            "Scorer Model Probability": 0.85, "Expected Minutes": 1.0,
            "xG per 90 (shrunk)": 0.98, "Shots on Target per 90": 0.95,
            "Finishing Quality": 0.85, "Team Attack Environment": 0.95,
            "Opponent Defensive Weakness": 0.88, "Recent Form": 0.95,
            "H2H vs Opponent": 0.90, "Home Advantage": 0.55, "L10 Goals": 1.0,
            "Penalty Taker Role": 0.70,
        }, 0.86, "Soccer"),
    ]
    for name, factors, wp, sport in scenarios:
        pick_shim = {
            "sport": sport,
            "player": f"synthetic_{name}",
            "player_name": f"synthetic_{name}",
            "canonical_player_id": f"synth_{name}",
            "player_id": f"synth_{name}",
            "player_identity_source": "canonical_player_id",
            "player_identity_stable": True,
            "simulator_provenance": "EMPIRICAL_INDEPENDENT",
            "factor_sources": [
                "player_game_actuals:nhl",
                "nhl_player_prop_model:poisson_plus_empirical",
                "nhl_l10:10", "nhl_l5:5", "nhl_season:40", "nhl_h2h:5",
                "nhl_toi_role",
            ] if sport == "NHL" else [
                "soccer_scorer_bridge_v2", "goal_scorer_engine_v2",
                "soccer_form:30games", "soccer_prior_season",
                "soccer_h2h:5", "soccer_penalty_role", "soccer_l10_goals",
            ],
            "evidence_count": 7,
            "opponent": "SynthOpp",
            "is_home": True,
            "h2h_games": 5,
            "toi_l10": 22.0,
        }
        try:
            lock, _ = compute_lock_score(factors, win_prob=wp * 100, pick=pick_shim)
            results.append((name, round(float(lock), 2),
                            tier(lock), sport))
        except Exception as e:
            results.append((name, "ERROR", str(e), sport))
    return results


def tier(lock: float) -> str:
    if lock >= 98: return "APEX_98_99"
    if lock >= 96: return "96_97"
    if lock >= 93: return "93_95"
    if lock >= 90: return "90_92"
    if lock >= 85: return "85_89"
    return "BELOW_85"


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────
async def main():
    print("=" * 78)
    print("P0 CANARY FUNNEL TRACE — NHL + SOCCER (LIVE ODDS API)")
    print("=" * 78)

    async with httpx.AsyncClient() as client:
        # NHL events
        nhl_events = await _get(
            client, f"{BASE}/sports/{NHL_SPORT_KEY}/events",
            {"dateFormat": "iso"},
        )
        if not isinstance(nhl_events, list):
            nhl_events = []
        print(f"\n[NHL] Discovered {len(nhl_events)} live/upcoming events.")

        print("\n" + "=" * 78)
        print("NHL CANARY TRACES")
        print("=" * 78)
        nhl_results = []
        for p in NHL_CANARIES:
            t = await trace_nhl_canary(client, p, nhl_events)
            nhl_results.append(t)
            print(json.dumps({
                "player": t["player"],
                "stop_reason": t["stop_reason"],
                "lock_score": t["lock_score"],
                "publication": t.get("publication_decision"),
                "markets_offered": len(t["markets_offered"]),
                "history_rows": t["history_rows"],
            }, default=str))

        print("\n" + "=" * 78)
        print("SOCCER LEAGUE SCANS")
        print("=" * 78)
        league_scans = {}
        for lbl, sk in SOCCER_LEAGUES:
            print(f"\n--- {lbl} ({sk}) ---")
            scan = await scan_soccer_league(client, lbl, sk)
            league_scans[lbl] = scan
            print(json.dumps({
                "events": scan.get("events"),
                "with_player_markets": scan.get("with_player_markets"),
                "with_anytime_scorer": scan.get("with_anytime_scorer"),
                "with_sot_market": scan.get("with_sot_market"),
                "scorer_outcome_count": scan.get("scorer_outcome_count"),
                "sot_outcome_count": scan.get("sot_outcome_count"),
                "provider_zero": scan.get("provider_zero"),
                "sample_offered_players":
                    (scan.get("sample_offered_players") or [])[:8],
            }, default=str))

        # Fill Ligue 1 / Serie A canaries from first available offered scorer
        for lbl in ("Ligue 1", "Serie A"):
            s = league_scans.get(lbl) or {}
            offered = s.get("sample_offered_players") or []
            if offered:
                SOCCER_LEAGUE_CANARY[lbl] = [offered[0]]

        print("\n" + "=" * 78)
        print("SOCCER CANARY TRACES")
        print("=" * 78)
        soccer_results = []
        for lbl, sk in SOCCER_LEAGUES:
            canaries = SOCCER_LEAGUE_CANARY.get(lbl) or []
            if not canaries:
                continue
            # Get events once per league
            evs = await _get(client, f"{BASE}/sports/{sk}/events",
                             {"dateFormat": "iso"})
            if not isinstance(evs, list):
                evs = []
            for pf in canaries:
                t = await trace_soccer_canary(client, lbl, sk, pf, evs)
                soccer_results.append(t)
                print(json.dumps({
                    "league": lbl,
                    "player": t["player"],
                    "stop_reason": t["stop_reason"],
                    "lock_score": t["lock_score"],
                    "publication": t.get("publication_decision"),
                    "events_scanned": t["events_scanned"],
                    "markets_offered": len(t["markets_offered"]),
                    "form_row_present": t["form_row_present"],
                }, default=str))
                # One canary per league is enough once resolved.
                if t.get("form_row_present") or t["stop_reason"] in (
                        "PROVIDER_ZERO", "APP_DROPPED_MARKET"):
                    break

        print("\n" + "=" * 78)
        print("HIGH-LOCK / APEX REACHABILITY (SYNTHETIC — STRUCTURAL PROOF)")
        print("=" * 78)
        apex = apex_reachability()
        for name, lock, tr, sport in apex:
            print(f"  {sport:7s} {name:22s} lock={lock}  tier={tr}")

        # Full result dump
        out = {
            "nhl_results":    nhl_results,
            "soccer_results": soccer_results,
            "league_scans":   league_scans,
            "apex_reachability": [
                {"name": n, "lock": l, "tier": t, "sport": s}
                for n, l, t, s in apex
            ],
        }
        with open("/tmp/p0_canary_full.json", "w") as f:
            json.dump(out, f, indent=2, default=str)
        print("\nFull result dump → /tmp/p0_canary_full.json")


if __name__ == "__main__":
    asyncio.run(main())
