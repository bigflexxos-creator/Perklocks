"""MLB Projected-Lineup enrichment (Block 2A.5.3, 2026-08).

────────────────────────────────────────────────────────────────
Authoritative projected-lineup source
────────────────────────────────────────────────────────────────
MLB StatsAPI's ``/schedule?hydrate=lineups,probablePitcher`` endpoint
returns per-game ``lineups.homePlayers`` and ``lineups.awayPlayers``
which contain the anticipated starting lineups from ~4 h before
first pitch (once teams post them) all the way through the confirmed
lineup card release.

Empirically verified 2026-08-13:
    In-progress games     → lineups = 9 / 9   (CONFIRMED)
    Pre-Game games         → lineups = 9 / 9   (PROJECTED — teams have posted)
    Scheduled (later today)→ lineups = 0 / 0   (NOT YET POSTED — fail closed)

Provenance contract (Block 2A.5.3 §3, §4):

    ``feed/live``  ``boxscore.battingOrder`` populated
        → confirmed_starter   (source: statsapi_feed_live_batting_order)

    ``feed/live``  battingOrder empty
        AND ``schedule?hydrate=lineups``  lineups.{home,away}Players non-empty
        → projected_starter   (source: statsapi_schedule_hydrate_lineups)

    neither populated
        → unknown             (fail closed)

CONFIRMED always overrides PROJECTED (Block 2A.5.3 §4).

────────────────────────────────────────────────────────────────
NOT a new external dependency
────────────────────────────────────────────────────────────────
* Same base URL (``statsapi.mlb.com/api/v1``) as every other MLB
  integration in this repository.
* Same auth model (none — public, free).
* Same schedule endpoint we ALREADY call in
  ``services.game_context`` (``hydrate=probablePitcher``) and
  ``services.enrichment.game_resolver``.  This module just widens
  the hydrate list to include ``lineups``.

No paid provider added.  No DFS-guessing.  No name-only heuristics.
No batting-order fabrication.  If MLB hasn't published a projected
lineup for a game, we return an empty map — the caller then fails
closed per the existing contract.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import aiohttp

logger = logging.getLogger("lockscore.mlb_projected_lineup")

_MLB_BASE = "https://statsapi.mlb.com/api/v1"
_MLB_LIVE = "https://statsapi.mlb.com/api/v1.1"

# ── In-process cache ─────────────────────────────────────────────────
# Projected lineups can flip in the pre-game window as teams update
# them and as the CONFIRMED lineup lands.  A 10-minute TTL keeps
# refreshes cheap without hoarding stale projections during the
# minutes leading up to first pitch.
_TTL_SECS = 10 * 60
_SCHED_CACHE: dict[str, tuple[float, dict]] = {}   # date_str → (ts, data)
_LIVE_CACHE:  dict[int, tuple[float, dict]] = {}   # game_pk → (ts, data)


# ═════════════════════════════════════════════════════════════════════
# HTTP helpers (small, defensive — never raise upstream)
# ═════════════════════════════════════════════════════════════════════
async def _fetch_json(url: str, timeout: float = 8.0) -> Optional[dict]:
    try:
        t = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=t) as sess:
            async with sess.get(url) as r:
                if r.status != 200:
                    return None
                return await r.json()
    except Exception as e:
        logger.debug("MLB projected-lineup fetch failed url=%s err=%s", url, e)
        return None


async def _fetch_schedule_with_lineups(date_str: str) -> Optional[dict]:
    now = time.time()
    cached = _SCHED_CACHE.get(date_str)
    if cached and (now - cached[0]) < _TTL_SECS:
        return cached[1]
    url = (
        f"{_MLB_BASE}/schedule"
        f"?sportId=1&date={date_str}"
        f"&hydrate=lineups,probablePitcher"
    )
    data = await _fetch_json(url)
    if data is None:
        _SCHED_CACHE[date_str] = (now, {})
        return {}
    _SCHED_CACHE[date_str] = (now, data)
    return data


async def _fetch_boxscore_live(game_pk: int) -> Optional[dict]:
    now = time.time()
    cached = _LIVE_CACHE.get(game_pk)
    if cached and (now - cached[0]) < _TTL_SECS:
        return cached[1]
    url = f"{_MLB_LIVE}/game/{game_pk}/feed/live"
    data = await _fetch_json(url)
    if data is None:
        _LIVE_CACHE[game_pk] = (now, {})
        return {}
    _LIVE_CACHE[game_pk] = (now, data)
    return data


# ═════════════════════════════════════════════════════════════════════
# Public: fetch_mlb_lineup_bundle
# ═════════════════════════════════════════════════════════════════════
async def fetch_mlb_lineup_bundle(
    *,
    home_team: str,
    away_team: str,
    commence_time_iso: Optional[str],
    game_pk: Optional[int] = None,
) -> dict[str, Any]:
    """Return a unified lineup bundle for the given MLB game.

    Precedence (Block 2A.5.3 §4):
        1. Confirmed batting order (``feed/live``) wins if present.
        2. Otherwise the projected lineup (``schedule?hydrate=lineups``).
        3. Otherwise empty (caller must fail closed).

    Return shape::

        {
            "status":     "confirmed" | "projected" | "unknown",
            "source":     "statsapi_feed_live_batting_order"
                            | "statsapi_schedule_hydrate_lineups"
                            | None,
            "game_pk":    int | None,
            "home":       [ { "id": int, "name": str, "slot": 1..9 } , ...],
            "away":       [ ... same ... ],
            "updated_at": iso8601 UTC,
        }

    ``home``/``away`` are batting-order-ordered when confirmed; for
    projected data ordering follows StatsAPI's returned list order
    when the game hasn't been finalized (StatsAPI orders projected
    lineups by the team's posted expected batting order).
    """
    bundle: dict[str, Any] = {
        "status":     "unknown",
        "source":     None,
        "game_pk":    game_pk,
        "home":       [],
        "away":       [],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    # Resolve date and gamePk from the schedule (needed for both paths).
    if not commence_time_iso:
        return bundle
    date_str = commence_time_iso[:10]
    if not date_str:
        return bundle

    sched = await _fetch_schedule_with_lineups(date_str)
    if not sched:
        return bundle

    def _tm_match(a: str, b: str) -> bool:
        a, b = (a or "").strip().lower(), (b or "").strip().lower()
        return bool(a) and bool(b) and (a in b or b in a)

    matched_game: Optional[dict] = None
    for d in sched.get("dates", []):
        for g in d.get("games", []):
            teams = g.get("teams") or {}
            hn = ((teams.get("home") or {}).get("team") or {}).get("name", "")
            an = ((teams.get("away") or {}).get("team") or {}).get("name", "")
            if _tm_match(home_team, hn) and _tm_match(away_team, an):
                matched_game = g
                break
        if matched_game:
            break

    if not matched_game:
        return bundle
    bundle["game_pk"] = matched_game.get("gamePk")

    # ── 1) CONFIRMED path (feed/live boxscore.battingOrder) ──────────
    if bundle["game_pk"]:
        live = await _fetch_boxscore_live(int(bundle["game_pk"]))
        if live:
            box = ((live.get("liveData") or {}).get("boxscore") or {})
            for side, key in (("home", "home"), ("away", "away")):
                team = (box.get("teams") or {}).get(side) or {}
                order = team.get("battingOrder") or []
                players = team.get("players") or {}
                out_rows: list[dict] = []
                for slot_idx, pid in enumerate(order[:9], start=1):
                    pkey = f"ID{pid}" if not str(pid).startswith("ID") else str(pid)
                    info = players.get(pkey) or {}
                    person = info.get("person") or {}
                    name = (person.get("fullName") or "").strip()
                    if not name:
                        continue
                    out_rows.append({
                        "id":   person.get("id") or pid,
                        "name": name,
                        "slot": slot_idx,
                    })
                if out_rows:
                    bundle[key] = out_rows
            if bundle["home"] and bundle["away"]:
                bundle["status"] = "confirmed"
                bundle["source"] = "statsapi_feed_live_batting_order"
                return bundle
            # Partial confirmed (one side posted) is still marked
            # "confirmed" for the side that has it; the other side
            # falls back to projected below.
            _partial_confirmed_home = bool(bundle["home"])
            _partial_confirmed_away = bool(bundle["away"])
        else:
            _partial_confirmed_home = False
            _partial_confirmed_away = False
    else:
        _partial_confirmed_home = False
        _partial_confirmed_away = False

    # ── 2) PROJECTED path (schedule?hydrate=lineups) ────────────────
    lu = matched_game.get("lineups") or {}
    home_players = lu.get("homePlayers") or []
    away_players = lu.get("awayPlayers") or []

    if home_players and not bundle["home"]:
        rows: list[dict] = []
        for slot_idx, p in enumerate(home_players[:9], start=1):
            name = (p.get("fullName") or "").strip()
            if not name:
                continue
            rows.append({"id": p.get("id"), "name": name, "slot": slot_idx})
        bundle["home"] = rows
    if away_players and not bundle["away"]:
        rows2: list[dict] = []
        for slot_idx, p in enumerate(away_players[:9], start=1):
            name = (p.get("fullName") or "").strip()
            if not name:
                continue
            rows2.append({"id": p.get("id"), "name": name, "slot": slot_idx})
        bundle["away"] = rows2

    # Decide unified status.  If BOTH sides confirmed → confirmed.  If
    # EITHER side is confirmed → the bundle is a MIX; we report
    # ``confirmed`` overall (safest for the caller) and let the
    # per-player provenance (populated by ``build_hitter_rows``) carry
    # the true per-hitter status.  If neither side is confirmed but
    # at least one has projected data → ``projected``.  Otherwise
    # ``unknown``.
    if _partial_confirmed_home and _partial_confirmed_away:
        bundle["status"] = "confirmed"
        bundle["source"] = "statsapi_feed_live_batting_order"
    elif _partial_confirmed_home or _partial_confirmed_away:
        # Mixed provenance: mark as confirmed at the bundle level for
        # backwards compatibility, but the per-player rows below
        # carry per-side truth.
        bundle["status"] = "confirmed"
        bundle["source"] = "statsapi_feed_live_batting_order"
    elif bundle["home"] or bundle["away"]:
        bundle["status"] = "projected"
        bundle["source"] = "statsapi_schedule_hydrate_lineups"

    return bundle


# ═════════════════════════════════════════════════════════════════════
# Public: build_hitter_rows
# ═════════════════════════════════════════════════════════════════════
def build_hitter_rows(
    bundle: dict,
    *,
    _partial_confirmed_home: bool = False,
    _partial_confirmed_away: bool = False,
) -> dict[str, dict]:
    """Turn a lineup bundle into ``{name_lower: {row}}`` for use in
    ``ctx["hitters"]``.  Every emitted row carries explicit provenance
    fields per Block 2A.5.3 §3:

        {
            "lineup_confirmed": bool,    # True only for confirmed
            "is_starter":       True,
            "lineup_slot":      int 1..9,
            "lineup_source":    "...",
            "lineup_updated_at": iso,
            "is_home":          bool,
            "mlb_player_id":    int,     # canonical player identity
        }
    """
    out: dict[str, dict] = {}
    # Track per-side status to preserve mixed-provenance correctness:
    # a bundle with confirmed home + projected away needs each row to
    # carry the actual per-side lineup_source.
    per_side_status: dict[str, tuple[bool, str]] = {}
    if bundle.get("status") == "confirmed":
        per_side_status["home"] = (True, "statsapi_feed_live_batting_order")
        per_side_status["away"] = (True, "statsapi_feed_live_batting_order")
    elif bundle.get("status") == "projected":
        per_side_status["home"] = (False, "statsapi_schedule_hydrate_lineups")
        per_side_status["away"] = (False, "statsapi_schedule_hydrate_lineups")
    else:
        return {}

    updated = bundle.get("updated_at") or datetime.now(timezone.utc).isoformat()
    for side, is_home in (("home", True), ("away", False)):
        confirmed, source = per_side_status.get(side, (False, "unknown"))
        for row in bundle.get(side, []) or []:
            name = (row.get("name") or "").strip()
            if not name:
                continue
            out[name.lower()] = {
                "lineup_confirmed":   confirmed,
                "is_starter":         True,
                "lineup_slot":        int(row.get("slot") or 0) or None,
                "lineup_source":      source,
                "lineup_updated_at":  updated,
                "is_home":            is_home,
                "mlb_player_id":      row.get("id"),
            }
    return out


# ═════════════════════════════════════════════════════════════════════
# Public: post-emission enrichment (called from prop refresh path)
# ═════════════════════════════════════════════════════════════════════
async def enrich_pick_with_projected_lineup(pick: dict) -> dict:
    """Attach a normalized ``pick["lineup_status"]`` dict when a
    projected-lineup is available and no confirmed lineup exists yet.

    Contract (Block 2A.5.3 §3):

        {
            "status":     "CONFIRMED" | "PROJECTED" | "UNKNOWN",
            "lineup_pos": 1..9 | None,
            "source":     str,
            "updated_at": iso,
        }

    Idempotent — if ``pick["lineup_status"]`` is already
    ``CONFIRMED``, this call is a no-op.  Otherwise the most
    authoritative signal available at call time is stamped.

    Safe on network failure: leaves the existing lineup_status
    untouched.
    """
    existing = pick.get("lineup_status") or {}
    if isinstance(existing, dict):
        _status = str(existing.get("status") or "").lower()
    else:
        _status = str(existing or "").lower()
    if _status in ("confirmed", "confirmed_start", "confirmed_starter"):
        # Confirmed cannot be downgraded.
        return pick

    player = ((pick.get("player_name") or "").strip().lower())
    if not player:
        return pick
    # Try to resolve home/away from the pick.
    event = pick.get("event") or ""
    home = pick.get("home_team") or ""
    away = pick.get("away_team") or ""
    if not (home and away) and " @ " in event:
        try:
            away, home = [s.strip() for s in event.split(" @ ", 1)]
        except Exception:
            pass
    if not (home and away):
        return pick
    commence = pick.get("event_time") or pick.get("commence_time") or ""
    if not commence:
        return pick
    try:
        bundle = await fetch_mlb_lineup_bundle(
            home_team=home, away_team=away, commence_time_iso=commence,
        )
    except Exception:
        return pick
    rows = build_hitter_rows(bundle)
    row = rows.get(player)
    if not row:
        # Player is not in projected OR confirmed lineup — fail closed
        # (mark unknown / bench per confirmed vs projected precedence).
        if bundle.get("status") == "confirmed":
            # Confirmed lineup exists but player is not in it → bench.
            pick["lineup_status"] = {
                "status":     "BENCH",
                "lineup_pos": None,
                "source":     bundle.get("source"),
                "updated_at": bundle.get("updated_at"),
            }
        else:
            # Neither confirmed nor projected has this player.
            pick["lineup_status"] = {
                "status":     "UNKNOWN",
                "lineup_pos": None,
                "source":     bundle.get("source"),
                "updated_at": bundle.get("updated_at"),
            }
        return pick
    # Found — stamp the correct provenance.
    if row.get("lineup_confirmed"):
        pick["lineup_status"] = {
            "status":     "CONFIRMED",
            "lineup_pos": row.get("lineup_slot"),
            "source":     row.get("lineup_source"),
            "updated_at": row.get("lineup_updated_at"),
        }
    else:
        pick["lineup_status"] = {
            "status":     "PROJECTED",
            "lineup_pos": row.get("lineup_slot"),
            "source":     row.get("lineup_source"),
            "updated_at": row.get("lineup_updated_at"),
        }
    return pick


__all__ = [
    "fetch_mlb_lineup_bundle",
    "build_hitter_rows",
    "enrich_pick_with_projected_lineup",
]
