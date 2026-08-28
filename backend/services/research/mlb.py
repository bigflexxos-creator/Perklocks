"""MLB research adapter — Strategy Lab 10X.

Reuses existing production intelligence (never rebuilds):
* `hot_hitters.build_hot_hitters` — L15 average / OBP / OPS / streak
* `services.mlb_hitter_intel` — batter recent form
* `services.mlb_statcast` — xBA / barrel% / hard-hit%
* `services.mlb_team_k_intel` — team K% vs hand
* `services.mlb_umpire` — umpire K/BB tendencies
* `services.mlb_stuff_plus` — pitcher stuff+
* `db.player_game_logs` for MLB (canonical opponent history)
* `db.pick_enrichment.history_shadow` for the shadow lane

All returned facts carry FACTUAL provenance unless explicitly marked
SHADOW_SIGNAL (only pattern/trend discoveries are SHADOW).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from deps import db
from .contract import (
    CanonicalResearchSnapshot, ResearchFact, ResearchProvenance,
    ResearchQuality, ResearchSection, ResearchShadowSignal,
)

log = logging.getLogger("lockscore.research.mlb")

# ── Section 4: Hot-hitters canonical trend radar ─────────────────────
async def hot_hitters(limit: int = 25) -> list[dict[str, Any]]:
    """Return the canonical MLB hot-hitters list. Wraps the existing
    engine so the workstation and the /api/lab/hot-hitters endpoint share
    one source of truth.
    """
    try:
        from hot_hitters import build_hot_hitters
        payload = await build_hot_hitters(limit=limit)
        return payload.get("hitters", [])
    except Exception as e:  # pragma: no cover
        log.warning("hot_hitters failed: %s", e)
        return []


# ── Batter subject snapshot ────────────────────────────────────────────
async def _batter_recent_form(player_name: str) -> ResearchFact | None:
    """Compute L15 hit rate + OPS from `player_game_logs` (MLB canonical)."""
    try:
        cursor = db.player_game_logs.find(
            {"sport": "mlb", "player_name": player_name},
            {"_id": 0, "stats": 1, "date": 1},
        ).sort("date", -1).limit(15)
        rows = await cursor.to_list(length=15)
        if not rows:
            return None
        hits, ab, tb, obp_sum = 0, 0, 0, 0.0
        n = 0
        for r in rows:
            stats = r.get("stats") or {}
            h = stats.get("hits") or 0
            a = stats.get("at_bats") or 0
            tb += stats.get("total_bases") or 0
            hits += h
            ab += a
            n += 1
        if ab <= 0:
            return None
        avg = hits / ab
        return ResearchFact(
            key="batter_l15_form",
            label=f"L{n} form",
            value={"avg": round(avg, 3), "hits": hits, "ab": ab, "tb": tb,
                   "games": n},
            section=ResearchSection.FORM,
            provenance=ResearchProvenance.FACTUAL,
            quality=ResearchQuality.STRONG if n >= 8 else ResearchQuality.PARTIAL,
            sample_size=n,
            source="mongo:player_game_logs",
        )
    except Exception as e:  # pragma: no cover
        log.debug("_batter_recent_form failed: %s", e)
        return None


async def _batter_h2h(player_name: str, opponent_pitcher: str | None) -> ResearchFact | None:
    if not opponent_pitcher:
        return None
    try:
        cursor = db.player_game_logs.find(
            {"sport": "mlb", "player_name": player_name,
             "opponent_pitcher": opponent_pitcher},
            {"_id": 0, "stats": 1},
        ).limit(30)
        rows = await cursor.to_list(length=30)
        if not rows:
            return None
        h, ab = 0, 0
        for r in rows:
            s = r.get("stats") or {}
            h += s.get("hits") or 0
            ab += s.get("at_bats") or 0
        if ab < 3:
            return ResearchFact(
                key="batter_h2h",
                label=f"vs {opponent_pitcher}",
                value={"hits": h, "ab": ab, "avg": None, "note": "small sample"},
                section=ResearchSection.MATCHUP,
                provenance=ResearchProvenance.FACTUAL,
                quality=ResearchQuality.PRIOR_ONLY,
                sample_size=ab,
                source="mongo:player_game_logs",
            )
        return ResearchFact(
            key="batter_h2h",
            label=f"vs {opponent_pitcher}",
            value={"hits": h, "ab": ab, "avg": round(h / ab, 3)},
            section=ResearchSection.MATCHUP,
            provenance=ResearchProvenance.FACTUAL,
            quality=ResearchQuality.PARTIAL if ab < 10 else ResearchQuality.STRONG,
            sample_size=ab,
            source="mongo:player_game_logs",
        )
    except Exception as e:  # pragma: no cover
        log.debug("_batter_h2h failed: %s", e)
        return None


async def _batter_statcast(player_name: str) -> list[ResearchFact]:
    """Read pre-ingested Statcast rows (xBA, barrel%, hard-hit%). No API call."""
    try:
        row = await db.mlb_statcast.find_one(
            {"player_name": player_name},
            {"_id": 0, "xba": 1, "barrel_pct": 1, "hard_hit_pct": 1,
             "avg_exit_velo": 1, "updated_at": 1},
        )
    except Exception:
        row = None
    if not row:
        return []
    out: list[ResearchFact] = []
    freshness = None
    if row.get("updated_at"):
        try:
            ts = row["updated_at"]
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            freshness = int((datetime.now(timezone.utc) - ts).total_seconds())
        except Exception:
            pass
    for k, label, unit in [
        ("xba", "xBA", ""),
        ("barrel_pct", "Barrel%", "%"),
        ("hard_hit_pct", "Hard-hit%", "%"),
        ("avg_exit_velo", "Exit velo", "mph"),
    ]:
        v = row.get(k)
        if v is None:
            continue
        out.append(ResearchFact(
            key=f"batter_{k}",
            label=label,
            value=v,
            unit=unit,
            section=ResearchSection.STATCAST,
            provenance=ResearchProvenance.FACTUAL,
            quality=ResearchQuality.STRONG,
            freshness_sec=freshness,
            source="mongo:mlb_statcast",
        ))
    return out


async def _pitcher_intel(pitcher_name: str) -> list[ResearchFact]:
    """Reuse services.mlb_stuff_plus + services.mlb_team_k_intel signals."""
    out: list[ResearchFact] = []
    try:
        row = await db.mlb_pitchers_intel.find_one(
            {"name_canonical": pitcher_name.lower()},
            {"_id": 0, "season_k_pct": 1, "handedness": 1, "ip_per_start": 1,
             "stuff_plus": 1, "l5_k_avg": 1},
        )
    except Exception:
        row = None
    if not row:
        return out
    for k, label in [
        ("season_k_pct", "Season K%"),
        ("stuff_plus", "Stuff+"),
        ("l5_k_avg", "L5 K avg"),
        ("ip_per_start", "IP/start"),
    ]:
        v = row.get(k)
        if v is None:
            continue
        out.append(ResearchFact(
            key=f"pitcher_{k}",
            label=label,
            value=v,
            section=ResearchSection.PITCHER,
            provenance=ResearchProvenance.FACTUAL,
            quality=ResearchQuality.STRONG,
            source="mongo:mlb_pitchers_intel",
        ))
    if row.get("handedness"):
        out.append(ResearchFact(
            key="pitcher_handedness", label="Throws",
            value=row["handedness"], section=ResearchSection.PITCHER,
            provenance=ResearchProvenance.FACTUAL, quality=ResearchQuality.FULL,
            source="mongo:mlb_pitchers_intel"))
    return out


async def _hot_streak_shadow(player_name: str) -> ResearchShadowSignal | None:
    """SHADOW-only trend flag — pattern discovered but NOT influencing Lock."""
    try:
        cursor = db.player_game_logs.find(
            {"sport": "mlb", "player_name": player_name},
            {"_id": 0, "stats": 1, "date": 1},
        ).sort("date", -1).limit(10)
        rows = await cursor.to_list(length=10)
    except Exception:
        rows = []
    if not rows:
        return None
    hits_games = sum(1 for r in rows
                     if ((r.get("stats") or {}).get("hits") or 0) >= 1)
    n = len(rows)
    hr = hits_games / n if n else 0.0
    if hr < 0.6 or n < 5:
        return None
    # Wilson lower bound
    z, p = 1.96, hr
    denom = 1 + (z * z) / n
    center = p + (z * z) / (2 * n)
    margin = (
        (p * (1 - p) / n + (z * z) / (4 * n * n)) ** 0.5
    ) * z
    wilson = max(0.0, (center - margin) / denom)
    strength = "strong" if wilson >= 0.6 else "moderate" if wilson >= 0.5 else "weak"
    return ResearchShadowSignal(
        key="hit_streak",
        label=f"Hits in {hits_games}/{n}",
        description=f"{player_name} has recorded ≥1 hit in {hits_games} of last {n} games",
        hits=hits_games, n=n, hit_rate=round(hr, 3),
        wilson_lower=round(wilson, 3), strength=strength,
        tags=["mlb", "streak"],
    )


# ── Section 8: Matchup DNA / H2H ─────────────────────────────────────
async def matchup_dna(player_name: str, opponent: str | None) -> dict[str, Any]:
    """Return actual-history DNA: player vs opponent — hit rate, TB, HR."""
    if not opponent:
        return {"available": False, "reason": "no_opponent"}
    try:
        cursor = db.player_game_logs.find(
            {"sport": "mlb", "player_name": player_name, "opponent": opponent},
            {"_id": 0, "stats": 1, "date": 1},
        ).sort("date", -1).limit(30)
        rows = await cursor.to_list(length=30)
    except Exception:
        rows = []
    if not rows:
        return {"available": False, "reason": "no_history"}
    ab, h, hr, tb = 0, 0, 0, 0
    game_hits_1p = 0
    for r in rows:
        s = r.get("stats") or {}
        a = s.get("at_bats") or 0
        hh = s.get("hits") or 0
        ab += a
        h += hh
        hr += s.get("hr") or 0
        tb += s.get("total_bases") or 0
        if hh >= 1:
            game_hits_1p += 1
    n = len(rows)
    return {
        "available": True, "sample_size": n,
        "vs": opponent,
        "hit_rate_per_game": round(game_hits_1p / n, 3) if n else None,
        "avg": round(h / ab, 3) if ab > 0 else None,
        "hits": h, "ab": ab, "hr": hr, "tb": tb,
    }


# ── Public: build canonical MLB snapshot ─────────────────────────────
async def build_snapshot(
    subject: str | None = None,
    opponent: str | None = None,
    event_id: str | None = None,
    event_label: str | None = None,
    role: str = "batter",  # batter | pitcher
    include_shadow: bool = True,
) -> CanonicalResearchSnapshot:
    t0 = time.time()
    facts: list[ResearchFact] = []
    shadow: list[ResearchShadowSignal] = []
    notes: list[str] = []
    if subject:
        if role == "batter":
            f = await _batter_recent_form(subject)
            if f: facts.append(f)
            statcast = await _batter_statcast(subject)
            facts.extend(statcast)
            h2h = await _batter_h2h(subject, opponent)
            if h2h: facts.append(h2h)
            if include_shadow:
                s = await _hot_streak_shadow(subject)
                if s: shadow.append(s)
        elif role == "pitcher":
            p = await _pitcher_intel(subject)
            facts.extend(p)
    dna = await matchup_dna(subject, opponent) if subject and role == "batter" else None
    notes.append(f"MLB adapter built in {int((time.time()-t0)*1000)}ms")
    return CanonicalResearchSnapshot(
        sport="MLB",
        generated_at=datetime.now(timezone.utc).isoformat(),
        subject=subject,
        event_id=event_id,
        event_label=event_label,
        facts=facts,
        shadow=shadow,
        matchup_dna=dna,
        notes=notes,
    )
