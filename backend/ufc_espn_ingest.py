"""UFC / MMA ingest from ESPN.

ESPN's `mma/ufc` scoreboard lists every card on the calendar with:
  • Fighter names + ESPN athlete IDs
  • Weight class
  • Country flag (via `athlete.flag.href`) — we treat as \"logo\"
  • Career records ('18-10-1')
  • Venue / city
  • Card ordering (main / prelim)

What ESPN does NOT expose:
  • Moneylines (the payload's `odds` array is always empty for UFC)
  • Method-of-victory props

So the picks we emit are model-only: derived from career win-rate
delta + streak. Fair-odds only, `is_extra=True`. When The Odds API
finally posts UFC lines (usually 3-4 days out), the main pipeline
takes over and this ingest deduplicates.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from services.espn_common import (
    american_from_prob,
    deterministic_pick_id,
    fetch_slate_multi,
    grade_from_conf,
)

logger = logging.getLogger("lockscore.ufc_espn")

UFC_SLUGS: list[tuple[str, str, str]] = [
    ("mma/ufc",   "UFC",           "mma_mixed_martial_arts"),
    ("mma/pfl",   "PFL",           "mma_pfl"),
    ("mma/bellator", "Bellator",   "mma_bellator"),
]

_MIN_CONF_FLOOR = 55.0
_SOURCE_TAG = "ufc_espn_v1"


def _record_win_rate(record: str) -> Optional[tuple[float, int, int, int]]:
    """'18-10-1' → (0.635, 18, 10, 1). Returns None on parse fail."""
    m = re.match(r"^\s*(\d+)\s*-\s*(\d+)(?:\s*-\s*(\d+))?", record or "")
    if not m:
        return None
    w = int(m.group(1)); loss = int(m.group(2))
    d = int(m.group(3) or 0)
    total = w + loss + d
    if total < 3:
        return None  # too small a sample
    return (w / total, w, loss, d)


def _build_ufc_pick(pe) -> Optional[dict]:
    """Fair-odds ML pick built from career records + weight-class prior.

    Confidence formula:
      base = 50
      +/- (win_rate_diff * 40)     — a 20-pt win-rate gap → +/-8 pts
      +/- (log(fights_A) - log(fights_B)) * 3   — experience nudge
      cap [30, 82] so we never over-claim on records alone
    """
    home_rec = _record_win_rate(pe.home.get("record") or "")
    away_rec = _record_win_rate(pe.away.get("record") or "")
    if not home_rec or not away_rec:
        return None
    hr, hw, hl, hd = home_rec
    ar, aw, al, ad = away_rec
    diff = hr - ar
    import math
    exp_delta = math.log(max(1, hw + hl + hd)) - math.log(max(1, aw + al + ad))
    home_conf = 50.0 + diff * 40.0 + exp_delta * 3.0
    home_conf = max(30.0, min(82.0, home_conf))
    away_conf = 100.0 - home_conf

    # Pick the more-confident side; require > floor.
    if home_conf >= away_conf:
        conf = round(home_conf, 1)
        sel = pe.home["name"]
        side = "home"
    else:
        conf = round(away_conf, 1)
        sel = pe.away["name"]
        side = "away"
    if conf < _MIN_CONF_FLOOR:
        return None

    fair_odds = american_from_prob(conf)
    event_name = f"{pe.away['name']} vs {pe.home['name']}"

    return {
        "id":               deterministic_pick_id(_SOURCE_TAG, pe.event_id, "ml", side),
        "external_id":      f"{pe.sport_key}-{pe.event_id}-ml-{side}",
        "sport":            "UFC",
        "league":           pe.league_label,
        "event":            event_name,
        "event_time":       pe.kickoff_utc,
        "market":           f"{sel} Moneyline",
        "selection":        sel,
        "win_probability":  conf,
        "implied_probability": conf,
        "book_odds":        fair_odds,
        "edge_percent":     0.0,
        "lock_score":       conf,
        "lock_score_v2":    conf,
        "grade":            grade_from_conf(conf),
        "pick_date":        datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "is_under_lock":    False,
        "no_bet":           conf < 60.0,
        "elite_player":     False,
        "deep_dive":        False,
        "source":           _SOURCE_TAG,
        "model_version":    "ufc.espn.v1.record",
        "bookmaker":        "Fair Odds (Model)",
        "created_at":       datetime.now(timezone.utc).isoformat(),
        "is_extra":         True,
        "fair_odds_model":  True,
        "sport_key":        pe.sport_key,
        "espn_event_id":    pe.event_id,
        "home_meta":        {"logo": pe.home.get("logo"), "record": pe.home.get("record")},
        "away_meta":        {"logo": pe.away.get("logo"), "record": pe.away.get("record")},
        "factors": {
            "Coverage Source": (
                "ESPN fight card — DraftKings hasn't posted markets yet. "
                "Model-only fair-odds based on career records."
            ),
            "Career Records": (
                f"{pe.home['name']}: {pe.home.get('record') or 'n/a'}  |  "
                f"{pe.away['name']}: {pe.away.get('record') or 'n/a'}"
            ),
            "Record Confidence": (
                f"{sel} at {conf}% — {round(hr*100)}%W vs {round(ar*100)}%W "
                f"({hw+hl+hd} vs {aw+al+ad} fights)."
            ),
        },
    }


async def sync_ufc_espn_picks(db, days_ahead: int = 21) -> dict:
    """Fetch UFC/PFL/Bellator cards → build picks → upsert. UFC schedule
    typically posts fights 3-4 weeks out so we window 3 weeks."""
    started = datetime.now(timezone.utc)
    events = await fetch_slate_multi(UFC_SLUGS, days_ahead=days_ahead,
                                     include_draw=False)

    picks: list[dict] = []
    for pe in events:
        p = _build_ufc_pick(pe)
        if p:
            picks.append(p)

    upserts = 0
    skipped_existing = 0
    for doc in picks:
        existing = await db.picks.find_one({
            "sport": "UFC",
            "event_time": doc["event_time"],
            "selection": doc["selection"],
            "source": {"$ne": _SOURCE_TAG},
        }, {"_id": 1})
        if existing:
            skipped_existing += 1
            continue
        await db.picks.update_one(
            {"id": doc["id"]},
            {"$set": doc, "$setOnInsert": {"first_seen": doc["created_at"]}},
            upsert=True,
        )
        upserts += 1

    finished = datetime.now(timezone.utc)
    summary = {
        "started_at":       started.isoformat(),
        "finished_at":      finished.isoformat(),
        "elapsed_ms":       int((finished - started).total_seconds() * 1000),
        "fixtures_seen":    len(events),
        "picks_generated":  len(picks),
        "upserts":          upserts,
        "skipped_existing": skipped_existing,
    }
    logger.info("UFC ESPN sync done: %s", summary)
    return summary


async def ufc_espn_loop(db) -> None:
    """Runs every 60 min. UFC schedules change slowly; hourly refresh
    is enough for card confirmations + late fight scratches."""
    await asyncio.sleep(60)
    while True:
        try:
            await sync_ufc_espn_picks(db, days_ahead=21)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("UFC ESPN loop error: %s", e)
        await asyncio.sleep(60 * 60)
