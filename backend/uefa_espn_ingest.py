"""ESPN-backed UEFA fixture ingest.

**Why this module exists**

The Odds API (our primary source) doesn't populate the UEFA
qualification rounds — the sport keys `soccer_uefa_champs_league_qualification`,
`soccer_uefa_europa_league`, and `soccer_uefa_europa_conference_league`
return `[]` events even the day before matches kick off. football-data.org
also gates Europa League behind TIER_TWO (403) and doesn't carry the
Conference League at all (404).

Linemate/DraftKings clearly *do* show these matches. So does ESPN's
free scoreboard API — with full DraftKings pricing on ML / Spread /
Total baked into the response.

This module bridges the gap by:
  1. Pulling scoreboards for CL, EL, ECL (each with qualification +
     group + knockout variants) from ESPN's public API — no key.
  2. Parsing the embedded DraftKings odds into moneyline / total picks
     with real book prices.
  3. Upserting into `picks` collection so the fixtures show on the
     app board alongside the football-data-backed leagues.

Deduplication: we skip the upsert if a pick with the same
`event_time + selection` already exists (usually created by the
football-data pipeline when CL group stage is active). ESPN is a
fallback layer, not a duplicator.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

logger = logging.getLogger("lockscore.uefa_espn")

_ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"

# ESPN slug → (league label shown on the pick card, sport-key for
# any downstream engine that keys off the Odds API name).
UEFA_COMPETITIONS: list[tuple[str, str, str]] = [
    # Champions League tree
    ("uefa.champions",       "UEFA Champions League",              "soccer_uefa_champs_league"),
    ("uefa.champions_qual",  "Champions League Qualification",     "soccer_uefa_champs_league_qualification"),
    # Europa League tree
    ("uefa.europa",          "UEFA Europa League",                 "soccer_uefa_europa_league"),
    ("uefa.europa_qual",     "Europa League Qualification",        "soccer_uefa_europa_league"),
    # Conference League tree
    ("uefa.europa.conf",     "UEFA Conference League",             "soccer_uefa_europa_conference_league"),
    ("uefa.europa.conf_qual","Conference League Qualification",    "soccer_uefa_europa_conference_league"),
    # National-team competitions that also get low Odds API coverage
    ("uefa.nations",         "UEFA Nations League",                "soccer_uefa_nations_league"),
]

# Confidence floor for surfacing an ML pick to the board. Below this we
# still ingest the fixture but skip generating a lock pick (fixtures
# still appear because the frontend fetches by event, not by pick doc).
_ML_CONFIDENCE_FLOOR = 50.0

_UEFA_SOURCE_TAG = "uefa_espn_v1"


# ── helpers ────────────────────────────────────────────────────────

def _parse_american(s: Any) -> Optional[int]:
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


def _american_to_implied_pct(odds: int) -> float:
    """American odds → implied probability (0–100)."""
    if odds == 0:
        return 50.0
    if odds > 0:
        return round(100.0 / (odds + 100) * 100, 2)
    return round((-odds) / ((-odds) + 100) * 100, 2)


def _dedvig_1x2(home_ml: int, away_ml: int, draw_ml: int) -> tuple[float, float, float]:
    """De-vig 1X2 moneylines to true probabilities. Returns (home, away, draw) in [0, 100]."""
    h = _american_to_implied_pct(home_ml) / 100.0
    a = _american_to_implied_pct(away_ml) / 100.0
    d = _american_to_implied_pct(draw_ml) / 100.0
    total = h + a + d
    if total <= 0:
        return (33.3, 33.3, 33.4)
    return (round(h/total*100, 1), round(a/total*100, 1), round(d/total*100, 1))


def _dedvig_ou(over_ml: int, under_ml: int) -> tuple[float, float]:
    """De-vig O/U pair. Returns (over_pct, under_pct)."""
    o = _american_to_implied_pct(over_ml) / 100.0
    u = _american_to_implied_pct(under_ml) / 100.0
    total = o + u
    if total <= 0:
        return (50.0, 50.0)
    return (round(o/total*100, 1), round(u/total*100, 1))


def _slug_deterministic_id(event_id: str, market: str, sel: str) -> str:
    """Stable id for upsert dedup."""
    raw = f"uefa_espn|{event_id}|{market}|{sel}".lower()
    h = hashlib.sha256(raw.encode()).hexdigest()[:24]
    return f"uefa-{h}"


def _grade_from_conf(conf: float) -> str:
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


# ── ESPN fetchers ──────────────────────────────────────────────────

async def _fetch_scoreboard(cx: httpx.AsyncClient, slug: str, date_yyyymmdd: str) -> list[dict]:
    """Pull one scoreboard for one date. Returns [] on any failure."""
    url = f"{_ESPN_BASE}/{slug}/scoreboard"
    try:
        r = await cx.get(url, params={"dates": date_yyyymmdd}, timeout=15)
        if r.status_code != 200:
            logger.debug("ESPN %s/%s returned %s", slug, date_yyyymmdd, r.status_code)
            return []
        data = r.json() or {}
        return data.get("events") or []
    except Exception as e:
        logger.warning("ESPN fetch %s/%s failed: %s", slug, date_yyyymmdd, e)
        return []


async def fetch_uefa_slate(days_ahead: int = 7) -> list[dict]:
    """Fetch the UEFA slate across every competition × every day in window.

    Returns a flat list of dicts:
      { event_id, home, away, kickoff_utc, league, sport_key, odds:{...} }
    """
    today = datetime.now(timezone.utc).date()
    dates = [(today + timedelta(days=i)).strftime("%Y%m%d")
             for i in range(days_ahead + 1)]

    out: list[dict] = []
    async with httpx.AsyncClient(headers={"User-Agent": "PerkLocks/1.0"}) as cx:
        # Fan out — 7 comps × 8 days = 56 tiny HTTP calls; ESPN doesn't
        # rate-limit publicly and each returns <100kb.
        tasks = []
        for slug, label, sport_key in UEFA_COMPETITIONS:
            for d in dates:
                tasks.append(_fetch_scoreboard(cx, slug, d))

        results = await asyncio.gather(*tasks, return_exceptions=True)

    # Flatten with metadata
    ti = 0
    for slug, label, sport_key in UEFA_COMPETITIONS:
        for _d in dates:
            res = results[ti]
            ti += 1
            if isinstance(res, Exception) or not res:
                continue
            for ev in res:
                parsed = _parse_espn_event(ev, label, sport_key)
                if parsed:
                    out.append(parsed)

    # Dedup on event_id (same event can appear in multiple slugs? unlikely
    # but be defensive).
    seen: set[str] = set()
    unique: list[dict] = []
    for e in out:
        if e["event_id"] in seen:
            continue
        seen.add(e["event_id"])
        unique.append(e)
    logger.info("UEFA ESPN slate: %d unique fixtures across %d competitions × %d days",
                len(unique), len(UEFA_COMPETITIONS), len(dates))
    return unique


def _parse_espn_event(ev: dict, league_label: str, sport_key: str) -> Optional[dict]:
    """Convert an ESPN event dict → a normalized fixture with parsed odds."""
    try:
        ev_id = str(ev.get("id") or "")
        if not ev_id:
            return None
        kickoff = ev.get("date")  # "2026-07-09T14:00Z"
        comp = (ev.get("competitions") or [{}])[0]
        competitors = comp.get("competitors") or []
        if len(competitors) < 2:
            return None
        home = away = None
        for c in competitors:
            team = c.get("team") or {}
            name = (team.get("displayName") or team.get("name") or "").strip()
            if not name:
                continue
            side = (c.get("homeAway") or "").lower()
            info = {
                "name": name,
                "abbrev": team.get("abbreviation"),
                "logo": team.get("logo"),
                # ESPN attaches a recent-form string (e.g. "LLLWL") on
                # per-competitor blocks even when there are no book odds.
                # We fall back to this to synthesize picks for Europa
                # and Champions Qual — where DraftKings hasn't posted
                # markets yet but Linemate still shows the games.
                "form": (c.get("form") or "").upper(),
            }
            if side == "home":
                home = info
            elif side == "away":
                away = info
        if not home or not away:
            return None

        status = ((comp.get("status") or {}).get("type") or {}) or {}
        status_state = (status.get("state") or "").lower()  # pre, in, post
        if status_state and status_state != "pre":
            return None  # only ingest pregame fixtures

        # Parse odds — take the first provider (DraftKings priority=1).
        odds_block = None
        for o in comp.get("odds") or []:
            if not o:
                continue
            ml = o.get("moneyline") or {}
            if ml.get("home") and ml.get("away") and ml.get("draw"):
                odds_block = o
                break
        parsed_odds: dict[str, Any] = {}
        if odds_block:
            ml = odds_block.get("moneyline") or {}
            def close_odds(side):
                return _parse_american(((ml.get(side) or {}).get("close") or {}).get("odds"))
            home_ml = close_odds("home")
            away_ml = close_odds("away")
            draw_ml = close_odds("draw")
            if home_ml is not None and away_ml is not None and draw_ml is not None:
                parsed_odds["moneyline"] = {
                    "home": home_ml, "away": away_ml, "draw": draw_ml,
                }
            # Total O/U
            tot = odds_block.get("total") or {}
            line_str = ((tot.get("over") or {}).get("close") or {}).get("line") or ""
            line_num_m = re.search(r"([0-9]+(?:\.[0-9]+)?)", str(line_str))
            over_odds  = _parse_american(((tot.get("over")  or {}).get("close") or {}).get("odds"))
            under_odds = _parse_american(((tot.get("under") or {}).get("close") or {}).get("odds"))
            if line_num_m and over_odds is not None and under_odds is not None:
                parsed_odds["total"] = {
                    "line": float(line_num_m.group(1)),
                    "over": over_odds,
                    "under": under_odds,
                }
            parsed_odds["bookmaker"] = (odds_block.get("provider") or {}).get("displayName") \
                                       or (odds_block.get("provider") or {}).get("name") \
                                       or "DraftKings"
            parsed_odds["deep_link"] = (odds_block.get("link") or {}).get("href")

        return {
            "event_id":    ev_id,
            "home":        home,
            "away":        away,
            "kickoff_utc": kickoff,
            "league":      league_label,
            "sport_key":   sport_key,
            "odds":        parsed_odds,
        }
    except Exception as e:
        logger.warning("Parse ESPN event failed: %s", e)
        return None


# ── pick doc builders ──────────────────────────────────────────────

def _build_moneyline_pick(fx: dict) -> Optional[dict]:
    """Pick the top-probability moneyline side (home / draw / away).
    Skip when confidence < floor or odds unavailable."""
    ml = (fx.get("odds") or {}).get("moneyline")
    if not ml:
        return None
    home_pct, away_pct, draw_pct = _dedvig_1x2(ml["home"], ml["away"], ml["draw"])
    # Pick the highest-probability outcome
    options = [
        ("home", home_pct, ml["home"], fx["home"]["name"]),
        ("away", away_pct, ml["away"], fx["away"]["name"]),
        ("draw", draw_pct, ml["draw"], "Draw"),
    ]
    side, conf, book_odds, sel = max(options, key=lambda x: x[1])
    if conf < _ML_CONFIDENCE_FLOOR:
        return None

    event_name = f"{fx['away']['name']} @ {fx['home']['name']}"
    if side == "draw":
        market_label = "Match Result · Draw"
    else:
        market_label = f"{sel} Moneyline"

    impl = _american_to_implied_pct(book_odds)
    edge = round(conf - impl, 2)

    return {
        "id":               _slug_deterministic_id(fx["event_id"], "ml", side),
        "external_id":      f"{fx['sport_key']}-{fx['event_id']}-ml-{side}",
        "sport":            "Soccer",
        "league":           fx["league"],
        "event":            event_name,
        "event_time":       fx["kickoff_utc"],
        "market":           market_label,
        "selection":        sel,
        "win_probability":  conf,
        "implied_probability": impl,
        "book_odds":        book_odds,
        "edge_percent":     edge,
        "lock_score":       conf,
        "lock_score_v2":    conf,
        "grade":            _grade_from_conf(conf),
        "pick_date":        datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "is_under_lock":    False,
        "no_bet":           conf < 60.0,
        "elite_player":     False,
        "deep_dive":        False,
        "source":           _UEFA_SOURCE_TAG,
        "model_version":    "uefa.espn.v1",
        "bookmaker":        (fx["odds"] or {}).get("bookmaker", "DraftKings"),
        "created_at":       datetime.now(timezone.utc).isoformat(),
        "is_extra":         False,
        "fair_odds_model":  False,
        "sport_key":        fx["sport_key"],
        "espn_event_id":    fx["event_id"],
        "factors": {
            "Coverage Source": (
                f"ESPN + {(fx['odds'] or {}).get('bookmaker','DraftKings')} — "
                "fetched via ESPN's public scoreboard because The Odds API "
                "doesn't populate this competition until closer to kickoff."
            ),
            "De-vig Confidence": (
                f"{sel} at {conf}% after removing sportsbook margin from the "
                f"1X2 market (H={home_pct}% / A={away_pct}% / D={draw_pct}%)."
            ),
        },
    }


def _build_total_pick(fx: dict) -> Optional[dict]:
    """Emit Over/Under 2.5 Goals when de-vig confidence exceeds floor."""
    tot = (fx.get("odds") or {}).get("total")
    if not tot:
        return None
    over_pct, under_pct = _dedvig_ou(tot["over"], tot["under"])
    if max(over_pct, under_pct) < _ML_CONFIDENCE_FLOOR:
        return None
    if over_pct >= under_pct:
        side, conf, book_odds = "over", over_pct, tot["over"]
        sel = f"Over {tot['line']}"
    else:
        side, conf, book_odds = "under", under_pct, tot["under"]
        sel = f"Under {tot['line']}"

    event_name = f"{fx['away']['name']} @ {fx['home']['name']}"
    impl = _american_to_implied_pct(book_odds)
    edge = round(conf - impl, 2)

    return {
        "id":               _slug_deterministic_id(fx["event_id"], "total", side + f"_{tot['line']}"),
        "external_id":      f"{fx['sport_key']}-{fx['event_id']}-total-{side}",
        "sport":            "Soccer",
        "league":           fx["league"],
        "event":            event_name,
        "event_time":       fx["kickoff_utc"],
        "market":           f"Total Goals {sel}",
        "selection":        sel,
        "win_probability":  conf,
        "implied_probability": impl,
        "book_odds":        book_odds,
        "edge_percent":     edge,
        "lock_score":       conf,
        "lock_score_v2":    conf,
        "grade":            _grade_from_conf(conf),
        "pick_date":        datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "is_under_lock":    False,
        "no_bet":           conf < 60.0,
        "elite_player":     False,
        "deep_dive":        False,
        "source":           _UEFA_SOURCE_TAG,
        "model_version":    "uefa.espn.v1",
        "bookmaker":        (fx["odds"] or {}).get("bookmaker", "DraftKings"),
        "created_at":       datetime.now(timezone.utc).isoformat(),
        "is_extra":         False,
        "fair_odds_model":  False,
        "sport_key":        fx["sport_key"],
        "espn_event_id":    fx["event_id"],
        "factors": {
            "Coverage Source": (
                f"ESPN + {(fx['odds'] or {}).get('bookmaker','DraftKings')} — "
                "totals market pulled from ESPN when The Odds API is blank."
            ),
            "De-vig Confidence": (
                f"{sel} at {conf}% after de-vig (Over={over_pct}% / Under={under_pct}%)."
            ),
        },
    }


def _build_double_chance_pick(fx: dict) -> Optional[dict]:
    """Emit a Win-or-Draw pick (double chance / 1X or X2). Threshold
    is intentionally loose (60%) because the ESPN Signal Engine
    (services.espn_signal_engine.apply_signals) runs *after* picks are
    built and adjusts probability up/down by up to ±8pp based on:
      • Wikipedia season W/D/L record delta
      • ESPN recent-form strings
      • Active injuries
    So a modest 60% base can climb into legit Rollover territory when
    the pick side has a strong season record vs opponent.
    """
    ml = (fx.get("odds") or {}).get("moneyline")
    if not ml:
        return None
    home_pct, away_pct, draw_pct = _dedvig_1x2(ml["home"], ml["away"], ml["draw"])
    home_or_draw = round(home_pct + draw_pct, 1)
    away_or_draw = round(away_pct + draw_pct, 1)
    if home_or_draw >= away_or_draw:
        side_team = fx["home"]["name"]
        conf = home_or_draw
    else:
        side_team = fx["away"]["name"]
        conf = away_or_draw
    if conf < 60.0:  # loosened from 75 — Signal Engine can rescue borderline picks
        return None

    # Derive a fair American price for the double-chance combo since
    # ESPN doesn't provide DC odds directly.
    p = max(0.001, min(0.999, conf / 100.0))
    fair_odds = -round(100 * p / (1 - p)) if p >= 0.5 else round(100 * (1 - p) / p)
    event_name = f"{fx['away']['name']} @ {fx['home']['name']}"
    sel = f"{side_team} or Draw"

    return {
        "id":               _slug_deterministic_id(fx["event_id"], "dc", side_team),
        "external_id":      f"{fx['sport_key']}-{fx['event_id']}-dc",
        "sport":            "Soccer",
        "league":           fx["league"],
        "event":            event_name,
        "event_time":       fx["kickoff_utc"],
        "market":           sel,
        "selection":        sel,
        "win_probability":  conf,
        "implied_probability": conf,   # fair-derived
        "book_odds":        fair_odds,
        "edge_percent":     0.0,
        "lock_score":       conf,
        "lock_score_v2":    conf,
        "grade":            _grade_from_conf(conf),
        "pick_date":        datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "is_under_lock":    False,
        "no_bet":           False,
        "elite_player":     False,
        "deep_dive":        False,
        "source":           _UEFA_SOURCE_TAG,
        "model_version":    "uefa.espn.v1",
        "bookmaker":        "Fair Odds (Model)",
        "created_at":       datetime.now(timezone.utc).isoformat(),
        "is_extra":         True,   # DC derived, mark as extended coverage
        "fair_odds_model":  True,
        "sport_key":        fx["sport_key"],
        "espn_event_id":    fx["event_id"],
        "factors": {
            "Coverage Source": (
                "Double-chance (Win-or-Draw) derived from ESPN 1X2 pricing. "
                "Rollover-tier market — confirm the line at your book."
            ),
            "De-vig Confidence": (
                f"{sel} at {conf}% (home={home_pct}% / away={away_pct}% / draw={draw_pct}%)."
            ),
        },
    }


def _form_win_share(form: str) -> float:
    """Recency-weighted form → normalized [0,1] score.
    Each char scored W=1, D=0.5, L=0. Weights favor the most recent
    match (rightmost char) with an exponential decay of 0.7."""
    if not form:
        return 0.5
    chars = [c for c in form.upper() if c in ("W", "D", "L")][-5:]
    if not chars:
        return 0.5
    n = len(chars)
    weights = [0.7 ** (n - 1 - i) for i in range(n)]
    total_w = sum(weights)
    pts = 0.0
    for c, w in zip(chars, weights):
        pts += ({"W": 1.0, "D": 0.5, "L": 0.0}[c]) * w
    return pts / total_w


def _synthetic_ml_from_form(fx: dict) -> Optional[dict]:
    """Emit a fair-odds ML pick from ESPN team-form strings when the
    fixture has no book odds. Used for EL Qual / CL Qual matches where
    DraftKings hasn't posted markets yet.

    Confidence is derived from (home_form - away_form + home_advantage)
    mapped through a logistic. Home advantage is a modest +7 percentage
    points (soccer avg). When one side lacks a form string we treat it
    as neutral (0.5) so we still surface the fixture.
    """
    home_form = fx["home"].get("form") or ""
    away_form = fx["away"].get("form") or ""
    if not home_form and not away_form:
        return None

    home_share = _form_win_share(home_form)
    away_share = _form_win_share(away_form)
    # Logistic mapping: diff in [-1, 1] → probability in [0.15, 0.85]
    # (bounded so we don't overclaim on a 5-game sample).
    diff = (home_share - away_share) + 0.07  # +7pp home advantage
    # Draw prob is roughly 25% in UEFA qualifiers → distribute the
    # remaining 75% between home and away by the diff-driven ratio.
    home_p = 0.375 + 0.30 * diff   # 0.375 - 0.675 range
    home_p = max(0.10, min(0.85, home_p))
    away_p = 0.75 - home_p
    away_p = max(0.05, min(0.75, away_p))
    # Normalize
    total = home_p + away_p + 0.25
    home_p, away_p, draw_p = home_p / total, away_p / total, 0.25 / total

    if home_p >= away_p and home_p >= draw_p:
        conf = round(home_p * 100, 1)
        sel = fx["home"]["name"]
        side = "home"
    elif away_p >= draw_p:
        conf = round(away_p * 100, 1)
        sel = fx["away"]["name"]
        side = "away"
    else:
        conf = round(draw_p * 100, 1)
        sel = "Draw"
        side = "draw"

    if conf < 40.0:  # softer floor for informational picks — the fixture
        return None    # still shows on the board unless it's a coin-flip

    # Fair American price from confidence
    p = max(0.001, min(0.999, conf / 100.0))
    fair_odds = -round(100 * p / (1 - p)) if p >= 0.5 else round(100 * (1 - p) / p)
    event_name = f"{fx['away']['name']} @ {fx['home']['name']}"
    market_label = "Match Result · Draw" if side == "draw" else f"{sel} Moneyline"

    return {
        "id":               _slug_deterministic_id(fx["event_id"], "ml_form", side),
        "external_id":      f"{fx['sport_key']}-{fx['event_id']}-ml-{side}",
        "sport":            "Soccer",
        "league":           fx["league"],
        "event":            event_name,
        "event_time":       fx["kickoff_utc"],
        "market":           market_label,
        "selection":        sel,
        "win_probability":  conf,
        "implied_probability": conf,
        "book_odds":        fair_odds,
        "edge_percent":     0.0,
        "lock_score":       conf,
        "lock_score_v2":    conf,
        "grade":            _grade_from_conf(conf),
        "pick_date":        datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "is_under_lock":    False,
        "no_bet":           conf < 60.0,
        "elite_player":     False,
        "deep_dive":        False,
        "source":           _UEFA_SOURCE_TAG,
        "model_version":    "uefa.espn.v1.form",
        "bookmaker":        "Fair Odds (Model)",
        "created_at":       datetime.now(timezone.utc).isoformat(),
        "is_extra":         True,
        "fair_odds_model":  True,
        "sport_key":        fx["sport_key"],
        "espn_event_id":    fx["event_id"],
        "factors": {
            "Coverage Source": (
                "Model-only fair-odds derived from ESPN recent-form strings — "
                "DraftKings hasn't posted markets yet on this fixture. "
                "Confirm the line at your book once it opens."
            ),
            "Recent Form": (
                f"{fx['home']['name']} (H): {home_form or 'n/a'} | "
                f"{fx['away']['name']} (A): {away_form or 'n/a'}"
            ),
        },
    }


# ── sync entry-point ───────────────────────────────────────────────

async def sync_uefa_espn_picks(db, days_ahead: int = 7) -> dict:
    """Fetch → parse → build picks → upsert. Returns a summary."""
    started = datetime.now(timezone.utc)
    slate = await fetch_uefa_slate(days_ahead=days_ahead)

    ml_picks: list[dict] = []
    total_picks: list[dict] = []
    dc_picks: list[dict] = []
    synth_picks: list[dict] = []
    for fx in slate:
        p = _build_moneyline_pick(fx)
        if p:
            ml_picks.append(p)
        else:
            # No book odds → try the form-based synthetic pick so EL
            # and CL Qual matches still appear on the board.
            sp = _synthetic_ml_from_form(fx)
            if sp:
                synth_picks.append(sp)
        t = _build_total_pick(fx)
        if t:
            total_picks.append(t)
        d = _build_double_chance_pick(fx)
        if d:
            dc_picks.append(d)

    all_new = ml_picks + total_picks + dc_picks + synth_picks

    # Fold ESPN Signal Engine adjustments into the freshly-built picks
    # BEFORE upsert. This is what promotes borderline picks (e.g.
    # Mornar-or-Draw at 60% base) into the visible slate when the
    # Wikipedia season-record signal boosts them past the no_bet /
    # lock thresholds.
    try:
        from services.espn_team_meta import enrich_pick as _meta
        from services.espn_form_cache import attach_form_to_pick as _form
        from services.espn_signal_engine import apply_signals as _sig
        for doc in all_new:
            try:
                await _meta(db, doc)
                await _form(db, doc)
                await _sig(db, doc)
                # Signal Engine may have raised the win_probability high
                # enough to clear the 60% no_bet floor — refresh the
                # gate now that the analysis has run.
                doc["no_bet"] = doc.get("win_probability", 0) < 60.0
            except Exception as e:
                logger.warning("signal enrichment failed for %s: %s",
                               doc.get("selection"), e)
    except Exception as e:
        logger.warning("signal enrichment stack unavailable: %s", e)

    # Dedup: skip if a football-data-backed pick already exists for the
    # same event_time + selection (avoid duplicating group-stage CL games
    # once The Odds API turns on).
    upserts = 0
    skipped_existing = 0
    for doc in all_new:
        existing = await db.picks.find_one({
            "sport": "Soccer",
            "event_time": doc["event_time"],
            "selection": doc["selection"],
            "source": {"$ne": _UEFA_SOURCE_TAG},
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
        "fixtures_seen":    len(slate),
        "ml_generated":     len(ml_picks),
        "total_generated": len(total_picks),
        "dc_generated":     len(dc_picks),
        "synth_generated":  len(synth_picks),
        "upserts":          upserts,
        "skipped_existing": skipped_existing,
    }
    logger.info("UEFA ESPN sync done: %s", summary)
    return summary


async def uefa_espn_loop(db) -> None:
    """Runs every 30 min. Cheap: <60 tiny HTTP calls with no API key."""
    await asyncio.sleep(45)  # let the rest of the app start
    while True:
        try:
            await sync_uefa_espn_picks(db, days_ahead=7)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("UEFA ESPN loop error: %s", e)
        await asyncio.sleep(30 * 60)
