"""Soccer Player Form (Understat-backed)

Per-player season statistics for the Top 5 European leagues, refreshed every
12 hours from Understat's `/main/getPlayersStats/` AJAX endpoint. Powers the
goalscorer-market form lift used by the Unified Probability Engine and the
HOT FORM / COLD chip on the frontend.

Why Understat
-------------
- Single POST per league returns ~500-600 players' season totals as clean
  JSON (no HTML scraping, no Cloudflare). Total: 5 requests per refresh.
- Provides xG, npxG, xA, shots, key_passes, time, goals, assists — the
  exact stats we need to detect hot finishers vs cold luck and volume of
  goal-scoring chances.
- 2026-verified: FBref is now Cloudflare-protected (HTTP 403) and
  Understat's player pages no longer embed JSON in HTML, so this AJAX
  endpoint is the cleanest viable path.

Polite scraping
---------------
- 5 second minimum spacing between requests to understat.com.
- Exponential backoff on 429/5xx responses (1s, 2s, 4s, 8s).
- User-Agent that identifies the project + a real browser fingerprint
  to avoid generic-bot heuristics.
- Defensive: never raises out of the refresh loop. Failures are logged
  and the previously cached data continues to serve.

MongoDB schema (`soccer_player_form` collection)
----------------------------------------------
{
  _id:                str   "<name_canonical>__<league>__<season>",
  player_name:        str,
  name_canonical:     str   (lowercase, diacritics stripped, used for fuzzy lookup)
  understat_id:       str,
  team:               str,
  league:             str   "EPL" | "La_liga" | "Bundesliga" | "Serie_A" | "Ligue_1",
  season:             str,
  position:           str,
  games:              int,
  minutes:            int,
  goals:              int,
  xg:                 float,
  npxg:               float,
  assists:            int,
  xa:                 float,
  shots:              int,
  key_passes:         int,
  # ─ Derived metrics ────────────────────────────────────────────────
  xg_per_90:          float,
  npxg_per_90:        float,
  goals_per_90:       float,
  goals_over_xg:      float,    >1 = overperforming xG (hot finisher),
                                <1 = underperforming (cold luck)
  shots_per_90:       float,
  # ─ Form classification ────────────────────────────────────────────
  form_label:         "HOT" | "COLD" | "NEUTRAL",
  form_score:         int 0-100,
  updated_at:         datetime UTC,
}
"""

from __future__ import annotations

import asyncio
import logging
import re
import time as _time
import unicodedata
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

logger = logging.getLogger("lockscore.soccer_player_form")

# ──────────────────────────────────────────────────────────────────────────
# Tunables
# ──────────────────────────────────────────────────────────────────────────

UNDERSTAT_BASE = "https://understat.com"

# Top 5 European leagues + their Understat slugs.
LEAGUES: tuple[str, ...] = (
    "EPL",
    "La_liga",
    "Bundesliga",
    "Serie_A",
    "Ligue_1",
)

# Polite spacing — 5s between requests is conservative but well within
# courteous-scraping norms. Total scrape time per refresh: ~25-30s.
MIN_INTERVAL_SEC = 5.0
REFRESH_INTERVAL_SEC = 12 * 60 * 60   # 12 hours

REQUEST_TIMEOUT = 25.0
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 PerksLocksScraper/1.0"
)

# Form classification thresholds. These are deliberately conservative so
# only genuinely-trending players light up the HOT/COLD chip.
HOT_NPXG_PER_90       = 0.50    # ≥ 0.5 npxG/90 = elite chance volume
HOT_GOALS_OVER_XG_MIN = 1.05    # finishing slightly above expected
COLD_NPXG_PER_90      = 0.20    # < 0.2 npxG/90 = thin volume
COLD_GOALS_OVER_XG    = 0.70    # significantly underperforming xG

# Form lift on probability for goalscorer markets. Spec'd by user as
# moderate ±6%. Applied to anytime_scorer / first_goal_scorer /
# score_or_assist markets ONLY.
FORM_LIFT_HOT  = +0.06
FORM_LIFT_COLD = -0.06

# Minimum minutes played before form is meaningful (bench warmers should
# fall through to NEUTRAL regardless of micro-sample over/under-performance)
MIN_MINUTES_FOR_FORM = 360   # 4 full games


# ──────────────────────────────────────────────────────────────────────────
# Internal: Understat AJAX client
# ──────────────────────────────────────────────────────────────────────────

_last_request_t: float = 0.0


async def _polite_post(
    client: httpx.AsyncClient,
    league: str,
    season: int,
) -> Optional[list[dict]]:
    """Hit Understat's getPlayersStats endpoint for a single league.

    Returns the parsed `players` array on success, or None on any
    failure (logged, never raised).
    """
    global _last_request_t
    now = _time.monotonic()
    delta = now - _last_request_t
    if delta < MIN_INTERVAL_SEC:
        await asyncio.sleep(MIN_INTERVAL_SEC - delta)

    headers = {
        "User-Agent": USER_AGENT,
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{UNDERSTAT_BASE}/league/{league}/{season}",
        "Accept": "application/json,text/javascript,*/*;q=0.01",
    }

    backoff_delays = [1.0, 2.0, 4.0, 8.0]
    last_err: Optional[Exception] = None
    for attempt, delay in enumerate([0.0, *backoff_delays]):
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            resp = await client.post(
                f"{UNDERSTAT_BASE}/main/getPlayersStats/",
                data={"league": league, "season": str(season)},
                headers=headers,
                timeout=REQUEST_TIMEOUT,
                follow_redirects=True,
            )
            _last_request_t = _time.monotonic()
            if resp.status_code == 429 or resp.status_code >= 500:
                last_err = RuntimeError(f"HTTP {resp.status_code}")
                continue
            if resp.status_code != 200:
                logger.warning(
                    "Understat getPlayersStats(%s/%s) HTTP %s",
                    league, season, resp.status_code,
                )
                return None
            payload = resp.json()
            if not payload.get("success"):
                logger.warning(
                    "Understat getPlayersStats(%s/%s) returned error: %s",
                    league, season, payload.get("error"),
                )
                return None
            return payload.get("players") or []
        except (httpx.HTTPError, ValueError) as e:
            last_err = e
            continue
    logger.warning(
        "Understat getPlayersStats(%s/%s) failed after retries: %s",
        league, season, last_err,
    )
    return None


# ──────────────────────────────────────────────────────────────────────────
# Internal: name canonicalisation for fuzzy lookup
# ──────────────────────────────────────────────────────────────────────────

_NAME_PUNCT_RE = re.compile(r"[\.\-'\"\u2019]")


def canonicalize_name(name: str) -> str:
    """Lowercase + strip diacritics + remove punctuation.

    Examples:
      "Vinícius Júnior"  → "vinicius junior"
      "N'Golo Kanté"     → "ngolo kante"
      "Cole Palmer"      → "cole palmer"

    Used both at scrape-write time AND at pick-lookup time so they
    always match. Without canonicalisation we'd miss ~15-20% of
    international players whose names carry accents.
    """
    if not name:
        return ""
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_only = nfkd.encode("ascii", "ignore").decode("ascii")
    cleaned = _NAME_PUNCT_RE.sub("", ascii_only).lower()
    return re.sub(r"\s+", " ", cleaned).strip()


# ──────────────────────────────────────────────────────────────────────────
# Internal: per-player metric derivation + form classification
# ──────────────────────────────────────────────────────────────────────────

def _safe_float(v: Any) -> float:
    try:
        return float(v) if v not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def _safe_int(v: Any) -> int:
    try:
        return int(float(v)) if v not in (None, "") else 0
    except (TypeError, ValueError):
        return 0


def _derive_form(metrics: dict, position: str = "") -> tuple[str, int]:
    """Classify the player's current run of form.

    Returns (form_label, form_score).
    form_score is a 0-100 ranking number — purely cosmetic, drives
    the "FORM 78" badge in the UI. Not used in probability math.

    Only applies HOT/COLD labels to attacking positions (Forwards and
    Midfielders). Defenders and goalkeepers stay NEUTRAL regardless of
    stats — a centre-back with 0.05 npxG/90 isn't "COLD", that's just
    their job. Understat encodes positions as "F S" (Forward Sub),
    "M S" (Midfielder Sub), "D" (Defender), "GK" — we look at the
    first character.
    """
    minutes      = metrics["minutes"]
    npxg_per_90  = metrics["npxg_per_90"]
    goals_over_xg = metrics["goals_over_xg"]

    if minutes < MIN_MINUTES_FOR_FORM:
        return "NEUTRAL", 50

    # Only forwards (F) and midfielders (M) get HOT/COLD labels —
    # form lift is meaningful only for goal-scoring positions.
    pos_first = (position or "").strip()[:1].upper()
    if pos_first not in {"F", "M"}:
        return "NEUTRAL", 50

    # HOT: high chance volume AND finishing at-or-above xG.
    if (npxg_per_90 >= HOT_NPXG_PER_90
            and goals_over_xg >= HOT_GOALS_OVER_XG_MIN):
        # Score scales 70..99 with how hot.
        bonus = min(29, int((npxg_per_90 - HOT_NPXG_PER_90) * 30
                              + (goals_over_xg - HOT_GOALS_OVER_XG_MIN) * 25))
        return "HOT", 70 + max(0, bonus)

    # COLD: thin volume OR significantly underperforming xG.
    # Tightened (vs first version): require BOTH thresholds to be
    # missed when a player has decent volume — otherwise volume
    # players who hit a finishing slump get spuriously cold-labelled.
    is_cold_volume   = npxg_per_90 < COLD_NPXG_PER_90
    is_cold_finishing = goals_over_xg < COLD_GOALS_OVER_XG
    if is_cold_volume or (is_cold_finishing and npxg_per_90 < HOT_NPXG_PER_90):
        # Score scales 1..30 — colder = lower.
        penalty = 0
        if is_cold_volume:
            penalty += int((COLD_NPXG_PER_90 - npxg_per_90) * 80)
        if is_cold_finishing:
            penalty += int((COLD_GOALS_OVER_XG - goals_over_xg) * 30)
        return "COLD", max(1, 30 - min(29, penalty))

    return "NEUTRAL", 50


def _build_form_doc(
    raw: dict,
    league: str,
    season: int,
) -> Optional[dict]:
    """Convert one Understat player row into a soccer_player_form doc."""
    name = raw.get("player_name") or ""
    if not name:
        return None
    minutes = _safe_int(raw.get("time"))
    games   = _safe_int(raw.get("games"))
    goals   = _safe_int(raw.get("goals"))
    assists = _safe_int(raw.get("assists"))
    shots   = _safe_int(raw.get("shots"))
    keypasses = _safe_int(raw.get("key_passes"))
    xg      = _safe_float(raw.get("xG"))
    npxg    = _safe_float(raw.get("npxG"))
    xa      = _safe_float(raw.get("xA"))

    # Derived per-90 + ratios
    if minutes > 0:
        xg_per_90    = round(xg     * 90.0 / minutes, 3)
        npxg_per_90  = round(npxg   * 90.0 / minutes, 3)
        goals_per_90 = round(goals  * 90.0 / minutes, 3)
        shots_per_90 = round(shots  * 90.0 / minutes, 3)
    else:
        xg_per_90 = npxg_per_90 = goals_per_90 = shots_per_90 = 0.0
    # goals_over_xg is undefined when xG is tiny — clamp the ratio to 1.0
    # in that case so we don't whip a cold-label onto an unproven sub.
    goals_over_xg = round(goals / max(0.5, npxg), 3) if npxg > 0.5 else 1.0

    canonical = canonicalize_name(name)
    position = raw.get("position") or ""
    metrics = {
        "minutes":       minutes,
        "xg_per_90":     xg_per_90,
        "npxg_per_90":   npxg_per_90,
        "goals_per_90":  goals_per_90,
        "shots_per_90":  shots_per_90,
        "goals_over_xg": goals_over_xg,
    }
    form_label, form_score = _derive_form(metrics, position)

    return {
        "_id":             f"{canonical}__{league}__{season}",
        "player_name":     name,
        "name_canonical":  canonical,
        "understat_id":    str(raw.get("id") or ""),
        "team":            raw.get("team_title") or "",
        "league":          league,
        "season":          str(season),
        "position":        raw.get("position") or "",
        "games":           games,
        "minutes":         minutes,
        "goals":           goals,
        "xg":              round(xg,   3),
        "npxg":            round(npxg, 3),
        "assists":         assists,
        "xa":              round(xa,   3),
        "shots":           shots,
        "key_passes":      keypasses,
        "xg_per_90":       xg_per_90,
        "npxg_per_90":     npxg_per_90,
        "goals_per_90":    goals_per_90,
        "shots_per_90":    shots_per_90,
        "goals_over_xg":   goals_over_xg,
        "form_label":      form_label,
        "form_score":      form_score,
        "updated_at":      datetime.now(timezone.utc),
        "source":          "understat",
    }


# ──────────────────────────────────────────────────────────────────────────
# Public — refresh job
# ──────────────────────────────────────────────────────────────────────────

def _current_understat_season() -> int:
    """Understat seasons are keyed by the year the season STARTS in.
    A 2025/2026 season is `season=2025`. The cutover happens around
    August 1st each year — anything before August 1st belongs to the
    previous calendar year's season."""
    now = datetime.now(timezone.utc)
    return now.year if now.month >= 8 else now.year - 1


async def refresh_soccer_player_form(db) -> dict:
    """Fetch all 5 leagues from Understat and upsert into MongoDB.

    Returns a summary dict (counts per league + errors). Never raises.
    """
    season = _current_understat_season()
    started = datetime.now(timezone.utc)
    summary: dict[str, Any] = {
        "started_at": started.isoformat(),
        "season":     season,
        "leagues":    {},
        "total_upserted": 0,
        "errors":     [],
    }
    try:
        async with httpx.AsyncClient(http2=False) as client:
            for league in LEAGUES:
                try:
                    players = await _polite_post(client, league, season)
                except Exception as e:                    # noqa: BLE001
                    logger.warning("league %s: scrape error: %s", league, e)
                    summary["errors"].append(f"{league}: {e}")
                    continue
                if players is None:
                    summary["leagues"][league] = {"upserted": 0, "skipped": True}
                    continue
                upserted = 0
                for raw in players:
                    doc = _build_form_doc(raw, league, season)
                    if doc is None:
                        continue
                    try:
                        await db.soccer_player_form.update_one(
                            {"_id": doc["_id"]},
                            {"$set": doc},
                            upsert=True,
                        )
                        upserted += 1
                    except Exception as e:                # noqa: BLE001
                        logger.warning(
                            "upsert failed for %s: %s", doc.get("player_name"), e,
                        )
                summary["leagues"][league] = {"upserted": upserted}
                summary["total_upserted"] += upserted
    except Exception as e:                                # noqa: BLE001
        logger.warning("refresh_soccer_player_form failed: %s", e)
        summary["errors"].append(f"top-level: {e}")

    finished = datetime.now(timezone.utc)
    summary["finished_at"] = finished.isoformat()
    summary["elapsed_ms"]  = int((finished - started).total_seconds() * 1000)
    logger.info("Soccer player form refreshed: %s", {
        "total": summary["total_upserted"],
        "leagues": {k: v.get("upserted", 0) for k, v in summary["leagues"].items()},
        "errors": len(summary["errors"]),
    })
    return summary


async def soccer_player_form_loop(db) -> None:
    """Background scheduler — runs the form refresh every 12 hours."""
    # Initial 60s delay so the rest of the app can start up first.
    await asyncio.sleep(60)
    while True:
        try:
            await refresh_soccer_player_form(db)
        except asyncio.CancelledError:
            break
        except Exception as e:                            # noqa: BLE001
            logger.warning("soccer_player_form_loop error: %s", e)
        await asyncio.sleep(REFRESH_INTERVAL_SEC)


# ──────────────────────────────────────────────────────────────────────────
# Public — lookup + form lift
# ──────────────────────────────────────────────────────────────────────────

async def get_player_form(db, player_name: str) -> Optional[dict]:
    """Fetch the most-recent form doc for a player by name.

    Uses canonicalised name + most-recent-updated_at to handle players
    who appear in multiple leagues (e.g. mid-season transfers). Returns
    None if no record exists or the player isn't in the Top-5 leagues.
    """
    if not player_name:
        return None
    canon = canonicalize_name(player_name)
    if not canon:
        return None
    cursor = (
        db.soccer_player_form
        .find({"name_canonical": canon})
        .sort("updated_at", -1)
        .limit(1)
    )
    docs = await cursor.to_list(length=1)
    return docs[0] if docs else None


def is_goalscorer_market(pick: dict) -> bool:
    """True if the pick is a soccer goalscorer market that should
    receive the form lift. Anything else (1x2, totals, AH) returns
    False — form lift is only meaningful for player goal markets."""
    if (pick.get("sport") or "").lower() != "soccer":
        return False
    market = (pick.get("market") or "").lower()
    bet = (pick.get("bet") or "").lower()
    haystack = f"{market} {bet}"
    return any(k in haystack for k in (
        "anytime goal scorer",
        "first goal scorer",
        "last goal scorer",
        "to score or assist",
        "anytime_scorer",
        "first_goal_scorer",
        "score_or_assist",
        "goalscorer",
    ))


def compute_form_lift(form_doc: Optional[dict]) -> float:
    """Map a form doc to a probability lift in [-0.06, +0.06].

    Returns 0.0 if the doc is missing or NEUTRAL — never null. Callers
    can add this directly to a calibrated probability.
    """
    if not form_doc:
        return 0.0
    label = form_doc.get("form_label")
    if label == "HOT":
        return FORM_LIFT_HOT
    if label == "COLD":
        return FORM_LIFT_COLD
    return 0.0
