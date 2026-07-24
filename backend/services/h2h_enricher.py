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


# ── Team-level H2H from dedicated game/match collections ─────────────────
async def _team_h2h_from_settled(db, sport: str, home: str, away: str,
                                 limit: int = 10) -> Optional[dict]:
    """Aggregate historical meetings between the two teams.

    Priority sources (all cheap DB scans):
      • MLB / NFL / NBA / NHL → `games` collection (schema: `home`, `away`,
        `date`, `result: {home:int, away:int}`, `status: 'Final'`).
      • Soccer                → `soccer_matches` collection (schema:
        `home_team`, `away_team`, `date`, `home_score`, `away_score`).
      • Fallback              → scan our own `picks` collection for rows
        where `final_score` is a team-keyed dict (`{'HomeTeam': '3',
        'AwayTeam': '2'}` — Soccer team-level picks). Player-prop
        dicts (`{'<Player> Strikeouts': 7.0}`) are ignored — those
        don't give us team-level scores.
    """
    if not (home and away):
        return None
    # Team-level H2H (aggregated score totals) doesn't make sense for
    # Tennis — set scores like "6-4, 7-5" get summed into meaningless
    # 22-24-style totals. Tennis has proper player-vs-player H2H in
    # `_tennis_player_h2h`, so return None here and let that path win.
    if sport == "Tennis":
        return None

    meetings: list[dict] = []
    home_l = home.strip().lower()
    away_l = away.strip().lower()
    src_label: Optional[str] = None  # audit trail — which collection served the data

    # 1) MLB / NFL / NBA / NHL — `games` collection
    if sport in {"MLB", "NFL", "NBA", "NHL"}:
        try:
            games_coll = db.games
            q = {
                "sport": sport.lower(),
                "status": {"$in": ["Final", "final", "FT", "Completed"]},
                "$or": [
                    {"home": {"$regex": f"^{re.escape(home)}$", "$options": "i"},
                     "away": {"$regex": f"^{re.escape(away)}$", "$options": "i"}},
                    {"home": {"$regex": f"^{re.escape(away)}$", "$options": "i"},
                     "away": {"$regex": f"^{re.escape(home)}$", "$options": "i"}},
                ],
            }
            cur = games_coll.find(q, {
                "_id": 0, "home": 1, "away": 1, "date": 1,
                "result": 1, "venue": 1,
            }).sort("date", -1).limit(limit)
            for g in await cur.to_list(length=limit):
                res = g.get("result") or {}
                h_score = res.get("home")
                a_score = res.get("away")
                if h_score is None or a_score is None:
                    continue
                g_home = str(g.get("home") or "")
                is_flipped = g_home.strip().lower() == away_l
                meetings.append({
                    "date": str(g.get("date") or "")[:10],
                    "score": f"{h_score}-{a_score}",
                    "home_team_score": int(a_score) if is_flipped else int(h_score),
                    "away_team_score": int(h_score) if is_flipped else int(a_score),
                    "venue": g.get("venue") or "",
                })
            if meetings:
                src_label = "games"
        except Exception as e:
            logger.debug("games coll scan failed: %s", e)

    # 2) Soccer — `soccer_matches` collection
    if sport == "Soccer" and not meetings:
        try:
            sm = db.soccer_matches
            q = {
                "status": {"$in": ["finished", "Finished", "FT", "Completed"]},
                "$or": [
                    {"home_team": {"$regex": f"^{re.escape(home)}$", "$options": "i"},
                     "away_team": {"$regex": f"^{re.escape(away)}$", "$options": "i"}},
                    {"home_team": {"$regex": f"^{re.escape(away)}$", "$options": "i"},
                     "away_team": {"$regex": f"^{re.escape(home)}$", "$options": "i"}},
                ],
            }
            cur = sm.find(q, {
                "_id": 0, "home_team": 1, "away_team": 1, "date": 1,
                "home_score": 1, "away_score": 1, "league": 1,
            }).sort("date", -1).limit(limit)
            for m in await cur.to_list(length=limit):
                h_score = m.get("home_score")
                a_score = m.get("away_score")
                if h_score is None or a_score is None:
                    continue
                is_flipped = str(m.get("home_team") or "").strip().lower() == away_l
                meetings.append({
                    "date": str(m.get("date") or "")[:10],
                    "score": f"{h_score}-{a_score}",
                    "home_team_score": int(a_score) if is_flipped else int(h_score),
                    "away_team_score": int(h_score) if is_flipped else int(a_score),
                    "venue": m.get("league") or "",
                })
            if meetings:
                src_label = "soccer_matches"
        except Exception as e:
            logger.debug("soccer_matches scan failed: %s", e)

    # 3) Fallback — settled picks with team-keyed final_score dict
    if not meetings:
        try:
            home_re = re.escape(home)
            away_re = re.escape(away)
            q = {
                "sport": sport,
                "status": {"$in": ["won", "lost", "push"]},
                "final_score": {"$type": "object"},
                "$or": [
                    {"event": {"$regex": f"^{home_re}\\s*@\\s*{away_re}$", "$options": "i"}},
                    {"event": {"$regex": f"^{away_re}\\s*@\\s*{home_re}$", "$options": "i"}},
                ],
            }
            cur = db.picks.find(q, {
                "_id": 0, "event": 1, "event_time": 1, "final_score": 1,
                "home_team": 1, "away_team": 1,
            }).sort("event_time", -1).limit(limit * 4)
            seen: set = set()
            for r in await cur.to_list(length=limit * 4):
                key = str(r.get("event_time") or "")[:10]
                if not key or key in seen:
                    continue
                fs = r.get("final_score") or {}
                if not isinstance(fs, dict):
                    continue
                # Only keep team-keyed dicts (both keys match team names).
                # Case-insensitive.
                keys_l = {str(k).strip().lower(): k for k in fs.keys()}
                if home_l in keys_l and away_l in keys_l:
                    try:
                        h_score = int(fs[keys_l[home_l]])
                        a_score = int(fs[keys_l[away_l]])
                    except (TypeError, ValueError):
                        continue
                    seen.add(key)
                    meetings.append({
                        "date": key,
                        "score": f"{h_score}-{a_score}",
                        "home_team_score": h_score,
                        "away_team_score": a_score,
                        "venue": r.get("event") or "",
                    })
                if len(meetings) >= limit:
                    break
            if meetings:
                src_label = "settled_picks_db"
        except Exception as e:
            logger.debug("h2h picks fallback failed: %s", e)

    if not meetings:
        return None

    # Aggregate
    home_wins = sum(1 for m in meetings if m["home_team_score"] > m["away_team_score"])
    away_wins = sum(1 for m in meetings if m["away_team_score"] > m["home_team_score"])
    totals = [m["home_team_score"] + m["away_team_score"] for m in meetings]
    recent = [{
        "date": m["date"],
        "score": m["score"],
        "winner": (home if m["home_team_score"] > m["away_team_score"]
                   else (away if m["away_team_score"] > m["home_team_score"] else "Draw")),
        "venue": m.get("venue") or "",
    } for m in meetings[:5]]

    return {
        "meetings": len(meetings),
        "record": f"{home_wins}-{away_wins}",
        "home_wins": home_wins,
        "away_wins": away_wins,
        "avg_total": round(sum(totals) / len(totals), 2) if totals else None,
        "last_meeting": recent[0] if recent else None,
        "recent": recent,
        "source": src_label,
    }


# ── Sport-specific player H2H (delegates to existing modules) ─────────────
async def _mlb_player_h2h(pick: dict) -> Optional[dict]:
    """MLB player-vs-team H2H, split by prop family:

    • Pitcher props (K / outs / walks / earned runs / hits allowed)
      → `mlb_pitcher_h2h.fetch_pitcher_h2h`, keyed off the pitcher's
        team abbrev in the market string.
    • Batter props (hits / HR / RBI / total bases / runs scored /
      singles / doubles / triples / stolen bases / at bats)
      → `mlb_batter_h2h.fetch_batter_h2h`. Same market-string parse
        (Player name in parens with team abbreviation) — we treat the
        parens team as the batter's team and derive the opponent from
        `pick.event`. `sample_size` becomes the batter's at-bats vs
        that opponent so the compact chip reads "3-for-12 vs KC (25%)"
        instead of the meaningless team-meetings count.
    """
    market_raw = pick.get("market") or ""
    market = market_raw.lower()
    # Parse "Firstname Lastname (KC) …" — same regex on both paths.
    import re as _re
    try:
        from mlb_pitcher_h2h import resolve_opp_team_name
    except Exception:
        return None
    m = _re.match(r"^\s*(.*?)\s*\(([A-Z]{2,4})\)\s+", market_raw)
    if not m:
        return None
    name = m.group(1).strip()
    abbr = m.group(2).strip()
    opp = resolve_opp_team_name(pick.get("event") or "", abbr)
    if not opp:
        return None

    # ── Pitcher branch ────────────────────────────────────────────
    if any(k in market for k in (
        "strikeout", "strikeouts", "outs recorded", "pitching outs",
        "walks", "walks recorded", "walks allowed",
        "earned runs", "hits allowed",
    )):
        try:
            from mlb_pitcher_h2h import fetch_pitcher_h2h
        except Exception:
            return None
        try:
            data = await fetch_pitcher_h2h(name, opp)
        except Exception as e:
            logger.debug("MLB pitcher H2H failed: %s", e)
            return None
        if not data or not data.get("ok"):
            return None
        starts = int(data.get("vs_team_starts") or 0)
        avg_k = data.get("vs_team_avg_k") or 0.0
        return {
            "player": name,
            "vs_opponent": opp,
            "sample_size": starts,
            "sample_unit": "starts",
            "primary_stat": "avg_k",
            "primary_value": float(avg_k),
            "primary_value_display": (
                f"{avg_k:.1f} K / start vs {opp}" if starts
                else "No prior starts"
            ),
            "season_avg_k": data.get("season_avg_k"),
            "season_starts": data.get("season_starts"),
            "recent": data.get("vs_team_recent") or [],
            "l5": data.get("last5"),
        }

    # ── Batter branch ─────────────────────────────────────────────
    if any(k in market for k in (
        "hits", "home run", "homer", "total bases", "rbi",
        "runs scored", "singles", "doubles", "triples",
        "stolen base", "at bats",
    )):
        try:
            from mlb_batter_h2h import fetch_batter_h2h
        except Exception:
            return None
        try:
            data = await fetch_batter_h2h(name, opp)
        except Exception as e:
            logger.debug("MLB batter H2H failed: %s", e)
            return None
        if not data or not data.get("ok"):
            return None
        vs_ab = int(data.get("vs_team_ab") or 0)
        vs_h = int(data.get("vs_team_hits") or 0)
        vs_avg = float(data.get("vs_team_avg") or 0.0)
        vs_games = int(data.get("vs_team_games") or 0)
        if vs_ab == 0:
            display = f"No prior at-bats vs {opp}"
        else:
            pct = int(round(vs_avg * 100))
            display = f"{vs_h}-for-{vs_ab} vs {opp} ({vs_avg:.3f} avg, {pct}%)"
        return {
            "player": name,
            "vs_opponent": opp,
            "sample_size": vs_ab,           # <-- at-bats, NOT team meetings
            "sample_unit": "AB",
            "primary_stat": "vs_team_avg",
            "primary_value": vs_avg,
            "primary_value_display": display,
            "season_avg": data.get("season_avg"),
            "season_ab": data.get("season_ab"),
            "season_hits": data.get("season_hits"),
            "season_games": data.get("season_games"),
            "vs_team_games": vs_games,
            "vs_team_hr": data.get("vs_team_hr"),
            "vs_team_rbi": data.get("vs_team_rbi"),
            "recent": data.get("vs_team_recent") or [],
        }

    return None


async def _tennis_player_h2h(db, pick: dict) -> Optional[dict]:
    """Tennis A-vs-B career H2H (surface-agnostic)."""
    sel = (pick.get("selection") or "").strip()
    event = (pick.get("event") or "").strip()
    # Event format is typically "Player A @ Player B" in our DB (some
    # older imports use "vs" / "v"). Split on ANY of them.
    parts = re.split(r"\s+(?:vs\.?|v\.?|@)\s+", event, maxsplit=1, flags=re.IGNORECASE)
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


def _avg_unit(sport: str) -> str:
    """Unit label for team H2H `avg_total` — MLB is runs, Soccer is goals,
    US-team sports are points. Prevents the compact chip from looking like
    it's mixing player-stat units with team-total units (user report:
    '7.11 avg' looked like strikeouts but it was runs).
    """
    if sport == "MLB":
        return "runs"
    if sport == "Soccer":
        return "goals"
    if sport in {"NBA", "NFL", "NHL"}:
        return "pts"
    return ""


def _is_player_prop_market(market: str) -> bool:
    """True if the pick is a player-specific prop (hits, HRs, strikeouts,
    goals, assists, receiving yards, points, rebounds, etc.) as opposed to
    a team/game bet (moneyline, spread, total).

    Team-total `avg_total` (avg runs / avg goals) is meaningful for game
    totals and moneylines but IRRELEVANT on a batter's hit prop or a
    goalscorer prop (user report: "7.11 avg shouldn't be on player hit
    cards don't make sense, should only be on total bets"). We use this
    classifier to strip the avg from the chip on player props.
    """
    if not market:
        return False
    ml = market.lower()
    # Player-prop keywords across all sports we support.
    for kw in (
        # MLB batter
        "hits", "home run", "homer", "total bases", "rbi", "runs scored",
        "singles", "doubles", "triples", "stolen base", "at bats",
        # MLB pitcher
        "strikeout", "strikeouts", "outs recorded", "pitching outs",
        "walks", "walks recorded", "walks allowed", "earned runs",
        "hits allowed", "pitches thrown",
        # Soccer
        "anytime goal scorer", "anytime scorer", "first goal scorer",
        "last goal scorer", "anytime assist", "to score or assist",
        "shots on target", "player shots", "player passes", "player tackles",
        "player cards", "to be booked", "to be carded",
        # Tennis
        "aces", "double faults", "player games", "sets won",
        # NBA player
        "points", "rebounds", "assists", "3-pointers", "three-pointers",
        "steals", "blocks", "double-double", "triple-double",
        "player rebounds", "player assists",
        # NFL player
        "passing yards", "rushing yards", "receiving yards", "receptions",
        "passing touchdown", "rushing touchdown", "receiving touchdown",
        "anytime touchdown", "first touchdown", "player interceptions",
    ):
        if kw in ml:
            return True
    return False


def _build_summary(sport: str, team_h2h: Optional[dict],
                   player_h2h: Optional[dict],
                   pick_market: str = "") -> str:
    """Compact one-liner for the LockPickCard chip. Keep it short (<48 chars).

    Rules:
    - Player H2H is shown ONLY when we have at least 1 real prior sample
      (sample_size > 0). "No prior meetings" is filtered out of the chip
      — it belongs on the deep-dive card, not the compact chip.
    - Team H2H is shown ONLY when at least one side has a win recorded
      (avoids the useless "H2H 0-0 L1" chip when we only have a scheduled
      meeting but no settled final score yet).
    - Team `avg` is labelled with its unit (runs / goals / pts) so the
      user can tell it apart from player-stat units at a glance.
    - Team `avg` is SUPPRESSED on player-prop markets (hits, HRs, K's,
      goals, assists, receiving yards, etc.) — the number is average
      game-total scoring and has no bearing on a player's individual
      stat, which was confusing users. Kept for totals / moneyline / spread.
    """
    bits: list[str] = []
    is_player = _is_player_prop_market(pick_market)
    if player_h2h and (player_h2h.get("sample_size") or 0) > 0:
        disp = str(player_h2h.get("primary_value_display") or "")
        if disp and "No prior" not in disp:
            bits.append(disp)
    if team_h2h:
        hw = int(team_h2h.get("home_wins") or 0)
        aw = int(team_h2h.get("away_wins") or 0)
        # On PLAYER-prop picks the team meetings count (L6, L10, etc.) is
        # confusing — users read the "L6" as a player at-bat sample count
        # (user report: "Make sure L3 represents At bats"). Suppress the
        # team-meeting bit entirely on player-prop chips; keep it on team
        # bets (moneyline / spread / total) where it's the primary signal.
        if hw + aw > 0 and not is_player:
            rec = team_h2h.get("record") or ""
            avg = team_h2h.get("avg_total")
            unit = _avg_unit(sport)
            avg_s = ""
            if avg is not None:
                avg_s = f" · {avg} avg {unit}".rstrip()
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

    # ── Fallback: parse home/away from `event` when the top-level
    # fields are null (common for Soccer/Tennis/UFC picks). Event
    # format is "Home @ Away" everywhere in the app. ──
    if not (home and away):
        event = (pick.get("event") or "").strip()
        if event:
            parts = re.split(r"\s+(?:vs\.?|v\.?|@)\s+", event, maxsplit=1,
                             flags=re.IGNORECASE)
            if len(parts) == 2:
                if not home:
                    home = parts[0].strip()
                if not away:
                    away = parts[1].strip()

    sources: list[str] = []

    # Team-level H2H — works for every sport that has final scores logged.
    team_h2h = await _team_h2h_from_settled(db, sport, home, away, limit=10)
    if team_h2h:
        # Use the specific collection label so the audit trail (`sources`)
        # accurately reflects where the H2H data actually came from.
        sources.append(team_h2h.get("source") or "settled_picks_db")

    # Player-level H2H — sport-specific.
    player_h2h: Optional[dict] = None
    try:
        if sport == "MLB":
            # MLB pitcher + batter H2H both hit the external MLB Stats
            # API but response times are ~200ms and the module maintains
            # a 12h in-process cache, so it's cheap enough to run in
            # fast_mode too. That gives us the batter's "X-for-Y vs OPP"
            # chip on /picks/today, not just on the deep-dive screen
            # (user report: "Make sure L3 represents At bats should also
            # h2h at bat against team").
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
        "summary": _build_summary(sport, team_h2h, player_h2h,
                                   pick_market=pick.get("market") or ""),
        "team_h2h": team_h2h,
        "player_h2h": player_h2h,
        "situational": situational,
        "sources": sources,
        # is_player_prop tells the frontend whether the team's `avg_total`
        # cell should be shown in the deep-dive team card — same rationale
        # as the compact chip: avg game score is irrelevant on a player prop.
        "is_player_prop": _is_player_prop_market(pick.get("market") or ""),
    }
    _cache_put(ck, bundle)
    return bundle


__all__ = ["build_h2h_bundle"]
