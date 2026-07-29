"""Advanced Similar-Situation Clustering (2026-07-28).

Upgrades the existing `similar_matchup_engine` by clustering opponents
into stylistic tiers instead of doing raw K-nearest lookups.

Public API
──────────
    result = await find_similar_situations(
        db, sport="NFL", player="Joe Burrow",
        stat="passing_yards", opponent="KC",
        n_clusters=4, threshold=249.5,
    )

    → {
        "clusters":         [ { id, size, mean_profile, teams } ],
        "target_cluster":   int,           # which cluster KC falls in
        "player_history_in_target_cluster": {
            "n_games":     ...,
            "avg_stat":    ...,
            "hit_rate":    ...,             # against `threshold`
        },
        "closest_comparisons": [
            {opponent, similarity, player_avg_in_matchup, ...}, ...
        ],
        "notes": [...],
      }

Uses the existing similar_matchup_engine profile builder + a light
k-means clustering (implemented in pure Python — no sklearn dep at
inference).  Read-only, never raises.
"""
from __future__ import annotations

import logging
import random
from typing import Optional

logger = logging.getLogger("lockscore.services.discovery.situation_clustering")


def _kmeans(profiles: dict[str, list[float]], k: int,
             max_iter: int = 25, seed: int = 42) -> dict[str, int]:
    """Minimal k-means. Returns {team: cluster_id}."""
    if not profiles:
        return {}
    keys = list(profiles.keys())
    rng = random.Random(seed)
    k = min(max(1, k), len(keys))
    centers = [profiles[k_] for k_ in rng.sample(keys, k)]
    assignments = {t: 0 for t in keys}
    for _ in range(max_iter):
        # Assign step.
        new_assign: dict[str, int] = {}
        for t, v in profiles.items():
            dists = [sum((a - b) ** 2 for a, b in zip(v, c))
                      for c in centers]
            new_assign[t] = dists.index(min(dists))
        if new_assign == assignments:
            break
        assignments = new_assign
        # Update centers.
        buckets: dict[int, list[list[float]]] = {}
        for t, cid in assignments.items():
            buckets.setdefault(cid, []).append(profiles[t])
        centers = []
        for cid in range(k):
            members = buckets.get(cid, [])
            if not members:
                centers.append([0.0] * len(next(iter(profiles.values()))))
                continue
            n_dims = len(members[0])
            centers.append([sum(m[i] for m in members) / len(members)
                             for i in range(n_dims)])
    return assignments


async def find_similar_situations(
    db,
    *,
    sport: str,
    player: str,
    stat: str,
    opponent: str,
    n_clusters: int = 4,
    threshold: Optional[float] = None,
) -> dict:
    sport_u = (sport or "").upper()
    out = {
        "sport": sport, "player": player, "stat": stat,
        "opponent": opponent, "clusters": [], "target_cluster": None,
        "player_history_in_target_cluster": None,
        "closest_comparisons": [], "notes": [],
    }
    try:
        from services.similar_matchup_engine import (
            _build_nfl_profiles, _build_mlb_profiles,
            _find_nearest_teams, _zscore_normalize,
        )
    except Exception as e:
        out["notes"].append(f"similar_matchup_engine import failed: {e}")
        return out

    if sport_u == "NFL":
        profiles = await _build_nfl_profiles(db)
    elif sport_u == "MLB":
        profiles = await _build_mlb_profiles(db)
    else:
        out["notes"].append(f"sport {sport_u} not supported for clustering")
        return out
    if not profiles:
        out["notes"].append("profile builder returned empty")
        return out

    # Normalise before clustering.
    z = _zscore_normalize(profiles)
    tgt = opponent.upper() if sport_u == "NFL" else opponent
    if tgt not in z:
        # Try case-insensitive match.
        matches = [t for t in z if t.lower() == tgt.lower()]
        if matches:
            tgt = matches[0]
        else:
            out["notes"].append(f"target team {opponent!r} not in profile set")
            return out

    assignments = _kmeans(z, k=n_clusters)
    # Build cluster summaries.
    clusters: list[dict] = []
    grouped: dict[int, list[str]] = {}
    for t, cid in assignments.items():
        grouped.setdefault(cid, []).append(t)
    for cid, teams in grouped.items():
        vecs = [z[t] for t in teams]
        n_dims = len(vecs[0]) if vecs else 0
        mean = [sum(v[i] for v in vecs) / len(vecs) for i in range(n_dims)]
        clusters.append({"id": cid, "size": len(teams),
                          "mean_profile": [round(x, 3) for x in mean],
                          "teams": sorted(teams)})
    clusters.sort(key=lambda c: c["id"])
    out["clusters"] = clusters
    target_cluster = assignments[tgt]
    out["target_cluster"] = int(target_cluster)

    # Nearest comparisons within same cluster.
    same_cluster_teams = [t for t, cid in assignments.items()
                           if cid == target_cluster and t != tgt]
    nn = _find_nearest_teams(profiles, tgt, k=5)
    out["closest_comparisons"] = [
        {"opponent": t, "similarity": round(s, 4)}
        for t, s in nn if t in same_cluster_teams
    ][:5]

    # Player history vs the target cluster's teams.
    try:
        from services.similar_matchup_engine import (
            _fetch_nfl_player_games_vs_teams,
        )
    except Exception:
        _fetch_nfl_player_games_vs_teams = None

    if sport_u == "NFL" and _fetch_nfl_player_games_vs_teams:
        rows = await _fetch_nfl_player_games_vs_teams(
            db, None, player, stat,
            teams=same_cluster_teams + [tgt],
            exclude_team=None, season_min=2019, limit=80,
        )
        vals = []
        for r in rows:
            v = r.get(stat)
            try:
                vals.append(float(v))
            except (TypeError, ValueError):
                continue
        if vals:
            hits = (sum(1 for v in vals if v > threshold)
                    if threshold is not None else None)
            out["player_history_in_target_cluster"] = {
                "n_games":     len(vals),
                "avg_stat":    round(sum(vals) / len(vals), 3),
                "hit_rate":    round(hits / len(vals), 4)
                                if hits is not None else None,
                "threshold":   threshold,
                "cluster_id":  int(target_cluster),
            }
        else:
            out["notes"].append(
                f"no player games against cluster-{target_cluster} teams"
            )
    return out


__all__ = ["find_similar_situations"]
