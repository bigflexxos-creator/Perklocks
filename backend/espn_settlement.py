"""ESPN-based fallback settlement engine.

The Odds API is slow / lacks coverage for tennis (set-by-set scores),
UFC (fight winners), and WNBA/NBA player props (box-score stats). This
module hits ESPN's free public APIs (no auth) to pick up the slack.

Endpoints (all GET, no auth required):
- Tennis ATP:   /apis/site/v2/sports/tennis/atp/scoreboard?dates=YYYYMMDD
- Tennis WTA:   /apis/site/v2/sports/tennis/wta/scoreboard?dates=YYYYMMDD
- UFC:          /apis/site/v2/sports/mma/ufc/scoreboard?dates=YYYYMMDD
- WNBA games:   /apis/site/v2/sports/basketball/wnba/scoreboard?dates=YYYYMMDD
- WNBA boxscr:  /apis/site/v2/sports/basketball/wnba/summary?event={id}
- NBA games:    /apis/site/v2/sports/basketball/nba/scoreboard?dates=YYYYMMDD
- NBA boxscore: /apis/site/v2/sports/basketball/nba/summary?event={id}

ESPN returns each match's:
- Tennis:   linescores per set, winner flag, completed status
- UFC:      competitor.winner boolean per fighter
- WNBA/NBA: per-athlete stats array indexed by `statistics[0].keys`
"""
from __future__ import annotations

import asyncio
import logging
import re
import unicodedata as _ud
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

logger = logging.getLogger("lockscore.espn_settle")

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


# ───────────────────────── Helpers ─────────────────────────

def _strip_accents(s: str) -> str:
    if not s:
        return ""
    return "".join(c for c in _ud.normalize("NFD", s) if _ud.category(c) != "Mn")


def _name_eq(a: str, b: str) -> bool:
    """Case-insensitive accent-stripped name match."""
    return _strip_accents((a or "").strip().lower()) == _strip_accents((b or "").strip().lower())


def _parse_event_teams(event_str: str) -> tuple[Optional[str], Optional[str]]:
    if not event_str or "@" not in event_str:
        return (None, None)
    parts = event_str.split("@", 1)
    return (parts[0].strip(), parts[1].strip())


async def _get(url: str, params: Optional[dict] = None) -> Optional[dict]:
    try:
        async with httpx.AsyncClient(timeout=15) as cx:
            r = await cx.get(url, params=params, headers={"User-Agent": UA, "Accept": "application/json"})
            if r.status_code != 200:
                return None
            return r.json()
    except Exception as e:
        logger.debug("ESPN fetch failed %s: %s", url, e)
        return None


def _extract_line(market: str) -> Optional[float]:
    if not market:
        return None
    m = re.search(r"([+-]?\d+(?:\.\d+)?)", market)
    return float(m.group(1)) if m else None


# ───────────────────────── Tennis ─────────────────────────

async def _fetch_espn_tennis_matches(date_str: str) -> list[dict]:
    """Fetch all ATP+WTA matches for a date. Returns flattened match list."""
    matches: list[dict] = []
    for league in ("atp", "wta"):
        d = await _get(f"{ESPN_BASE}/tennis/{league}/scoreboard",
                       {"dates": date_str.replace("-", "")})
        if not d:
            continue
        for tournament in d.get("events", []):
            for grouping in tournament.get("groupings", []):
                for comp in grouping.get("competitions", []):
                    # Only include completed matches.
                    status = comp.get("status", {}).get("type", {})
                    if not status.get("completed"):
                        continue
                    matches.append(comp)
    return matches


def _tennis_total_games(comp: dict) -> int:
    """Sum games won across all sets, both players (proxy for Total Games)."""
    total = 0
    for c in comp.get("competitors", []):
        for ls in c.get("linescores", []):
            try:
                total += int(ls.get("value") or 0)
            except Exception:
                pass
    return total


def _tennis_pick_outcome(pick: dict, comp: dict) -> Optional[str]:
    market_l = (pick.get("market") or "").lower()
    selection = (pick.get("selection") or "").strip()
    sel_lower = selection.lower()
    competitors = comp.get("competitors", [])
    if len(competitors) < 2:
        return None
    # Identify players & winner.
    winner_name = None
    loser_name = None
    for c in competitors:
        name = (c.get("athlete") or {}).get("displayName") or ""
        if c.get("winner"):
            winner_name = name
        else:
            loser_name = name

    # ── Moneyline / "to win"
    if "moneyline" in market_l:
        if not winner_name:
            return None
        # Selection might be the player name; match accent-insensitively.
        if _name_eq(selection, winner_name):
            return "won"
        if _name_eq(selection, loser_name or ""):
            return "lost"
        return None

    # ── Spread (Player +X.X / -X.X) — straight games-margin compare.
    if "spread" in market_l:
        line = _extract_line(market_l)
        if line is None:
            return None
        # Find the player the pick is on.
        team_score = opp_score = None
        for c in competitors:
            name = (c.get("athlete") or {}).get("displayName") or ""
            games = sum(int(ls.get("value") or 0) for ls in c.get("linescores", []))
            if _name_eq(selection, name):
                team_score = games
            else:
                opp_score = games
        if team_score is None or opp_score is None:
            return None
        margin = team_score - opp_score + line
        if abs(margin) < 0.01:
            return "push"
        return "won" if margin > 0 else "lost"

    # ── Totals (Total Games Over/Under X.X)
    if "total" in market_l or "over" in sel_lower or "under" in sel_lower:
        line = _extract_line(market_l)
        if line is None:
            return None
        total = _tennis_total_games(comp)
        if "over" in sel_lower:
            if total > line:
                return "won"
            if total < line:
                return "lost"
            return "push"
        if "under" in sel_lower:
            if total < line:
                return "won"
            if total > line:
                return "lost"
            return "push"
    return None


def _match_tennis_comp_for_pick(pick: dict, comps: list[dict]) -> Optional[dict]:
    away, home = _parse_event_teams(pick.get("event") or "")
    if not away or not home:
        return None
    for c in comps:
        names = [(cc.get("athlete") or {}).get("displayName", "")
                 for cc in c.get("competitors", [])]
        if len(names) < 2:
            continue
        a_match = any(_name_eq(n, away) for n in names)
        h_match = any(_name_eq(n, home) for n in names)
        if a_match and h_match:
            return c
    return None


async def settle_tennis_via_espn(db) -> dict:
    counts = {"settled": 0, "won": 0, "lost": 0, "push": 0, "skipped": 0, "no_match": 0}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
    picks = await db.picks.find(
        {"sport": "Tennis", "status": {"$in": [None, "pending"]}},
        {"_id": 0},
    ).to_list(length=1000)
    if not picks:
        return counts

    # Group picks by date so we minimise ESPN calls.
    by_date: dict[str, list[dict]] = {}
    for p in picks:
        et = p.get("event_time") or ""
        try:
            dt = datetime.strptime(et, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if dt > cutoff:
                counts["skipped"] += 1
                continue
        except Exception:
            counts["skipped"] += 1
            continue
        by_date.setdefault(dt.strftime("%Y-%m-%d"), []).append(p)

    # Pull ESPN data once per date.
    comps_by_date: dict[str, list[dict]] = {}
    for d in by_date.keys():
        comps_by_date[d] = await _fetch_espn_tennis_matches(d)

    for d, picks_on_d in by_date.items():
        comps = comps_by_date.get(d, [])
        for pick in picks_on_d:
            c = _match_tennis_comp_for_pick(pick, comps)
            if not c:
                # Try the next calendar day in case the match crossed UTC midnight.
                next_d = (datetime.strptime(d, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
                if next_d not in comps_by_date:
                    comps_by_date[next_d] = await _fetch_espn_tennis_matches(next_d)
                c = _match_tennis_comp_for_pick(pick, comps_by_date.get(next_d, []))
            if not c:
                counts["no_match"] += 1
                continue
            outcome = _tennis_pick_outcome(pick, c)
            if not outcome:
                counts["skipped"] += 1
                continue
            await _record_settlement(db, pick, outcome, c, source="espn_tennis")
            counts[outcome] += 1
            counts["settled"] += 1
    logger.info("Tennis ESPN settler: %s", counts)
    return counts


# ───────────────────────── UFC ─────────────────────────

async def _fetch_espn_ufc_fights(date_str: str) -> list[dict]:
    """Fetch all UFC fights for a date."""
    d = await _get(f"{ESPN_BASE}/mma/ufc/scoreboard",
                   {"dates": date_str.replace("-", "")})
    if not d:
        return []
    fights: list[dict] = []
    for ev in d.get("events", []):
        for comp in ev.get("competitions", []):
            status = comp.get("status", {}).get("type", {})
            if not status.get("completed"):
                continue
            fights.append(comp)
        # Some payloads put fights under groupings, not competitions
        for g in ev.get("groupings", []):
            for comp in g.get("competitions", []):
                status = comp.get("status", {}).get("type", {})
                if not status.get("completed"):
                    continue
                fights.append(comp)
    return fights


def _match_ufc_fight_for_pick(pick: dict, fights: list[dict]) -> Optional[dict]:
    away, home = _parse_event_teams(pick.get("event") or "")
    if not away or not home:
        return None
    for f in fights:
        names = [(c.get("athlete") or {}).get("displayName", "")
                 for c in f.get("competitors", [])]
        if any(_name_eq(n, away) for n in names) and any(_name_eq(n, home) for n in names):
            return f
    return None


def _ufc_pick_outcome(pick: dict, fight: dict) -> Optional[str]:
    market_l = (pick.get("market") or "").lower()
    selection = (pick.get("selection") or "").strip()
    winner = None
    for c in fight.get("competitors", []):
        if c.get("winner"):
            winner = (c.get("athlete") or {}).get("displayName") or ""
            break
    # Moneyline
    if "moneyline" in market_l:
        if not winner:
            return "push"  # draw is rare; treat as no-action
        return "won" if _name_eq(selection, winner) else "lost"
    # Method of victory ("wins by KO/TKO", "wins by Submission", "wins by Decision")
    if "wins by" in market_l:
        if not winner:
            return "lost"
        if not _name_eq(selection, winner):
            return "lost"
        # Determine actual method from fight status detail.
        detail = (fight.get("status", {}).get("type", {}).get("detail") or "").lower()
        if "ko" in market_l or "tko" in market_l:
            return "won" if ("ko" in detail or "tko" in detail) else "lost"
        if "submission" in market_l or "sub" in market_l:
            return "won" if ("submission" in detail or "sub" in detail) else "lost"
        if "decision" in market_l:
            return "won" if "decision" in detail else "lost"
    return None


async def settle_ufc_via_espn(db) -> dict:
    counts = {"settled": 0, "won": 0, "lost": 0, "push": 0, "skipped": 0, "no_match": 0}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
    picks = await db.picks.find(
        {"sport": "UFC", "status": {"$in": [None, "pending"]}},
        {"_id": 0},
    ).to_list(length=500)
    if not picks:
        return counts
    by_date: dict[str, list[dict]] = {}
    for p in picks:
        et = p.get("event_time") or ""
        try:
            dt = datetime.strptime(et, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if dt > cutoff:
                counts["skipped"] += 1
                continue
        except Exception:
            counts["skipped"] += 1
            continue
        by_date.setdefault(dt.strftime("%Y-%m-%d"), []).append(p)
    fights_by_date: dict[str, list[dict]] = {}
    for d in by_date.keys():
        fights_by_date[d] = await _fetch_espn_ufc_fights(d)
    for d, picks_on_d in by_date.items():
        fights = fights_by_date.get(d, [])
        for pick in picks_on_d:
            f = _match_ufc_fight_for_pick(pick, fights)
            if not f:
                # Cards often span 2 days UTC, try ±1 day.
                for offset in (-1, 1):
                    alt = (datetime.strptime(d, "%Y-%m-%d") + timedelta(days=offset)).strftime("%Y-%m-%d")
                    if alt not in fights_by_date:
                        fights_by_date[alt] = await _fetch_espn_ufc_fights(alt)
                    f = _match_ufc_fight_for_pick(pick, fights_by_date.get(alt, []))
                    if f:
                        break
            if not f:
                counts["no_match"] += 1
                continue
            outcome = _ufc_pick_outcome(pick, f)
            if not outcome:
                counts["skipped"] += 1
                continue
            await _record_settlement(db, pick, outcome, f, source="espn_ufc")
            counts[outcome] += 1
            counts["settled"] += 1
    logger.info("UFC ESPN settler: %s", counts)
    return counts


# ───────────────────────── WNBA / NBA Player Props ─────────────────────────

# ESPN box-score stat-key → which prop markets it settles.
_STAT_INDEX_NAMES = {
    "points": "points",
    "rebounds": "rebounds",
    "assists": "assists",
}


def _stat_value_for_athlete(boxscore: dict, athlete_name: str, stat_key: str) -> Optional[float]:
    """Find athlete's stat value in the boxscore.players[] array."""
    for team in boxscore.get("players", []):
        for grp in team.get("statistics", []):
            keys = grp.get("keys", [])
            if stat_key not in keys:
                continue
            idx = keys.index(stat_key)
            for a in grp.get("athletes", []):
                name = (a.get("athlete") or {}).get("displayName", "")
                if _name_eq(name, athlete_name):
                    vals = a.get("stats", [])
                    if idx < len(vals):
                        try:
                            return float(vals[idx])
                        except Exception:
                            return None
    return None


async def _fetch_espn_boxscores(league: str, date_str: str) -> dict[str, dict]:
    """Fetch all NBA/WNBA games for a date and pull each game's boxscore.
    Returns { 'away|home': boxscore_dict }."""
    sc = await _get(f"{ESPN_BASE}/basketball/{league}/scoreboard",
                    {"dates": date_str.replace("-", "")})
    if not sc:
        return {}
    out: dict[str, dict] = {}
    for ev in sc.get("events", []):
        if not ev.get("status", {}).get("type", {}).get("completed"):
            continue
        eid = ev.get("id")
        comp = ev.get("competitions", [{}])[0]
        comps = comp.get("competitors", [])
        if len(comps) < 2:
            continue
        team_names = []
        for c in comps:
            tn = c.get("team", {}).get("displayName", "")
            team_names.append(tn)
        # Fetch box.
        summ = await _get(f"{ESPN_BASE}/basketball/{league}/summary",
                          {"event": eid})
        if not summ:
            continue
        # Key by either team-name combination so we can match flexibly.
        key1 = f"{team_names[0]}|{team_names[1]}".lower()
        key2 = f"{team_names[1]}|{team_names[0]}".lower()
        out[key1] = summ.get("boxscore") or {}
        out[key2] = summ.get("boxscore") or {}
    return out


def _player_prop_outcome(pick: dict, boxscore: dict) -> Optional[str]:
    market_l = (pick.get("market") or "").lower()
    selection = (pick.get("selection") or "").strip()
    # Determine the stat (points / rebounds / assists).
    stat_key = None
    for kw, sk in _STAT_INDEX_NAMES.items():
        if kw in market_l:
            stat_key = sk
            break
    if not stat_key:
        return None
    line = _extract_line(market_l)
    if line is None:
        return None
    value = _stat_value_for_athlete(boxscore, selection, stat_key)
    if value is None:
        return None
    side_over = "over" in market_l
    side_under = "under" in market_l
    if side_over:
        if value > line:
            return "won"
        if value < line:
            return "lost"
        return "push"
    if side_under:
        if value < line:
            return "won"
        if value > line:
            return "lost"
        return "push"
    # No explicit over/under → assume Over (default for "alt" markets).
    return "won" if value > line else "lost"


async def settle_player_props_via_espn(db) -> dict:
    counts = {"settled": 0, "won": 0, "lost": 0, "push": 0, "skipped": 0, "no_match": 0}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
    # NBA + WNBA player props.
    picks = await db.picks.find({
        "sport": {"$in": ["NBA", "WNBA"]},
        "status": {"$in": [None, "pending"]},
        "league": {"$regex": "Props"},
    }, {"_id": 0}).to_list(length=2000)
    if not picks:
        return counts

    # Bucket by (league, date).
    by_key: dict[tuple, list[dict]] = {}
    for p in picks:
        et = p.get("event_time") or ""
        try:
            dt = datetime.strptime(et, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if dt > cutoff:
                counts["skipped"] += 1
                continue
        except Exception:
            counts["skipped"] += 1
            continue
        league = "nba" if p.get("sport") == "NBA" else "wnba"
        by_key.setdefault((league, dt.strftime("%Y-%m-%d")), []).append(p)

    boxscores_cache: dict[tuple, dict] = {}
    for (league, d) in by_key.keys():
        boxscores_cache[(league, d)] = await _fetch_espn_boxscores(league, d)

    for (league, d), picks_on_d in by_key.items():
        boxes = boxscores_cache.get((league, d), {})
        for pick in picks_on_d:
            away, home = _parse_event_teams(pick.get("event") or "")
            if not away or not home:
                counts["no_match"] += 1
                continue
            key = f"{away}|{home}".lower()
            box = boxes.get(key)
            # NBA/WNBA late-night games (UTC after midnight) get tagged with
            # the UTC calendar day but ESPN files them under the US-Eastern
            # game day (one day earlier). Fall back to d-1 if no match.
            if not box:
                alt_d = (datetime.strptime(d, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
                if (league, alt_d) not in boxscores_cache:
                    boxscores_cache[(league, alt_d)] = await _fetch_espn_boxscores(league, alt_d)
                box = boxscores_cache.get((league, alt_d), {}).get(key)
            if not box:
                counts["no_match"] += 1
                continue
            outcome = _player_prop_outcome(pick, box)
            if not outcome:
                counts["skipped"] += 1
                continue
            await _record_settlement(db, pick, outcome, {}, source=f"espn_{league}_prop")
            counts[outcome] += 1
            counts["settled"] += 1
    logger.info("Player-prop ESPN settler: %s", counts)
    return counts


# ───────────────────────── Shared persistence ─────────────────────────

async def _record_settlement(db, pick: dict, outcome: str, ref: dict, source: str) -> None:
    """Write the settled status, units profit, CLV to MongoDB."""
    from analytics import (american_profit_per_unit, clv_units,
                            confidence_bucket)
    odds_used = pick.get("closing_odds") or pick.get("book_odds")
    units_profit = american_profit_per_unit(odds_used or 0, outcome)
    clv = clv_units(pick.get("odds_at_pick"),
                    pick.get("closing_odds") or pick.get("book_odds"))
    # Build final-score dict where possible (Tennis: games per player; UFC: just winner).
    final_score: dict = {}
    for c in ref.get("competitors", []) if isinstance(ref, dict) else []:
        name = (c.get("athlete") or {}).get("displayName") or (c.get("team") or {}).get("displayName") or ""
        games = sum(int(ls.get("value") or 0) for ls in c.get("linescores", []))
        if name and games:
            final_score[name] = games
        elif name and c.get("winner") is not None:
            final_score[name] = "W" if c.get("winner") else "L"
    await db.picks.update_one(
        {"id": pick["id"]},
        {"$set": {
            "status": outcome,
            "settled_at": datetime.now(timezone.utc).isoformat(),
            "final_score": final_score,
            "units_risked": 1.0 if outcome != "push" else 0.0,
            "units_profit": units_profit,
            "clv_value": clv,
            "confidence_bucket": confidence_bucket(pick.get("lock_score")),
            "settlement_source": source,
        }},
    )


# ───────────────────────── Unified entry point ─────────────────────────

async def settle_via_espn(db) -> dict:
    """Run all ESPN settlers in sequence. Safe to call alongside the
    primary Odds-API settler — each handler only touches its own sport."""
    out = {"tennis": {}, "ufc": {}, "props": {}}
    try:
        out["tennis"] = await settle_tennis_via_espn(db)
    except Exception as e:
        logger.warning("ESPN tennis settler failed: %s", e)
    try:
        out["ufc"] = await settle_ufc_via_espn(db)
    except Exception as e:
        logger.warning("ESPN UFC settler failed: %s", e)
    try:
        out["props"] = await settle_player_props_via_espn(db)
    except Exception as e:
        logger.warning("ESPN player-prop settler failed: %s", e)
    return out
