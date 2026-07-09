"""Generic ESPN scoreboard client + parser.

ESPN's free `site.api.espn.com` endpoints are shockingly rich:
  • Fixtures + kickoff times (all sports, weeks out)
  • Team logos, primary/alt colors, abbreviations
  • Recent form strings for team sports (\"LLLWL\")
  • DraftKings odds when the sportsbook has posted markets
  • Fighter records (win-loss-draw) for combat sports
  • Live scoreboards + play-by-play (already used elsewhere in the app)

This module is the *shared* substrate for every ESPN-backed ingest
module (`uefa_espn_ingest`, `ufc_espn_ingest`, future MLB/NFL/NBA
fill-in ingest, live-scoreboard poller, etc.). It centralises:

  1. HTTP client (with sane timeouts + user-agent)
  2. Common parsers (american-odds, de-vig 1X2/OU, fair-price math,
     deterministic pick IDs)
  3. `SportEndpoint` config so each sport is ~10 lines instead of a
     reimplementation.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

logger = logging.getLogger("lockscore.services.espn_common")

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"

# ── generic math ────────────────────────────────────────────────────

def parse_american(s: Any) -> Optional[int]:
    """'+165', '-195', 165 → int; None on failure."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return int(s)
    s = str(s).strip()
    m = re.match(r"^([+-]?)(\d+)$", s)
    if not m:
        return None
    sign = -1 if m.group(1) == "-" else 1
    return sign * int(m.group(2))


def american_to_implied_pct(odds: int) -> float:
    """American odds → implied probability (0–100)."""
    if odds == 0:
        return 50.0
    if odds > 0:
        return round(100.0 / (odds + 100) * 100, 2)
    return round((-odds) / ((-odds) + 100) * 100, 2)


def american_from_prob(prob_pct: float) -> int:
    """probability (0–100) → fair American price."""
    p = max(0.001, min(0.999, prob_pct / 100.0))
    return -round(100 * p / (1 - p)) if p >= 0.5 else round(100 * (1 - p) / p)


def dedvig_1x2(home_ml: int, away_ml: int, draw_ml: int) -> tuple[float, float, float]:
    """De-vig 1X2 moneylines → true probabilities (home, away, draw) 0-100."""
    h = american_to_implied_pct(home_ml) / 100.0
    a = american_to_implied_pct(away_ml) / 100.0
    d = american_to_implied_pct(draw_ml) / 100.0
    total = h + a + d
    if total <= 0:
        return (33.3, 33.3, 33.4)
    return (round(h/total*100, 1), round(a/total*100, 1), round(d/total*100, 1))


def dedvig_pair(pos_ml: int, neg_ml: int) -> tuple[float, float]:
    """De-vig a two-way market (H2H, O/U). Returns (pos_pct, neg_pct)."""
    p = american_to_implied_pct(pos_ml) / 100.0
    n = american_to_implied_pct(neg_ml) / 100.0
    total = p + n
    if total <= 0:
        return (50.0, 50.0)
    return (round(p/total*100, 1), round(n/total*100, 1))


def form_win_share(form: str) -> float:
    """Recency-weighted form (last 5) → normalized [0,1] win share.
    W=1 D=0.5 L=0, exponentially decayed with newest weighted ~3× oldest."""
    if not form:
        return 0.5
    chars = [c for c in form.upper() if c in ("W", "D", "L")][-5:]
    if not chars:
        return 0.5
    n = len(chars)
    weights = [0.7 ** (n - 1 - i) for i in range(n)]
    total_w = sum(weights)
    pts = sum({"W": 1.0, "D": 0.5, "L": 0.0}[c] * w
              for c, w in zip(chars, weights))
    return pts / total_w


def deterministic_pick_id(source: str, event_id: str, market: str, sel: str) -> str:
    """Stable id so re-syncs upsert instead of duplicating."""
    raw = f"{source}|{event_id}|{market}|{sel}".lower()
    h = hashlib.sha256(raw.encode()).hexdigest()[:24]
    return f"{source}-{h}"


def grade_from_conf(conf: float) -> str:
    """Delegates to the canonical grader in sports_engine so all
    ingesters emit the same tier vocabulary."""
    try:
        from sports_engine import _grade as _spec_grade
        return _spec_grade(float(conf))
    except Exception:
        if conf >= 98:
            return "Elite Lock"
        if conf >= 95:
            return "Strong Lock"
        if conf >= 90:
            return "Lock"
        if conf >= 80:
            return "Playable"
        return "Pass"


# ── HTTP client ─────────────────────────────────────────────────────

class ESPNClient:
    """Tiny wrapper so all ESPN calls share one client + backoff.

    ESPN doesn't publish an official rate limit but their WAF starts
    responding with 403 if you slam it. We keep a per-slug 15-min cache
    to keep the request count low.
    """

    def __init__(self, ttl_seconds: int = 15 * 60) -> None:
        self._ttl = ttl_seconds
        self._cache: dict[str, tuple[float, Any]] = {}

    async def _get_json(self, cx: httpx.AsyncClient, path: str,
                        params: Optional[dict] = None) -> dict:
        cache_key = f"{path}?{sorted((params or {}).items())}"
        now = datetime.now(timezone.utc).timestamp()
        cached = self._cache.get(cache_key)
        if cached and now - cached[0] < self._ttl:
            return cached[1]
        try:
            r = await cx.get(f"{ESPN_BASE}/{path}", params=params, timeout=15)
            if r.status_code != 200:
                logger.debug("ESPN %s → %s", path, r.status_code)
                return {}
            data = r.json() or {}
            self._cache[cache_key] = (now, data)
            return data
        except Exception as e:
            logger.warning("ESPN fetch %s failed: %s", path, e)
            return {}

    async def scoreboard(self, cx: httpx.AsyncClient, slug: str,
                         date_yyyymmdd: Optional[str] = None) -> list[dict]:
        params = {"dates": date_yyyymmdd} if date_yyyymmdd else None
        data = await self._get_json(cx, f"{slug}/scoreboard", params)
        return data.get("events") or []

    async def teams(self, cx: httpx.AsyncClient, slug: str) -> list[dict]:
        """League team roster with logos/colors.
        e.g. slug='football/nfl' → all 32 NFL teams w/ meta."""
        data = await self._get_json(cx, f"{slug}/teams")
        sports = data.get("sports") or []
        if not sports:
            return []
        leagues = sports[0].get("leagues") or []
        if not leagues:
            return []
        return [t.get("team") for t in (leagues[0].get("teams") or [])
                if t.get("team")]

    async def team_injuries(self, cx: httpx.AsyncClient, slug: str,
                            team_id: str) -> list[dict]:
        """Injury report for a single team. Available for NFL/NBA/CFB."""
        data = await self._get_json(cx, f"{slug}/teams/{team_id}/injuries")
        return data.get("injuries") or []


espn_client = ESPNClient()


# ── date helpers ────────────────────────────────────────────────────

def date_window_yyyymmdd(days_ahead: int) -> list[str]:
    today = datetime.now(timezone.utc).date()
    return [(today + timedelta(days=i)).strftime("%Y%m%d")
            for i in range(days_ahead + 1)]


# ── competition/event parsing ───────────────────────────────────────

@dataclass
class ParsedOdds:
    moneyline: Optional[dict[str, int]] = None   # {home, away, draw?}
    total: Optional[dict[str, Any]] = None       # {line, over, under}
    spread: Optional[dict[str, Any]] = None      # {line, home_odds, away_odds}
    bookmaker: str = "DraftKings"
    deep_link: Optional[str] = None


@dataclass
class ParsedEvent:
    event_id: str
    home: dict
    away: dict
    kickoff_utc: Optional[str]
    status_state: str          # "pre" | "in" | "post"
    odds: ParsedOdds
    league_label: str
    sport_key: str
    raw: dict


def parse_scoreboard_event(ev: dict, league_label: str, sport_key: str,
                            include_draw: bool = True) -> Optional[ParsedEvent]:
    """Normalize an ESPN event into a `ParsedEvent`. Works for team
    sports (soccer, NFL, NBA, MLB, NHL, CFB) and individual sports
    where competitors are athletes (UFC, tennis, golf).

    include_draw=False disables draw parsing (UFC, tennis).
    """
    try:
        ev_id = str(ev.get("id") or "")
        if not ev_id:
            return None
        kickoff = ev.get("date")
        comp = (ev.get("competitions") or [{}])[0]
        competitors = comp.get("competitors") or []
        if len(competitors) < 2:
            return None

        def _parse_side(c: dict) -> Optional[dict]:
            team = c.get("team") or c.get("athlete") or {}
            name = (team.get("displayName") or team.get("name")
                    or team.get("fullName") or "").strip()
            if not name:
                return None
            record = ""
            for r in c.get("records") or []:
                if r.get("type") == "total" or r.get("name") == "overall":
                    record = r.get("summary") or ""
                    break
            return {
                "name": name,
                "abbrev": team.get("abbreviation"),
                "logo": team.get("logo") or (team.get("flag") or {}).get("href"),
                "color": team.get("color"),
                "alt_color": team.get("alternateColor"),
                "team_id": team.get("id"),
                "form": (c.get("form") or "").upper(),
                "record": record,
            }

        home = away = None
        for c in competitors:
            side = (c.get("homeAway") or "").lower()
            info = _parse_side(c)
            if not info:
                continue
            # For combat sports there's no home/away — use order.
            if side == "home":
                home = info
            elif side == "away":
                away = info
        if not home or not away:
            # Fallback: use first two competitors as home/away
            infos = [_parse_side(c) for c in competitors]
            infos = [i for i in infos if i]
            if len(infos) >= 2:
                home = home or infos[0]
                away = away or infos[1]
            if not home or not away:
                return None

        status = ((comp.get("status") or {}).get("type") or {}) or {}
        status_state = (status.get("state") or "pre").lower()

        # Parse odds — take first non-null provider (DraftKings priority=1)
        parsed_odds = ParsedOdds()
        for o in comp.get("odds") or []:
            if not o:
                continue
            ml = o.get("moneyline") or {}

            def close_odds(side: str) -> Optional[int]:
                return parse_american(
                    ((ml.get(side) or {}).get("close") or {}).get("odds")
                )

            if include_draw:
                if ml.get("home") and ml.get("away") and ml.get("draw"):
                    h_ml = close_odds("home")
                    a_ml = close_odds("away")
                    d_ml = close_odds("draw")
                    if h_ml is not None and a_ml is not None and d_ml is not None:
                        parsed_odds.moneyline = {
                            "home": h_ml, "away": a_ml, "draw": d_ml,
                        }
            else:
                if ml.get("home") and ml.get("away"):
                    h_ml = close_odds("home")
                    a_ml = close_odds("away")
                    if h_ml is not None and a_ml is not None:
                        parsed_odds.moneyline = {
                            "home": h_ml, "away": a_ml,
                        }

            # Total O/U
            tot = o.get("total") or {}
            over_odds = parse_american(
                ((tot.get("over") or {}).get("close") or {}).get("odds"))
            under_odds = parse_american(
                ((tot.get("under") or {}).get("close") or {}).get("odds"))
            line_str = ((tot.get("over") or {}).get("close") or {}).get("line") or ""
            line_m = re.search(r"([0-9]+(?:\.[0-9]+)?)", str(line_str))
            if line_m and over_odds is not None and under_odds is not None:
                parsed_odds.total = {
                    "line": float(line_m.group(1)),
                    "over": over_odds,
                    "under": under_odds,
                }

            # Spread
            sp = o.get("pointSpread") or {}
            sp_home_line = ((sp.get("home") or {}).get("close") or {}).get("line")
            sp_home_odds = parse_american(
                ((sp.get("home") or {}).get("close") or {}).get("odds"))
            sp_away_odds = parse_american(
                ((sp.get("away") or {}).get("close") or {}).get("odds"))
            if sp_home_line and sp_home_odds is not None and sp_away_odds is not None:
                sp_m = re.search(r"([+-]?[0-9]+(?:\.[0-9]+)?)", str(sp_home_line))
                if sp_m:
                    parsed_odds.spread = {
                        "line_home": float(sp_m.group(1)),
                        "home_odds": sp_home_odds,
                        "away_odds": sp_away_odds,
                    }

            parsed_odds.bookmaker = (o.get("provider") or {}).get("displayName") \
                                    or (o.get("provider") or {}).get("name") \
                                    or "DraftKings"
            parsed_odds.deep_link = (o.get("link") or {}).get("href")
            break  # first provider wins

        return ParsedEvent(
            event_id=ev_id,
            home=home,
            away=away,
            kickoff_utc=kickoff,
            status_state=status_state,
            odds=parsed_odds,
            league_label=league_label,
            sport_key=sport_key,
            raw=ev,
        )
    except Exception as e:
        logger.warning("parse_scoreboard_event failed: %s", e)
        return None


# ── multi-slug fetch ────────────────────────────────────────────────

async def fetch_slate_multi(
    slug_configs: list[tuple[str, str, str]],
    days_ahead: int = 7,
    include_draw: bool = True,
) -> list[ParsedEvent]:
    """Fetch scoreboards across (slug, league_label, sport_key) tuples
    for today + N days ahead. Returns deduped list of parsed events.

    Concurrent-safe; uses `espn_client` shared cache.
    """
    dates = date_window_yyyymmdd(days_ahead)
    async with httpx.AsyncClient(headers={"User-Agent": "PerkLocks/1.0"}) as cx:
        tasks = []
        for slug, _label, _sk in slug_configs:
            for d in dates:
                tasks.append(espn_client.scoreboard(cx, slug, d))
        results = await asyncio.gather(*tasks, return_exceptions=True)

    parsed: list[ParsedEvent] = []
    ti = 0
    for slug, label, sport_key in slug_configs:
        for _d in dates:
            res = results[ti]
            ti += 1
            if isinstance(res, Exception) or not res:
                continue
            for ev in res:
                pe = parse_scoreboard_event(
                    ev, label, sport_key, include_draw=include_draw)
                if pe and pe.status_state == "pre":
                    parsed.append(pe)

    # Dedup by event_id
    seen: set[str] = set()
    unique: list[ParsedEvent] = []
    for pe in parsed:
        if pe.event_id in seen:
            continue
        seen.add(pe.event_id)
        unique.append(pe)
    logger.info("ESPN slate: %d unique across %d slugs × %d days",
                len(unique), len(slug_configs), len(dates))
    return unique
