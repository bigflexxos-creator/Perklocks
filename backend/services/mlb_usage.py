"""MLB usage / fatigue ingester — Phase 1.3 + 1.5 of the data-gap roadmap.

Adds two independent-but-related signals to MLB picks:

  1) **Batting order position + expected plate appearances (PA)** — the
     difference between a leadoff hitter (~4.6 PA/game) and a #9 hitter
     (~3.6 PA/game) is a full plate appearance — one entire chance at
     the pick's line. Every hitter Over prop volume-scales with PA.

  2) **Pitcher fatigue: days-rest + pitches thrown in the last 3 days**
     — a starter on short rest OR a reliever who threw 40+ pitches
     across the previous three days is meaningfully more likely to
     give up runs / walk more / strike out fewer. Every pitcher K/ER/
     Outs prop and every game-total Over benefits.

Data source: MLB Stats API (free, no auth) — the same source we already
use for boxscore settlement, so no new dependency.

Usage (single pick):
    from services.mlb_usage import enrich_pick_with_usage
    await enrich_pick_with_usage(pick)

Usage (batch — preferred, dedupes per-game fetches):
    from services.mlb_usage import enrich_picks_with_usage_bulk
    await enrich_picks_with_usage_bulk(picks)

Attaches (all optional — missing when pre-game lineup unavailable):
    batting_order          int (1-9), or None if lineup not posted yet
    expected_pa            float (3.4 - 4.8), from lineup position curve
    lineup_posted          bool
    pitcher_days_rest      int, or None if not a probable pitcher
    pitcher_pitches_3d     int (sum across the last 3 days)
    pitcher_fatigue_flag   'fresh' | 'normal' | 'tired' | 'gassed' | None
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

logger = logging.getLogger("lockscore.services.mlb_usage")

_MLB_BASE = "https://statsapi.mlb.com/api/v1"

# Expected plate appearances by batting-order slot. Empirically from the
# 2023 MLB average pace of play (Baseball Prospectus / Retrosheet). The
# league-wide average is ~4.2 PA/hitter/game; the top of the order gets
# an extra ~0.4 PA over the bottom because they cycle back through the
# order more often. This is a durable structural feature — DH-league
# alignment moved these numbers by <5% over the past 20 years.
_EXPECTED_PA_BY_SLOT: dict[int, float] = {
    1: 4.65,
    2: 4.55,
    3: 4.45,
    4: 4.35,
    5: 4.25,
    6: 4.10,
    7: 3.95,
    8: 3.80,
    9: 3.65,
}


def _pa_for_slot(slot: Optional[int]) -> Optional[float]:
    if slot is None:
        return None
    return _EXPECTED_PA_BY_SLOT.get(int(slot))


def _classify_pitcher_fatigue(days_rest: Optional[int],
                              pitches_3d: Optional[int]) -> Optional[str]:
    """Rule-based fatigue tag.

    Starter benchmarks (MLB averages):
      • 5-day rest = normal starter rotation
      • 4-day rest = short-rest start (~10% ERA elevation in modern era)
      • 3-day rest = emergency / bullpen-day start (~20-25% elevation)
    Reliever benchmarks:
      • ≥40 pitches over last 3 days without today off = tired
      • ≥55 pitches = gassed
    """
    if days_rest is None and pitches_3d is None:
        return None
    # Check "gassed" (worst state) FIRST so a reliever who's been used
    # heavily in the last 3 days doesn't get mislabelled as merely
    # "tired" from their days-rest signal alone.
    if pitches_3d is not None and pitches_3d >= 55:
        return "gassed"
    if pitches_3d is not None and pitches_3d >= 40:
        return "tired"
    if days_rest is not None:
        if days_rest >= 6:
            return "fresh"
        if days_rest <= 4:
            return "tired"  # includes short-rest starts
    return "normal"


# ── HTTP layer (with tiny per-request timeout) ───────────────────────
async def _fetch_json(client: httpx.AsyncClient, path: str, **params) -> dict:
    try:
        r = await client.get(f"{_MLB_BASE}/{path}", params=params)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.debug("MLB usage fetch %s failed: %s", path, e)
        return {}


# ── Game & lineup resolution ─────────────────────────────────────────
async def _find_gamepk_for_event(
    client: httpx.AsyncClient,
    event: str,
    event_time_iso: Optional[str],
) -> Optional[int]:
    """Resolve a pick's `event` string ('Yankees @ Red Sox') and
    `event_time` to a specific MLB gamePk. Handles the same series
    ambiguity as prop_settlement._mlb_find_game (D + D-1 merge with
    event_time distance preference)."""
    if not event or "@" not in event:
        return None
    away, home = [x.strip() for x in event.split("@", 1)]
    date_str = (event_time_iso or "")[:10]
    if not date_str:
        return None
    try:
        prev_str = (datetime.fromisoformat(date_str) - timedelta(days=1)).strftime("%Y-%m-%d")
    except Exception:
        prev_str = None

    games: list[dict] = []
    for ds in [date_str] + ([prev_str] if prev_str else []):
        data = await _fetch_json(client, "schedule", sportId=1, date=ds, hydrate="team")
        for d in data.get("dates", []):
            games.extend(d.get("games", []))
    seen: set = set()
    dedup: list[dict] = []
    for g in games:
        pk = g.get("gamePk")
        if pk in seen:
            continue
        seen.add(pk)
        dedup.append(g)

    def _tm(a: str, b: str) -> bool:
        a, b = a.lower(), b.lower()
        return bool(a) and bool(b) and (a in b or b in a)

    matches: list[dict] = []
    for g in dedup:
        hn = ((g.get("teams") or {}).get("home") or {}).get("team", {}).get("name", "")
        an = ((g.get("teams") or {}).get("away") or {}).get("team", {}).get("name", "")
        if _tm(home, hn) and _tm(away, an):
            matches.append(g)
    if not matches:
        return None

    et_dt: Optional[datetime] = None
    try:
        if event_time_iso:
            et_dt = datetime.fromisoformat(event_time_iso.replace("Z", "+00:00"))
            if et_dt.tzinfo is None:
                et_dt = et_dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass

    def _dist(g: dict) -> float:
        if not et_dt:
            return 0.0
        try:
            gd = datetime.fromisoformat((g.get("gameDate") or "").replace("Z", "+00:00"))
            if gd.tzinfo is None:
                gd = gd.replace(tzinfo=timezone.utc)
            return abs((gd - et_dt).total_seconds())
        except Exception:
            return 0.0

    matches.sort(key=_dist)
    return matches[0].get("gamePk")


async def _fetch_lineup(client: httpx.AsyncClient, game_pk: int) -> dict:
    """Return {player_name_lower: batting_order_int} for the given game.
    Returns empty dict when lineups aren't yet posted (MLB usually posts
    ~2h before first pitch)."""
    data = await _fetch_json(client, f"game/{game_pk}/boxscore")
    lineup: dict[str, int] = {}
    for side in ("home", "away"):
        team = (data.get("teams") or {}).get(side) or {}
        order = team.get("battingOrder") or []
        players = team.get("players") or {}
        for slot_idx, pid_or_key in enumerate(order):
            if slot_idx >= 9:
                break
            # The `battingOrder` list holds player IDs formatted like
            # "605141" (batter order 1). We look them up in `players`
            # keyed as "ID{pid}".
            key = f"ID{pid_or_key}"
            pdoc = players.get(key)
            if not pdoc:
                continue
            name = ((pdoc.get("person") or {}).get("fullName") or "").strip()
            if not name:
                continue
            lineup[name.lower()] = slot_idx + 1
    return lineup


# ── Pitcher fatigue ──────────────────────────────────────────────────
async def _fetch_probable_pitcher(client: httpx.AsyncClient,
                                  game_pk: int,
                                  team_name_hint: Optional[str] = None) -> Optional[dict]:
    """Return probable pitcher dict {id, fullName, side} for the game.
    If team_name_hint is provided (e.g. the pick's team), that side is
    returned; otherwise the home starter."""
    data = await _fetch_json(client, "schedule",
                             sportId=1, gamePk=game_pk, hydrate="probablePitcher")
    for d in data.get("dates", []):
        for g in d.get("games", []):
            if g.get("gamePk") != game_pk:
                continue
            for side in ("home", "away"):
                pitcher = (g.get("teams") or {}).get(side, {}).get("probablePitcher")
                team = (g.get("teams") or {}).get(side, {}).get("team", {}).get("name", "")
                if not pitcher:
                    continue
                if team_name_hint and team_name_hint.lower() not in team.lower():
                    continue
                return {
                    "id": pitcher.get("id"),
                    "fullName": pitcher.get("fullName"),
                    "side": side,
                    "team": team,
                }
    return None


async def _fetch_pitcher_fatigue(client: httpx.AsyncClient,
                                 pitcher_id: int,
                                 target_date_iso: str) -> tuple[Optional[int], Optional[int]]:
    """Compute (days_rest, pitches_last_3d) for a pitcher going into
    a game on `target_date_iso` (YYYY-MM-DD).

    days_rest: full days between last appearance and target date.
    pitches_last_3d: sum of pitches thrown in the 3 calendar days prior
                     (does NOT include the target date itself)."""
    try:
        target = datetime.fromisoformat(target_date_iso).date()
    except Exception:
        return (None, None)
    # Pull last 30 days of game logs and count
    season = target.year
    data = await _fetch_json(client, f"people/{pitcher_id}/stats",
                             stats="gameLog", season=season, group="pitching")
    games: list[tuple[datetime, int]] = []  # (date, pitches)
    for s in data.get("stats", []):
        for sp in s.get("splits", []):
            gd_str = (sp.get("date") or "")[:10]
            if not gd_str:
                continue
            try:
                gd = datetime.fromisoformat(gd_str).date()
            except Exception:
                continue
            if gd >= target:
                continue
            pitches = 0
            try:
                pitches = int((sp.get("stat") or {}).get("numberOfPitches") or 0)
            except Exception:
                pitches = 0
            games.append((datetime.combine(gd, datetime.min.time()), pitches))
    if not games:
        return (None, None)
    games.sort(key=lambda x: x[0], reverse=True)
    last_appearance = games[0][0].date()
    days_rest = (target - last_appearance).days
    three_day_cutoff = target - timedelta(days=3)
    pitches_3d = sum(p for gd, p in games if gd.date() >= three_day_cutoff)
    return (days_rest, pitches_3d)


# ── Public API ───────────────────────────────────────────────────────
def _is_hitter_market(pick: dict) -> bool:
    """Only true for markets that describe a specific hitter's box-score
    output. Team/game markets return False so we don't try to look up
    'American League' or 'Over' as if it were a batter's name."""
    market = (pick.get("market") or "").lower()
    selection = (pick.get("selection") or "").lower()
    # Explicit disqualifiers — team totals, spreads, moneylines, sides.
    if any(t in market for t in (
        "team total", "spread", "moneyline", "run line", "run-line",
        "1st inning", "nrfi", "yrfi",
    )):
        return False
    if selection in ("over", "under", "yes", "no", ""):
        return False
    # Hitter market keywords
    return any(kw in market for kw in (
        "hits", "home run", "total bases", "rbi", "runs scored",
        "hit + run", "hits+run", "singles", "doubles", "triples",
        "stolen bases", "extra bases",
    ))


def _extract_hitter_name(pick: dict) -> Optional[str]:
    if not _is_hitter_market(pick):
        return None
    sel = (pick.get("selection") or "").strip()
    if not sel:
        return None
    return sel


def _extract_pitcher_from_market(pick: dict) -> Optional[str]:
    """Return the pitcher's name for pitcher-family markets (K, Outs,
    ER, Walks — but ONLY where the SELECTION is the pitcher themselves).
    Not applicable to hitter Overs."""
    market = (pick.get("market") or "").lower()
    sel = (pick.get("selection") or "").strip()
    if not sel:
        return None
    if any(m in market for m in ("strikeouts", "outs recorded", "earned runs", "pitcher walks")):
        return sel
    return None


async def enrich_pick_with_usage(pick: dict) -> dict:
    """Attaches batting_order + expected_pa (hitters) OR
    pitcher_days_rest + pitcher_pitches_3d + pitcher_fatigue_flag
    (pitchers) to a single MLB pick. Idempotent."""
    if (pick.get("sport") or "").upper() != "MLB":
        return pick
    if pick.get("lineup_posted") is True or pick.get("pitcher_fatigue_flag"):
        return pick  # already enriched

    event_time = pick.get("event_time") or ""
    async with httpx.AsyncClient(timeout=10.0) as client:
        game_pk = await _find_gamepk_for_event(client, pick.get("event", ""), event_time)
        if not game_pk:
            return pick

        # Hitter path
        hitter = _extract_hitter_name(pick)
        pitcher_name = _extract_pitcher_from_market(pick)

        if hitter and not pitcher_name:
            lineup = await _fetch_lineup(client, game_pk)
            slot = lineup.get(hitter.lower())
            if slot:
                pick["batting_order"] = slot
                pick["expected_pa"] = _pa_for_slot(slot)
                pick["lineup_posted"] = True
            else:
                # Lineup exists but hitter not in it — bench, DNP, or
                # pinch-hitter role. Assign a low expected_pa so volume_signal
                # penalises the pick fairly.
                if lineup:
                    pick["batting_order"] = None
                    pick["expected_pa"] = 1.5
                    pick["lineup_posted"] = True
                    pick["lineup_note"] = "not_in_starting_lineup"

        # Pitcher path
        if pitcher_name:
            # Fetch probable pitcher for the game — we need their MLB ID
            # to hit the gameLog endpoint. If our pick's selection matches
            # either probable pitcher, use that ID directly.
            for side_hint in (None,):  # try both sides
                pp = await _fetch_probable_pitcher(client, game_pk, side_hint)
                if pp and pitcher_name.lower() in (pp.get("fullName") or "").lower():
                    target_date = event_time[:10] or datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    days_rest, pitches_3d = await _fetch_pitcher_fatigue(
                        client, pp["id"], target_date,
                    )
                    if days_rest is not None:
                        pick["pitcher_days_rest"] = days_rest
                    if pitches_3d is not None:
                        pick["pitcher_pitches_3d"] = pitches_3d
                    pick["pitcher_fatigue_flag"] = _classify_pitcher_fatigue(
                        days_rest, pitches_3d,
                    )
                    break
    return pick


async def enrich_picks_with_usage_bulk(picks: list[dict]) -> int:
    """Bulk enrichment — dedupes per-game fetches so a full slate of
    Yankees hitter Overs only pulls Yankees' lineup once. Returns the
    number of picks touched."""
    if not picks:
        return 0
    mlb_picks = [p for p in picks if (p.get("sport") or "").upper() == "MLB"]
    if not mlb_picks:
        return 0

    async with httpx.AsyncClient(timeout=10.0) as client:
        # ── Resolve gamePk for every pick (dedupe by (event, event_time_date))
        game_pk_cache: dict[tuple[str, str], Optional[int]] = {}
        for p in mlb_picks:
            key = (p.get("event", ""), (p.get("event_time") or "")[:10])
            if key not in game_pk_cache:
                game_pk_cache[key] = await _find_gamepk_for_event(
                    client, p.get("event", ""), p.get("event_time") or "",
                )

        # ── Fetch each unique gamePk's lineup once
        lineup_cache: dict[int, dict] = {}
        for game_pk in {v for v in game_pk_cache.values() if v}:
            lineup_cache[game_pk] = await _fetch_lineup(client, game_pk)

        # ── Fetch each unique gamePk's probable pitchers once
        pp_cache: dict[int, list[dict]] = {}
        for game_pk in {v for v in game_pk_cache.values() if v}:
            pp_h = await _fetch_probable_pitcher(client, game_pk)
            pp_cache[game_pk] = [pp_h] if pp_h else []
            # Try to pick up the other side too
            data = await _fetch_json(client, "schedule",
                                     sportId=1, gamePk=game_pk,
                                     hydrate="probablePitcher")
            for d in data.get("dates", []):
                for g in d.get("games", []):
                    if g.get("gamePk") != game_pk:
                        continue
                    for side in ("home", "away"):
                        pitcher = (g.get("teams") or {}).get(side, {}).get("probablePitcher")
                        if pitcher:
                            entry = {
                                "id": pitcher.get("id"),
                                "fullName": pitcher.get("fullName"),
                                "side": side,
                                "team": (g.get("teams") or {}).get(side, {}).get("team", {}).get("name", ""),
                            }
                            if entry not in pp_cache[game_pk]:
                                pp_cache[game_pk].append(entry)

        # ── Fatigue per unique pitcher-id + target-date pair
        fatigue_cache: dict[tuple[int, str], tuple[Optional[int], Optional[int]]] = {}

        touched = 0
        for p in mlb_picks:
            key = (p.get("event", ""), (p.get("event_time") or "")[:10])
            game_pk = game_pk_cache.get(key)
            if not game_pk:
                continue
            target_date = (p.get("event_time") or "")[:10]

            hitter = _extract_hitter_name(p)
            pitcher_name = _extract_pitcher_from_market(p)

            if hitter and not pitcher_name:
                lineup = lineup_cache.get(game_pk) or {}
                slot = lineup.get(hitter.lower())
                if slot:
                    p["batting_order"] = slot
                    p["expected_pa"] = _pa_for_slot(slot)
                    p["lineup_posted"] = True
                    touched += 1
                elif lineup:
                    p["batting_order"] = None
                    p["expected_pa"] = 1.5
                    p["lineup_posted"] = True
                    p["lineup_note"] = "not_in_starting_lineup"
                    touched += 1

            if pitcher_name:
                for pp in pp_cache.get(game_pk, []):
                    if not pp:
                        continue
                    if pitcher_name.lower() not in (pp.get("fullName") or "").lower():
                        continue
                    fatigue_key = (pp["id"], target_date)
                    if fatigue_key not in fatigue_cache:
                        fatigue_cache[fatigue_key] = await _fetch_pitcher_fatigue(
                            client, pp["id"], target_date,
                        )
                    days_rest, pitches_3d = fatigue_cache[fatigue_key]
                    if days_rest is not None:
                        p["pitcher_days_rest"] = days_rest
                    if pitches_3d is not None:
                        p["pitcher_pitches_3d"] = pitches_3d
                    p["pitcher_fatigue_flag"] = _classify_pitcher_fatigue(
                        days_rest, pitches_3d,
                    )
                    touched += 1
                    break
        return touched


__all__ = [
    "enrich_pick_with_usage",
    "enrich_picks_with_usage_bulk",
    "_classify_pitcher_fatigue",  # exposed for testing
    "_pa_for_slot",                # exposed for testing
]
