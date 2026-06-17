"""Sportsbook Mapping Engine.

Builds a *sportsbook-independent* selection object for every pick and a
per-book mapping with the deepest URL we can reach without a partner API
key. The mapping is attached to the pick at generation time, so the
frontend's "Open in FanDuel / DraftKings / BetMGM …" button can land the
user as close to the actual bet as we can get.

DATA MODEL ───────────────────────────────────────────────────────────────

Every enriched pick gets two new fields:

``selection_v2`` — the canonical, book-agnostic representation of the bet
    {
      "league":        "soccer_epl",                  # normalised league key
      "league_label":  "Premier League",              # display label
      "sport":         "Soccer",
      "event": {
        "home":      "Manchester City",
        "away":      "Liverpool",
        "kickoff":   "2026-06-18T19:30:00Z",          # ISO-8601 UTC
        "date":      "20260618",                      # YYYYMMDD
        "slug":      "soccer_liverpool_city_20260618",
      },
      "market": {
        "family":   "moneyline" | "spread" | "totals" | "player_prop"
                    | "btts" | "draw_no_bet" | "double_chance" | "method"
                    | "first_half" | "other",
        "subtype":  "match_winner" | "anytime_scorer" | "to_record_hit"
                    | "ko_tko" | "spread_run_line" | ...
        "label":    "Anytime Goal Scorer",            # human readable
      },
      "selection": {
        "side":     "home" | "away" | "draw" | "over" | "under"
                    | "yes" | "no" | "player" | "team" | None,
        "team":     "Manchester City" | None,
        "player":   "Erling Haaland" | None,
        "line":     -1.5 | 2.5 | None,
        "label":    "Erling Haaland (Anytime Goal Scorer)",
      },
    }

``sportsbook_mapping`` — best-effort URLs per book
    {
      "FanDuel": {
        "supports_deep_link":  False,           # True once we have partner API
        "deep_link":           None,
        "event_url":           "https://...",   # event-level (one tap to bet)
        "search_url":          "https://...",   # search-redirect
        "league_url":          "https://...",   # league landing page
        "home_url":            "https://...",   # sportsbook home
        "best_link":           "<the deepest available>",
        "best_depth":          "search" | "event" | "league" | "home",
        "search_query":        "Erling Haaland Anytime Goal Scorer",
      },
      "DraftKings": { ... },
      "BetMGM":     { ... },
      "Caesars":    { ... },
      "ESPNBet":    { ... },
    }

REGION ───────────────────────────────────────────────────────────────────

URL templates are US-focused (the books we list above are US sportsbooks).
KBO links land on the generic baseball page since none of the US books
prominently feature KBO — the user still gets the right sport.

USER RESPONSIBILITY ──────────────────────────────────────────────────────

Per the spec: we never auto-submit a bet. The user is responsible for the
final confirmation inside the sportsbook app. We just shorten the path.
"""
from __future__ import annotations

import logging
import re
import urllib.parse as _url
from typing import Any, Optional

from event_matcher import build_event_slug, extract_teams, normalize_team

logger = logging.getLogger("lockscore.sportsbook_mapper")


# ──────────────────────────────────────────────────────────────────────
# League normalization
# ──────────────────────────────────────────────────────────────────────
# Map (sport, league_label) → canonical key used by selection_v2.league.

_LEAGUE_KEY = {
    ("NBA", "NBA"):                     ("nba",            "NBA"),
    ("WNBA", "WNBA"):                   ("wnba",           "WNBA"),
    ("MLB", "MLB"):                     ("mlb",            "MLB"),
    ("NFL", "NFL"):                     ("nfl",            "NFL"),
    ("NHL", "NHL"):                     ("nhl",            "NHL"),
    ("UFC", "UFC"):                     ("ufc",            "UFC"),
    ("Tennis", "ATP"):                  ("tennis_atp",     "ATP"),
    ("Tennis", "WTA"):                  ("tennis_wta",     "WTA"),
    ("KBO", "KBO"):                     ("baseball_kbo",   "KBO"),
}


def _league_key(sport: str, league: str) -> tuple[str, str]:
    """Return (canonical_key, display_label)."""
    if not sport:
        return ("other", league or "")
    key = _LEAGUE_KEY.get((sport, league))
    if key:
        return key
    # Soccer fan-out: many leagues share the same sport label.
    if sport == "Soccer":
        league_l = (league or "").lower()
        if "premier league" in league_l or "epl" in league_l:
            return ("soccer_epl", "Premier League")
        if "la liga" in league_l or "laliga" in league_l:
            return ("soccer_laliga", "La Liga")
        if "serie a" in league_l:
            return ("soccer_seriea", "Serie A")
        if "bundesliga" in league_l:
            return ("soccer_bundesliga", "Bundesliga")
        if "ligue 1" in league_l:
            return ("soccer_ligue1", "Ligue 1")
        if "uefa champions" in league_l or "ucl" in league_l:
            return ("soccer_ucl", "UEFA Champions League")
        if "mls" in league_l:
            return ("soccer_mls", "MLS")
        if "world cup" in league_l:
            return ("soccer_world_cup", "World Cup")
        if "copa" in league_l:
            return ("soccer_copa", "Copa América")
        if "euro" in league_l:
            return ("soccer_euro", "Euro Championship")
        return ("soccer_other", league or "Soccer")
    return (sport.lower(), league or sport)


def _league_from_pick(sport: str, league_raw: str) -> tuple[str, str]:
    """Public wrapper that also handles WTA / ATP variants like "WTA Queen's Club"."""
    if sport == "Tennis":
        l = (league_raw or "").lower()
        if l.startswith("wta") or "women" in l:
            return ("tennis_wta", "WTA")
        if l.startswith("atp") or "men" in l:
            return ("tennis_atp", "ATP")
    return _league_key(sport, league_raw)


# ──────────────────────────────────────────────────────────────────────
# Market family classifier
# ──────────────────────────────────────────────────────────────────────

_LINE_RE = re.compile(r"(-?\d+(?:\.\d+)?)")


def _extract_line(text: str) -> Optional[float]:
    """Pull the first signed decimal/integer out of a market/selection string."""
    if not text:
        return None
    m = _LINE_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _classify_market(sport: str, market: str, selection: str) -> tuple[str, str, str]:
    """Return (family, subtype, side).

    family   — coarse bucket used for routing / URL construction
    subtype  — finer-grained machine key
    side     — home/away/over/under/yes/no/draw/player/team or empty
    """
    m = (market or "").lower()
    s = (selection or "").lower()

    # Player props — checked first because they tend to have team names too.
    if any(k in m for k in (
        "anytime goal scorer", "first goal scorer", "to score or assist",
        "shots on target", "player to record",
        "to record a hit", "hits", "total bases", "home run",
        "strikeout", "outs recorded",
        "points", "rebounds", "assists", "threes", "blocks", "steals",
        "method of victory", "by ko", "by submission", "by decision",
    )):
        # Method-of-victory is its own family
        if "method" in m or "by ko" in m or "by submission" in m or "by decision" in m:
            return ("method", _subtype_for_method(m), "player")
        return ("player_prop", _subtype_for_prop(m), "player")

    # BTTS / Both teams to score
    if "both teams to score" in m or m.startswith("btts"):
        return ("btts", "btts", "yes" if "yes" in s else "no")

    # Draw No Bet
    if "draw no bet" in m or "dnb" in m:
        return ("draw_no_bet", "dnb", "team")

    # Double Chance / Win or Draw
    if "double chance" in m or "win or draw" in m:
        return ("double_chance", "win_or_draw", "team")

    # Moneyline / Match Winner
    if "moneyline" in m or "money line" in m or "match winner" in m or "to win" in m:
        return ("moneyline", "match_winner", "team")

    # Spread / Run Line / Puck Line / Point Spread
    if "run line" in m or "runline" in m:
        return ("spread", "spread_run_line", "team")
    if "puck line" in m:
        return ("spread", "spread_puck_line", "team")
    if "spread" in m or "handicap" in m:
        return ("spread", "spread_points", "team")

    # Totals
    if "total" in m or "over/under" in m or "o/u" in m:
        side = "over" if "over" in s or "over" in m else ("under" if "under" in s or "under" in m else "")
        return ("totals", "game_total", side)

    # First-half / period markets
    if "1st half" in m or "first half" in m or "half time" in m:
        if "moneyline" in m or "winner" in m:
            return ("first_half", "h1_winner", "team")
        if "total" in m:
            return ("first_half", "h1_total", "over" if "over" in s else "under")
        return ("first_half", "h1_other", "")

    # Tennis sets / games
    if "set" in m and ("over" in m or "under" in m or "winner" in m):
        return ("totals", "sets", "over" if "over" in s else "under")
    if "games" in m and ("over" in m or "under" in m):
        return ("totals", "games", "over" if "over" in s else "under")

    return ("other", "other", "")


def _subtype_for_prop(market_l: str) -> str:
    if "anytime goal" in market_l:                return "anytime_scorer"
    if "first goal" in market_l:                  return "first_scorer"
    if "to score or assist" in market_l:          return "score_or_assist"
    if "shots on target" in market_l:             return "shots_on_target"
    if "hits" in market_l:                        return "to_record_hit"
    if "total bases" in market_l:                 return "total_bases"
    if "home run" in market_l:                    return "home_run"
    if "strikeout" in market_l:                   return "strikeouts"
    if "outs recorded" in market_l:               return "outs_recorded"
    if "rebounds" in market_l:                    return "rebounds"
    if "assists" in market_l:                     return "assists"
    if "points" in market_l:                      return "points"
    if "threes" in market_l:                      return "threes"
    if "blocks" in market_l:                      return "blocks"
    if "steals" in market_l:                      return "steals"
    return "prop_other"


def _subtype_for_method(market_l: str) -> str:
    if "ko" in market_l:           return "ko_tko"
    if "submission" in market_l:   return "submission"
    if "decision" in market_l:     return "decision"
    return "method_other"


_PLAYER_PROP_KEYWORDS = (
    "anytime goal scorer", "first goal scorer", "to score or assist",
    "shots on target", "to record",
    "hits", "total bases", "home run", "rbi", "strikeouts", "outs recorded",
    "points", "rebounds", "assists", "threes", "blocks", "steals", "pra",
    "method of victory",
)
# Strip a "(TEAM)" suffix like "Wilyer Abreu (BOS)" → "Wilyer Abreu".
_TEAM_PAREN_RE = re.compile(r"\s*\([A-Z0-9]{2,4}\)\s*")
# Strip the side+stat tail like "Over 0.5 Hits" / "Anytime Goal Scorer".
_PROP_TAIL_RE = re.compile(
    r"\s+(over|under|anytime|first|to)\b.*$",
    re.IGNORECASE,
)


def _extract_player_from_market(market: str) -> Optional[str]:
    """Best-effort: pull "<Player Name>" out of a player-prop market label.

    Examples:
      • "Wilyer Abreu (BOS) Over 0.5 Hits"    → "Wilyer Abreu"
      • "Erling Haaland Anytime Goal Scorer"  → "Erling Haaland"
      • "Aaron Judge Over 1.5 Total Bases"    → "Aaron Judge"
    Returns None if the market doesn't look like a player prop.
    """
    if not market:
        return None
    m_l = market.lower()
    if not any(k in m_l for k in _PLAYER_PROP_KEYWORDS):
        return None
    # Remove "(TEAM)" suffix
    cleaned = _TEAM_PAREN_RE.sub(" ", market).strip()
    # Drop the side/stat tail
    cleaned = _PROP_TAIL_RE.sub("", cleaned).strip()
    # Whatever remains is the player name. Sanity-check: ≥ 2 word tokens.
    tokens = cleaned.split()
    if len(tokens) < 2 or len(tokens) > 5:
        return None
    return cleaned or None


def _split_pick_side(pick: dict, home_team: str = "", away_team: str = "") -> tuple[Optional[str], Optional[str]]:
    """Try to identify (team, player) from the pick.

    Heuristics:
      • If `pick.player_name` exists → that's the player.
      • If market starts with a team name (e.g. "Lakers Moneyline") → team.
      • Otherwise empty.
    """
    player = pick.get("player_name") or pick.get("player")
    market = pick.get("market") or ""
    if not player:
        player = _extract_player_from_market(market)
    home = home_team or pick.get("home_team") or ""
    away = away_team or pick.get("away_team") or ""
    selection = pick.get("selection") or ""
    team = None
    if player:
        player = str(player).strip()
    # Look for "<Team> Moneyline / Spread / Run Line" pattern
    for cand in (home, away):
        if cand:
            last_token = cand.split()[-1].lower()
            full = cand.lower()
            if last_token in market.lower() or full in market.lower():
                team = cand
                break
    # Selection sometimes carries team name
    if not team:
        for cand in (home, away):
            if cand:
                last_token = cand.split()[-1].lower()
                full = cand.lower()
                if last_token in selection.lower() or full in selection.lower():
                    team = cand
                    break
    return team, player


# ──────────────────────────────────────────────────────────────────────
# Build selection_v2
# ──────────────────────────────────────────────────────────────────────

def build_selection_v2(pick: dict) -> dict:
    """Return the canonical, book-agnostic selection object for a pick."""
    sport = pick.get("sport") or ""
    league_raw = pick.get("league") or sport
    league_key, league_label = _league_from_pick(sport, league_raw)

    home_team = pick.get("home_team") or ""
    away_team = pick.get("away_team") or ""
    if not home_team or not away_team:
        h, a = extract_teams(pick.get("event") or "")
        home_team = home_team or h
        away_team = away_team or a

    market_str = pick.get("market") or ""
    selection_str = pick.get("selection") or ""
    family, subtype, side = _classify_market(sport, market_str, selection_str)

    team, player = _split_pick_side(pick, home_team, away_team)
    line = _extract_line(selection_str) if any(k in selection_str.lower() for k in ("over", "under")) else _extract_line(market_str)

    event_time = pick.get("event_time") or pick.get("commence_time") or ""
    date_str = ""
    if event_time and isinstance(event_time, str):
        try:
            date_str = event_time.split("T")[0].replace("-", "")
        except Exception:
            date_str = ""

    return {
        "league":       league_key,
        "league_label": league_label,
        "sport":        sport,
        "event": {
            "home":    home_team,
            "away":    away_team,
            "kickoff": event_time,
            "date":    date_str,
            "slug":    build_event_slug(sport, home_team, away_team, event_time),
        },
        "market": {
            "family":  family,
            "subtype": subtype,
            "label":   market_str,
        },
        "selection": {
            "side":   side or None,
            "team":   team,
            "player": player,
            "line":   line,
            "label":  selection_str or market_str,
        },
    }


# ──────────────────────────────────────────────────────────────────────
# Per-book URL templates
# ──────────────────────────────────────────────────────────────────────
# Top-level sport landing pages (`league_url` fallback).
# Plus a "search" endpoint that accepts a free-text query — empirically the
# most reliable way to land users on the matching event in the absence of
# partner API access.

_BOOK_CONFIG = {
    "FanDuel": {
        "home":   "https://sportsbook.fanduel.com/",
        "search": "https://sportsbook.fanduel.com/search?query={q}",
        "league": {
            "nba":             "https://sportsbook.fanduel.com/navigation/nba",
            "wnba":            "https://sportsbook.fanduel.com/navigation/wnba",
            "mlb":             "https://sportsbook.fanduel.com/navigation/mlb",
            "nfl":             "https://sportsbook.fanduel.com/navigation/nfl",
            "nhl":             "https://sportsbook.fanduel.com/navigation/nhl",
            "ufc":             "https://sportsbook.fanduel.com/navigation/mma",
            "tennis_atp":      "https://sportsbook.fanduel.com/navigation/tennis",
            "tennis_wta":      "https://sportsbook.fanduel.com/navigation/tennis",
            "soccer_epl":      "https://sportsbook.fanduel.com/navigation/soccer",
            "soccer_laliga":   "https://sportsbook.fanduel.com/navigation/soccer",
            "soccer_seriea":   "https://sportsbook.fanduel.com/navigation/soccer",
            "soccer_bundesliga": "https://sportsbook.fanduel.com/navigation/soccer",
            "soccer_ligue1":   "https://sportsbook.fanduel.com/navigation/soccer",
            "soccer_ucl":      "https://sportsbook.fanduel.com/navigation/soccer",
            "soccer_mls":      "https://sportsbook.fanduel.com/navigation/soccer",
            "soccer_other":    "https://sportsbook.fanduel.com/navigation/soccer",
            "baseball_kbo":    "https://sportsbook.fanduel.com/navigation/baseball",
        },
    },
    "DraftKings": {
        "home":   "https://sportsbook.draftkings.com/",
        "search": "https://sportsbook.draftkings.com/search?searchString={q}",
        "league": {
            "nba":             "https://sportsbook.draftkings.com/leagues/basketball/nba",
            "wnba":            "https://sportsbook.draftkings.com/leagues/basketball/wnba",
            "mlb":             "https://sportsbook.draftkings.com/leagues/baseball/mlb",
            "nfl":             "https://sportsbook.draftkings.com/leagues/football/nfl",
            "nhl":             "https://sportsbook.draftkings.com/leagues/hockey/nhl",
            "ufc":             "https://sportsbook.draftkings.com/leagues/mma/ufc",
            "tennis_atp":      "https://sportsbook.draftkings.com/leagues/tennis/atp",
            "tennis_wta":      "https://sportsbook.draftkings.com/leagues/tennis/wta",
            "soccer_epl":      "https://sportsbook.draftkings.com/leagues/soccer/english-premier-league",
            "soccer_laliga":   "https://sportsbook.draftkings.com/leagues/soccer/spain---la-liga",
            "soccer_seriea":   "https://sportsbook.draftkings.com/leagues/soccer/italy---serie-a",
            "soccer_bundesliga": "https://sportsbook.draftkings.com/leagues/soccer/germany---bundesliga",
            "soccer_ligue1":   "https://sportsbook.draftkings.com/leagues/soccer/france---ligue-1",
            "soccer_ucl":      "https://sportsbook.draftkings.com/leagues/soccer/uefa-champions-league",
            "soccer_mls":      "https://sportsbook.draftkings.com/leagues/soccer/usa---mls",
            "soccer_other":    "https://sportsbook.draftkings.com/leagues/soccer",
            "baseball_kbo":    "https://sportsbook.draftkings.com/leagues/baseball",
        },
    },
    "BetMGM": {
        "home":   "https://sports.betmgm.com/en",
        # BetMGM uses a different search endpoint; this one is the reliable mobile one
        "search": "https://sports.betmgm.com/en/sports/search?search={q}",
        "league": {
            "nba":             "https://sports.betmgm.com/en/sports/basketball-7/betting/usa-9/nba-6004",
            "wnba":            "https://sports.betmgm.com/en/sports/basketball-7/betting/usa-9/wnba-6066",
            "mlb":             "https://sports.betmgm.com/en/sports/baseball-23/betting/usa-9/mlb-75",
            "nfl":             "https://sports.betmgm.com/en/sports/football-11/betting/usa-9/nfl-35",
            "nhl":             "https://sports.betmgm.com/en/sports/hockey-12/betting/usa-9/nhl-34",
            "ufc":             "https://sports.betmgm.com/en/sports/mma-15/betting/world-5/ufc-1",
            "tennis_atp":      "https://sports.betmgm.com/en/sports/tennis-5",
            "tennis_wta":      "https://sports.betmgm.com/en/sports/tennis-5",
            "soccer_epl":      "https://sports.betmgm.com/en/sports/soccer-4/betting/england-14/premier-league-102841",
            "soccer_laliga":   "https://sports.betmgm.com/en/sports/soccer-4/betting/spain-50",
            "soccer_seriea":   "https://sports.betmgm.com/en/sports/soccer-4/betting/italy-26",
            "soccer_bundesliga": "https://sports.betmgm.com/en/sports/soccer-4/betting/germany-21",
            "soccer_ligue1":   "https://sports.betmgm.com/en/sports/soccer-4/betting/france-19",
            "soccer_ucl":      "https://sports.betmgm.com/en/sports/soccer-4",
            "soccer_mls":      "https://sports.betmgm.com/en/sports/soccer-4/betting/usa-9",
            "soccer_other":    "https://sports.betmgm.com/en/sports/soccer-4",
            "baseball_kbo":    "https://sports.betmgm.com/en/sports/baseball-23",
        },
    },
    "Caesars": {
        "home":   "https://www.caesars.com/sportsbook-and-casino",
        "search": "https://www.caesars.com/sportsbook-and-casino/search?q={q}",
        "league": {
            "nba":             "https://www.caesars.com/sportsbook-and-casino/sports/basketball/events/nba",
            "wnba":            "https://www.caesars.com/sportsbook-and-casino/sports/basketball/events/wnba",
            "mlb":             "https://www.caesars.com/sportsbook-and-casino/sports/baseball/events/mlb",
            "nfl":             "https://www.caesars.com/sportsbook-and-casino/sports/football/events/nfl",
            "nhl":             "https://www.caesars.com/sportsbook-and-casino/sports/hockey/events/nhl",
            "ufc":             "https://www.caesars.com/sportsbook-and-casino/sports/mma/events/ufc",
            "tennis_atp":      "https://www.caesars.com/sportsbook-and-casino/sports/tennis",
            "tennis_wta":      "https://www.caesars.com/sportsbook-and-casino/sports/tennis",
            "soccer_epl":      "https://www.caesars.com/sportsbook-and-casino/sports/soccer/events/premier-league",
            "soccer_laliga":   "https://www.caesars.com/sportsbook-and-casino/sports/soccer/events/la-liga",
            "soccer_seriea":   "https://www.caesars.com/sportsbook-and-casino/sports/soccer/events/serie-a",
            "soccer_bundesliga": "https://www.caesars.com/sportsbook-and-casino/sports/soccer/events/bundesliga",
            "soccer_ligue1":   "https://www.caesars.com/sportsbook-and-casino/sports/soccer/events/ligue-1",
            "soccer_ucl":      "https://www.caesars.com/sportsbook-and-casino/sports/soccer/events/uefa-champions-league",
            "soccer_mls":      "https://www.caesars.com/sportsbook-and-casino/sports/soccer/events/mls",
            "soccer_other":    "https://www.caesars.com/sportsbook-and-casino/sports/soccer",
            "baseball_kbo":    "https://www.caesars.com/sportsbook-and-casino/sports/baseball",
        },
    },
    "ESPNBet": {
        "home":   "https://espnbet.com/",
        "search": "https://espnbet.com/search?searchTerm={q}",
        "league": {
            "nba":             "https://espnbet.com/sport/basketball/organization/united-states/competition/nba",
            "wnba":            "https://espnbet.com/sport/basketball/organization/united-states/competition/wnba",
            "mlb":             "https://espnbet.com/sport/baseball/organization/united-states/competition/mlb",
            "nfl":             "https://espnbet.com/sport/football/organization/united-states/competition/nfl",
            "nhl":             "https://espnbet.com/sport/hockey/organization/united-states/competition/nhl",
            "ufc":             "https://espnbet.com/sport/mma/organization/united-states/competition/ufc",
            "tennis_atp":      "https://espnbet.com/sport/tennis",
            "tennis_wta":      "https://espnbet.com/sport/tennis",
            "soccer_epl":      "https://espnbet.com/sport/soccer/organization/england/competition/premier-league",
            "soccer_laliga":   "https://espnbet.com/sport/soccer",
            "soccer_seriea":   "https://espnbet.com/sport/soccer",
            "soccer_bundesliga": "https://espnbet.com/sport/soccer",
            "soccer_ligue1":   "https://espnbet.com/sport/soccer",
            "soccer_ucl":      "https://espnbet.com/sport/soccer",
            "soccer_mls":      "https://espnbet.com/sport/soccer",
            "soccer_other":    "https://espnbet.com/sport/soccer",
            "baseball_kbo":    "https://espnbet.com/sport/baseball",
        },
    },
}

SUPPORTED_BOOKS = tuple(_BOOK_CONFIG.keys())


# ──────────────────────────────────────────────────────────────────────
# Search query builder — the deepest practical link we can produce.
# ──────────────────────────────────────────────────────────────────────

def selection_search_query(sel: dict) -> str:
    """Build the most informative search string for a selection.

    Heuristics:
      • Player props → "<Player> <stat-tail>"  e.g. "Erling Haaland Anytime Goal Scorer"
        (the player name is stripped from the market label so we don't duplicate it).
      • Game markets → "<Away> <Home>"           e.g. "Lakers Warriors"
      • Falls back to event_label or league_label so we never return empty.
    """
    market = sel.get("market", {}) or {}
    selection = sel.get("selection", {}) or {}
    event = sel.get("event", {}) or {}

    player = selection.get("player")
    if player:
        market_label = market.get("label") or ""
        # Strip the "(TEAM)" suffix and the player name from the front of the
        # market label to get just the stat tail.
        tail = _TEAM_PAREN_RE.sub(" ", market_label).strip()
        if tail.lower().startswith(player.lower()):
            tail = tail[len(player):].strip()
        if not tail:
            tail = _humanize_subtype(market.get("subtype") or "")
        return f"{player} {tail}".strip()

    away, home = event.get("away") or "", event.get("home") or ""
    if home and away:
        return f"{away} {home}".strip()
    if home:
        return home
    if away:
        return away
    return sel.get("league_label") or sel.get("sport") or ""


def _humanize_subtype(s: str) -> str:
    return s.replace("_", " ").title()


# ──────────────────────────────────────────────────────────────────────
# Build per-book mapping (one entry per supported book)
# ──────────────────────────────────────────────────────────────────────

def _book_league_url(book: str, league_key: str) -> str:
    cfg = _BOOK_CONFIG.get(book) or {}
    league_table = cfg.get("league") or {}
    return league_table.get(league_key) or cfg.get("home") or ""


def _book_search_url(book: str, query: str) -> str:
    cfg = _BOOK_CONFIG.get(book) or {}
    template = cfg.get("search")
    if not template or not query:
        return ""
    return template.replace("{q}", _url.quote_plus(query))


def _book_home(book: str) -> str:
    return (_BOOK_CONFIG.get(book) or {}).get("home", "")


def build_sportsbook_mapping(selection: dict, partner_event_ids: Optional[dict] = None) -> dict:
    """Build the per-book URL bundle.

    ``partner_event_ids``  — optional dict of {book: {event_id, market_id,
    selection_id, deep_link}} captured from a partner API. When present,
    the mapping records ``supports_deep_link=True`` and ``best_depth='selection'``.
    """
    league_key = selection.get("league", "other")
    query = selection_search_query(selection)
    out: dict[str, dict] = {}
    for book in SUPPORTED_BOOKS:
        league_url = _book_league_url(book, league_key)
        search_url = _book_search_url(book, query)
        home_url = _book_home(book)
        deep = (partner_event_ids or {}).get(book) or {}
        deep_link = deep.get("deep_link")
        event_url = deep.get("event_url") or ""

        # Pick the deepest available link.
        if deep_link:
            best, depth = deep_link, "selection"
        elif event_url:
            best, depth = event_url, "event"
        elif search_url:
            best, depth = search_url, "search"
        elif league_url:
            best, depth = league_url, "league"
        else:
            best, depth = home_url, "home"

        out[book] = {
            "supports_deep_link": bool(deep_link),
            "deep_link":          deep_link,
            "event_id":           deep.get("event_id"),
            "market_id":          deep.get("market_id"),
            "selection_id":       deep.get("selection_id"),
            "event_url":          event_url or None,
            "search_url":         search_url or None,
            "league_url":         league_url or None,
            "home_url":           home_url,
            "best_link":          best,
            "best_depth":         depth,
            "search_query":       query,
        }
    return out


# ──────────────────────────────────────────────────────────────────────
# PUBLIC: enrich a pick (single or batch)
# ──────────────────────────────────────────────────────────────────────

def enrich_pick_with_mapping(pick: dict) -> dict:
    """Attach ``selection_v2`` + ``sportsbook_mapping`` to a pick (in place)."""
    try:
        selection = build_selection_v2(pick)
        pick["selection_v2"] = selection
        # Partner event IDs aren't available without an affiliate deal; pass
        # ``None`` so the mapping falls through to search/league/home depth.
        pick["sportsbook_mapping"] = build_sportsbook_mapping(selection, None)
    except Exception as e:                # pragma: no cover — defensive
        logger.warning("sportsbook mapping failed for pick %s: %s",
                       pick.get("id") or pick.get("event"), e)
        pick.setdefault("selection_v2", None)
        pick.setdefault("sportsbook_mapping", {})
    return pick


def enrich_picks_with_mapping(picks: list[dict]) -> list[dict]:
    for p in picks:
        enrich_pick_with_mapping(p)
    return picks


# ──────────────────────────────────────────────────────────────────────
# Frontend helper export: minimal subset for the UI layer (kept tiny)
# ──────────────────────────────────────────────────────────────────────

def mapping_summary(pick: dict, book: str) -> dict:
    """Return the smallest payload the UI needs to open a sportsbook for this pick.

    Used by the optional ``GET /api/picks/{id}/sportsbook/{book}`` route.
    """
    mp = (pick.get("sportsbook_mapping") or {}).get(book) or {}
    return {
        "book":           book,
        "best_link":      mp.get("best_link"),
        "best_depth":     mp.get("best_depth"),
        "search_query":   mp.get("search_query"),
        "supports_deep_link": bool(mp.get("supports_deep_link")),
    }
