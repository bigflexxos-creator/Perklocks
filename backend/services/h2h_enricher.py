"""Unified Head-to-Head (H2H) enrichment service.

Purpose
-------
Given a pick document, return a normalised H2H bundle the frontend can render
consistently across sports (MLB, Soccer, Tennis, NFL, NBA).

Bundle shape (stable contract with the frontend)
------------------------------------------------
{
  "ok": bool,
  "sport": "MLB" | "Soccer" | "Tennis" | "NFL" | "NBA",
  "summary": str,                # compact one-liner for the LockPickCard chip
                                  # e.g. "H2H 3-2 L5 · 8.2 avg K"
  "team_h2h": {                  # last N meetings between the two teams,
      "meetings": int,           # sourced from our own settled picks DB
      "record": str,             # e.g. "3-2" (home vs away perspective)
      "home_wins": int,
      "away_wins": int,
      "avg_total": float | None, # avg combined score/goals if available
      "last_meeting": {
          "date": str, "score": str, "venue": str | None,
      } | None,
      "recent": [{"date","score","winner","venue"}]
  } | None,
  "player_h2h": {                # player-specific splits vs the opponent
      "player": str,
      "vs_opponent": str,
      "sample_size": int,        # e.g. career/season starts, meetings
      "primary_stat": str,       # e.g. "avg_k", "avg_goals", "win_pct"
      "primary_value": float,
      "primary_value_display": str,   # "8.2 K/GS" ready to render
      "recent": [{...}]          # last 5 events, keys sport-dependent
  } | None,
  "situational": {               # venue / weather / referee / rest
      "venue": str | None,
      "notes": [str],
  } | None,
  "sources": [str],              # audit trail: which pipelines contributed
}

The service prefers cheap DB lookups (our own settled picks + tennis_matches_
history) and only calls external APIs when the sport-specific enricher deems
it worth the network cost. All external calls are cached by that enricher.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Optional

logger = logging.getLogger("lockscore.h2h")

# ── Local process cache, 6h TTL ───────────────────────────────────────────
_CACHE: dict[str, tuple[float, dict]] = {}
_TTL = 6 * 3600  # 6h


def _cache_key(pick: dict) -> str:
    return "|".join([
        (pick.get("sport") or "").strip().lower(),
        (pick.get("home_team") or "").strip().lower(),
        (pick.get("away_team") or "").strip().lower(),
        (pick.get("selection") or "").strip().lower(),
        (pick.get("market") or "").strip().lower(),
    ])


def _cache_get(k: str) -> Optional[dict]:
    row = _CACHE.get(k)
    if not row:
        return None
    ts, val = row
    if time.time() - ts > _TTL:
        _CACHE.pop(k, None)
        return None
    return val


def _cache_put(k: str, val: dict) -> None:
    _CACHE[k] = (time.time(), val)


# ── Team-level H2H from our own settled picks (works for every sport) ─────
async def _team_h2h_from_settled(db, sport: str, home: str, away: str,
                                 limit: int = 10) -> Optional[dict]:
    """Aggregate our own historical picks (settled outcomes) for the
    home-vs-away pairing. Cheap: just a Mongo aggregate.
    """
    if not (home and away):
        return None
    home_re = re.escape(home)
    away_re = re.escape(away)
    # Match either "home @ away" or "away @ home" event strings.
    q = {
        "sport": sport,
        "status": {"$in": ["won", "lost", "push"]},
        "$or": [
            {"event": {"$regex": f"^{home_re}\\s*@\\s*{away_re}$", "$options": "i"}},
            {"event": {"$regex": f"^{away_re}\\s*@\\s*{home_re}$", "$options": "i"}},
        ],
    }
    projection = {
        "_id": 0, "event": 1, "market": 1, "selection": 1, "status": 1,
        "event_time": 1, "pick_date": 1, "final_score": 1,
        "home_team": 1, "away_team": 1, "settled_at": 1,
    }
    try:
        cur = db.picks.find(q, projection).sort("event_time", -1).limit(limit * 3)
        rows = await cur.to_list(length=limit * 3)
    except Exception as e:
        logger.warning("h2h team lookup failed: %s", e)
        return None
    if not rows:
        return None
    # Group by unique event_time so we get one entry per meeting.
    by_meeting: dict[str, dict] = {}
    for r in rows:
        key = str(r.get("event_time") or r.get("pick_date") or "")
        if not key or key in by_meeting:
            continue
        by_meeting[key] = r
        if len(by_meeting) >= limit:
            break
    meetings = list(by_meeting.values())
    if not meetings:
        return None

    # Parse final_score if present ("3-2", "108-102", "1-1").
    home_wins = 0
    away_wins = 0
    totals: list[float] = []
    recent: list[dict] = []
    for r in meetings:
        fs = str(r.get("final_score") or "").strip()
        # Which side is home in the STORED row?
        row_home = (r.get("home_team") or "").strip()
        row_away = (r.get("away_team") or "").strip()
        # Recognise formats like "3-2", "3 - 2", "3:2"
        m = re.match(r"^\s*(\d+)\s*[-:]\s*(\d+)\s*$", fs)
        winner = None
        score_str = fs or "—"
        if m:
            h, a = int(m.group(1)), int(m.group(2))
            totals.append(h + a)
            if h > a:
                winner = row_home
            elif a > h:
                winner = row_away
            # Attribute the win to OUR home team (function arg), not stored home.
            if winner and winner.strip().lower() == home.strip().lower():
                home_wins += 1
            elif winner and winner.strip().lower() == away.strip().lower():
                away_wins += 1
        recent.append({
            "date": str(r.get("event_time") or r.get("pick_date") or "")[:10],
            "score": score_str,
            "winner": winner or "—",
            "venue": r.get("event") or "",
        })
    last_meeting = recent[0] if recent else None
    return {
        "meetings": len(meetings),
        "record": f"{home_wins}-{away_wins}",
        "home_wins": home_wins,
        "away_wins": away_wins,
        "avg_total": round(sum(totals) / len(totals), 2) if totals else None,
        "last_meeting": last_meeting,
        "recent": recent[:5],
    }


# ── Sport-specific player H2H (delegates to existing modules) ─────────────
async def _mlb_player_h2h(pick: dict) -> Optional[dict]:
    """MLB pitcher-vs-team H2H via mlb_pitcher_h2h (already cached)."""
    market = (pick.get("market") or "").lower()
    # Only strikeout / outs / walks-recorded pitcher props for now.
    if not any(k in market for k in ("strikeout", "outs recorded", "walks", "earned runs")):
        return None
    try:
        from mlb_pitcher_h2h import fetch_pitcher_h2h, resolve_opp_team_name
    except Exception:
        return None
    # Pull pitcher name + team abbreviation from "Firstname Lastname (KC) Over 6.5 Strikeouts"
    m = re.match(r"^\s*(.*?)\s*\(([A-Z]{2,4})\)\s+", pick.get("market") or "")
    if not m:
        return None
    pitcher = m.group(1).strip()
    abbr = m.group(2).strip()
    opp = resolve_opp_team_name(pick.get("event") or "", abbr)
    if not opp:
        return None
    try:
        data = await fetch_pitcher_h2h(pitcher, opp)
    except Exception as e:
        logger.debug("MLB pitcher H2H failed: %s", e)
        return None
    if not data or not data.get("ok"):
        return None
    starts = int(data.get("vs_team_starts") or 0)
    avg_k = data.get("vs_team_avg_k") or 0.0
    return {
        "player": pitcher,
        "vs_opponent": opp,
        "sample_size": starts,
        "primary_stat": "avg_k",
        "primary_value": float(avg_k),
        "primary_value_display": f"{avg_k:.1f} K / start vs {opp}" if starts else "No prior starts",
        "season_avg_k": data.get("season_avg_k"),
        "season_starts": data.get("season_starts"),
        "recent": data.get("vs_team_recent") or [],
        "l5": data.get("last5"),
    }


async def _tennis_player_h2h(db, pick: dict) -> Optional[dict]:
    """Tennis A-vs-B career H2H (surface-agnostic)."""
    sel = (pick.get("selection") or "").strip()
    event = (pick.get("event") or "").strip()
    # Event format: "Player A vs Player B"
    parts = re.split(r"\s+(?:vs\.?|v\.?)\s+", event, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) != 2:
        return None
    a, b = parts[0].strip(), parts[1].strip()
    if not (a and b):
        return None
    opp = b if sel.lower() == a.lower() else a
    if opp.lower() == sel.lower():
        return None
    try:
        from services.tennis.fallback import get_h2h
    except Exception:
        return None
    try:
        h2h = await get_h2h(db, sel, opp)
    except Exception as e:
        logger.debug("Tennis H2H failed: %s", e)
        return None
    a_wins = int(h2h.get("a_wins") or 0)
    b_wins = int(h2h.get("b_wins") or 0)
    total = a_wins + b_wins
    if total == 0:
        return {
            "player": sel,
            "vs_opponent": opp,
            "sample_size": 0,
            "primary_stat": "record",
            "primary_value": 0.0,
            "primary_value_display": f"No prior meetings vs {opp}",
            "recent": [],
        }
    pct = round(a_wins / total * 100.0, 1) if total else 0.0
    return {
        "player": sel,
        "vs_opponent": opp,
        "sample_size": total,
        "primary_stat": "win_pct",
        "primary_value": pct,
        "primary_value_display": f"{a_wins}-{b_wins} vs {opp} ({pct:.0f}%)",
        "recent": [],
    }


async def _soccer_player_h2h(db, pick: dict) -> Optional[dict]:
    """Soccer player's goals/assists vs a specific opponent, from our own
    settled picks history."""
    sel = (pick.get("selection") or "").strip()
    market = (pick.get("market") or "").lower()
    if not sel or not any(k in market for k in ("goal scorer", "assist", "score or assist")):
        return None
    home = (pick.get("home_team") or "").strip()
    away = (pick.get("away_team") or "").strip()
    # Determine opponent from the pick's team (if we have it) or fall back
    # to "either home or away — the one that isn't the player's team".
    team_hint = (pick.get("team") or "").strip()
    if team_hint and home and team_hint.lower() == home.lower():
        opp = away
    elif team_hint and away and team_hint.lower() == away.lower():
        opp = home
    else:
        opp = away or home
    if not opp:
        return None
    home_re = re.escape(home) if home else ".+"
    away_re = re.escape(away) if away else ".+"
    q = {
        "sport": "Soccer",
        "selection": sel,
        "status": {"$in": ["won", "lost"]},
        "$or": [
            {"event": {"$regex": f"^{home_re}\\s*@\\s*{away_re}$", "$options": "i"}},
            {"event": {"$regex": f"^{away_re}\\s*@\\s*{home_re}$", "$options": "i"}},
        ],
    }
    try:
        cur = db.picks.find(
            q, {"_id": 0, "event_time": 1, "market": 1, "status": 1, "event": 1},
        ).sort("event_time", -1).limit(15)
        rows = await cur.to_list(length=15)
    except Exception as e:
        logger.debug("Soccer player H2H DB lookup failed: %s", e)
        return None
    if not rows:
        return None
    hits = sum(1 for r in rows if r.get("status") == "won")
    total = len(rows)
    pct = round(hits / total * 100.0, 1) if total else 0.0
    return {
        "player": sel,
        "vs_opponent": opp,
        "sample_size": total,
        "primary_stat": "hit_rate",
        "primary_value": pct,
        "primary_value_display": f"{hits}/{total} hits vs {opp} ({pct:.0f}%)",
        "recent": [{"date": str(r.get("event_time") or "")[:10],
                     "result": r.get("status") or "—",
                     "event": r.get("event") or ""} for r in rows[:5]],
    }


def _situational(pick: dict) -> Optional[dict]:
    notes: list[str] = []
    venue = pick.get("venue") or pick.get("event_venue")
    weather = pick.get("weather") or {}
    if isinstance(weather, dict):
        temp = weather.get("temp_f") or weather.get("temp")
        wind = weather.get("wind_mph") or weather.get("wind")
        if temp:
            notes.append(f"Temp {temp}°F")
        if wind:
            notes.append(f"Wind {wind} mph")
    referee = pick.get("referee")
    if referee:
        notes.append(f"Ref: {referee}")
    if not (venue or notes):
        return None
    return {"venue": venue, "notes": notes}


def _build_summary(sport: str, team_h2h: Optional[dict],
                   player_h2h: Optional[dict]) -> str:
    """Compact one-liner for the LockPickCard chip. Keep it short (<48 chars).

    Rules:
    - Player H2H is shown ONLY when we have at least 1 real prior sample
      (sample_size > 0). "No prior meetings" is filtered out of the chip
      — it belongs on the deep-dive card, not the compact chip.
    - Team H2H is shown ONLY when at least one side has a win recorded
      (avoids the useless "H2H 0-0 L1" chip when we only have a scheduled
      meeting but no settled final score yet).
    """
    bits: list[str] = []
    if player_h2h and (player_h2h.get("sample_size") or 0) > 0:
        disp = str(player_h2h.get("primary_value_display") or "")
        if disp and "No prior" not in disp:
            bits.append(disp)
    if team_h2h:
        hw = int(team_h2h.get("home_wins") or 0)
        aw = int(team_h2h.get("away_wins") or 0)
        if hw + aw > 0:
            rec = team_h2h.get("record") or ""
            avg = team_h2h.get("avg_total")
            avg_s = f" · {avg} avg" if avg is not None else ""
            bits.append(f"H2H {rec} L{team_h2h['meetings']}{avg_s}")
    return " · ".join([b for b in bits if b]) or ""


async def build_h2h_bundle(db, pick: dict, *, fast_mode: bool = False) -> dict:
    """Main entry — returns the H2H bundle for a single pick.

    Args:
        fast_mode: when True, skip external API calls (MLB Stats, football-
            data) so this is safe to call in tight loops like /picks/today.
            The compact `summary` is still populated from cheap DB lookups.
    """
    if not pick:
        return {"ok": False, "reason": "no_pick"}
    ck = _cache_key(pick)
    if fast_mode:
        ck = ck + "|fast"
    cached = _cache_get(ck)
    if cached is not None:
        return cached

    sport = (pick.get("sport") or "").strip() or "Unknown"
    home = (pick.get("home_team") or "").strip()
    away = (pick.get("away_team") or "").strip()

    sources: list[str] = []

    # Team-level H2H — works for every sport that has final scores logged.
    team_h2h = await _team_h2h_from_settled(db, sport, home, away, limit=10)
    if team_h2h:
        sources.append("settled_picks_db")

    # Player-level H2H — sport-specific.
    player_h2h: Optional[dict] = None
    try:
        if sport == "MLB" and not fast_mode:
            # MLB pitcher H2H hits the external MLB Stats API (~200-800ms
            # per pitcher). Skip in fast_mode; deep-dive endpoint still
            # computes it because it calls without fast_mode.
            player_h2h = await _mlb_player_h2h(pick)
            if player_h2h:
                sources.append("mlb_stats_api")
        elif sport == "Tennis":
            # Tennis H2H hits our own Mongo collection — cheap; keep it on.
            player_h2h = await _tennis_player_h2h(db, pick)
            if player_h2h:
                sources.append("tennis_matches_history")
        elif sport == "Soccer":
            # Soccer player H2H uses our own settled picks DB — cheap.
            player_h2h = await _soccer_player_h2h(db, pick)
            if player_h2h:
                sources.append("settled_picks_db")
        # NFL/NBA — team-level from settled DB is enough for MVP;
        # player-vs-opp splits deferred to a follow-up when we have the data.
    except Exception as e:
        logger.debug("player H2H failed for sport=%s: %s", sport, e)

    situational = _situational(pick)

    bundle = {
        "ok": bool(team_h2h or player_h2h),
        "sport": sport,
        "summary": _build_summary(sport, team_h2h, player_h2h),
        "team_h2h": team_h2h,
        "player_h2h": player_h2h,
        "situational": situational,
        "sources": sources,
    }
    _cache_put(ck, bundle)
    return bundle


__all__ = ["build_h2h_bundle"]
