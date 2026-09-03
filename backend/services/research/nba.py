"""NBA research adapter — Strategy Lab 10X.

Reuses:
* `services.nba_feature_engine` — minutes / usage / pace factors
* `db.player_game_logs` (sport=nba) — L10 gamelog rows
* `db.team_form` (sport=nba) — pace / DEF rating
* `db.pick_enrichment.history_shadow` — read-only shadow

Facts (FACTUAL):
  * L10 minutes / usage / points / rebounds / assists / PRA
  * Pace + opponent defensive rating
  * Rest days
Shadow (UI-only):
  * hot-scoring streaks, matchup outlier hits
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

log = logging.getLogger("lockscore.research.nba")


async def _recent_gamelog(player_name: str) -> tuple[list[ResearchFact], list[dict]]:
    facts: list[ResearchFact] = []
    try:
        cursor = db.player_game_logs.find(
            {"sport": "nba", "player_name": player_name},
            {"_id": 0, "stats": 1, "date": 1, "opponent": 1, "minutes": 1},
        ).sort("date", -1).limit(10)
        rows = await cursor.to_list(length=10)
    except Exception:
        rows = []
    if not rows:
        return facts, []
    n = len(rows)
    tot = {"pts": 0.0, "reb": 0.0, "ast": 0.0, "min": 0.0, "fg3m": 0.0,
           "pra": 0.0, "stl": 0.0, "blk": 0.0}
    for r in rows:
        s = r.get("stats") or {}
        tot["pts"] += s.get("points") or 0
        tot["reb"] += s.get("rebounds") or 0
        tot["ast"] += s.get("assists") or 0
        tot["fg3m"] += s.get("three_pointers_made") or 0
        tot["stl"] += s.get("steals") or 0
        tot["blk"] += s.get("blocks") or 0
        tot["min"] += s.get("minutes") or r.get("minutes") or 0
        tot["pra"] += ((s.get("points") or 0) + (s.get("rebounds") or 0)
                       + (s.get("assists") or 0))
    q = ResearchQuality.STRONG if n >= 5 else ResearchQuality.PARTIAL
    for k, label in [
        ("pts", "L10 PTS"), ("reb", "L10 REB"), ("ast", "L10 AST"),
        ("pra", "L10 PRA"), ("fg3m", "L10 3PM"), ("min", "L10 MIN"),
        ("stl", "L10 STL"), ("blk", "L10 BLK"),
    ]:
        facts.append(ResearchFact(
            key=f"nba_l10_{k}",
            label=label,
            value=round(tot[k] / n, 2),
            section=ResearchSection.FORM,
            provenance=ResearchProvenance.FACTUAL,
            quality=q,
            sample_size=n,
            source="mongo:player_game_logs",
        ))
    return facts, rows


async def _pace_opponent(opponent: str | None) -> list[ResearchFact]:
    if not opponent:
        return []
    try:
        row = await db.team_form.find_one(
            {"sport": "nba", "team_canonical": opponent.lower()},
            {"_id": 0, "pace": 1, "def_rating": 1, "opp_pts_pg": 1},
        )
    except Exception:
        row = None
    if not row:
        return []
    out: list[ResearchFact] = []
    for k, label, section in [
        ("pace", "Opp pace", ResearchSection.PACE),
        ("def_rating", "Opp DEF rating", ResearchSection.MATCHUP),
        ("opp_pts_pg", "Opp PPG allowed", ResearchSection.MATCHUP),
    ]:
        v = row.get(k)
        if v is None:
            continue
        out.append(ResearchFact(
            key=f"nba_opp_{k}", label=label, value=v,
            section=section, provenance=ResearchProvenance.FACTUAL,
            quality=ResearchQuality.STRONG,
            source="mongo:team_form",
        ))
    return out


async def _hot_scoring_shadow(rows: list[dict], threshold: float = 20.0) -> ResearchShadowSignal | None:
    if len(rows) < 5:
        return None
    hits = 0
    n = len(rows)
    for r in rows:
        p = (r.get("stats") or {}).get("points") or 0
        if p >= threshold:
            hits += 1
    if hits / n < 0.6:
        return None
    hr = hits / n
    # PERKLOCKS MAIN 36 · P1 — use REAL Wilson lower bound, not the
    # heuristic hr × 0.75 the old label claimed.
    from services.discovery.confidence_system import wilson_lower_bound
    return ResearchShadowSignal(
        key="scoring_streak",
        label=f"{int(threshold)}+ PTS in {hits}/{n}",
        description=f"Scored {int(threshold)}+ in {hits} of last {n} games",
        hits=hits, n=n, hit_rate=round(hr, 3),
        wilson_lower=round(wilson_lower_bound(hits, n), 3),
        strength="strong" if hr >= 0.75 else "moderate",
        tags=["nba", "streak"],
    )


async def matchup_dna(player_name: str, opponent: str | None) -> dict[str, Any]:
    if not opponent:
        return {"available": False, "reason": "no_opponent"}
    try:
        cursor = db.player_game_logs.find(
            {"sport": "nba", "player_name": player_name, "opponent": opponent},
            {"_id": 0, "stats": 1, "date": 1},
        ).sort("date", -1).limit(10)
        rows = await cursor.to_list(length=10)
    except Exception:
        rows = []
    if not rows:
        return {"available": False, "reason": "no_history"}
    n = len(rows)
    pts = reb = ast = 0.0
    for r in rows:
        s = r.get("stats") or {}
        pts += s.get("points") or 0
        reb += s.get("rebounds") or 0
        ast += s.get("assists") or 0
    return {
        "available": True, "sample_size": n, "vs": opponent,
        "avg_pts": round(pts / n, 1),
        "avg_reb": round(reb / n, 1),
        "avg_ast": round(ast / n, 1),
        "avg_pra": round((pts + reb + ast) / n, 1),
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
    rows: list[dict] = []
    if subject:
        f_l10, rows = await _recent_gamelog(subject)
        facts.extend(f_l10)
        facts.extend(await _pace_opponent(opponent))
        if include_shadow:
            s = await _hot_scoring_shadow(rows)
            if s: shadow.append(s)
    dna = await matchup_dna(subject, opponent) if subject else None
    notes.append(f"NBA adapter built in {int((time.time()-t0)*1000)}ms")
    return CanonicalResearchSnapshot(
        sport="NBA",
        generated_at=datetime.now(timezone.utc).isoformat(),
        subject=subject,
        event_id=event_id,
        event_label=event_label,
        facts=facts,
        shadow=shadow,
        matchup_dna=dna,
        notes=notes,
    )
