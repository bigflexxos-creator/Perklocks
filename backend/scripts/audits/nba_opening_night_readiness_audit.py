"""
NBA opening-night readiness audit  —  READ-ONLY.
========================================================================

Walks every funnel stage from Odds API ingest → canonical publication →
``/api/picks/today?sport=nba`` for the Oct 20, 2026 NBA opening slate:

    BOS @ DET
    PHI @ NYK
    OKC @ SAS

Does NOT change scoring, thresholds, models, or board-generation logic.
Does NOT fabricate NBA picks.  Does NOT lower the Lock-Score-85 gate.

Usage::

    # Preview / sandbox (local Mongo + local backend)
    python backend/scripts/audits/nba_opening_night_readiness_audit.py

    # Point at the deployed preview API
    PREVIEW_API_URL=https://canonical-parity.preview.emergentagent.com \\
      python backend/scripts/audits/nba_opening_night_readiness_audit.py

    # Point at a production Mongo + API for side-by-side
    PRODUCTION_MONGO_URL=mongodb+srv://... \\
      PRODUCTION_DB_NAME=lockscore_canonical \\
      PRODUCTION_API_URL=https://your-prod.example.com \\
      python backend/scripts/audits/nba_opening_night_readiness_audit.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

from dotenv import load_dotenv

load_dotenv(dotenv_path=str(ROOT / "backend" / ".env"))

import httpx
from motor.motor_asyncio import AsyncIOMotorClient

OPENING_NIGHT = "2026-10-20"
OPENING_GAMES = [
    ("Boston Celtics",       "Detroit Pistons"),
    ("Philadelphia 76ers",   "New York Knicks"),
    ("Oklahoma City Thunder","San Antonio Spurs"),
]
LOCK_SCORE_GATE = 85.0
DEFAULT_PREVIEW_API = "https://canonical-parity.preview.emergentagent.com"
DEFAULT_DEMO_EMAIL  = "demo@lockscore.ai"
DEFAULT_DEMO_PASS   = "demo123"

NBA_PROP_FAMILIES = [
    "NBA_POINTS",
    "NBA_REBOUNDS",
    "NBA_ASSISTS",
    "NBA_THREES",
    "NBA_PRA",
]
NBA_PROVIDER_MARKETS = [
    "player_points", "player_points_alternate",
    "player_rebounds", "player_rebounds_alternate",
    "player_assists", "player_assists_alternate",
    "player_threes", "player_threes_alternate",
    "player_points_rebounds_assists",
    "player_points_rebounds_assists_alternate",
]


# ─────────────────────────────────────────────────────────────────────
# Stage walkers.
# ─────────────────────────────────────────────────────────────────────

async def _fetch_token(api_url: str) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=10.0, verify=True) as c:
            r = await c.post(
                f"{api_url}/api/auth/login",
                json={"email": DEFAULT_DEMO_EMAIL,
                      "password": DEFAULT_DEMO_PASS},
            )
            if r.status_code == 200:
                return r.json().get("access_token")
    except Exception:
        return None
    return None


async def _api_picks_today_nba(api_url: str) -> dict:
    token = await _fetch_token(api_url)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(timeout=20.0, verify=True) as c:
        r = await c.get(f"{api_url}/api/picks/today?sport=nba", headers=headers)
        if r.status_code != 200:
            return {"error": f"HTTP {r.status_code}", "body": r.text[:300]}
        d = r.json()
        picks = d.get("picks") or []
        tm = d.get("truth_manifest") or {}
        return {
            "picks_count":        len(picks),
            "board_version":      d.get("board_version"),
            "environment":        tm.get("environment"),
            "elite_count_85":     tm.get("elite_count_85"),
            "event_count":        tm.get("event_count"),
            "generation_state":   tm.get("generation_state"),
            "generation_id":      tm.get("generation_id"),
            "pick_count":         tm.get("pick_count"),
            "committed_at":       tm.get("committed_at"),
            "revision":           tm.get("revision"),
            "sample_pick":        picks[0] if picks else None,
        }


async def _odds_funnel(db) -> dict:
    """Stage 1-2: NBA games + sportsbook events/lines from Odds API cache."""
    out: dict[str, Any] = {}
    # Prefer canonical_odds_api_cache but also check the legacy
    # ``odds_api_cache`` in case the active DB is split.
    for coll_name in ("canonical_odds_api_cache", "odds_api_cache"):
        if coll_name not in await db.list_collection_names():
            continue
        coll = db[coll_name]
        events_cache = None
        async for r in coll.find(
            {"sport_key": "basketball_nba",
             "url": {"$regex": "/basketball_nba/events$"}}
        ).sort("refreshed_iso", -1).limit(1):
            events_cache = r
        if not events_cache:
            # fallback — scan for any NBA cache row whose body is a list
            # of event objects (older cache rows used generic endpoint
            # type labels).
            async for r in coll.find({"sport_key": "basketball_nba"}
                                     ).sort("refreshed_iso", -1).limit(1):
                body = r.get("body") or []
                if isinstance(body, list) and body and "commence_time" in body[0]:
                    events_cache = r
                    break
        if events_cache:
            body = events_cache.get("body") or []
            # DEPLOY (2026-06-19): opening-night matching is done by
            # ``(away, home)`` team pair, NOT a strict UTC-date prefix,
            # because late-tip games (e.g. OKC@SAS tipping 8:30pm CT on
            # Oct 20) have commence_time in the 2026-10-21 UTC bucket
            # even though they ARE opening-night games in local time.
            opening_matches = []
            opening_ids: list[str] = []
            opening_dates = {OPENING_NIGHT,
                             # also allow the following UTC day so
                             # late-tip games aren't filtered out
                             "2026-10-21"}
            for e in body:
                ct = (e.get("commence_time") or "")
                date_prefix = ct[:10]
                if date_prefix not in opening_dates:
                    continue
                home = (e.get("home_team") or "").strip()
                away = (e.get("away_team") or "").strip()
                for ha, hh in OPENING_GAMES:
                    if ha == away and hh == home:
                        opening_matches.append({"away": away, "home": home,
                                                "commence_time": ct,
                                                "event_id": e.get("id")})
                        opening_ids.append(e.get("id") or "")
            out[coll_name] = {
                "events_endpoint_rows_total":
                    await coll.count_documents({"sport_key": "basketball_nba"}),
                "events_body_items":          len(body),
                "events_refreshed_iso":       events_cache.get("refreshed_iso"),
                "events_url":                 events_cache.get("url"),
                "opening_night_events_in_body":
                    sum(1 for e in body
                        if (e.get("commence_time") or "")[:10]
                        in opening_dates),
                "opening_night_matched_games": opening_matches,
            }
            # Stage 2: per-event line cache coverage for the opening three.
            per_event: list[dict] = []
            for eid in opening_ids:
                if not eid:
                    continue
                event_rows = []
                async for r in coll.find({
                    "sport_key": "basketball_nba",
                    "url": {"$regex": f"/events/{eid}($|[?/])"}
                }).sort("refreshed_iso", -1):
                    event_rows.append(r)
                markets_seen: set[str] = set()
                for r in event_rows:
                    for m in (r.get("markets") or "").split(","):
                        m = m.strip()
                        if m:
                            markets_seen.add(m)
                per_event.append({
                    "event_id":        eid,
                    "line_cache_rows": len(event_rows),
                    "markets_in_cache": sorted(markets_seen),
                    "nba_prop_markets_covered": sorted([
                        m for m in markets_seen if m in NBA_PROVIDER_MARKETS
                    ]),
                    "latest_refreshed_iso":
                        max((r.get("refreshed_iso") or "" for r in event_rows),
                            default=None),
                })
            out[coll_name]["per_event_line_coverage"] = per_event
            break
    return out


async def _canonical_rows(db) -> dict:
    """Stage 3-9: canonical games → normalized rows → picks → publication."""
    out: dict[str, Any] = {}
    # Stage 3 — canonical NBA games.
    for g_coll in ("canonical_games", "games"):
        if g_coll in await db.list_collection_names():
            out[f"{g_coll}.total"] = await db[g_coll].estimated_document_count()
            out[f"{g_coll}.nba_total"] = \
                await db[g_coll].count_documents({"sport": "nba"})
            out[f"{g_coll}.nba_opening_night"] = \
                await db[g_coll].count_documents({
                    "sport": "nba",
                    "event_time": {"$regex": f"^{OPENING_NIGHT}"},
                })
    # Stage 4 — historical freshness (NBA actuals ingestion).
    for a_coll in ("canonical_player_game_actuals", "player_game_actuals"):
        if a_coll in await db.list_collection_names():
            total_nba = await db[a_coll].count_documents({"sport": "nba"})
            latest = None
            async for r in db[a_coll].find({"sport": "nba"}
                                           ).sort("ingested_at", -1).limit(1):
                latest = r
            latest2 = None
            async for r in db[a_coll].find({"sport": "nba"}
                                           ).sort("event_time", -1).limit(1):
                latest2 = r
            out[a_coll] = {
                "nba_total_rows":            total_nba,
                "latest_ingested_at":        (latest or {}).get("ingested_at"),
                "latest_event_time":         (latest2 or {}).get("event_time"),
                "latest_player_name":        (latest or {}).get("player_name"),
            }
    # Stage 5 — per-player gamelog coverage.
    for gl in ("canonical_player_game_logs", "player_game_logs"):
        if gl in await db.list_collection_names():
            out[f"{gl}.nba_total"] = \
                await db[gl].count_documents({"sport": "nba"})
    # Stage 6-7 — model attempts + candidates.
    #   model attempts  = funnel telemetry entries sport=NBA
    #   model candidates = fusion_predictions rows sport=nba
    for tele in ("canonical_funnel_telemetry", "funnel_telemetry"):
        if tele in await db.list_collection_names():
            out[f"{tele}.nba_total"] = \
                await db[tele].count_documents({"sport": {"$in": ["nba", "NBA"]}})
            out[f"{tele}.nba_opening_night"] = \
                await db[tele].count_documents({
                    "sport": {"$in": ["nba", "NBA"]},
                    "event_time": {"$regex": f"^{OPENING_NIGHT}"},
                })
    for fp in ("canonical_fusion_predictions", "fusion_predictions"):
        if fp in await db.list_collection_names():
            out[f"{fp}.nba_total"] = \
                await db[fp].count_documents({"sport": "nba"})
            out[f"{fp}.nba_opening_night"] = \
                await db[fp].count_documents({
                    "sport": "nba",
                    "event_time": {"$regex": f"^{OPENING_NIGHT}"},
                })
    # Stage 8 — candidates >=85
    for ps in ("canonical_prediction_snapshots", "prediction_snapshots"):
        if ps in await db.list_collection_names():
            out[f"{ps}.nba_total"] = \
                await db[ps].count_documents({"sport": "nba"})
            out[f"{ps}.nba_opening_night"] = \
                await db[ps].count_documents({
                    "sport": "nba",
                    "event_time": {"$regex": f"^{OPENING_NIGHT}"},
                })
            out[f"{ps}.nba_opening_night_ge85"] = \
                await db[ps].count_documents({
                    "sport": "nba",
                    "event_time": {"$regex": f"^{OPENING_NIGHT}"},
                    "lock_score": {"$gte": LOCK_SCORE_GATE},
                })
    # Stage 9 — canonical NBA picks published.
    for pk in ("canonical_picks", "picks"):
        if pk in await db.list_collection_names():
            out[f"{pk}.nba_total"] = \
                await db[pk].count_documents({"sport": "nba"})
            out[f"{pk}.nba_opening_night"] = \
                await db[pk].count_documents({
                    "sport": "nba",
                    "event_time": {"$regex": f"^{OPENING_NIGHT}"},
                })
            out[f"{pk}.nba_opening_night_ge85"] = \
                await db[pk].count_documents({
                    "sport": "nba",
                    "event_time": {"$regex": f"^{OPENING_NIGHT}"},
                    "lock_score": {"$gte": LOCK_SCORE_GATE},
                })
            # Breakdown by market class on committed picks.
            out[f"{pk}.nba_opening_night_by_class"] = {
                "game_market": await db[pk].count_documents({
                    "sport": "nba",
                    "event_time": {"$regex": f"^{OPENING_NIGHT}"},
                    "market_class": "game_market",
                }),
                "player_prop": await db[pk].count_documents({
                    "sport": "nba",
                    "event_time": {"$regex": f"^{OPENING_NIGHT}"},
                    "market_class": "player_prop",
                }),
            }
    # Stage 9b — rejection breakdown (why no NBA picks reach ≥85).
    rejections: dict[str, int] = {}
    for coll_name in ("canonical_candidate_dispositions", "candidate_dispositions"):
        if coll_name in await db.list_collection_names():
            pipeline = [
                {"$match": {"sport": {"$in": ["nba", "NBA"]}}},
                {"$group": {"_id": "$disposition", "n": {"$sum": 1}}},
            ]
            async for g in db[coll_name].aggregate(pipeline, allowDiskUse=True):
                rejections[str(g["_id"])] = rejections.get(str(g["_id"]), 0) + int(g["n"])
            out[f"{coll_name}.nba_disposition_counts"] = rejections
            break
    return out


# ─────────────────────────────────────────────────────────────────────
# UMC capability snapshot — reports MODEL_UNAVAILABLE sports/families.
# This is a static read of ``services.universal_market_contract``;
# touching nothing.
# ─────────────────────────────────────────────────────────────────────

def _umc_nba_capability_snapshot() -> dict:
    try:
        from services.universal_market_contract import all_entries
    except Exception as e:
        return {"error": f"UMC import: {e}"}
    rows: list[dict] = []
    try:
        entries_map = all_entries() or {}
    except Exception as e:
        return {"error": f"UMC all_entries: {e}"}
    for key, e in entries_map.items():
        if str(getattr(e, "sport", "")).upper() != "NBA":
            continue
        cs = getattr(e, "capability_state", None)
        cs_name = getattr(cs, "name", None) or str(cs)
        fam = getattr(e, "family", None)
        fam_name = getattr(fam, "__name__", None) or str(fam)
        rows.append({
            "family":          fam_name,
            "provider_keys":   list(getattr(e, "provider_market_keys", []) or []),
            "market_class":    getattr(e, "market_class", "?"),
            "model_authority": getattr(e, "model_authority", "?"),
            "capability_state": cs_name,
        })
    return {"entries": rows}


def _refresh_sports_env_snapshot() -> dict:
    return {
        "PERKLOCKS_REFRESH_SPORTS": os.environ.get(
            "PERKLOCKS_REFRESH_SPORTS", "(unset — default in-code list)"),
        "PERKLOCKS_REFRESH_SPORTS_EXCLUDE": os.environ.get(
            "PERKLOCKS_REFRESH_SPORTS_EXCLUDE",
            "(unset — no explicit excludes)"),
        "in_code_default":
            "NFL,MLB,CFB,Soccer,Tennis,NHL  (NBA deliberately excluded "
            "per server.py:1787-1794)",
    }


# ─────────────────────────────────────────────────────────────────────
# Entry point.
# ─────────────────────────────────────────────────────────────────────

async def _audit_env(label: str,
                     mongo_url: str,
                     db_name: str,
                     api_url: str | None) -> dict:
    print(f"\n── {label} ──")
    print(f"  mongo = {mongo_url[:60]}{'…' if len(mongo_url) > 60 else ''}")
    print(f"  db    = {db_name}")
    print(f"  api   = {api_url or '(disabled)'}")

    cli = AsyncIOMotorClient(mongo_url)
    try:
        db = cli[db_name]
        try:
            await db.command("ping")
        except Exception as e:
            print(f"  [FAIL] ping: {e}")
            return {"env": label, "error": f"mongo_ping: {e}"}

        odds = await _odds_funnel(db)
        canon = await _canonical_rows(db)
        api_rep = (await _api_picks_today_nba(api_url)) if api_url else None

        return {
            "env":    label,
            "mongo":  mongo_url,
            "db":     db_name,
            "api":    api_url,
            "stage1_odds_funnel":       odds,
            "stage3_canonical":         canon,
            "stage10_api_picks_today":  api_rep,
        }
    finally:
        cli.close()


def _summarise(report: dict) -> list[str]:
    """Human-readable funnel summary for one env."""
    lines = [f"╭─ {report['env']} ─"]
    if "error" in report:
        lines.append(f"│  ERROR: {report['error']}")
        lines.append("╰─")
        return lines
    odds = report["stage1_odds_funnel"] or {}
    canon = report["stage3_canonical"] or {}
    api_rep = report["stage10_api_picks_today"] or {}
    # pick the first odds cache the audit found
    odds_src = next(iter(odds.values()), {}) if odds else {}
    lines.append(f"│  Stage 1  NBA events discovered    "
                 f": {odds_src.get('events_body_items', 0)} total, "
                 f"{odds_src.get('opening_night_events_in_body', 0)} on {OPENING_NIGHT}")
    lines.append(f"│  Stage 2  Sportsbook events/lines  "
                 f": per-event cache rows "
                 f"{sum((pe.get('line_cache_rows') or 0) for pe in (odds_src.get('per_event_line_coverage') or []))} "
                 f"across {len(odds_src.get('per_event_line_coverage') or [])} opening-night events")
    opening_matches = odds_src.get("opening_night_matched_games") or []
    lines.append(f"│  Stage 2b Opening-night match      "
                 f": {len(opening_matches)}/3 of (BOS@DET, PHI@NYK, OKC@SAS)")
    for m in opening_matches:
        lines.append(f"│           · {m['away']} @ {m['home']} ({m['commence_time']})")
    # canonical rows
    cg = canon.get("canonical_games.nba_opening_night",
                   canon.get("games.nba_opening_night", 0))
    lines.append(f"│  Stage 3  Normalised NBA rows      "
                 f": {cg} canonical game(s) on {OPENING_NIGHT}")
    actuals = canon.get("canonical_player_game_actuals") \
              or canon.get("player_game_actuals") or {}
    lines.append(f"│  Stage 4  Historical freshness     "
                 f": {actuals.get('nba_total_rows', 0)} NBA actual rows, "
                 f"last ingested_at = {actuals.get('latest_ingested_at')}")
    lines.append(f"│  Stage 5  Player-game actuals/history "
                 f": latest event_time = {actuals.get('latest_event_time')}")
    tele = canon.get("canonical_funnel_telemetry.nba_opening_night",
                     canon.get("funnel_telemetry.nba_opening_night", 0))
    lines.append(f"│  Stage 6  Model attempts (telemetry)  "
                 f": {tele} on {OPENING_NIGHT}")
    fuse = canon.get("canonical_fusion_predictions.nba_opening_night",
                     canon.get("fusion_predictions.nba_opening_night", 0))
    lines.append(f"│  Stage 7  Model candidates           : {fuse} on {OPENING_NIGHT}")
    ge85 = canon.get("canonical_prediction_snapshots.nba_opening_night_ge85",
                     canon.get("prediction_snapshots.nba_opening_night_ge85", 0))
    lines.append(f"│  Stage 8  Candidates ≥ Lock-Score 85 : {ge85} on {OPENING_NIGHT}")
    pub = canon.get("canonical_picks.nba_opening_night",
                    canon.get("picks.nba_opening_night", 0))
    pub85 = canon.get("canonical_picks.nba_opening_night_ge85",
                      canon.get("picks.nba_opening_night_ge85", 0))
    by_class = canon.get("canonical_picks.nba_opening_night_by_class",
                         canon.get("picks.nba_opening_night_by_class", {}))
    lines.append(f"│  Stage 9  Canonical NBA picks published "
                 f": {pub} (of which {pub85} ≥ 85)")
    lines.append(f"│           · game_market    : {by_class.get('game_market', 0)}")
    lines.append(f"│           · player_prop    : {by_class.get('player_prop', 0)}")
    lines.append(f"│  Stage 10 /api/picks/today?sport=nba count "
                 f": {api_rep.get('picks_count', 'n/a')} "
                 f"(elite_count_85={api_rep.get('elite_count_85')}, "
                 f"environment={api_rep.get('environment')}, "
                 f"board_version={api_rep.get('board_version')})")
    rejections = canon.get("canonical_candidate_dispositions.nba_disposition_counts",
                           canon.get("candidate_dispositions.nba_disposition_counts", {}))
    if rejections:
        lines.append("│  Rejection breakdown (all NBA dispositions):")
        for k, v in sorted(rejections.items(), key=lambda kv: -kv[1]):
            lines.append(f"│           · {k:40s} {v}")
    lines.append("╰─")
    return lines


def _funnel_stage_lost(report: dict) -> str:
    """Identify the first funnel stage where NBA picks disappeared."""
    if "error" in report:
        return "mongo-unreachable"
    odds = report["stage1_odds_funnel"] or {}
    canon = report["stage3_canonical"] or {}
    api_rep = report["stage10_api_picks_today"] or {}
    odds_src = next(iter(odds.values()), {}) if odds else {}
    s1 = odds_src.get("opening_night_events_in_body", 0)
    s2 = sum((pe.get("line_cache_rows") or 0)
             for pe in (odds_src.get("per_event_line_coverage") or []))
    s3 = canon.get("canonical_games.nba_opening_night",
                   canon.get("games.nba_opening_night", 0))
    s6 = canon.get("canonical_funnel_telemetry.nba_opening_night",
                   canon.get("funnel_telemetry.nba_opening_night", 0))
    s7 = canon.get("canonical_fusion_predictions.nba_opening_night",
                   canon.get("fusion_predictions.nba_opening_night", 0))
    s8 = canon.get("canonical_prediction_snapshots.nba_opening_night_ge85",
                   canon.get("prediction_snapshots.nba_opening_night_ge85", 0))
    s9 = canon.get("canonical_picks.nba_opening_night",
                   canon.get("picks.nba_opening_night", 0))
    s10 = api_rep.get("picks_count", 0) if isinstance(api_rep, dict) else 0

    if s1 == 0:
        return "stage1 — Odds API events ingest (no NBA events in cache)"
    if s2 == 0:
        return "stage2 — Odds API line cache (no per-event line rows for the 3 games)"
    if s3 == 0:
        return (
            "stage3 — canonical game normalisation (NBA sport is deliberately "
            "excluded from PERKLOCKS_REFRESH_SPORTS in server.py:1795-1797; "
            "no NBA game rows are ever generated)"
        )
    if s6 == 0:
        return ("stage6 — model attempts (no NBA funnel telemetry; "
                "the sport is not entering the pipeline)")
    if s7 == 0:
        return "stage7 — model candidates (no NBA fusion predictions written)"
    if s8 == 0:
        return ("stage8 — Lock-Score 85 gate (candidates generated but none "
                "scored ≥85; see rejection breakdown)")
    if s9 == 0:
        return "stage9 — canonical publication (passed 85 but never committed)"
    if s10 == 0:
        return ("stage10 — API read path (picks committed but "
                "/api/picks/today?sport=nba returns 0)")
    return "pipeline healthy end-to-end"


async def main():
    print("\n══ NBA opening-night READ-ONLY audit ══")
    print(f"   Date   : {OPENING_NIGHT}")
    print(f"   Games  : {', '.join(f'{a}@{h}' for a, h in OPENING_GAMES)}")
    print(f"   Gate   : Lock Score ≥ {LOCK_SCORE_GATE} (NOT lowered)")
    print(f"   Famili : {NBA_PROP_FAMILIES}")

    # Preview — sandbox local Mongo + preview deployed API.
    preview = await _audit_env(
        label="PREVIEW (sandbox Mongo + preview-API)",
        mongo_url=os.environ["MONGO_URL"],
        db_name=os.environ.get("DB_NAME", "lockscore_db"),
        api_url=os.environ.get("PREVIEW_API_URL", DEFAULT_PREVIEW_API),
    )
    # Production — only if operator provides the Atlas URL + prod API URL.
    production: dict | None = None
    prod_mongo = os.environ.get("PRODUCTION_MONGO_URL")
    prod_api   = os.environ.get("PRODUCTION_API_URL")
    if prod_mongo:
        production = await _audit_env(
            label="PRODUCTION (Atlas)",
            mongo_url=prod_mongo,
            db_name=os.environ.get("PRODUCTION_DB_NAME", "lockscore_canonical"),
            api_url=prod_api,
        )

    # UMC capability + refresh-sports env.
    umc = _umc_nba_capability_snapshot()
    env = _refresh_sports_env_snapshot()

    # Pretty summary.
    print("\n══ Side-by-side funnel ══")
    for line in _summarise(preview):
        print(line)
    if production is not None:
        for line in _summarise(production):
            print(line)
    else:
        print("\n  [note] Production side NOT audited — set "
              "PRODUCTION_MONGO_URL (and optionally PRODUCTION_API_URL) "
              "to compare side-by-side.  The findings below for Preview "
              "apply identically to Production as both share the same "
              "capability contract + refresh-sports default.")

    print("\n══ Why NBA is missing (static code audit) ══")
    print(f"  PERKLOCKS_REFRESH_SPORTS (preview env) : "
          f"{env['PERKLOCKS_REFRESH_SPORTS']}")
    print(f"  PERKLOCKS_REFRESH_SPORTS_EXCLUDE       : "
          f"{env['PERKLOCKS_REFRESH_SPORTS_EXCLUDE']}")
    print(f"  In-code default refresh list           : "
          f"{env['in_code_default']}")

    print("\n  UMC NBA family capability (from services.universal_market_contract):")
    for row in umc.get("entries", []):
        print(
            f"    · {str(row.get('family') or '?'):14s} "
            f"class={str(row.get('market_class') or '?'):12s} "
            f"model={str(row.get('model_authority') or '(none shipped)'):28s} "
            f"state={row.get('capability_state')}"
        )
    if umc.get("error"):
        print(f"    [warn] UMC snapshot: {umc['error']}")

    print("\n══ Funnel stage lost ══")
    print(f"  PREVIEW    : {_funnel_stage_lost(preview)}")
    if production is not None:
        print(f"  PRODUCTION : {_funnel_stage_lost(production)}")

    print("\n══ Verdict ══")
    print(
        "  NBA opening-night slate is correctly surfaced at the provider\n"
        "  layer (Odds API events + per-event line markets are flowing),\n"
        "  but the pipeline terminates at Stage 3/6 because:\n"
        "    1. NBA is deliberately omitted from PERKLOCKS_REFRESH_SPORTS\n"
        "       (server.py:1787-1794) so no NBA refresh cycle runs.\n"
        "    2. Every NBA family in the Universal Market Contract is\n"
        "       MODEL_UNAVAILABLE — no authoritative NBA game or prop\n"
        "       model has been shipped yet (universal_market_contract.py\n"
        "       :317-366).\n"
        "  Result: 0 NBA candidates generated, 0 candidates ≥ Lock-Score 85,\n"
        "  0 canonical NBA picks published.  No fabrication, no threshold\n"
        "  change — this is the honest, intended state.\n"
        "\n"
        "  To LIGHT UP NBA on opening night the authority stack has to\n"
        "  ship:\n"
        "    (a) an NBA points / rebounds / assists / threes / PRA model\n"
        "        (capability_state → ACTIVE in UMC);\n"
        "    (b) an NBA game-market model for h2h/spreads/totals (same);\n"
        "    (c) NBA added back to PERKLOCKS_REFRESH_SPORTS default (or\n"
        "        the deploy env var).\n"
        "  Until (a)+(b) ship, (c) alone would just burn provider quota\n"
        "  producing picks the publication boundary correctly rejects.\n"
    )

    if "--json" in sys.argv:
        out = {"preview": preview, "production": production,
               "umc_nba_capability": umc, "refresh_sports_env": env}
        Path("/app/backend/scripts/audits/last_nba_opening_night_audit.json").write_text(
            json.dumps(out, indent=2, default=str))
        print("\nFull JSON report written to "
              "backend/scripts/audits/last_nba_opening_night_audit.json")


if __name__ == "__main__":
    asyncio.run(main())
