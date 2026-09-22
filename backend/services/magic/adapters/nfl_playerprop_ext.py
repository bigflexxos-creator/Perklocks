"""NFL Player-Prop MAGIC evidence extension.

Bridges the NFL Props V2 distribution/context stack and the persisted
NFL feature-engine factors into the Magic evidence-category authority.

BEFORE this module, ``build_playerprop_evidence`` emitted only 3
evidence types for NFL picks (HISTORICAL_EXACT_THRESHOLD /
MODEL_PROBABILITY / SPORTSBOOK_CONSENSUS) — populating only 3 of the
6 independent Magic categories (`history_exact`, `model_family`,
`market_intel`).  Apex #6 (≥5 positive categories) and Apex #7
(``role_opportunity`` OR ``matchup`` positive) were therefore
structurally unreachable for every NFL player prop.

This module adds:
    * ``RECENT_FORM``      — L5/L10 avg-vs-line signal from NFL
      feature-engine factors + V2 distribution windows.  Distinct
      source_key so ``collapse_history_form`` still enforces the
      shared-source guard when appropriate.
    * ``ROLE_OPPORTUNITY`` — snap-% / usage from ``nfl_player_usage``
      + V2 role-status when the usage collection is absent.
    * ``MATCHUP``          — opponent allowance from
      ``player_game_actuals`` for the pick's opponent+position;
      fail-closed to UNAVAILABLE when opponent history is missing
      (no fabrication, no L5/L10 hit-rate laundering).

Independence contract:
    * HISTORY  → source_key = "player_game_actuals::nfl:L20_threshold"
    * FORM     → source_key = "nfl_feature_engine::L5_avg_vs_line"
      (nominally shares game log rows but the *derived metric* is a
      strict recency-average, not the L20 threshold rate; the existing
      ``collapse_history_form`` guard will still zero FORM's positive
      vote when source_key matches HISTORY — this is by design)
    * ROLE     → source_key = "nfl_player_usage::snap_pct"
    * MATCHUP  → source_key = f"player_game_actuals::opponent={opp}"
      (different rows entirely — opponent's other players, not the
      pick's player)

Never:
    - Fabricates matchup evidence.
    - Uses sportsbook odds as any category's data.
    - Emits ROLE/MATCHUP with AVAILABLE availability unless real
      backing rows were fetched.
    - Overrides the direction contract (positive iff genuine signal).
"""
from __future__ import annotations

from typing import Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from services.magic.contract import (
    Availability, EvidenceItem, EvidenceType, MagicOutput,
)


# ── Market → stat mapping (mirrors gold_evidence_nfl._nfl_market_stat)
def _nfl_market_stat(market: str) -> Optional[str]:
    m = (market or "").lower()
    if "pass" in m and "yard" in m:                   return "passing_yards"
    if "pass" in m and "td" in m:                     return "passing_tds"
    if "rush" in m and "yard" in m:                   return "rushing_yards"
    if "rush" in m and "td" in m:                     return "rushing_tds"
    if "receiv" in m and "yard" in m:                 return "receiving_yards"
    if "receiv" in m and "td" in m:                   return "receiving_tds"
    if "reception" in m or "recept" in m:             return "receptions"
    return None


def _stat_from_actuals(actuals: dict, stat: str) -> Optional[float]:
    aliases = {
        "passing_yards":   ("passing_yards", "pass_yds"),
        "passing_tds":     ("passing_tds",   "pass_tds"),
        "rushing_yards":   ("rushing_yards", "rush_yds"),
        "rushing_tds":     ("rushing_tds",   "rush_tds"),
        "receiving_yards": ("receiving_yards", "rec_yds"),
        "receiving_tds":   ("receiving_tds",   "rec_tds"),
        "receptions":      ("receptions",   "rec"),
    }
    for k in aliases.get(stat, (stat,)):
        v = actuals.get(k)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


# ── RECENT_FORM ────────────────────────────────────────────────────
def _build_recent_form(pick: dict) -> EvidenceItem:
    """L5 avg-vs-line signal from the NFL feature-engine factors
    already stamped on the pick.  No DB call — this is a re-emission
    of already-persisted derived numbers with new category label."""
    factors = pick.get("factors") or {}
    l5 = factors.get("L5 Avg vs Line")
    l3_trend = factors.get("L3 vs Season Trend")

    if l5 is None:
        return EvidenceItem(
            evidence_type=EvidenceType.RECENT_FORM,
            availability=Availability.UNAVAILABLE,
            sport="NFL", market=pick.get("market"),
            selection=pick.get("selection"), line=pick.get("line"),
            canonical_player_id=pick.get("canonical_player_id"),
            source="nfl_feature_engine",
            source_class="nfl_feature_engine::L5_avg_vs_line",
            notes="nfl_feature_engine L5 Avg vs Line factor not present",
        )

    try:
        v = float(l5)
    except (TypeError, ValueError):
        return EvidenceItem(
            evidence_type=EvidenceType.RECENT_FORM,
            availability=Availability.UNAVAILABLE,
            sport="NFL",
            source="nfl_feature_engine",
            source_class="nfl_feature_engine::L5_avg_vs_line",
            notes=f"non-numeric L5 factor {l5!r}",
        )

    # Direction: L5 avg is normalised to a factor in [0,1] where 0.6+
    # is a positive recency signal (already tuned by the feature
    # engine to be threshold-relative).
    direction = ("positive" if v >= 0.60
                 else "negative" if v <= 0.40
                 else "neutral")
    confidence = min(1.0, abs(v - 0.5) * 2.0 + 0.2)
    return EvidenceItem(
        evidence_type=EvidenceType.RECENT_FORM,
        availability=Availability.AVAILABLE,
        sport="NFL", market=pick.get("market"),
        selection=pick.get("selection"), line=pick.get("line"),
        canonical_player_id=pick.get("canonical_player_id"),
        value=round(v, 4), direction=direction,
        confidence=round(confidence, 3),
        time_window="last_5",
        source="nfl_feature_engine",
        source_class="nfl_feature_engine::L5_avg_vs_line",
        provenance={
            "L5_avg_vs_line":       v,
            "L3_vs_season_trend":   l3_trend,
            "provenance_note": "derived from persisted nfl_feature_engine factors",
        },
        label=f"L5 vs line = {round(v, 3)}",
    )


# ── ROLE_OPPORTUNITY ────────────────────────────────────────────────
async def _build_role_opportunity(
    db: AsyncIOMotorDatabase, pick: dict,
) -> EvidenceItem:
    """Snap-% / usage share from ``nfl_player_usage`` (authoritative),
    with V2 role_status as a partial fallback when the usage table is
    empty for this player."""
    cpid = pick.get("canonical_player_id")
    v2 = pick.get("nfl_props_v2_evidence") or {}
    role_status_v2 = str(v2.get("role_status") or "").upper() or None

    if not cpid:
        return EvidenceItem(
            evidence_type=EvidenceType.ROLE_OPPORTUNITY,
            availability=Availability.UNAVAILABLE,
            sport="NFL", market=pick.get("market"),
            source="nfl_player_usage",
            source_class="nfl_player_usage::snap_pct",
            notes="missing canonical_player_id",
        )

    row = None
    try:
        row = await db.nfl_player_usage.find_one(
            {"player_id": str(cpid)}, sort=[("season", -1)])
        if not row:
            row = await db.nfl_player_usage.find_one(
                {"player_id": cpid}, sort=[("season", -1)])
    except Exception:
        row = None

    if row:
        snap_pct = row.get("snap_pct_avg")
        try:
            sp = float(snap_pct) if snap_pct is not None else None
        except (TypeError, ValueError):
            sp = None
        if sp is None:
            avail = Availability.PARTIAL
            direction = "neutral"
        else:
            avail = Availability.AVAILABLE
            direction = ("positive" if sp >= 0.65
                         else "negative" if sp <= 0.30
                         else "neutral")
        return EvidenceItem(
            evidence_type=EvidenceType.ROLE_OPPORTUNITY,
            availability=avail, sport="NFL", market=pick.get("market"),
            selection=pick.get("selection"), line=pick.get("line"),
            canonical_player_id=str(cpid),
            value=(round(sp, 4) if sp is not None else None),
            direction=direction,
            confidence=min(1.0, (sp or 0.0) + 0.15),
            time_window="season",
            source="nfl_player_usage",
            source_class="nfl_player_usage::snap_pct",
            provenance={
                "snap_pct_avg":      row.get("snap_pct_avg"),
                "offense_snaps_sum": row.get("offense_snaps_sum"),
                "position":          row.get("position"),
                "team":              row.get("team"),
                "season":            row.get("season"),
                "games":             row.get("games"),
                "source":            "nfl_player_usage",
            },
            label=f"snap%={sp}",
        )

    # No usage row — V2 role_status partial fallback.  Never marks
    # AVAILABLE without real usage rows.
    if role_status_v2 in ("AVAILABLE", "PARTIAL", "PRIMARY"):
        return EvidenceItem(
            evidence_type=EvidenceType.ROLE_OPPORTUNITY,
            availability=Availability.PARTIAL, sport="NFL",
            market=pick.get("market"),
            canonical_player_id=str(cpid),
            direction="positive" if role_status_v2 == "PRIMARY" else "neutral",
            confidence=0.4,
            source="nfl_props_v2::role_evidence",
            source_class="nfl_props_v2::role_status",
            provenance={
                "role_status_v2":   role_status_v2,
                "role_evidence":    v2.get("role_evidence"),
                "fallback_reason":  "no nfl_player_usage row for player_id",
            },
        )
    return EvidenceItem(
        evidence_type=EvidenceType.ROLE_OPPORTUNITY,
        availability=Availability.UNAVAILABLE, sport="NFL",
        market=pick.get("market"), canonical_player_id=str(cpid),
        source="nfl_player_usage",
        source_class="nfl_player_usage::snap_pct",
        notes="no nfl_player_usage row and no V2 role_status fallback",
    )


# ── MATCHUP ─────────────────────────────────────────────────────────
async def _build_matchup(
    db: AsyncIOMotorDatabase, pick: dict,
) -> EvidenceItem:
    """Opponent allowance vs the pick's position — fetched from
    ``player_game_actuals`` for the opponent (NOT the pick's player).
    Fail-closed to UNAVAILABLE when opponent+position+stat cannot be
    resolved.  This is DIFFERENT DATA from HISTORY/FORM (different
    player rows entirely) — genuinely independent."""
    stat = _nfl_market_stat(pick.get("market") or "")
    opp = pick.get("opponent") or pick.get("opponent_team")
    position = pick.get("position")

    ev = EvidenceItem(
        evidence_type=EvidenceType.MATCHUP,
        availability=Availability.UNAVAILABLE,
        sport="NFL", market=pick.get("market"),
        selection=pick.get("selection"), line=pick.get("line"),
        canonical_player_id=pick.get("canonical_player_id"),
        source="player_game_actuals",
        source_class=f"player_game_actuals::opponent={opp or 'unknown'}",
    )
    if not (stat and opp):
        ev.notes = "matchup requires stat + opponent"
        return ev

    # Pre-game cutoff to avoid leakage.
    cutoff_iso = pick.get("event_time") or pick.get("commence_time")
    q: dict = {"sport": "nfl", "opponent": str(opp).upper()}
    if cutoff_iso:
        q["event_time"] = {"$lt": cutoff_iso}
    if position:
        q["position"] = position

    rows: list[dict] = []
    try:
        cursor = db.player_game_actuals.find(
            q, {"event_time": 1, "actuals": 1, "position": 1, "_id": 0}
        ).sort([("event_time", -1)]).limit(200)
        async for r in cursor:
            rows.append(r)
    except Exception:
        rows = []

    if not rows:
        ev.notes = f"no opponent history rows for opp={opp}"
        return ev

    values = [
        v for v in (_stat_from_actuals(r.get("actuals") or {}, stat)
                    for r in rows)
        if v is not None
    ]
    if not values:
        ev.availability = Availability.PARTIAL
        ev.notes = "rows found but stat missing on all"
        return ev

    avg = sum(values) / len(values)
    line = pick.get("line")
    direction = "neutral"
    if line is not None:
        try:
            L = float(line)
            direction = "positive" if avg > L else "negative"
        except (TypeError, ValueError):
            pass

    ev.availability = (Availability.AVAILABLE if len(values) >= 10
                       else Availability.PARTIAL)
    ev.value = round(avg, 3)
    ev.direction = direction
    ev.confidence = min(1.0, len(values) / 50.0)
    ev.sample_size = len(values)
    ev.time_window = "opponent_season"
    ev.provenance = {
        "opponent":       opp,
        "position":       position,
        "stat":           stat,
        "opp_avg":        round(avg, 3),
        "n_samples":      len(values),
        "line":           line,
        "cutoff":         cutoff_iso,
        "temporal_rule":  "event_time < cutoff (no leakage)",
    }
    ev.label = f"{opp} avg {stat}={round(avg, 2)} vs line={line}"
    return ev


# ── Public entrypoint ──────────────────────────────────────────────
async def emit_nfl_extended_evidence(
    db: AsyncIOMotorDatabase, pick: dict, out: MagicOutput,
) -> None:
    """Add RECENT_FORM / ROLE_OPPORTUNITY / MATCHUP EvidenceItems to
    the MagicOutput.  Non-destructive — never removes existing items."""
    out.add(_build_recent_form(pick))
    out.add(await _build_role_opportunity(db, pick))
    out.add(await _build_matchup(db, pick))


__all__ = ["emit_nfl_extended_evidence"]
