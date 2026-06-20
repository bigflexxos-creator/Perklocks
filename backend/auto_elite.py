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

    Stores results in `auto_elite_players` collection. Safe to call
    repeatedly — uses upsert + cleans stale entries.

    Returns a summary dict with counts for monitoring.
    """
    cutoff = (_dt.datetime.now(_dt.timezone.utc)
              - _dt.timedelta(days=LOOKBACK_DAYS)).isoformat()

    # Aggregate goalscorer hits per (sport, player)
    by_player: dict[tuple[str, str], dict] = {}
    cur = db.picks.find(
        {
            "market":      {"$regex": "goal scorer", "$options": "i"},
            "status":      {"$in": ["won", "lost"]},
            "event_time":  {"$gte": cutoff},
        },
        {"_id": 0, "sport": 1, "market": 1, "status": 1},
    )
    async for p in cur:
        player = _extract_player(p.get("market") or "")
        if not player:
            continue
        sport = p.get("sport") or "Soccer"
        k = (sport, player)
        r = by_player.setdefault(k, {"sport": sport, "name": player, "n": 0, "wins": 0})
        r["n"] += 1
        if p.get("status") == "won":
            r["wins"] += 1

    # Build promotion list
    promoted = []
    for r in by_player.values():
        r["hit_rate"] = r["wins"] / r["n"] if r["n"] else 0.0
        if (r["n"] >= MIN_PICKS_FOR_AUTO_ELITE
                and r["hit_rate"] >= MIN_HIT_RATE_AUTO_ELITE):
            promoted.append({
                "_id":            f"{r['sport']}::{r['name'].lower()}",
                "sport":          r["sport"],
                "name":           r["name"],
                "n_picks":        r["n"],
                "wins":           r["wins"],
                "hit_rate":       round(r["hit_rate"], 3),
                "promoted_at":    _dt.datetime.now(_dt.timezone.utc).isoformat(),
            })

    # Replace collection wholesale (small enough — typically <100 docs)
    await db.auto_elite_players.delete_many({})
    if promoted:
        await db.auto_elite_players.insert_many(promoted, ordered=False)

    logger.info(
        "Auto-elite scorers recomputed: scanned %d (sport,player) pairs, "
        "promoted %d players (n>=%d, hit_rate>=%.0f%%)",
        len(by_player), len(promoted),
        MIN_PICKS_FOR_AUTO_ELITE, MIN_HIT_RATE_AUTO_ELITE * 100,
    )
    return {
        "scanned":  len(by_player),
        "promoted": len(promoted),
        "min_picks": MIN_PICKS_FOR_AUTO_ELITE,
        "min_hit_rate": MIN_HIT_RATE_AUTO_ELITE,
        "promoted_names": [p["name"] for p in promoted],
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
