"""Magic Finder (2026-07-28) — the unified discovery-layer API.

Combines the four discovery subsystems into a single response
consumed by Strategy Lab, Why This Pick, and future parlay planning.

    payload = await magic_find(
        db, sport="NFL", player="Joe Burrow",
        stat="passing_yards", opponent="KC",
        threshold=249.5,
    )

    → {
        "player":       "Joe Burrow",
        "stat":         "passing_yards",
        "sport":        "NFL",
        "opponent":     "KC",
        "threshold":    249.5,
        "generated_at": "2026-07-28T...",
        "threshold_analysis":   { ... },   # threshold_discovery
        "alt_line_recommendation": { ... }, # alt_line_intelligence
        "patterns":     [ ... ],           # pattern_discovery
        "similar_situations": { ... },     # situation_clustering
        "explanations": [ str, ... ],
        "notes":        [ ... ],
      }

Never raises — sub-engine errors fold into `notes`.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("lockscore.services.discovery.magic_finder")


async def magic_find(
    db,
    *,
    sport: str,
    player: str,
    stat: str,
    opponent: Optional[str] = None,
    threshold: Optional[float] = None,
) -> dict:
    payload: dict = {
        "player":       player,
        "stat":         stat,
        "sport":        sport,
        "opponent":     opponent,
        "threshold":    threshold,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "threshold_analysis":       None,
        "alt_line_recommendation":  None,
        "patterns":                 [],
        "similar_situations":       None,
        "explanations":             [],
        "notes":                    [],
    }

    # 1. Threshold ladder.
    try:
        from .threshold_discovery import analyse_thresholds
        payload["threshold_analysis"] = await analyse_thresholds(
            db, sport=sport, player=player, stat=stat,
        )
    except Exception as e:
        payload["notes"].append(f"threshold_discovery error: {e}")

    # 2. Alt-line recommendation.
    try:
        from .alt_line_intelligence import recommend_alt_lines
        payload["alt_line_recommendation"] = await recommend_alt_lines(
            db, sport=sport, player=player, stat=stat,
        )
    except Exception as e:
        payload["notes"].append(f"alt_line_intelligence error: {e}")

    # 3. Discovered patterns.
    try:
        from .pattern_discovery import discover_patterns
        payload["patterns"] = await discover_patterns(
            db, sport=sport, player=player, stat=stat,
        )
    except Exception as e:
        payload["notes"].append(f"pattern_discovery error: {e}")

    # 4. Situation clustering.
    if opponent:
        try:
            from .situation_clustering import find_similar_situations
            payload["similar_situations"] = await find_similar_situations(
                db, sport=sport, player=player, stat=stat,
                opponent=opponent, threshold=threshold,
            )
        except Exception as e:
            payload["notes"].append(f"situation_clustering error: {e}")

    # 5. Explanations — the "Why This Pick"-ready lines.
    ta = payload["threshold_analysis"] or {}
    if ta.get("safest"):
        s = ta["safest"]
        payload["explanations"].append(
            f"{player} exceeds {s['threshold']} {stat.replace('_',' ')} in "
            f"{s['hit_rate']*100:.0f}% of games ({s['games']} games, Grade {s['grade']})."
        )
    if ta.get("strongest"):
        s = ta["strongest"]
        payload["explanations"].append(
            f"Statistically strongest historical threshold: "
            f"{s['threshold']} (Wilson lower bound {s['lb95']*100:.0f}%)."
        )
    if payload["patterns"]:
        top = payload["patterns"][0]
        payload["explanations"].append(
            f"Pattern: {top.get('note') or top['pattern_id']} — Grade {top['grade']}."
        )
    ss = payload["similar_situations"] or {}
    hist = ss.get("player_history_in_target_cluster") if ss else None
    if hist and hist.get("n_games"):
        hr = hist.get("hit_rate")
        payload["explanations"].append(
            f"In {hist['n_games']} similar-defense games "
            f"(cluster {hist['cluster_id']}), {player} averages "
            f"{hist['avg_stat']}" +
            (f" and clears {hist.get('threshold')} "
              f"{hr*100:.0f}% of the time" if hr is not None else "")
            + "."
        )
    return payload


__all__ = ["magic_find"]
