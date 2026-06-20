"""End-to-end soccer prediction pipeline (football-data.org backed).

Flow:
  1. Pull today's matches from /matches
  2. For each unique competition (PL, BL1, PD, SA, FL1, CL, etc.), fetch
     standings (cached for 15 min)
  3. Build a team_id → standing-row index
  4. Run the predictor against each SCHEDULED/TIMED match
  5. Dual-write upserts to:
       • soccer_predictions  — canonical store for this module
       • picks               — merged so they show in Locks/Killer/
                                Rollover (when confidence ≥ 85)

football-data.org free tier allows 10 req/min on free competitions only.
The cache ensures the pipeline costs at most ~12 requests per run
(1 matches + N standings calls, capped at MAX_COMPETITIONS).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone
from typing import Any

from .client import SoccerAPIError, client
from .normalize import (
    normalize_form_string,
    normalize_standing_row,
    status_is_pregame,
)
from .predictor import build_prediction, to_picks_collection_doc

logger = logging.getLogger("lockscore.soccer.pipeline")

# Hard cap on distinct competitions per pipeline run.
# Free-tier rate limit is 10 req/min → keep it well under.
_MAX_COMPETITIONS = 8


async def run_prediction_pipeline(db) -> dict:
    """Run the full pipeline once. Returns a summary dict."""
    started = datetime.now(timezone.utc)
    summary: dict[str, Any] = {
        "started_at": started.isoformat(),
        "matches_seen": 0,
        "matches_pregame": 0,
        "predictions_made": 0,
        "merged_into_picks": 0,
        "competitions_loaded": 0,
        "errors": [],
    }
    try:
        match_resp = await client.matches_by_date(date.today())
    except SoccerAPIError as e:
        logger.warning("Pipeline aborted — matches call failed: %s", e)
        summary["errors"].append(f"matches: {e}")
        return summary

    raw_matches = (match_resp or {}).get("matches") or []
    summary["matches_seen"] = len(raw_matches)

    # Distinct competition codes in today's slate.
    comp_codes: list[str] = []
    pregame: list[dict] = []
    for raw in raw_matches:
        if not status_is_pregame(raw.get("status")):
            continue
        pregame.append(raw)
        comp = raw.get("competition") or {}
        code = comp.get("code")
        if code and code not in comp_codes:
            comp_codes.append(code)
    summary["matches_pregame"] = len(pregame)

    # Cap competitions to respect rate limit. Prefer top leagues if many.
    comp_codes = comp_codes[:_MAX_COMPETITIONS]

    # Fetch standings concurrently.
    standings_index: dict[int, dict] = {}
    if comp_codes:
        results = await asyncio.gather(
            *[client.standings(c) for c in comp_codes],
            return_exceptions=True,
        )
        for code, r in zip(comp_codes, results):
            if isinstance(r, Exception):
                summary["errors"].append(f"standings({code}): {r}")
                continue
            # /competitions/{code}/standings → response.standings[].table[]
            for std in (r or {}).get("standings") or []:
                # Only the overall ("TOTAL") table — skip HOME/AWAY tables.
                if std.get("type") and std["type"] != "TOTAL":
                    continue
                for row in std.get("table") or []:
                    nr = normalize_standing_row(row)
                    if nr["team_id"]:
                        # Normalize form to contiguous "WWDLW" since
                        # football-data.org returns "W,W,D,L,W".
                        nr["form"] = normalize_form_string(nr.get("form"))
                        standings_index[nr["team_id"]] = nr
        summary["competitions_loaded"] = len(comp_codes)

    # Build predictions + dual-write. For each fixture, kick off the
    # H2H lookup concurrently — these are tiny GETs and the 24h cache
    # means each unique team pair only hits the API once per day.
    upserts_pred: list[dict] = []
    upserts_pick: list[dict] = []
    # Fan out H2H calls in parallel, capped to one per fixture.
    h2h_lookups = await asyncio.gather(
        *[client.h2h_matches(
            (raw.get("homeTeam") or {}).get("id") or 0,
            (raw.get("awayTeam") or {}).get("id") or 0,
            limit=6,
        ) for raw in pregame],
        return_exceptions=True,
    )
    for raw, h2h in zip(pregame, h2h_lookups):
        if isinstance(h2h, Exception):
            h2h = []
        pred = build_prediction(raw, standings_index, h2h_matches=h2h)
        if pred is None:
            continue
        upserts_pred.append(pred)
        if (pred.get("confidence") or 0) >= 85.0:
            upserts_pick.append(to_picks_collection_doc(pred))
        # Synthesize Over 1.5 Goals from team strengths (no Odds API
        # dependency). Only emits when the Poisson model is confident
        # the goal floor will clear — see make_over_1_5_pick().
        try:
            from .predictor import make_over_1_5_pick
            ov = make_over_1_5_pick(pred)
            if ov:
                upserts_pick.append(ov)
        except Exception as _ov_err:
            logger.warning("Over 1.5 synthesis skipped for %s: %s",
                           pred.get("event"), _ov_err)

    summary["predictions_made"]  = len(upserts_pred)
    summary["merged_into_picks"] = len(upserts_pick)

    for d in upserts_pred:
        await db.soccer_predictions.update_one(
            {"id": d["id"]},
            {"$set": d, "$setOnInsert": {"first_seen": d["created_at"]}},
            upsert=True,
        )
    for d in upserts_pick:
        await db.picks.update_one(
            {"id": d["id"]},
            {"$set": d, "$setOnInsert": {"first_seen": d["created_at"]}},
            upsert=True,
        )

    finished = datetime.now(timezone.utc)
    summary["finished_at"] = finished.isoformat()
    summary["elapsed_ms"] = int((finished - started).total_seconds() * 1000)
    logger.info("Soccer pipeline done: %s", summary)
    return summary


async def soccer_pipeline_loop(db) -> None:
    """Pregame scheduler — runs the pipeline every 15 minutes."""
    await asyncio.sleep(20)  # let the rest of the app start
    while True:
        try:
            await run_prediction_pipeline(db)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("Soccer pipeline loop error: %s", e)
        await asyncio.sleep(15 * 60)
