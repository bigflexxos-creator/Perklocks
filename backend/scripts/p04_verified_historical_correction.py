"""P0.4 (2026-06 / user-authorized) — VERIFIED HISTORICAL SETTLEMENT
CORRECTION (dry-run).

Read-only reconciliation of every historical suspicious_actual_zero_loss
row per the strict P0.4 spec.  This script performs NO writes.  It
emits a proposal audit trail to::

    /tmp/p04_historical_correction_report.json

Source-of-truth rules (spec-exact):

MLB
  Primary   — MLB StatsAPI  (statsapi.mlb.com)
  Fallback  — ESPN          (site.api.espn.com)
  Conflict  — mark ``source_conflict = True`` and
              ``settlement_verified = False``; DO NOT auto-correct.

Soccer
  Primary   — FotMob        (fotmob.com/api/data)
  Fallback  — ESPN          (site.api.espn.com  soccer)
  Team-level markets (moneyline / match winner) may use either source
  when the event match is exact — the players_count reconciliation
  path is player-specific and requires FotMob first.
  Conflict  — mark ``source_conflict = True`` /
              ``settlement_verified = False``; DO NOT auto-correct.

NBA
  Primary   — ESPN

Alt-line invariant (P0.4 spec §4):
  One authoritative actual per (event, participant, stat/market)
  family.  Every alt threshold grades from the SAME actual.
  Do NOT independently re-fetch an actual per alt threshold.

Historical zero rule (P0.4 spec §5):
  If we cannot authoritatively confirm ``actual == 0``, we do NOT
  leave the row as a graded zero.  We propose
    { corrected_actual: None,
      proposed_result:  "unresolved",
      settlement_status: "unresolved",
      settlement_verified: False }.
  Missing data ≠ zero — every time.

Non-numeric markets (moneyline / match winner / team result) are
classified separately — NOT flagged as suspicious numeric-zero.

Correction safety (P0.4 spec §7):
  A row is only proposed for correction when we prove:
    * correct event         (schedule / date+teams match)
    * correct player/participant  (fullName match; boxscore hit)
    * correct market/stat
    * correct threshold/line where applicable
    * authoritative completed-event result
    * authoritative actual where numeric
  If ANY required identity is ambiguous → propose ``unresolved``.

Do NOT touch: 79 legacy synthetic book_odds rows, scoring,
ranking, simulators, Lock Score gate, Magic Layer, Player History.
Do NOT deploy.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import unicodedata as _ud
from collections import defaultdict, Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient

from services.universal_settlement_contract import (
    grade_over_under, grade_milestone,
    RESULT_UNRESOLVED, RESULT_WON, RESULT_LOST, RESULT_PUSH, RESULT_VOID,
)


# ─── HTTP config ────────────────────────────────────────────────────
_TIMEOUT = 15.0
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_FOTMOB_HEADERS = {
    "User-Agent": _UA, "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9", "x-mas": "static",
}
_ESPN_HEADERS = {"Accept": "application/json"}
_MLB_HEADERS  = {"User-Agent": _UA, "Accept": "application/json"}

MLB_SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"
MLB_BOXSCORE_URL = "https://statsapi.mlb.com/api/v1/game/{pk}/boxscore"
MLB_LIVE_URL     = "https://statsapi.mlb.com/api/v1.1/game/{pk}/feed/live"
ESPN_MLB_BOX     = ("https://site.api.espn.com/apis/site/v2/sports/"
                     "baseball/mlb/summary")
ESPN_MLB_SCHED   = ("https://site.api.espn.com/apis/site/v2/sports/"
                     "baseball/mlb/scoreboard")
ESPN_NBA_BOX     = ("https://site.api.espn.com/apis/site/v2/sports/"
                     "basketball/nba/summary")
ESPN_NBA_SCHED   = ("https://site.api.espn.com/apis/site/v2/sports/"
                     "basketball/nba/scoreboard")
FOTMOB_MATCHES   = "https://www.fotmob.com/api/data/matches"
FOTMOB_DETAIL    = "https://www.fotmob.com/api/data/matchDetails"


# ─── Text / name helpers ────────────────────────────────────────────
def _norm(s: Optional[str]) -> str:
    if not s: return ""
    s = "".join(c for c in _ud.normalize("NFD", s) if _ud.category(c) != "Mn")
    return s.strip().lower()


def _names_match(a: Optional[str], b: Optional[str]) -> bool:
    if not a or not b: return False
    na, nb = _norm(a), _norm(b)
    if not na or not nb: return False
    if na == nb: return True
    suffix_pat = re.compile(
        r"\b(fc|cf|ec|ac|sc|afc|cfc|sk|fk|ud|cd)\b\.?", re.IGNORECASE)
    a2 = re.sub(r"\s+", " ", suffix_pat.sub("", na).strip())
    b2 = re.sub(r"\s+", " ", suffix_pat.sub("", nb).strip())
    if a2 and b2 and a2 == b2: return True
    if len(a2) >= 3 and len(b2) >= 3 and (a2 in b2 or b2 in a2): return True
    ta, tb = set(a2.split()), set(b2.split())
    if ta and tb:
        common = ta & tb
        if len(common) >= 1 and len(common) / max(len(ta), len(tb)) >= 0.5:
            return True
    sa, sb = a2.split(), b2.split()
    if sa and sb and sa[0] == sb[0] and len(sa[0]) >= 3:
        return True
    return False


def _player_match(pick_name: str, box_name: str) -> tuple[bool, float]:
    """Return (matched, confidence 0..1)."""
    if not pick_name or not box_name:
        return False, 0.0
    a, b = _norm(pick_name), _norm(box_name)
    if a == b:
        return True, 1.0
    sa, sb = a.split(), b.split()
    if sa and sb and sa[-1] == sb[-1] and len(sa[-1]) >= 4:
        if len(sa) >= 2 and len(sb) >= 2 and sa[0][:1] == sb[0][:1]:
            return True, 0.9
        return True, 0.75
    if a in b or b in a:
        return True, 0.7
    return False, 0.0


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s: return None
    try:
        if s.endswith("Z"): s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _candidate_yyyymmdd(dt: datetime) -> list[str]:
    return [(dt + timedelta(days=d)).strftime("%Y%m%d")
             for d in (0, -1, 1)]


def _candidate_yyyy_mm_dd(dt: datetime) -> list[str]:
    return [(dt + timedelta(days=d)).strftime("%Y-%m-%d")
             for d in (0, -1, 1)]


# ─── MLB StatsAPI resolution ────────────────────────────────────────
class _MLBSource:
    """MLB StatsAPI game-pk resolver + boxscore fetcher (cached)."""
    def __init__(self) -> None:
        self._schedule_cache: dict[str, list[dict]] = {}
        self._boxscore_cache: dict[str, dict] = {}

    async def _schedule(self, client: httpx.AsyncClient, date: str) -> list[dict]:
        if date in self._schedule_cache:
            return self._schedule_cache[date]
        try:
            r = await client.get(MLB_SCHEDULE_URL,
                                  params={"sportId": 1, "date": date},
                                  headers=_MLB_HEADERS, timeout=_TIMEOUT)
            r.raise_for_status()
            j = r.json()
        except Exception:
            self._schedule_cache[date] = []
            return []
        out: list[dict] = []
        for d in (j.get("dates") or []):
            out.extend(d.get("games") or [])
        self._schedule_cache[date] = out
        return out

    async def find_game_pk(self, client: httpx.AsyncClient, *,
                             event_time_iso: Optional[str],
                             home_team_id: Optional[int],
                             away_team_id: Optional[int],
                             home_name: Optional[str],
                             away_name: Optional[str]) -> tuple[Optional[int], float]:
        """Find MLB gamePk.  For doubleheaders / split games, ALWAYS
        pick the game whose ``gameDate`` is closest to the pick's
        ``event_time``.  Never take the first match blindly — a
        double-header on the same day would silently pull the wrong
        boxscore (spec §7 event identity violation)."""
        dt = _parse_iso(event_time_iso) or datetime.now(timezone.utc)
        candidates: list[tuple[int, datetime, float]] = []
        for d in _candidate_yyyy_mm_dd(dt):
            games = await self._schedule(client, d)
            for g in games:
                h = g["teams"]["home"]["team"]
                a = g["teams"]["away"]["team"]
                conf = 0.0
                if home_team_id and away_team_id:
                    if h.get("id") == home_team_id and a.get("id") == away_team_id:
                        conf = 1.0
                if conf == 0.0 and home_name and away_name:
                    if (_names_match(h.get("name"), home_name)
                            and _names_match(a.get("name"), away_name)):
                        conf = 0.9
                if conf == 0.0:
                    continue
                gd = _parse_iso(g.get("gameDate"))
                if gd is None:
                    # Cannot temporally disambiguate; keep but with
                    # a far-away timestamp so it's least-preferred.
                    gd = datetime.max.replace(tzinfo=timezone.utc)
                candidates.append((g.get("gamePk"), gd, conf))
        if not candidates:
            return None, 0.0
        # Pick the candidate whose gameDate is closest to event_time.
        candidates.sort(key=lambda t: abs((t[1] - dt).total_seconds()))
        best = candidates[0]
        return best[0], best[2]

    async def boxscore(self, client: httpx.AsyncClient, game_pk: int) -> Optional[dict]:
        key = str(game_pk)
        if key in self._boxscore_cache:
            return self._boxscore_cache[key]
        try:
            r = await client.get(MLB_BOXSCORE_URL.format(pk=game_pk),
                                  headers=_MLB_HEADERS, timeout=_TIMEOUT)
            r.raise_for_status()
            j = r.json()
        except Exception:
            self._boxscore_cache[key] = {}
            return None
        self._boxscore_cache[key] = j
        return j

    async def game_final(self, client: httpx.AsyncClient,
                          game_pk: int) -> Optional[bool]:
        """Confirm game reached Final."""
        for d in list(self._schedule_cache.values()):
            for g in d:
                if g.get("gamePk") == game_pk:
                    st = (g.get("status") or {}).get("abstractGameState")
                    return st == "Final"
        # No cached — do a live fetch:
        try:
            r = await client.get(MLB_LIVE_URL.format(pk=game_pk),
                                  headers=_MLB_HEADERS, timeout=_TIMEOUT)
            r.raise_for_status()
            state = (((r.json().get("gameData") or {}).get("status")
                       or {}).get("abstractGameState"))
            return state == "Final"
        except Exception:
            return None

    def extract_player_stats(self, box: dict, player_name: str) -> tuple[Optional[dict], Optional[str], float, Optional[str]]:
        """Return (batting_and_pitching_stats_dict, side, confidence,
        matched_full_name)."""
        best: tuple[float, Optional[dict], Optional[str], Optional[str]] = (0.0, None, None, None)
        for side in ("home", "away"):
            team = ((box.get("teams") or {}).get(side) or {})
            for pid, pdata in (team.get("players") or {}).items():
                person = pdata.get("person") or {}
                nm = person.get("fullName")
                ok, conf = _player_match(player_name, nm or "")
                if ok and conf > best[0]:
                    stats = pdata.get("stats") or {}
                    best = (conf, stats, side, nm)
        if best[1] is None:
            return None, None, 0.0, None
        return best[1], best[2], best[0], best[3]


# ─── ESPN resolution (MLB / NBA / Soccer fallback) ──────────────────
class _ESPNSource:
    def __init__(self) -> None:
        self._sched_cache: dict[tuple[str, str], list[dict]] = {}
        self._summary_cache: dict[tuple[str, str], dict] = {}

    async def _sched(self, client: httpx.AsyncClient, base: str,
                       date: str, sport_key: str) -> list[dict]:
        key = (sport_key, date)
        if key in self._sched_cache:
            return self._sched_cache[key]
        try:
            r = await client.get(base, params={"dates": date},
                                  headers=_ESPN_HEADERS, timeout=_TIMEOUT)
            r.raise_for_status()
            j = r.json()
        except Exception:
            self._sched_cache[key] = []
            return []
        events = j.get("events") or []
        self._sched_cache[key] = events
        return events

    async def find_event(self, client: httpx.AsyncClient, *,
                          sport: str, event_time_iso: Optional[str],
                          home_name: str, away_name: str
                          ) -> tuple[Optional[str], float]:
        base = ESPN_MLB_SCHED if sport == "MLB" else ESPN_NBA_SCHED
        dt = _parse_iso(event_time_iso) or datetime.now(timezone.utc)
        for d in _candidate_yyyymmdd(dt):
            events = await self._sched(client, base, d, sport)
            for e in events:
                comps = (e.get("competitions") or [])
                if not comps: continue
                teams = comps[0].get("competitors") or []
                home = next((t for t in teams
                              if (t.get("homeAway") or "").lower() == "home"),
                             {})
                away = next((t for t in teams
                              if (t.get("homeAway") or "").lower() == "away"),
                             {})
                hname = ((home.get("team") or {}).get("displayName") or "")
                aname = ((away.get("team") or {}).get("displayName") or "")
                if _names_match(hname, home_name) and _names_match(aname, away_name):
                    return e.get("id"), 0.9
        return None, 0.0

    async def summary(self, client: httpx.AsyncClient, sport: str,
                       event_id: str) -> Optional[dict]:
        key = (sport, event_id)
        if key in self._summary_cache:
            return self._summary_cache[key]
        base = ESPN_MLB_BOX if sport == "MLB" else ESPN_NBA_BOX
        try:
            r = await client.get(base, params={"event": event_id},
                                  headers=_ESPN_HEADERS, timeout=_TIMEOUT)
            r.raise_for_status()
            j = r.json()
        except Exception:
            self._summary_cache[key] = {}
            return None
        self._summary_cache[key] = j
        return j


# ─── FotMob resolution ──────────────────────────────────────────────
class _FotMobSource:
    def __init__(self) -> None:
        self._matches_cache: dict[str, list[dict]] = {}
        self._detail_cache: dict[str, dict] = {}

    async def _matches(self, client: httpx.AsyncClient, date_str: str) -> list[dict]:
        if date_str in self._matches_cache:
            return self._matches_cache[date_str]
        try:
            r = await client.get(FOTMOB_MATCHES,
                                  params={"date": date_str},
                                  headers=_FOTMOB_HEADERS, timeout=_TIMEOUT)
            r.raise_for_status()
            data = r.json()
        except Exception:
            self._matches_cache[date_str] = []
            return []
        out: list[dict] = []
        for lg in (data.get("leagues") or []):
            for m in (lg.get("matches") or []):
                m["__league"] = lg.get("name")
                out.append(m)
        self._matches_cache[date_str] = out
        return out

    async def find_match(self, client: httpx.AsyncClient, *,
                          home_team: str, away_team: str,
                          event_time_iso: Optional[str]
                          ) -> tuple[Optional[dict], float]:
        dt = _parse_iso(event_time_iso) or datetime.now(timezone.utc)
        for ds in _candidate_yyyymmdd(dt):
            for m in await self._matches(client, ds):
                mh = ((m.get("home") or {}).get("longName") or
                      (m.get("home") or {}).get("name") or "")
                ma = ((m.get("away") or {}).get("longName") or
                      (m.get("away") or {}).get("name") or "")
                if _names_match(mh, home_team) and _names_match(ma, away_team):
                    return m, 0.9
        return None, 0.0

    async def detail(self, client: httpx.AsyncClient,
                      match_id) -> Optional[dict]:
        key = str(match_id)
        if key in self._detail_cache:
            return self._detail_cache[key]
        try:
            r = await client.get(FOTMOB_DETAIL,
                                  params={"matchId": key},
                                  headers=_FOTMOB_HEADERS, timeout=_TIMEOUT)
            r.raise_for_status()
            j = r.json()
        except Exception:
            self._detail_cache[key] = {}
            return None
        self._detail_cache[key] = j
        return j

    def player_goals_assists(self, detail: dict, player_name: str
                              ) -> tuple[Optional[int], Optional[int], float, Optional[str]]:
        """Count goals + assists for the named player from matchFacts
        events.  Ignores own-goals and missed penalties.

        Return: (goals, assists, confidence, matched_name).  If we
        can't find the player in the lineup at all → (None, None, 0, None)
        (unresolved).  If the player played but has no goals/assists →
        (0, 0, confidence, name).
        """
        if not detail:
            return None, None, 0.0, None
        content = detail.get("content") or {}
        # Check lineup for participation first.
        lineup = content.get("lineup") or {}
        played_name: Optional[str] = None
        played_conf = 0.0
        for side_key in ("homeTeam", "awayTeam"):
            side = lineup.get(side_key) or {}
            for group in ("starters", "subs"):
                for pl in (side.get(group) or []):
                    nm = pl.get("name") or ""
                    if not nm: continue
                    ok, conf = _player_match(player_name, nm)
                    if ok and conf > played_conf:
                        # For subs we need to check if they came on.
                        if group == "subs":
                            perf = pl.get("performance") or {}
                            events = perf.get("substitutionEvents") or []
                            came_on = any((e.get("type") or "").lower() == "subin"
                                            for e in events)
                            if not (came_on or perf.get("rating") is not None
                                     or perf.get("minutesPlayed")):
                                # Unused sub — DNP.
                                continue
                        played_name = nm
                        played_conf = conf
        if not played_name:
            return None, None, 0.0, None

        # Now count goals and assists from match events.
        mf = content.get("matchFacts") or {}
        events = mf.get("events") or {}
        ev_list = events.get("events") if isinstance(events, dict) else events
        goals = 0
        assists = 0
        for e in (ev_list or []):
            type_ = (e.get("type") or "").lower()
            if "goal" in type_ and "own" not in type_ and "miss" not in type_:
                pl = e.get("player") or {}
                nm = pl.get("name") if isinstance(pl, dict) else str(pl or "")
                if nm:
                    ok, _ = _player_match(player_name, nm)
                    if ok:
                        goals += 1
                # assist
                ap = e.get("assistPlayer") or e.get("assist") or None
                anm = (ap.get("name") if isinstance(ap, dict)
                        else (str(ap) if ap else ""))
                if anm:
                    ok, _ = _player_match(player_name, anm)
                    if ok:
                        assists += 1
        return goals, assists, played_conf, played_name


# ─── ESPN Soccer fallback (moneyline / final-score only) ────────────
ESPN_SOCCER_SCHED = ("https://site.api.espn.com/apis/site/v2/sports/"
                      "soccer/{league_slug}/scoreboard")
ESPN_SOCCER_SUMM  = ("https://site.api.espn.com/apis/site/v2/sports/"
                      "soccer/{league_slug}/summary")

_LEAGUE_HINT_TO_SLUGS: dict[str, list[str]] = {
    "mls":                ["usa.1"],
    "premier league":     ["eng.1"],
    "epl":                ["eng.1"],
    "la liga":            ["esp.1"],
    "serie a":            ["ita.1"],
    "bundesliga":         ["ger.1"],
    "ligue 1":            ["fra.1"],
    "champions league":   ["uefa.champions"],
    "europa league":      ["uefa.europa"],
}

def _guess_espn_slugs(league_field: str) -> list[str]:
    lf = (league_field or "").lower()
    for k, v in _LEAGUE_HINT_TO_SLUGS.items():
        if k in lf: return v
    return []


# ─── Pick classification / bucketing ────────────────────────────────
_STAT_ALIASES = {
    "hits": "batting.hits",
    "rbi": "batting.rbi",
    "hits+runs+rbi": "batting.hits+batting.runs+batting.rbi",
    "strikeOuts_pitcher": "pitching.strikeOuts",
    "strikeOuts_batter":  "batting.strikeOuts",
    "outs": "pitching.outs",
    "goals":   "goals",
    "assists": "assists",
}


def _is_non_numeric_market(pick: dict) -> bool:
    """Moneyline / winner / team result markets — do NOT require
    numeric actuals so they are classified separately in the report."""
    m = (pick.get("market") or "").lower()
    return any(tok in m for tok in (
        "moneyline", "match winner", "match result", "1x2",
        "team win", "team result", "double chance",
        "both teams to score", "btts"))


def _mlb_stat_from_market_or_detail(pick: dict) -> Optional[str]:
    stat = ((pick.get("settlement_detail") or {}).get("stat") or "").strip()
    m    = (pick.get("market") or "").lower()
    if stat == "strikeOuts":
        # Disambiguate pitcher vs batter K's
        # Pitcher markets always mention "Strikeouts" in a pitcher
        # context; batter K markets in Perklocks use "Batter Strikeouts".
        if "batter" in m or "batter_strikeout" in (
                pick.get("external_id") or "").lower():
            return "strikeOuts_batter"
        return "strikeOuts_pitcher"
    if stat in _STAT_ALIASES:
        return stat
    return stat or None


def _threshold(pick: dict) -> Optional[float]:
    sd = pick.get("settlement_detail") or {}
    for k in ("line",):
        if sd.get(k) is not None:
            try: return float(sd[k])
            except: pass
    for k in ("line", "published_line", "sim_threshold"):
        if pick.get(k) is not None:
            try: return float(pick[k])
            except: pass
    m = re.search(r"\b(?:over|under)\s+([\d.]+)\b",
                   pick.get("market") or "", re.I)
    if m:
        try: return float(m.group(1))
        except: pass
    return None


def _side_of(pick: dict) -> str:
    m = (pick.get("market") or "").lower()
    if ("anytime" in m or "goal scorer" in m
            or "score or assist" in m or "score & assist" in m
            or "score and assist" in m):
        return "milestone"  # anytime = 1+ occurrence
    if "over " in m: return "over"
    if "under " in m: return "under"
    return "over"


def _threshold_for_anytime(side: str) -> Optional[float]:
    return 1 if side == "milestone" else None


# ─── MLB stat extraction ────────────────────────────────────────────
def _extract_mlb_actual(mlb_stats: dict, stat_key: str) -> Optional[float]:
    b = (mlb_stats or {}).get("batting") or {}
    p = (mlb_stats or {}).get("pitching") or {}
    if stat_key == "hits":                 return b.get("hits")
    if stat_key == "rbi":                  return b.get("rbi")
    if stat_key == "hits+runs+rbi":
        h, r_, rb = b.get("hits"), b.get("runs"), b.get("rbi")
        if h is None or r_ is None or rb is None: return None
        return int(h) + int(r_) + int(rb)
    if stat_key == "strikeOuts_pitcher":   return p.get("strikeOuts")
    if stat_key == "strikeOuts_batter":    return b.get("strikeOuts")
    if stat_key == "outs":
        # Prefer pitching.outs (StatsAPI provides it).  Fallback:
        # inningsPitched → outs.
        o = p.get("outs")
        if o is not None:
            try: return int(o)
            except: pass
        ip = p.get("inningsPitched")
        if ip is not None:
            try:
                whole, part = str(ip).split(".") if "." in str(ip) else (str(ip), "0")
                return int(whole) * 3 + int(part)
            except Exception:
                return None
    return None


def _extract_espn_mlb_actual(summary: dict, player_name: str, stat_key: str
                              ) -> tuple[Optional[float], float, Optional[str]]:
    """ESPN MLB summary is deeply nested; return (actual, conf, name)."""
    best: tuple[float, Optional[float], Optional[str]] = (0.0, None, None)
    for grp in (summary.get("boxscore") or {}).get("players") or []:
        for stat_group in (grp.get("statistics") or []):
            name = (stat_group.get("name") or "").lower()
            athletes = stat_group.get("athletes") or []
            labels = stat_group.get("keys") or []
            for a in athletes:
                ath = a.get("athlete") or {}
                fn = ath.get("displayName")
                ok, conf = _player_match(player_name, fn or "")
                if not ok: continue
                stats = a.get("stats") or []
                # ESPN provides column list — index by "keys".
                mp = dict(zip(labels, stats)) if labels else {}
                v: Optional[float] = None
                if stat_key == "hits":
                    v = _to_num(mp.get("hits"))
                elif stat_key == "rbi":
                    v = _to_num(mp.get("RBI") or mp.get("rbi"))
                elif stat_key == "hits+runs+rbi":
                    h_ = _to_num(mp.get("hits"))
                    r_ = _to_num(mp.get("runs"))
                    rb = _to_num(mp.get("RBI") or mp.get("rbi"))
                    if h_ is not None and r_ is not None and rb is not None:
                        v = h_ + r_ + rb
                elif stat_key == "strikeOuts_pitcher" and "pitch" in name:
                    v = _to_num(mp.get("SO") or mp.get("strikeouts"))
                elif stat_key == "strikeOuts_batter" and "bat" in name:
                    v = _to_num(mp.get("SO") or mp.get("strikeouts")
                                 or mp.get("K"))
                elif stat_key == "outs" and "pitch" in name:
                    ip = mp.get("IP")
                    v = None
                    if ip is not None:
                        try:
                            whole, part = (str(ip).split(".")
                                             if "." in str(ip)
                                             else (str(ip), "0"))
                            v = int(whole) * 3 + int(part)
                        except Exception:
                            v = None
                if v is not None and conf > best[0]:
                    best = (conf, v, fn)
    return best[1], best[0], best[2]


def _to_num(x) -> Optional[float]:
    if x is None: return None
    try: return float(str(x))
    except (TypeError, ValueError): return None


# ─── Alt-line grouping key ──────────────────────────────────────────
def _teams_from_pick(pick: dict) -> tuple[str, str]:
    """Return (home_team, away_team).  Fallback: parse ``event``
    string of shape ``"Away @ Home"`` — some sports (NBA) don't
    persist home/away fields on the pick."""
    home = pick.get("home_team") or ""
    away = pick.get("away_team") or ""
    if home and away:
        return home, away
    event = pick.get("event") or ""
    parts = re.split(r"\s+@\s+", event)
    if len(parts) == 2:
        return parts[1].strip(), parts[0].strip()
    return home, away


def _group_key(pick: dict, stat_key_hint: Optional[str]) -> tuple:
    et = (pick.get("event_time") or "")[:10]  # date only
    home_name, away_name = _teams_from_pick(pick)
    home = _norm(home_name)
    away = _norm(away_name)
    player = _norm((pick.get("settlement_detail") or {}).get("player")
                     or pick.get("selection") or "")
    stat = stat_key_hint or (pick.get("settlement_detail") or {}).get("stat")
    return (et, home, away, player, stat)


# ─── Grader helpers using universal contract ────────────────────────
def _grade_from_actual(actual: Optional[float], threshold: Optional[float],
                        side: str) -> tuple[str, str]:
    """Grade a single pick using the universal settlement contract.
    Return (result, settlement_status). If actual is None → unresolved.
    """
    if actual is None:
        return RESULT_UNRESOLVED, "unresolved"
    if side == "milestone":
        env = grade_milestone(actual, threshold if threshold is not None else 1)
    else:
        env = grade_over_under(actual, threshold, side)
    return env["result"], env["settlement_status"]


# ─── Main reconciliation drivers ────────────────────────────────────
async def _reconcile_mlb(client: httpx.AsyncClient, mlb: _MLBSource,
                          espn: _ESPNSource, picks: list[dict]
                          ) -> list[dict]:
    proposals: list[dict] = []

    # Group by (date, home, away, player, stat) → one authoritative actual per group
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for p in picks:
        stat_key = _mlb_stat_from_market_or_detail(p)
        groups[_group_key(p, stat_key)].append(p)

    for key, group_picks in groups.items():
        stat_key = _mlb_stat_from_market_or_detail(group_picks[0])
        player = ((group_picks[0].get("settlement_detail") or {}).get("player")
                   or group_picks[0].get("selection") or "")
        home_team, away_team = _teams_from_pick(group_picks[0])
        event_time = group_picks[0].get("event_time")
        home_id = group_picks[0].get("home_team_id")
        away_id = group_picks[0].get("away_team_id")

        game_pk, evt_conf = await mlb.find_game_pk(
            client, event_time_iso=event_time,
            home_team_id=home_id, away_team_id=away_id,
            home_name=home_team, away_name=away_team)

        primary_actual: Optional[float] = None
        primary_conf = 0.0
        primary_name = None
        primary_final: Optional[bool] = None
        if game_pk:
            primary_final = await mlb.game_final(client, game_pk)
            if primary_final:
                box = await mlb.boxscore(client, game_pk)
                if box:
                    stats, side, conf, matched_name = mlb.extract_player_stats(
                        box, player)
                    if stats is not None:
                        primary_actual = _extract_mlb_actual(stats, stat_key or "")
                        primary_conf = conf
                        primary_name = matched_name

        # Fallback: ESPN if primary unresolved
        fallback_actual: Optional[float] = None
        fallback_conf = 0.0
        fallback_name = None
        if primary_actual is None:
            espn_id, espn_evt_conf = await espn.find_event(
                client, sport="MLB", event_time_iso=event_time,
                home_name=home_team, away_name=away_team)
            if espn_id:
                summ = await espn.summary(client, "MLB", espn_id)
                if summ:
                    fallback_actual, fallback_conf, fallback_name = \
                        _extract_espn_mlb_actual(summ, player, stat_key or "")

        # Resolve conflict / choose authoritative
        source_conflict = False
        authoritative_actual: Optional[float] = None
        authoritative_source = "none"
        if primary_actual is not None and fallback_actual is not None:
            try:
                if float(primary_actual) != float(fallback_actual):
                    source_conflict = True
                    authoritative_source = "mlb_statsapi+espn_conflict"
                else:
                    authoritative_actual = float(primary_actual)
                    authoritative_source = "mlb_statsapi"
            except Exception:
                authoritative_actual = primary_actual
                authoritative_source = "mlb_statsapi"
        elif primary_actual is not None:
            authoritative_actual = float(primary_actual)
            authoritative_source = "mlb_statsapi"
        elif fallback_actual is not None:
            authoritative_actual = float(fallback_actual)
            authoritative_source = "espn_fallback"

        # Emit ONE proposal per pick in the group, using ONE authoritative_actual
        for p in group_picks:
            side = _side_of(p)
            th = _threshold(p) or _threshold_for_anytime(side)
            previous_actual = ((p.get("settlement_detail") or {}).get("value"))
            previous_result = p.get("status") or p.get("result")

            if source_conflict:
                proposed_result = "unresolved"
                proposed_status = "unresolved"
                settlement_verified = False
                reason = "source_conflict_mlb_vs_espn"
                unresolved_bucket = "source_conflict"
            elif authoritative_actual is None:
                proposed_result = "unresolved"
                proposed_status = "unresolved"
                settlement_verified = False
                if not game_pk:
                    reason = "event_not_resolved"
                    unresolved_bucket = "event_not_resolvable"
                elif primary_final is False:
                    reason = "event_not_final"
                    unresolved_bucket = "event_not_final"
                elif primary_conf == 0.0 and fallback_conf == 0.0:
                    reason = "player_not_found_in_boxscore"
                    unresolved_bucket = "player_not_in_boxscore"
                else:
                    reason = "player_found_component_missing"
                    unresolved_bucket = "player_found_component_missing"
            else:
                r, s = _grade_from_actual(authoritative_actual, th, side)
                proposed_result = r
                proposed_status = s
                settlement_verified = (r != RESULT_UNRESOLVED)
                reason = "graded_from_authoritative_actual"
                unresolved_bucket = None

            proposals.append({
                "sport": "MLB",
                "market": p.get("market"),
                "pick_id": p.get("id"),
                "player": player,
                "matched_player_name": primary_name or fallback_name,
                "event": p.get("event"),
                "event_time": event_time,
                "home_team": home_team, "away_team": away_team,
                "provider_event_id": game_pk,
                "provider_player_id": None,
                "event_match_confidence": evt_conf,
                "player_match_confidence": max(primary_conf, fallback_conf),
                "line": th, "side": side,
                "previous_actual": previous_actual,
                "authoritative_actual": authoritative_actual,
                "previous_result": previous_result,
                "proposed_result": proposed_result,
                "proposed_status": proposed_status,
                "settlement_verified": settlement_verified,
                "authoritative_source": authoritative_source,
                "source_conflict": source_conflict,
                "correction_reason": reason,
                "unresolved_bucket": unresolved_bucket,
                "published_lock_score": (p.get("published_lock_score")
                                          or p.get("lock_score")),
                "downstream_dependencies": _downstream_flags(p),
            })
    return proposals


async def _reconcile_soccer(client: httpx.AsyncClient, fm: _FotMobSource,
                              picks: list[dict]) -> list[dict]:
    proposals: list[dict] = []

    groups: dict[tuple, list[dict]] = defaultdict(list)
    for p in picks:
        groups[_group_key(p, None)].append(p)

    for key, group_picks in groups.items():
        player = ((group_picks[0].get("settlement_detail") or {}).get("player")
                   or group_picks[0].get("selection") or "")
        home_team, away_team = _teams_from_pick(group_picks[0])
        event_time = group_picks[0].get("event_time")
        stat = (group_picks[0].get("settlement_detail") or {}).get("stat") or ""

        # FotMob primary
        match, evt_conf = await fm.find_match(
            client, home_team=home_team, away_team=away_team,
            event_time_iso=event_time)
        primary_goals = primary_assists = None
        primary_conf = 0.0
        primary_name = None
        provider_match_id = None
        if match:
            provider_match_id = match.get("id")
            detail = await fm.detail(client, provider_match_id)
            if detail:
                g, a, conf, mn = fm.player_goals_assists(detail, player)
                primary_goals, primary_assists = g, a
                primary_conf = conf
                primary_name = mn

        # No ESPN fallback for player-level soccer stats (extremely
        # unreliable for non-major leagues), except:
        # if FotMob returned player-not-in-lineup → mark unresolved.
        # If FotMob event itself not found → unresolved (do NOT guess).

        for p in group_picks:
            side = _side_of(p)
            th = _threshold(p) or _threshold_for_anytime(side)
            previous_actual = ((p.get("settlement_detail") or {}).get("value"))
            previous_result = p.get("status") or p.get("result")

            # Choose actual based on stat
            actual_val: Optional[float] = None
            if stat == "goals":            actual_val = primary_goals
            elif stat == "assists":        actual_val = primary_assists
            elif stat == "scoreOrAssist":
                # "To Score or Assist" — combined goals + assists,
                # milestone (>=1).  Missing components ⇒ None per
                # universal contract §5 (grade_derived).
                if primary_goals is None or primary_assists is None:
                    actual_val = None
                else:
                    actual_val = int(primary_goals) + int(primary_assists)

            source_conflict = False  # no dual-source reconciliation for player stats

            if actual_val is None:
                proposed_result = "unresolved"
                proposed_status = "unresolved"
                settlement_verified = False
                if not match:
                    reason = "fotmob_event_not_found"
                    unresolved_bucket = "event_not_resolvable"
                    authoritative_source = "none"
                elif primary_name is None:
                    reason = "fotmob_player_not_in_lineup"
                    unresolved_bucket = "player_not_in_lineup"
                    authoritative_source = "fotmob"
                else:
                    reason = "fotmob_component_missing"
                    unresolved_bucket = "player_found_component_missing"
                    authoritative_source = "fotmob"
            else:
                r, s = _grade_from_actual(actual_val, th, side)
                proposed_result = r
                proposed_status = s
                settlement_verified = (r != RESULT_UNRESOLVED)
                reason = "graded_from_authoritative_actual"
                authoritative_source = "fotmob"
                unresolved_bucket = None

            proposals.append({
                "sport": "Soccer",
                "market": p.get("market"),
                "pick_id": p.get("id"),
                "player": player,
                "matched_player_name": primary_name,
                "event": p.get("event"),
                "event_time": event_time,
                "home_team": home_team, "away_team": away_team,
                "provider_event_id": provider_match_id,
                "provider_player_id": None,
                "event_match_confidence": evt_conf,
                "player_match_confidence": primary_conf,
                "line": th, "side": side,
                "previous_actual": previous_actual,
                "authoritative_actual": actual_val,
                "previous_result": previous_result,
                "proposed_result": proposed_result,
                "proposed_status": proposed_status,
                "settlement_verified": settlement_verified,
                "authoritative_source": authoritative_source,
                "source_conflict": source_conflict,
                "correction_reason": reason,
                "unresolved_bucket": unresolved_bucket,
                "published_lock_score": (p.get("published_lock_score")
                                          or p.get("lock_score")),
                "downstream_dependencies": _downstream_flags(p),
            })
    return proposals


async def _reconcile_nba(client: httpx.AsyncClient, espn: _ESPNSource,
                          picks: list[dict]) -> list[dict]:
    proposals: list[dict] = []
    for p in picks:
        player = ((p.get("settlement_detail") or {}).get("player")
                   or p.get("selection") or "")
        stat = (p.get("settlement_detail") or {}).get("stat") or ""
        home_team, away_team = _teams_from_pick(p)
        event_time = p.get("event_time")

        espn_id, evt_conf = await espn.find_event(
            client, sport="NBA", event_time_iso=event_time,
            home_name=home_team, away_name=away_team)
        actual_val: Optional[float] = None
        player_conf = 0.0
        matched_name = None
        if espn_id:
            summ = await espn.summary(client, "NBA", espn_id)
            if summ:
                # Walk NBA boxscore player stats
                for team_grp in (summ.get("boxscore") or {}).get("players") or []:
                    for stat_group in (team_grp.get("statistics") or []):
                        labels = stat_group.get("keys") or stat_group.get("names") or []
                        athletes = stat_group.get("athletes") or []
                        for a in athletes:
                            ath = a.get("athlete") or {}
                            fn = ath.get("displayName")
                            ok, conf = _player_match(player, fn or "")
                            if not ok: continue
                            stats_vals = a.get("stats") or []
                            mp = dict(zip(labels, stats_vals)) if labels else {}
                            v = None
                            if stat == "assists":
                                v = _to_num(mp.get("AST") or mp.get("assists"))
                            elif stat == "points":
                                v = _to_num(mp.get("PTS") or mp.get("points"))
                            elif stat == "rebounds":
                                v = _to_num(mp.get("REB") or mp.get("rebounds"))
                            elif stat == "steals":
                                v = _to_num(mp.get("STL"))
                            elif stat == "blocks":
                                v = _to_num(mp.get("BLK"))
                            if v is not None and conf > player_conf:
                                actual_val = v
                                player_conf = conf
                                matched_name = fn

        side = _side_of(p)
        th = _threshold(p) or _threshold_for_anytime(side)
        previous_actual = ((p.get("settlement_detail") or {}).get("value"))
        previous_result = p.get("status") or p.get("result")

        if actual_val is None:
            proposed_result = "unresolved"
            proposed_status = "unresolved"
            settlement_verified = False
            if not espn_id:
                reason = "espn_event_not_resolved"
                unresolved_bucket = "event_not_resolvable"
            elif player_conf == 0.0:
                reason = "espn_player_not_in_boxscore"
                unresolved_bucket = "player_not_in_boxscore"
            else:
                reason = "espn_component_missing"
                unresolved_bucket = "player_found_component_missing"
            src = "espn" if espn_id else "none"
        else:
            r, s = _grade_from_actual(actual_val, th, side)
            proposed_result = r
            proposed_status = s
            settlement_verified = (r != RESULT_UNRESOLVED)
            reason = "graded_from_authoritative_actual"
            src = "espn"
            unresolved_bucket = None

        proposals.append({
            "sport": "NBA",
            "market": p.get("market"),
            "pick_id": p.get("id"),
            "player": player,
            "matched_player_name": matched_name,
            "event": p.get("event"),
            "event_time": event_time,
            "home_team": home_team, "away_team": away_team,
            "provider_event_id": espn_id,
            "provider_player_id": None,
            "event_match_confidence": evt_conf,
            "player_match_confidence": player_conf,
            "line": th, "side": side,
            "previous_actual": previous_actual,
            "authoritative_actual": actual_val,
            "previous_result": previous_result,
            "proposed_result": proposed_result,
            "proposed_status": proposed_status,
            "settlement_verified": settlement_verified,
            "authoritative_source": src,
            "source_conflict": False,
            "correction_reason": reason,
            "unresolved_bucket": unresolved_bucket,
            "published_lock_score": (p.get("published_lock_score")
                                      or p.get("lock_score")),
            "downstream_dependencies": _downstream_flags(p),
        })
    return proposals


# ─── Downstream flagging (FLAG ONLY — no writes) ────────────────────
def _downstream_flags(pick: dict) -> dict:
    pid = pick.get("id")
    return {
        # These are flags for a future propagation pass.  We do NOT
        # inspect them here; the caller will run a separate propagation
        # pass that queries these collections deterministically.
        "flag_parlay_legs":     True,
        "flag_rollovers":       True,
        "flag_tracked_bets":    True,
        "flag_post_mortem":     True,
        "flag_learning":        True,
        "flag_calibration":     True,
        "pick_id":              pid,
    }


# ─── Report aggregation ─────────────────────────────────────────────
def _aggregate(proposals: list[dict]) -> dict:
    by = defaultdict(lambda: defaultdict(Counter))
    high_conf = defaultdict(lambda: defaultdict(Counter))
    for pr in proposals:
        sport = pr["sport"] or "unknown"
        # Market bucket — coarse.  IMPORTANT: check derived markets
        # BEFORE simple substrings so "Hits + Runs + RBIs" is classified
        # as its own family and NOT accidentally lumped into "hits".
        m = (pr.get("market") or "").lower()
        m_norm = re.sub(r"\s+", "", m)
        if ("hits+runs+rbi" in m_norm or "h+r+rbi" in m_norm):
            mb = "hits+runs+rbi"
        elif "total bases" in m:                        mb = "total_bases"
        elif "home run" in m or " hr " in f" {m} ":     mb = "home_runs"
        elif "strikeout" in m:                          mb = "strikeouts"
        elif "pitcher outs" in m or "outs recorded" in m or " outs" in m:
            mb = "pitcher_outs"
        elif "rbi" in m and "runs" not in m:            mb = "rbi"
        elif "runs" in m and "rbi" not in m and "runs+rbi" not in m_norm:
            mb = "runs"
        elif "hits" in m:                               mb = "hits"
        elif "goal scorer" in m or "anytime" in m:      mb = "anytime_scorer"
        elif "score or assist" in m or "score & assist" in m or "score and assist" in m:
            mb = "score_or_assist"
        elif "assist" in m:                             mb = "assists"
        elif "point" in m:                              mb = "points"
        elif "rebound" in m:                            mb = "rebounds"
        else:                                            mb = "other"

        by[sport][mb]["scanned"] += 1

        # Verification outcome
        if pr["source_conflict"]:
            by[sport][mb]["source_conflicts"] += 1
        if pr["authoritative_actual"] is not None:
            by[sport][mb]["authoritatively_verified"] += 1
        elif pr["proposed_result"] == "unresolved":
            by[sport][mb]["unable_to_verify"] += 1

        prev = (pr.get("previous_result") or "").lower()
        prop = (pr.get("proposed_result") or "").lower()

        if prop == "unresolved":
            by[sport][mb]["proposed_unresolved"] += 1
        elif prev == "lost" and prop == "won":
            by[sport][mb]["proposed_loss_to_win"] += 1
        elif prev == "won" and prop == "lost":
            by[sport][mb]["proposed_win_to_loss"] += 1
        elif prop == "push" or prop == "void":
            by[sport][mb]["proposed_push_or_void"] += 1
        elif prev == prop:
            by[sport][mb]["unchanged"] += 1

        try:
            ls = float(pr.get("published_lock_score") or 0)
        except (TypeError, ValueError):
            ls = 0.0
        if ls > 85:
            high_conf[sport][mb][
                "gt85_" + (
                    "unresolved" if prop == "unresolved" else
                    "loss_to_win" if prev == "lost" and prop == "won" else
                    "win_to_loss" if prev == "won" and prop == "lost" else
                    "unchanged")] += 1

    return {
        "totals_by_sport_and_market": {
            s: {mb: dict(c) for mb, c in bm.items()} for s, bm in by.items()},
        "gt85_by_sport_and_market": {
            s: {mb: dict(c) for mb, c in bm.items()}
            for s, bm in high_conf.items()},
    }


# ─── Main entry point ───────────────────────────────────────────────
async def run(db, *, limit_per_sport: Optional[int] = None) -> dict:
    """Full read-only P0.4 dry-run reconciliation."""
    generated_at = datetime.now(timezone.utc).isoformat()

    query_base = {
        "status": "lost",
        "$or": [{"settlement_detail.value": 0},
                 {"settlement_detail.value": 0.0}],
    }

    all_proposals: list[dict] = []
    non_numeric_by_sport: dict[str, int] = Counter()

    # -- MLB -----------------------------------------------------------
    mlb_picks: list[dict] = []
    async for p in db.picks.find({**query_base, "sport": "MLB"}):
        if _is_non_numeric_market(p):
            non_numeric_by_sport["MLB"] += 1
            continue
        mlb_picks.append(p)
    if limit_per_sport is not None:
        mlb_picks = mlb_picks[:limit_per_sport]

    # -- Soccer --------------------------------------------------------
    soccer_picks: list[dict] = []
    async for p in db.picks.find({**query_base, "sport": "Soccer"}):
        if _is_non_numeric_market(p):
            non_numeric_by_sport["Soccer"] += 1
            continue
        soccer_picks.append(p)
    if limit_per_sport is not None:
        soccer_picks = soccer_picks[:limit_per_sport]

    # -- NBA -----------------------------------------------------------
    nba_picks: list[dict] = []
    async for p in db.picks.find({**query_base, "sport": "NBA"}):
        if _is_non_numeric_market(p):
            non_numeric_by_sport["NBA"] += 1
            continue
        nba_picks.append(p)
    if limit_per_sport is not None:
        nba_picks = nba_picks[:limit_per_sport]

    mlb  = _MLBSource()
    espn = _ESPNSource()
    fm   = _FotMobSource()

    async with httpx.AsyncClient() as client:
        if mlb_picks:
            print(f"[MLB]    reconciling {len(mlb_picks)} picks ...",
                   flush=True)
            all_proposals.extend(await _reconcile_mlb(
                client, mlb, espn, mlb_picks))
        if soccer_picks:
            print(f"[Soccer] reconciling {len(soccer_picks)} picks ...",
                   flush=True)
            all_proposals.extend(await _reconcile_soccer(
                client, fm, soccer_picks))
        if nba_picks:
            print(f"[NBA]    reconciling {len(nba_picks)} picks ...",
                   flush=True)
            all_proposals.extend(await _reconcile_nba(
                client, espn, nba_picks))

    aggregates = _aggregate(all_proposals)

    # Seymour regression check (never mutated but included in report)
    seymour_pick = await db.picks.find_one(
        {"id": "6f163552-16fa-5c04-aa73-ebc2bb08ee73"})
    seymour_check = None
    if seymour_pick:
        seymour_check = {
            "pick_id": seymour_pick.get("id"),
            "market": seymour_pick.get("market"),
            "status": seymour_pick.get("status"),
            "settlement_verified": seymour_pick.get("settlement_verified"),
            "settlement_detail_value": (
                seymour_pick.get("settlement_detail") or {}).get("value"),
            "expected": {"actual": 7, "result": "won",
                          "settlement_verified": True},
            "ok": (seymour_pick.get("status") == "won" and
                    seymour_pick.get("settlement_verified") is True and
                    ((seymour_pick.get("settlement_detail") or {})
                     .get("value") in (7, 7.0))),
        }

    return {
        "phase": "P0.4",
        "mode": "DRY_RUN_READ_ONLY",
        "generated_at": generated_at,
        "totals": {
            "MLB_scanned":    len(mlb_picks),
            "Soccer_scanned": len(soccer_picks),
            "NBA_scanned":    len(nba_picks),
            "non_numeric_markets_skipped": dict(non_numeric_by_sport),
            "total_proposals": len(all_proposals),
        },
        "aggregates": aggregates,
        "seymour_regression_check": seymour_check,
        "proposals": all_proposals,
    }


async def _main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit-per-sport", type=int, default=None,
                     help="Limit picks per sport (debug only)")
    ap.add_argument("--output", type=str,
                     default="/tmp/p04_historical_correction_report.json")
    args = ap.parse_args()

    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = client[os.environ.get("DB_NAME", "perkslocks_production")]
    report = await run(db, limit_per_sport=args.limit_per_sport)

    with open(args.output, "w") as fh:
        json.dump(report, fh, indent=2, default=str)

    print()
    print("=" * 78)
    print("P0.4 — VERIFIED HISTORICAL SETTLEMENT CORRECTION (DRY RUN)")
    print("=" * 78)
    print(f"Mode:       {report['mode']}")
    print(f"Generated:  {report['generated_at']}")
    print(f"Report:     {args.output}")
    print()
    print("── Totals ──")
    for k, v in report["totals"].items():
        print(f"    {k}: {v}")
    print()
    print("── Aggregates by sport / market ──")
    for sport, mkts in report["aggregates"]["totals_by_sport_and_market"].items():
        print(f"── {sport} ──")
        for mb, counts in mkts.items():
            print(f"    {mb:>18s}  {dict(counts)}")
    print()
    print("── Seymour regression check ──")
    print(json.dumps(report["seymour_regression_check"], indent=2))
    print()
    print("STOP FOR REVIEW — DO NOT APPLY WITHOUT EXPLICIT AUTHORIZATION.")
    client.close()


if __name__ == "__main__":
    asyncio.run(_main())


__all__ = ["run"]
