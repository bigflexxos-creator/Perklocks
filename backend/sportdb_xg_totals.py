"""SportDB xG enrichment for soccer totals (Over/Under) picks.

Why this exists:
    The Odds API gives us a totals market line (e.g. Over 2.5 @ -120) but
    no underlying signal — we just compute a model probability vs the
    market price and call it a day. SportDB exposes per-match `Expected
    goals (xG)` for every team in their database. xG is a vastly better
    predictor of future goal-scoring than raw goal totals because it
    smooths variance from finishing luck.

Pipeline:
    1. For each soccer team in a totals pick, pull last N completed matches.
    2. From each match, extract:
        - team xG (offence proxy)
        - opponent xG against (defence proxy)
    3. Compute team_xg_pm = avg(xG_for) and team_xga_pm = avg(xG_against)
       over the rolling window.
    4. Expected event total = (home_xG_pm + away_xGA_pm)/2 +
                              (away_xG_pm + home_xGA_pm)/2
       — symmetric blend of attack vs defence.
    5. Adjust the existing pick's `win_probability` toward what xG implies:
        - If xG model agrees with the model's pick direction → boost lock_score
        - If xG model disagrees → temper lock_score and add a warning insight
    6. Always add a `sportdb_signal` field surfacing the xG numbers so the
       user can SEE the data behind the boost/temper.

Costs:
    Per team: 1 results-page call + N (=5) match-stats calls.
    Per match: ~12 SportDB credits worst-case, but cached for 12h so the
    second time a team is touched, it's free.

Implementation note:
    This MUTATES the pick in-place (adds keys + adjusts lock_score). It does
    NOT change `selection`, `market`, `book_odds`, or any user-facing pick
    identity. The pick remains the same bet, just better-calibrated.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("lockscore.sportdb_xg")

SPORTDB_KEY = os.getenv("SPORTDB_API_KEY")
SPORTDB_BASE = "https://api.sportdb.dev/api/flashscore"
_TIMEOUT = httpx.Timeout(12.0, connect=6.0)
_SEM = asyncio.Semaphore(2)
_REQUEST_DELAY = 0.6

# Daily budget — share global cap with sportdb_player_scorer.
_DAILY_LIMIT = 250
_USAGE = {"date": None, "used": 0}

# Rolling window for xG aggregation. 5 matches balances signal vs noise.
_XG_WINDOW = 5
# Cache TTL: team xG profile updates after each match — 12h is safe.
_TEAM_XG_TTL = timedelta(hours=12)
# Match stats cache TTL: matches are immutable once finished — 30 days.
_MATCH_STATS_TTL = timedelta(days=30)


def _budget_ok() -> bool:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _USAGE["date"] != today:
        _USAGE["date"] = today
        _USAGE["used"] = 0
    return _USAGE["used"] < _DAILY_LIMIT


def _budget_inc():
    _USAGE["used"] += 1


# ─────────────────────── HTTP ───────────────────────


async def _get(path: str) -> Optional[Any]:
    if not SPORTDB_KEY or not _budget_ok():
        return None
    url = path if path.startswith("http") else f"{SPORTDB_BASE}{path}"
    headers = {"X-API-Key": SPORTDB_KEY}
    async with _SEM:
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, headers=headers) as cx:
                r = await cx.get(url)
                _budget_inc()
                if r.status_code == 429:
                    await asyncio.sleep(2.0)
                    r = await cx.get(url)
                    _budget_inc()
                if r.status_code != 200:
                    return None
                await asyncio.sleep(_REQUEST_DELAY)
                return r.json()
        except Exception as e:
            logger.warning("SportDB xG %s failed: %s", path, e)
            return None


# ─────────────────────── Cache ───────────────────────


async def _cache_get(db, key: str, ttl: timedelta) -> Optional[Any]:
    if db is None:
        return None
    doc = await db.sportdb_xg_cache.find_one({"_id": key})
    if not doc:
        return None
    try:
        ts = datetime.fromisoformat(doc.get("fetched_at", ""))
    except Exception:
        return None
    if datetime.now(timezone.utc) - ts > ttl:
        return None
    return doc.get("data")


async def _cache_set(db, key: str, data: Any):
    if db is None or data is None:
        return
    await db.sportdb_xg_cache.update_one(
        {"_id": key},
        {"$set": {"data": data, "fetched_at": datetime.now(timezone.utc).isoformat()}},
        upsert=True,
    )


# ─────────────────────── xG extraction ───────────────────────


def _extract_xg(match_stats_payload: Any, home_id: str, away_id: str) -> Optional[tuple[float, float]]:
    """Parse SportDB /match/{id}/stats payload, return (home_xG, away_xG).
    Returns None if xG field isn't present (older matches or some leagues
    don't track it).
    """
    if not isinstance(match_stats_payload, list):
        return None
    for period in match_stats_payload:
        if (period.get("period") or "").lower() != "match":
            continue
        for stat in (period.get("stats") or []):
            sname = (stat.get("statName") or "").lower()
            if "expected goals" in sname or sname == "xg":
                try:
                    home_v = float(stat.get("homeValue"))
                    away_v = float(stat.get("awayValue"))
                    return (home_v, away_v)
                except (TypeError, ValueError):
                    return None
    return None


async def get_team_xg_profile(db, country: str, comp: str, season: str,
                              team_id: str, team_slug: str) -> Optional[dict]:
    """Build a team's recent xG profile.

    Returns dict {xg_for_pm, xg_against_pm, sample_size, matches_used}
    or None if no xG data is recoverable.
    """
    if not team_id:
        return None
    cache_key = f"team_xg:{team_id}"
    cached = await _cache_get(db, cache_key, _TEAM_XG_TTL)
    if cached is not None:
        return cached
    # 1. Pull last results page for this league
    results = await _get(f"/football/{country}/{comp}/{season}/results?page=1")
    if not isinstance(results, list):
        return None
    # Filter to matches where THIS team played + completed
    team_events = []
    for e in results:
        if e.get("eventStage") != "FINISHED":
            continue
        home_pid = e.get("homeEventParticipantId") or ""
        away_pid = e.get("awayEventParticipantId") or ""
        if team_id not in (home_pid, away_pid, e.get("homeParticipantIds"), e.get("awayParticipantIds")):
            # Loose match: participantIds is the more stable field
            if team_id != (e.get("homeParticipantIds") or "") and team_id != (e.get("awayParticipantIds") or ""):
                continue
        team_events.append(e)
    team_events = team_events[: _XG_WINDOW]
    if not team_events:
        return None

    xg_for_sum = 0.0
    xg_against_sum = 0.0
    sample = 0
    matches_used = []
    for ev in team_events:
        mid = ev.get("eventId")
        if not mid:
            continue
        # match stats are immutable — long cache
        ms_cache_key = f"match_stats:{mid}"
        ms = await _cache_get(db, ms_cache_key, _MATCH_STATS_TTL)
        if ms is None:
            ms = await _get(f"/match/{mid}/stats")
            if ms is not None:
                await _cache_set(db, ms_cache_key, ms)
        if not ms:
            continue
        home_pid = ev.get("homeParticipantIds") or ev.get("homeEventParticipantId") or ""
        away_pid = ev.get("awayParticipantIds") or ev.get("awayEventParticipantId") or ""
        xg = _extract_xg(ms, home_pid, away_pid)
        if not xg:
            continue
        h_xg, a_xg = xg
        if team_id == home_pid:
            xg_for_sum += h_xg
            xg_against_sum += a_xg
        elif team_id == away_pid:
            xg_for_sum += a_xg
            xg_against_sum += h_xg
        else:
            continue
        sample += 1
        matches_used.append({
            "match_id": mid,
            "home": ev.get("homeName"),
            "away": ev.get("awayName"),
            "score": f"{ev.get('homeScore')}-{ev.get('awayScore')}",
            "home_xg": h_xg, "away_xg": a_xg,
        })
    if sample == 0:
        return None
    profile = {
        "xg_for_pm": round(xg_for_sum / sample, 3),
        "xg_against_pm": round(xg_against_sum / sample, 3),
        "sample_size": sample,
        "matches_used": matches_used,
    }
    await _cache_set(db, cache_key, profile)
    return profile


# ─────────────────────── Pick adjustment ───────────────────────


def _is_totals_pick(pick: dict) -> bool:
    m = (pick.get("market") or "").lower()
    s = (pick.get("selection") or "").lower()
    return "total" in m or s.startswith("over") or s.startswith("under")


def _is_over(pick: dict) -> bool:
    s = (pick.get("selection") or "").lower()
    return s.startswith("over")


def _extract_line(pick: dict) -> Optional[float]:
    """Pull the totals line from `selection` field — e.g. 'Over 2.5'."""
    s = pick.get("selection") or ""
    for tok in s.split():
        try:
            return float(tok)
        except (ValueError, TypeError):
            continue
    return pick.get("line") if isinstance(pick.get("line"), (int, float)) else None


async def enrich_totals_pick_with_xg(
    db,
    pick: dict,
    sport_key: str,
    home_team: str,
    away_team: str,
) -> dict:
    """Add xG signal to a soccer totals pick. Mutates `pick` in place AND
    returns it for convenience.

    Adjusts `lock_score` and `lock_score_v2` by ±3 points depending on
    xG agreement, capped to [55, 95] so we never make a model-only signal
    override the underlying market math.
    """
    if not _is_totals_pick(pick):
        return pick
    try:
        from sportdb_player_scorer import LEAGUE_MAP, _resolve_team_id  # type: ignore
    except Exception:
        return pick
    if sport_key not in LEAGUE_MAP:
        return pick
    country, comp, season = LEAGUE_MAP[sport_key]
    home_res = await _resolve_team_id(db, country, comp, season, home_team)
    away_res = await _resolve_team_id(db, country, comp, season, away_team)
    if not home_res or not away_res:
        return pick
    home_profile = await get_team_xg_profile(db, country, comp, season, *home_res)
    away_profile = await get_team_xg_profile(db, country, comp, season, *away_res)
    if not home_profile or not away_profile:
        return pick

    # Predicted match total = blend of home offence vs away defence and
    # vice versa. This is the standard symmetric xG forecast.
    pred_total = (
        (home_profile["xg_for_pm"] + away_profile["xg_against_pm"]) / 2
        + (away_profile["xg_for_pm"] + home_profile["xg_against_pm"]) / 2
    )
    line = _extract_line(pick)
    if line is None:
        # Without a line we can still surface the xG insight but can't
        # adjust direction-aware lock score.
        pick.setdefault("key_insights", []).append(
            f"📊 SportDB xG: {home_team} {home_profile['xg_for_pm']} for / "
            f"{home_profile['xg_against_pm']} against per match · "
            f"{away_team} {away_profile['xg_for_pm']} / {away_profile['xg_against_pm']} "
            f"(rolling {home_profile['sample_size']}-match window)."
        )
        pick["sportdb_xg_predicted_total"] = round(pred_total, 2)
        return pick

    is_over = _is_over(pick)
    # Agreement = pick direction matches xG prediction
    delta = pred_total - line  # >0 = xG suggests Over
    if (is_over and delta > 0.25) or (not is_over and delta < -0.25):
        # Strong agreement — boost lock by min(|delta|, 0.6) * 5  (max +3)
        boost = min(abs(delta), 0.6) * 5
        agree = True
    elif (is_over and delta < -0.25) or (not is_over and delta > 0.25):
        # Disagreement — temper lock
        boost = -min(abs(delta), 0.6) * 5
        agree = False
    else:
        boost = 0.0
        agree = None

    if boost != 0:
        for k in ("lock_score", "lock_score_v2"):
            v = pick.get(k)
            if isinstance(v, (int, float)):
                pick[k] = float(max(55.0, min(v + boost, 95.0)))

    insight = (
        f"📊 SportDB xG ({home_profile['sample_size']}-match avg): predicted total "
        f"{pred_total:.2f} vs line {line:.1f}. "
    )
    if agree is True:
        insight += f"xG model AGREES with {'Over' if is_over else 'Under'} pick → lock +{boost:.1f}."
    elif agree is False:
        insight += f"⚠️ xG model DISAGREES with {'Over' if is_over else 'Under'} pick → lock {boost:.1f}."
    else:
        insight += "xG model is NEUTRAL on this line."

    pick.setdefault("key_insights", []).insert(0, insight)
    pick["sportdb_xg_predicted_total"] = round(pred_total, 2)
    pick["sportdb_xg_lock_adjustment"] = round(boost, 2)
    pick["sportdb_signal"] = (
        f"xG: {home_team} {home_profile['xg_for_pm']:.2f} for / "
        f"{home_profile['xg_against_pm']:.2f} against · "
        f"{away_team} {away_profile['xg_for_pm']:.2f} for / "
        f"{away_profile['xg_against_pm']:.2f} against per match. "
        f"Predicted total {pred_total:.2f} vs line {line:.1f}."
    )
    return pick
