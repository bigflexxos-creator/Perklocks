"""Alt-Line Intelligence Engine (2026-07-28).

Recommends the "best value" thresholds for a (sport, player, stat)
purely from historical performance — never from sportsbook lines.

Public API
──────────
    result = await recommend_alt_lines(
        db, sport="NBA", player="Luka Doncic",
        stat="points",
    )

    → {
        "player":   "Luka Doncic",
        "stat":     "points",
        "sport":    "NBA",
        "games_used":   ...,
        "ladder":       [ {threshold, hit_rate, grade, ...}, ...],
        "safest":       {threshold, hit_rate, ...},      # highest hit-rate strong bet
        "strongest":    {threshold, wilson_lb, ...},     # tightest lower bound
        "best_value":   {threshold, ev_score, ...},      # highest hit-rate × payout proxy
        "highest_confidence": {threshold, grade, ...},
        "notes":    [...],
      }

Recommendations are heuristic bets driven purely by historical hit
rate + confidence — no book prices, no vig math.
"""
from __future__ import annotations

from typing import Optional

from .threshold_discovery import analyse_thresholds


def _ev_proxy(hit_rate: float, threshold: float, avg: float) -> float:
    """A rough EV proxy: hit_rate × (avg - threshold) / (avg + 1).

    Rewards thresholds that are below the player's average AND have
    a strong hit rate. Never uses payout / vig — purely internal signal.
    """
    if avg <= 0:
        return 0.0
    return hit_rate * max(0.0, (avg - threshold)) / (avg + 1.0)


async def recommend_alt_lines(
    db,
    *,
    sport: str,
    player: str,
    stat: str,
    thresholds: Optional[list[float]] = None,
    min_games: int = 8,
) -> dict:
    tr = await analyse_thresholds(db, sport=sport, player=player,
                                    stat=stat, thresholds=thresholds)

    out = {
        "player":              player,
        "stat":                stat,
        "sport":               sport,
        "games_used":          tr["games_used"],
        "average_output":      tr["average_output"],
        "ladder":              tr["thresholds"],
        "safest":              None,
        "strongest":           None,
        "highest_confidence":  None,
        "best_value":          None,
        "notes":               list(tr["notes"]),
    }
    if tr["games_used"] < min_games:
        out["notes"].append(f"insufficient games ({tr['games_used']} < {min_games})")
        return out

    # Safest = highest hit-rate with grade ≥ B.
    safest = None
    safest_hit_rate = -1.0
    strongest = None
    strongest_lb = -1.0
    highest_conf = None
    highest_score = -1.0
    best_value = None
    best_ev = -1.0
    for row in tr["thresholds"]:
        # Safest.
        if row["grade"] in {"A+", "A", "B"} and row["hit_rate"] > safest_hit_rate:
            safest = row
            safest_hit_rate = row["hit_rate"]
        # Strongest by Wilson lower bound.
        if row["lb95"] > strongest_lb:
            strongest = row; strongest_lb = row["lb95"]
        # Highest confidence — favours A+ grades with large samples.
        conf_score = row["lb95"] * (1.0 if row["grade"] == "A+" else
                                       0.85 if row["grade"] == "A" else
                                       0.70 if row["grade"] == "B" else 0.5)
        if conf_score > highest_score:
            highest_score = conf_score
            highest_conf = row
        # Best value.
        ev = _ev_proxy(row["hit_rate"], row["threshold"],
                       tr["average_output"])
        if ev > best_ev:
            best_ev = ev
            best_value = {**row, "ev_score": round(ev, 4)}

    out["safest"] = safest
    out["strongest"] = strongest
    out["highest_confidence"] = highest_conf
    out["best_value"] = best_value
    return out


__all__ = ["recommend_alt_lines"]
