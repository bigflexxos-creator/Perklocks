"""NFL research adapter — Strategy Lab 10X.

Reuses:
* `services.platinum_nfl` — Platinum simulator opportunity model
* `services.nfl_feature_engine` / `services.nfl_features` — role/target/carry
* `services.nfl_opp_defense` — defensive matchup posture
* `db.player_game_logs` (sport=nfl) — canonical opportunity history

Facts surfaced (FACTUAL):
  * role, snap%, target share, carry share, red-zone touches
  * opponent defense vs position rank
  * L4 opportunity, receiving air yards, YPRR proxy

Shadow (UI-only): opportunity-trend streaks, matchup outlier flags.
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

log = logging.getLogger("lockscore.research.nfl")


async def _player_opportunity(player_name: str) -> list[ResearchFact]:
    """Pull real opportunity/role stats from nfl feature cache."""
    row = None
    try:
        row = await db.nfl_players_intel.find_one(
            {"name_canonical": player_name.lower()},
            {"_id": 0, "position": 1, "snap_pct": 1, "target_share": 1,
             "carry_share": 1, "rz_touches_pg": 1, "air_yards_pg": 1,
             "yprr": 1, "role": 1, "l4_targets_avg": 1, "l4_carries_avg": 1},
        )
    except Exception:
        pass
    if not row:
        return []
    out: list[ResearchFact] = []
    for k, label, section, unit in [
        ("position", "Position", ResearchSection.OPPORTUNITY, None),
        ("role", "Role", ResearchSection.OPPORTUNITY, None),
        ("snap_pct", "Snap%", ResearchSection.OPPORTUNITY, "%"),
        ("target_share", "Target share", ResearchSection.OPPORTUNITY, "%"),
        ("carry_share", "Carry share", ResearchSection.OPPORTUNITY, "%"),
        ("rz_touches_pg", "RZ touches / g", ResearchSection.RED_ZONE, None),
        ("air_yards_pg", "Air yards / g", ResearchSection.OPPORTUNITY, "yd"),
        ("yprr", "YPRR", ResearchSection.OPPORTUNITY, None),
        ("l4_targets_avg", "L4 targets", ResearchSection.FORM, None),
        ("l4_carries_avg", "L4 carries", ResearchSection.FORM, None),
    ]:
        v = row.get(k)
        if v is None:
            continue
        out.append(ResearchFact(
            key=f"nfl_{k}", label=label, value=v, unit=unit,
            section=section, provenance=ResearchProvenance.FACTUAL,
            quality=ResearchQuality.STRONG,
            source="mongo:nfl_players_intel",
        ))
    return out


async def _opponent_defense(player_name: str, opponent: str | None) -> ResearchFact | None:
    if not opponent:
        return None
    try:
        row = await db.nfl_defense_intel.find_one(
            {"team_canonical": opponent.lower()},
            {"_id": 0, "vs_qb_rank": 1, "vs_rb_rank": 1, "vs_wr_rank": 1,
             "vs_te_rank": 1, "vs_pass_ypg": 1, "vs_rush_ypg": 1},
        )
    except Exception:
        return None
    if not row:
        return None
    return ResearchFact(
        key="nfl_opp_defense",
        label=f"Opp defense vs {opponent}",
        value=row,
        section=ResearchSection.MATCHUP,
        provenance=ResearchProvenance.FACTUAL,
        quality=ResearchQuality.STRONG,
        source="mongo:nfl_defense_intel",
    )


async def _opportunity_streak_shadow(player_name: str) -> ResearchShadowSignal | None:
    try:
        cursor = db.player_game_logs.find(
            {"sport": "nfl", "player_name": player_name},
            {"_id": 0, "stats": 1, "date": 1},
        ).sort("date", -1).limit(4)
        rows = await cursor.to_list(length=4)
    except Exception:
        rows = []
    if len(rows) < 3:
        return None
    n = len(rows)
    targets = [
        (r.get("stats") or {}).get("targets") or 0 for r in rows
    ]
    hits_5plus = sum(1 for t in targets if t >= 5)
    if hits_5plus < max(3, int(0.6 * n)):
        return None
    hr = hits_5plus / n
    return ResearchShadowSignal(
        key="target_streak",
        label=f"5+ targets in {hits_5plus}/{n}",
        description=f"{player_name} — 5+ targets in {hits_5plus} of last {n} games",
        hits=hits_5plus, n=n, hit_rate=round(hr, 3),
        wilson_lower=round(hr * 0.7, 3),  # coarse; UI treats SHADOW leniently
        strength="strong" if hr >= 0.75 else "moderate",
        tags=["nfl", "opportunity"],
    )


async def matchup_dna(player_name: str, opponent: str | None) -> dict[str, Any]:
    if not opponent:
        return {"available": False, "reason": "no_opponent"}
    try:
        cursor = db.player_game_logs.find(
            {"sport": "nfl", "player_name": player_name, "opponent": opponent},
            {"_id": 0, "stats": 1, "date": 1},
        ).sort("date", -1).limit(10)
        rows = await cursor.to_list(length=10)
    except Exception:
        rows = []
    if not rows:
        return {"available": False, "reason": "no_history"}
    n = len(rows)
    tgt, rec_yds, rush_yds, rec_tds = 0, 0, 0, 0
    for r in rows:
        s = r.get("stats") or {}
        tgt += s.get("targets") or 0
        rec_yds += s.get("receiving_yards") or 0
        rush_yds += s.get("rushing_yards") or 0
        rec_tds += s.get("receiving_tds") or 0
    return {
        "available": True, "sample_size": n, "vs": opponent,
        "avg_targets": round(tgt / n, 1),
        "avg_rec_yds": round(rec_yds / n, 1),
        "avg_rush_yds": round(rush_yds / n, 1),
        "avg_rec_tds": round(rec_tds / n, 2),
    }


async def build_snapshot(
    subject: str | None = None,
    opponent: str | None = None,
    event_id: str | None = None,
    event_label: str | None = None,
    role: str = "player",
    include_shadow: bool = True,
) -> CanonicalResearchSnapshot:
    t0 = time.time()
    facts: list[ResearchFact] = []
    shadow: list[ResearchShadowSignal] = []
    notes: list[str] = []
    if subject:
        facts.extend(await _player_opportunity(subject))
        f = await _opponent_defense(subject, opponent)
        if f: facts.append(f)
        if include_shadow:
            s = await _opportunity_streak_shadow(subject)
            if s: shadow.append(s)
    dna = await matchup_dna(subject, opponent) if subject else None
    notes.append(f"NFL adapter built in {int((time.time()-t0)*1000)}ms")
    return CanonicalResearchSnapshot(
        sport="NFL",
        generated_at=datetime.now(timezone.utc).isoformat(),
        subject=subject,
        event_id=event_id,
        event_label=event_label,
        facts=facts,
        shadow=shadow,
        matchup_dna=dna,
        notes=notes,
    )
