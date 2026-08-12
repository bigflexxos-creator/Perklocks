"""ESPN Soccer Fixture Fallback (iter-97, 2026-07-26).

The Odds API subscription is currently returning 401 for lower-tier
leagues (China Super League, Sweden Allsvenskan, Norway Eliteserien,
Finland Veikkausliiga). Until it renews, this module pulls upcoming
fixtures from ESPN's PUBLIC scoreboard endpoints and emits minimal
moneyline picks so those leagues stay visible on the board.

Endpoint pattern
----------------
    GET https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/scoreboard?dates=YYYYMMDD-YYYYMMDD

Slug mapping
    chn.1 → China Super League       → sport_key = soccer_china_superleague
    swe.1 → Sweden Allsvenskan       → sport_key = soccer_sweden_allsvenskan
    nor.1 → Norway Eliteserien       → sport_key = soccer_norway_eliteserien
    fin.1 → Finland Veikkausliiga    → sport_key = soccer_finland_veikkausliiga

Rules
-----
- Emit at most ONE moneyline pick per fixture (the home-team side by
  default, or the ESPN-favoured side if a moneyline exists on ESPN).
- `odds_source = "espn_fallback"` — the odds decorator preserves this.
- `edge_percent = None` — no real book to measure against.
- `confidence_penalty = -8` — mild soft-dock so these picks don't
  overshoot the Elite tier.
- Idempotent: pick id is deterministic (sha256 of source + event id).
- Downstream: `soccer_hot_scorers.hot_scorers_loop` will discover these
  fixtures via the `db.picks` scan and attach anytime-goalscorer picks
  from the Wikipedia top-scorer list.

Runtime
-------
- `refresh_once()` — one full pass over all 4 slugs (~30 events).
- `refresh_loop()` — long-running background task, every 30 min.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

logger = logging.getLogger("lockscore.espn_soccer_fixtures")

_SOURCE_TAG = "espn_fallback"
_ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"

# ESPN slug → (sport_key, league display name)
_LEAGUE_MAP: dict[str, tuple[str, str]] = {
    "chn.1": ("soccer_china_superleague",    "China Super League"),
    "swe.1": ("soccer_sweden_allsvenskan",   "Allsvenskan"),
    "nor.1": ("soccer_norway_eliteserien",   "Eliteserien"),
    "fin.1": ("soccer_finland_veikkausliiga", "Veikkausliiga"),
}

# Only fetch fixtures within this horizon.
_FIXTURE_HORIZON_HOURS = 96      # a bit past the board's 72h so hot-scorers can pre-warm
_HTTP_TIMEOUT           = 15


def _norm(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _pick_id(sport_key: str, event_id: str, side: str) -> str:
    raw = f"{_SOURCE_TAG}|{sport_key}|{event_id}|{side}".lower()
    return f"espnfx-{hashlib.sha256(raw.encode()).hexdigest()[:24]}"


def _american_to_prob(ml: Optional[float]) -> Optional[float]:
    if ml is None:
        return None
    ml = float(ml)
    if ml == 0:
        return None
    if ml > 0:
        return round(100.0 / (ml + 100.0), 4)
    return round(-ml / (-ml + 100.0), 4)


def _prob_to_american(p: float) -> int:
    """DEPRECATED — Session A (2026-06) purge.

    This function used to synthesize sportsbook American odds from a
    model probability.  Doing so was the exact "model prob → book
    odds" pipe the P0 Session A directive removes.  It is retained
    only as a stub that RAISES so any accidental re-use is caught
    loudly in tests / CI instead of leaking synthetic prices.
    """
    raise NotImplementedError(
        "_prob_to_american is purged by Session A — do not synthesize "
        "sportsbook American odds from model probability.  If no real "
        "sportsbook line exists, set book_odds=None + "
        "no_real_book_line=True + odds_source='MODEL_ONLY'.",
    )


async def _fetch_scoreboard(cx: httpx.AsyncClient, slug: str) -> list[dict]:
    """Fetch a week of upcoming fixtures for the given ESPN league slug."""
    now = datetime.now(timezone.utc)
    hi = now + timedelta(hours=_FIXTURE_HORIZON_HOURS)
    dates = f"{now.strftime('%Y%m%d')}-{hi.strftime('%Y%m%d')}"
    url = f"{_ESPN_BASE}/{slug}/scoreboard"
    try:
        r = await cx.get(url, params={"dates": dates})
        if r.status_code != 200:
            logger.warning("espn scoreboard %s HTTP %d", slug, r.status_code)
            return []
        return (r.json() or {}).get("events") or []
    except Exception as e:
        logger.warning("espn scoreboard %s error: %s", slug, e)
        return []


def _parse_event(ev: dict) -> Optional[dict]:
    """Extract the fields we need from one ESPN event.

    Returns None if the event is not upcoming (state != 'pre') or
    is missing critical fields.
    """
    comp = (ev.get("competitions") or [{}])[0]
    state = ((comp.get("status") or {}).get("type") or {}).get("state")
    if state != "pre":
        return None
    comps = comp.get("competitors") or []
    home = next((t for t in comps if t.get("homeAway") == "home"), None) or {}
    away = next((t for t in comps if t.get("homeAway") == "away"), None) or {}
    home_name = ((home.get("team") or {}).get("displayName") or "").strip()
    away_name = ((away.get("team") or {}).get("displayName") or "").strip()
    if not (home_name and away_name):
        return None

    odds_list = comp.get("odds") or []
    odds = odds_list[0] if odds_list else {}
    home_ml = (odds.get("homeTeamOdds") or {}).get("moneyLine")
    away_ml = (odds.get("awayTeamOdds") or {}).get("moneyLine")
    draw_ml = (odds.get("drawOdds")     or {}).get("moneyLine")

    return {
        "event_id":   str(ev.get("id") or ""),
        "date":       ev.get("date"),
        "home":       home_name,
        "away":       away_name,
        "home_ml":    home_ml,
        "away_ml":    away_ml,
        "draw_ml":    draw_ml,
        "home_form":  (home.get("form")   or "").strip(),
        "away_form":  (away.get("form")   or "").strip(),
        "home_record": (home.get("records") or [{}])[0].get("summary", "") if home.get("records") else "",
        "away_record": (away.get("records") or [{}])[0].get("summary", "") if away.get("records") else "",
    }


def _select_side(ev: dict) -> Optional[dict]:
    """Choose which side to pick for the moneyline.

    Priority:
    1. If ESPN provides moneylines for BOTH sides, pick the favourite
       (lower-magnitude negative) with implied prob 0.50 – 0.75.
    2. If only one side has a moneyline, pick that side if implied
       prob is 0.55 – 0.80.
    3. Otherwise pick HOME with a conservative default 0.48 win
       probability (home-field-advantage baseline).

    Returns a dict {side, team, probability, book_odds_source} or None
    if no confident pick can be made.
    """
    home_p = _american_to_prob(ev["home_ml"])
    away_p = _american_to_prob(ev["away_ml"])

    # Case 1: both moneylines present.
    if home_p and away_p:
        fav_side, fav_team, fav_p = ("home", ev["home"], home_p) if home_p >= away_p \
                                    else ("away", ev["away"], away_p)
        if 0.50 <= fav_p <= 0.75:
            return {
                "side": fav_side, "team": fav_team,
                "probability": fav_p,
                "book_odds_source": "espn",
            }
        return None

    # Case 2: only one moneyline present.
    if home_p and 0.55 <= home_p <= 0.80:
        return {"side": "home", "team": ev["home"], "probability": home_p,
                "book_odds_source": "espn"}
    if away_p and 0.55 <= away_p <= 0.80:
        return {"side": "away", "team": ev["away"], "probability": away_p,
                "book_odds_source": "espn"}

    # Case 3: no moneyline. Fall back to conservative HOME baseline.
    # Only if we have SOME priors — read form strings ("WWDLW") for
    # a rough tilt.
    def _form_score(f: str) -> float:
        # Map W=3, D=1, L=0 over the last 5.
        if not f:
            return 0.5
        pts = 0
        n   = 0
        for c in f.upper()[:5]:
            if   c == "W": pts += 3; n += 1
            elif c == "D": pts += 1; n += 1
            elif c == "L": pts += 0; n += 1
        return (pts / (3 * n)) if n else 0.5

    home_score = _form_score(ev["home_form"])
    away_score = _form_score(ev["away_form"])
    diff = home_score - away_score      # -1 .. +1
    # Home-field advantage baseline (~55%) + form tilt.
    home_baseline_p = 0.55 + diff * 0.18      # 37% .. 73%
    # Always emit home ML — this ensures every ESPN fixture appears on
    # the board even when ESPN has no odds and no form data. Baseline
    # 0.55 is the empirical home win-rate across soccer historically.
    return {
        "side": "home", "team": ev["home"],
        "probability": round(max(0.42, min(0.72, home_baseline_p)), 4),
        "book_odds_source": ("form" if (ev["home_form"] or ev["away_form"]) else "hfa_baseline"),
    }


def _build_pick(sport_key: str, league: str, ev: dict, sel: dict,
                today_str: str) -> dict:
    """Turn one parsed event + side selection into a pick document.

    Session A (2026-06) — synthetic-odds purge.  Historically this
    fallback computed `book_odds = _prob_to_american(prob)` when ESPN
    did not carry a real moneyline for the event.  That price was
    NOT a sportsbook line — it was the model probability rendered
    back as American odds, which the P0 Session A directive forbids.

    Post-purge behaviour:

    * When ESPN provides a REAL moneyline for the chosen side
      (``sel['book_odds_source'] == 'espn'``) we take THAT American
      price verbatim and mark ``odds_source='espn'`` so the boundary
      accepts it as REAL.
    * When there is no ESPN moneyline (form / hfa_baseline branches)
      we mark ``book_odds=None`` + ``no_real_book_line=True`` +
      ``odds_source='MODEL_ONLY'``.  ``edge_percent`` is None (never 0).
      Model provenance is preserved as ``model_probability``.
    """
    event_str = f"{ev['away']} @ {ev['home']}"
    prob   = float(sel["probability"])
    src    = sel.get("book_odds_source") or "hfa_baseline"

    # ── Real ESPN sportsbook moneyline branch ────────────────────
    real_book_odds: Optional[int] = None
    no_real_line = False
    odds_source_val = "MODEL_ONLY"
    if src == "espn":
        # sel came from Case 1 or Case 2 in _select_side, where we
        # trusted an ESPN-published moneyline.  Convert THAT value
        # (already an American int) to book_odds verbatim.
        raw_ml = (
            ev.get("home_ml") if sel.get("side") == "home"
            else ev.get("away_ml")
        )
        try:
            real_book_odds = int(round(float(raw_ml)))
            odds_source_val = "espn"
        except (TypeError, ValueError):
            real_book_odds = None
    if real_book_odds is None:
        no_real_line = True
        odds_source_val = "MODEL_ONLY"

    pick_id = _pick_id(sport_key, ev["event_id"], sel["side"])
    return {
        "id":                   pick_id,
        "sport":                "Soccer",
        "sport_key":            sport_key,
        "league":               league,
        "event":                event_str,
        "event_time":           ev["date"],
        "home_team":            ev["home"],
        "away_team":            ev["away"],
        "market":               f"{sel['team']} Moneyline",
        "market_type":          "moneyline",
        "selection":            sel["team"],
        "selected_team":        sel["team"],
        # Model probability is authoritative — publication boundary
        # requires it (rule 3: model provenance).
        "model_probability":    round(prob, 4),
        "model_win_prob":       round(prob, 4),
        "win_probability":      round(prob * 100, 2),
        "book_odds":            real_book_odds,
        "implied_probability":  round(prob * 100, 2),
        "confidence":           "MEDIUM",
        "lock_score":           round(50 + (prob - 0.5) * 80, 1),   # 50..90
        # Strict edge gate — no real sportsbook = no edge.  Preserved
        # as `None` (missing/UNKNOWN), never coerced to 0.
        "edge_percent":         None,
        "odds_source":          odds_source_val,
        "odds_status":          ("real" if real_book_odds is not None
                                  else "no_book_line"),
        "no_real_book_line":    no_real_line,
        "no_model_probability_reason": None,
        "confidence_penalty":   -8,
        "source":               _SOURCE_TAG,
        "grade":                "Playable",
        "no_bet":               False,
        "off_board":            False,
        "status":               "pending",
        "pick_date":            today_str,
        "external_id":          f"espn-{ev['event_id']}",
        "pick_rationale": {
            "engine":  _SOURCE_TAG,
            "engine_version": "espn_soccer_fixtures.v2_no_synth_odds",
            "summary": (
                f"{sel['team']} moneyline · model p ~{prob*100:.0f}% · "
                + ("ESPN sportsbook moneyline"
                    if real_book_odds is not None
                    else "no real book line (MODEL_ONLY)")
            ),
            "evidence": [
                f"📊 ESPN scoreboard: {ev['home_form'] or '?'} home form vs "
                f"{ev['away_form'] or '?'} away form",
                (f"💵 Real ESPN moneyline: {real_book_odds:+d}"
                    if real_book_odds is not None
                    else f"🏷 MODEL_ONLY coverage ({src}) — no real "
                          f"sportsbook line, book_odds omitted"),
            ],
            "concerns": (
                []
                if real_book_odds is not None
                else [
                    "This tournament is not carried by our primary US "
                    "sportsbook feed and ESPN has no moneyline for this "
                    "event — pick is model-only.  No book line, no edge.",
                ]
            ),
        },
    }


async def refresh_once() -> dict:
    """One full refresh pass — fetch scoreboards for all 4 leagues,
    upsert picks. Returns per-league counters."""
    from deps import db
    from pymongo import ReplaceOne

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    started   = datetime.now(timezone.utc)
    result: dict[str, dict] = {}

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as cx:
        for slug, (sport_key, league) in _LEAGUE_MAP.items():
            counts = {"events": 0, "upcoming": 0, "picks": 0, "skipped": 0}
            events = await _fetch_scoreboard(cx, slug)
            counts["events"] = len(events)

            ops = []
            picks_for_publication: list[dict] = []
            now_iso = started.isoformat().replace("+00:00", "Z")
            for raw in events:
                ev = _parse_event(raw)
                if not ev:
                    continue
                # Ignore events already in the past.
                if ev["date"] and ev["date"] < now_iso:
                    continue
                counts["upcoming"] += 1
                sel = _select_side(ev)
                if not sel:
                    counts["skipped"] += 1
                    continue
                pick = _build_pick(sport_key, league, ev, sel, today_str)
                # Derive per-event pick_date from event_time so the /picks/today
                # 72-hour horizon works cleanly.
                try:
                    et = datetime.fromisoformat(str(ev["date"]).replace("Z", "+00:00"))
                    dh = (et - started).total_seconds() / 3600.0
                    if dh > 24:
                        pick["pick_date"] = et.astimezone(timezone.utc).strftime("%Y-%m-%d")
                except Exception:
                    pass
                pick["created_at"] = started
                pick["updated_at"] = started
                ops.append(ReplaceOne({"id": pick["id"]}, pick, upsert=True))
                picks_for_publication.append(pick)

            if ops:
                r = await db.picks.bulk_write(ops, ordered=False)
                counts["picks"] = (r.upserted_count or 0) + (r.modified_count or 0)
                # ── P0-2 canonical publication ─────────────────────
                # ESPN scoreboard fallback picks (Odds API 401) are
                # legitimate user-facing predictions; they must pass
                # through the publication service so the canonical
                # board eligibility gate accepts them.
                try:
                    from services.publication_helpers import (
                        publish_upserted_picks,
                    )
                    if picks_for_publication:
                        await publish_upserted_picks(
                            db, picks_for_publication,
                            publication_source=_SOURCE_TAG,
                            caller_label=f"espn_soccer_fixtures[{slug}]",
                        )
                except Exception as _pub_err:
                    logger.warning(
                        "espn_soccer_fixtures[%s] publication step "
                        "failed: %s", slug, _pub_err,
                    )
            result[slug] = counts
            logger.info("espn_soccer_fixtures[%s] %s", slug, counts)

    return result


async def refresh_loop() -> None:
    """Long-running loop — refresh every 30 min."""
    while True:
        try:
            await refresh_once()
        except Exception as e:
            logger.warning("espn_soccer_fixtures.refresh_loop error: %s", e)
        await asyncio.sleep(30 * 60)


__all__ = ["refresh_once", "refresh_loop", "_LEAGUE_MAP"]
