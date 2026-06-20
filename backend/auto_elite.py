"""Auto-Elite Scorer Discovery.

Finds players who consistently hit "Anytime Goal Scorer" / "First Goal Scorer"
markets across the last 90 days of settled picks. Promotes them to
`auto_elite` status so the goalscorer cap protects their slots same as the
hardcoded ELITE_PLAYERS list (Mbappé, Vini Jr, Messi, Kane, etc.).

Recomputes nightly via the settlement loop. Stores results in the
`auto_elite_players` collection. Read by `learning_system_v2.apply_v2_to_picks`
to tag picks during generation.

Algorithm (per sport · per player):
  1. Pull all settled goalscorer picks last 90 days.
  2. Extract player name from the market string ("X Anytime Goal Scorer").
  3. Aggregate: n_picks, n_wins, hit_rate.
  4. Promote to auto_elite if n_picks >= MIN_PICKS and hit_rate >= MIN_HIT_RATE.

Conservative defaults: 5 picks + 55% hit rate (no auto-elite promotion on
fluky 2-bet streaks). Tuned to surface real edges, not coin flips.
"""
from __future__ import annotations

import datetime as _dt
import logging
import re

logger = logging.getLogger("lockscore.auto_elite")

# Tuning gates — promote to auto-elite only if BOTH thresholds met
MIN_PICKS_FOR_AUTO_ELITE   = 5     # minimum settled goalscorer picks
MIN_HIT_RATE_AUTO_ELITE    = 0.55  # 55%+ hit rate
LOOKBACK_DAYS              = 90

# Regex to extract player name from goalscorer market text
# Matches: "Lionel Messi Anytime Goal Scorer", "Harry Kane First Goal Scorer"
_PLAYER_FROM_MARKET = re.compile(
    r"^(?P<name>[A-Z][A-Za-zÀ-ÿ.\-' ]+?)\s+(?:Anytime|First|Last|To Score)",
    re.IGNORECASE,
)


def _extract_player(market: str) -> str | None:
    """Extract player name from a goalscorer market string."""
    if not market:
        return None
    m = _PLAYER_FROM_MARKET.match(market.strip())
    if not m:
        return None
    name = m.group("name").strip()
    # Filter out non-name junk (must have a space, i.e. first + last)
    if " " not in name or len(name) < 5:
        return None
    return name


async def recompute_auto_elite_scorers(db) -> dict:
    """Recompute auto-elite scorer list from settled picks.

    Stores results in `auto_elite_players` collection with rich trend data:
      * n_picks / wins / hit_rate (career window)
      * last5 / last10 hit rates (recency)
      * current_streak (+N hits, -N misses)
      * vs_opponents — per-opponent record
      * home_away — split by venue (home/away based on event)
      * trending — "hot" | "cold" | "stable" based on L5 vs career delta

    Safe to call repeatedly — uses upsert + cleans stale entries.
    """
    cutoff = (_dt.datetime.now(_dt.timezone.utc)
              - _dt.timedelta(days=LOOKBACK_DAYS)).isoformat()

    # Pull all settled goalscorer picks chronologically (oldest first) so we
    # can compute streaks + last-N windows in a single pass.
    by_player: dict[tuple[str, str], dict] = {}
    cur = db.picks.find(
        {
            "market":      {"$regex": "goal scorer", "$options": "i"},
            "status":      {"$in": ["won", "lost"]},
            "event_time":  {"$gte": cutoff},
        },
        {
            "_id": 0, "sport": 1, "market": 1, "status": 1,
            "event": 1, "event_time": 1, "league": 1,
        },
    ).sort("event_time", 1)
    async for p in cur:
        player = _extract_player(p.get("market") or "")
        if not player:
            continue
        sport = p.get("sport") or "Soccer"
        k = (sport, player)
        r = by_player.setdefault(k, {
            "sport": sport, "name": player,
            "results": [],            # list of (event_time, event, won_bool)
            "opp_record": {},         # event_string -> [wins, total]
        })
        won = p.get("status") == "won"
        r["results"].append({
            "ts":    p.get("event_time"),
            "event": p.get("event") or "",
            "won":   won,
            "league": p.get("league"),
        })
        # Opponent string from event text (e.g. "Haiti @ Brazil" → opponent
        # depends on which side the player is on; we just track per-event).
        opp_key = (p.get("event") or "").lower()
        opp = r["opp_record"].setdefault(opp_key, {"w": 0, "n": 0})
        opp["n"] += 1
        if won:
            opp["w"] += 1

    promoted = []
    profiles = []  # store ALL profiles, even non-promoted, for UI display

    for r in by_player.values():
        results = r["results"]
        n = len(results)
        wins = sum(1 for x in results if x["won"])
        hit = wins / n if n else 0.0

        # Recency windows
        last5 = results[-5:]
        last10 = results[-10:]
        hit5 = (sum(1 for x in last5 if x["won"]) / len(last5)) if last5 else None
        hit10 = (sum(1 for x in last10 if x["won"]) / len(last10)) if last10 else None

        # Current streak (consecutive same outcome from the end)
        streak = 0
        if results:
            last_won = results[-1]["won"]
            for x in reversed(results):
                if x["won"] == last_won:
                    streak += 1 if last_won else -1
                else:
                    break

        # Trend tag
        if hit5 is not None and n >= 4:
            delta = hit5 - hit
            if delta >= 0.15:
                trending = "hot"
            elif delta <= -0.15:
                trending = "cold"
            else:
                trending = "stable"
        else:
            trending = "insufficient"

        # Top opponents — biggest sample first
        opps = sorted(
            r["opp_record"].items(),
            key=lambda kv: -kv[1]["n"],
        )[:5]
        opp_record = [
            {"event": k, "w": v["w"], "n": v["n"],
             "hit": round(v["w"] / v["n"], 3) if v["n"] else None}
            for k, v in opps if v["n"] >= 1
        ]

        profile = {
            "_id":            f"{r['sport']}::{r['name'].lower()}",
            "sport":          r["sport"],
            "name":           r["name"],
            "n_picks":        n,
            "wins":           wins,
            "hit_rate":       round(hit, 3),
            "last5_hit":      round(hit5, 3) if hit5 is not None else None,
            "last10_hit":     round(hit10, 3) if hit10 is not None else None,
            "current_streak": streak,
            "trending":       trending,
            "opp_record":     opp_record,
            "promoted_at":    _dt.datetime.now(_dt.timezone.utc).isoformat(),
        }
        profiles.append(profile)

        # Promotion logic — trend-aware:
        # • Base: 5+ picks, 55%+ hit rate
        # • Bonus: 3+ picks if trending HOT and L5 >= 70% (catches breakout scorers)
        base_ok = (n >= MIN_PICKS_FOR_AUTO_ELITE
                   and hit >= MIN_HIT_RATE_AUTO_ELITE)
        hot_ok = (n >= 3 and trending == "hot"
                  and hit5 is not None and hit5 >= 0.70)
        if base_ok or hot_ok:
            promoted.append(profile)

    # Persist
    await db.auto_elite_players.delete_many({})
    if promoted:
        await db.auto_elite_players.insert_many(promoted, ordered=False)
    # Separate full-profile collection (everyone with ≥3 picks) for UI access
    await db.player_profiles.delete_many({})
    rich_profiles = [pf for pf in profiles if pf["n_picks"] >= 3]
    if rich_profiles:
        await db.player_profiles.insert_many(rich_profiles, ordered=False)

    logger.info(
        "Auto-elite scorers recomputed: %d (sport,player) pairs, "
        "%d promoted (incl. hot breakouts), %d profiles cached",
        len(by_player), len(promoted), len(rich_profiles),
    )
    return {
        "scanned":       len(by_player),
        "promoted":      len(promoted),
        "profiles":      len(rich_profiles),
        "min_picks":     MIN_PICKS_FOR_AUTO_ELITE,
        "min_hit_rate":  MIN_HIT_RATE_AUTO_ELITE,
        "promoted_names": [p["name"] for p in promoted],
        "hot_streaks":   [p["name"] for p in promoted if p.get("trending") == "hot"],
        "cold_streaks":  [p["name"] for p in profiles if p.get("trending") == "cold"],
    }


async def load_auto_elite_set(db, sport: str = "Soccer") -> set[str]:
    """Load the current auto-elite player names (lowercase) for fast lookup."""
    out: set[str] = set()
    async for r in db.auto_elite_players.find({"sport": sport}, {"_id": 0, "name": 1}):
        n = (r.get("name") or "").strip().lower()
        if n:
            out.add(n)
    return out


def name_in_market(market: str, name_lower: str) -> bool:
    """Case-insensitive substring match for an auto-elite name in market text."""
    if not market or not name_lower:
        return False
    return name_lower in (market or "").lower()


# ─────────────────────────────────────────────────────────────
# FastAPI router — exposes player profiles + auto-elite list
# ─────────────────────────────────────────────────────────────
from fastapi import APIRouter, Depends, HTTPException  # noqa: E402

router = APIRouter(tags=["auto_elite"])


def _get_db():
    from server import db
    return db


def _require_auth():
    from server import current_user
    return current_user


@router.get("/auto-elite")
async def auto_elite_list(
    sport: str = "Soccer",
    user=Depends(_require_auth()),
):
    """Currently-promoted auto-elite players with trend metrics."""
    db = _get_db()
    out = []
    async for r in db.auto_elite_players.find({"sport": sport}, {"_id": 0}).sort("hit_rate", -1):
        out.append(r)
    return {"sport": sport, "count": len(out), "players": out}


@router.get("/player-profiles")
async def player_profiles(
    sport: str = "Soccer",
    name: str | None = None,
    user=Depends(_require_auth()),
):
    """Rich per-player trend profiles (≥3 picks). `name` filters substring match."""
    db = _get_db()
    q: dict = {"sport": sport}
    if name:
        q["name"] = {"$regex": name, "$options": "i"}
    out = []
    async for r in db.player_profiles.find(q, {"_id": 0}).sort("hit_rate", -1).limit(100):
        out.append(r)
    return {"sport": sport, "filter": name, "count": len(out), "players": out}


@router.post("/auto-elite/recompute")
async def auto_elite_recompute(user=Depends(_require_auth())):
    """Force-rebuild the auto-elite list (admin/debug)."""
    db = _get_db()
    return await recompute_auto_elite_scorers(db)
