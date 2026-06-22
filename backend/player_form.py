"""Per-player rolling form tracker (Phase 2 of Learning System).

Tracks each player's recent track record on our picks so the next pick we
make on them can be biased by their hot/cold streak. Complements the
existing market-bucket learning (which learns at the aggregate level) by
adding a player-specific signal.

Storage: `player_form` collection (one row per (sport, player, market_family))
keyed for fast pick-generation lookup.

Schema:
  {
    sport: "MLB",
    player: "Aaron Judge",            # extracted from market label
    market_family: "MLB Hits",        # from _market_label()
    last10_n: 8,                      # decisive picks counted
    last10_wins: 6,
    last10_hit_rate: 75.0,
    last10_roi: 18.5,
    streak: "hot" | "cold" | "neutral",
    decayed_lock_delta: +2.5,         # how much to nudge future lock_score
    last_updated: ISO timestamp,
  }

Applied: at pick-generation, after lock_v2 + auto-elite. Adds at most ±5
to lock_score so it can't dominate the engine — just a tilt.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Optional

from analytics import _market_label  # type: ignore

# How many recent picks per player to consider.
WINDOW_SIZE = 10

# Min decisive picks before we trust the player-form signal at all.
MIN_DECISIVE = 4

# Lock-score nudge caps. Capped low — player form is a *tilt*, not the engine.
MAX_FORM_DELTA = 5.0    # max ±5 lock_score points from form

# Hot/cold streak thresholds (last N decisive picks).
HOT_THRESHOLD = 0.65    # >= 65% hit rate over last N → "hot"
COLD_THRESHOLD = 0.35   # <= 35% hit rate over last N → "cold"

# Time-decay half-life (days) — matches the global learning engine.
HALF_LIFE_DAYS = 30


# ── Player-name extraction ───────────────────────────────────────────────
# Mirrors the goalscorer dedup extractor + handles MLB props like
# "Aaron Judge (NYY) Over 1.5 Hits" → "Aaron Judge".

_TRAILING_PATTERNS = [
    r"\s*\([A-Z]{2,4}\)\s*",          # "(NYY)" / "(LAD)" team tag
    r"\s*\(\d{4}\)\s*",                # "(2002)" year-of-birth disambig
    r"\s+Over\s+\d+\.\d+.*$",          # "Over 1.5 Hits..."
    r"\s+Under\s+\d+\.\d+.*$",         # "Under 0.5 Hits..."
    r"\s+Anytime Goal Scorer.*$",
    r"\s+First Goal Scorer.*$",
    r"\s+To Score or Assist.*$",
    r"\s+·\s+ALT LOCK\s*$",
    r"\s+\(Alt\)\s*$",
]


def extract_player(market: str | None) -> str:
    """Pull the player name out of a market label. Empty string if not a player prop."""
    if not market:
        return ""
    name = market
    for pat in _TRAILING_PATTERNS:
        name = re.sub(pat, "", name, flags=re.I)
    name = name.strip()
    # If the leftover looks like a player name (2+ words, alphabetic), keep
    # it. Otherwise, this was probably a game-level market (Moneyline /
    # Spread / Total).
    if not name or len(name.split()) < 2:
        return ""
    if not re.match(r"^[A-Za-zÀ-ÿ' \-.]+$", name):
        return ""
    return name


# ── Aggregate recent form ────────────────────────────────────────────────


def _age_weight(ts: str | None, now: datetime) -> float:
    """Exponential time-decay matching learning_engine."""
    if not ts:
        return 1.0
    try:
        iso = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_days = max(0.0, (now - dt).total_seconds() / 86400.0)
        return math.exp(-age_days / HALF_LIFE_DAYS)
    except Exception:
        return 1.0


async def recompute_player_form(db) -> int:
    """Rebuild the `player_form` collection from recent settled picks.

    Runs nightly (or after each settlement pass). Cheap — pure DB aggregation.

    Returns the number of player-form rows persisted.
    """
    now = datetime.now(timezone.utc)
    cursor = db.picks.find(
        {"status": {"$in": ["won", "lost", "push"]}},
        {"_id": 0, "sport": 1, "market": 1, "status": 1,
         "win_probability": 1, "units_profit": 1, "units_risked": 1,
         "event_time": 1, "settled_at": 1},
    ).sort([("settled_at", -1), ("event_time", -1)])
    raw = await cursor.to_list(length=50_000)

    # Group by (sport, player, market_family) — keep only the last WINDOW_SIZE
    # decisive picks per group.
    groups: dict[tuple, list[dict]] = {}
    for p in raw:
        player = extract_player(p.get("market"))
        if not player:
            continue
        sport = p.get("sport") or "Unknown"
        family = _market_label(p.get("market"))
        key = (sport, player, family)
        lst = groups.setdefault(key, [])
        if len(lst) < WINDOW_SIZE:
            lst.append(p)

    rows: list[dict] = []
    for (sport, player, family), picks in groups.items():
        decisive = [p for p in picks if p["status"] in ("won", "lost")]
        if len(decisive) < MIN_DECISIVE:
            continue
        wins = sum(1 for p in decisive if p["status"] == "won")
        n = len(decisive)
        hit_rate = wins / n

        # Time-decayed ROI / hit-rate
        d_n = d_wins = d_risked = d_profit = 0.0
        for p in decisive:
            w = _age_weight(p.get("settled_at") or p.get("event_time"), now)
            d_n += w
            if p["status"] == "won":
                d_wins += w
            d_risked += float(p.get("units_risked") or 1.0) * w
            d_profit += float(p.get("units_profit") or 0) * w
        d_hit_rate = d_wins / d_n if d_n else hit_rate
        d_roi = (d_profit / d_risked * 100.0) if d_risked else 0.0

        # Streak label
        if d_hit_rate >= HOT_THRESHOLD:
            streak = "hot"
        elif d_hit_rate <= COLD_THRESHOLD:
            streak = "cold"
        else:
            streak = "neutral"

        # Lock-score delta: positive for hot, negative for cold. Scaled by
        # margin past the threshold + ROI signal. Capped at ±MAX_FORM_DELTA.
        hr_signal = (d_hit_rate - 0.5) * 10.0      # ±5 from hit-rate alone
        roi_signal = max(-3.0, min(3.0, d_roi / 6.0))  # capped ±3 from ROI
        # Shrinkage: small samples get partial credit
        shrinkage = n / (n + 6.0)
        delta = max(-MAX_FORM_DELTA, min(MAX_FORM_DELTA, (hr_signal + roi_signal) * shrinkage))

        rows.append({
            "sport": sport,
            "player": player,
            "market_family": family,
            "last10_n": n,
            "last10_wins": wins,
            "last10_hit_rate": round(hit_rate * 100.0, 1),
            "decayed_hit_rate": round(d_hit_rate * 100.0, 1),
            "decayed_roi": round(d_roi, 2),
            "streak": streak,
            "decayed_lock_delta": round(delta, 2),
            "last_updated": now.isoformat(),
        })

    # Persist atomically: wipe + insert
    if rows:
        await db.player_form.delete_many({})
        await db.player_form.insert_many(rows)
    return len(rows)


# ── Apply player form to picks at generation time ──────────────────────


async def apply_player_form(picks: list[dict], db) -> dict:
    """Adjust each pick's lock_score by the player's recent form delta.

    Idempotent — adds the delta to a virgin pick. Non-destructive: also
    stores `player_form_delta` and `player_form_streak` on the pick so the
    UI can show "🔥 Hot Streak +3" badges later.

    Returns {applied, hot, cold, neutral}.
    """
    if not picks:
        return {"applied": 0, "hot": 0, "cold": 0, "neutral": 0}

    # Load all player-form rows into a fast dict
    form_rows = await db.player_form.find({}, {"_id": 0}).to_list(length=10_000)
    form_map: dict[tuple, dict] = {
        (r["sport"], r["player"], r["market_family"]): r for r in form_rows
    }
    counts = {"applied": 0, "hot": 0, "cold": 0, "neutral": 0}
    for p in picks:
        player = extract_player(p.get("market"))
        if not player:
            continue
        family = _market_label(p.get("market"))
        key = (p.get("sport") or "", player, family)
        row = form_map.get(key)
        if not row:
            continue
        delta = float(row.get("decayed_lock_delta") or 0)
        streak = row.get("streak") or "neutral"
        if delta == 0 and streak == "neutral":
            continue
        # Apply to lock_score (cap at 0-99 to stay in valid band)
        cur_lock = float(p.get("lock_score") or 0)
        new_lock = max(0.0, min(99.0, cur_lock + delta))
        p["lock_score"] = round(new_lock, 1)
        p["player_form_delta"] = delta
        p["player_form_streak"] = streak
        p["player_form_n"] = row.get("last10_n")
        p["player_form_hit_rate"] = row.get("decayed_hit_rate")
        counts["applied"] += 1
        counts[streak] = counts.get(streak, 0) + 1
    return counts
