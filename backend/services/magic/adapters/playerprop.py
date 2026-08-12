"""Magic Layer 2.0 — Generic Player-Prop adapter (MLB / NBA / NFL).

Sport-agnostic scaffold that pulls exact-threshold history from
``player_game_actuals`` + emits model/market convergence + risk flags.
The stat_key mapping is per-sport but the shape is identical.

Composite market handling (NBA)
───────────────────────────────
For markets like PRA / PR / PA / RA the adapter fetches ALL
component atoms from the SAME event row (never combining points
from Game A + rebounds from Game B).  ``compute_exact_threshold_evidence``
is called with a synthetic stat key ("pra" etc.) derived AT-QUERY-TIME
by summing the atomic fields from the same event.  This preserves
the same-game invariant demanded by the directive.
"""
from __future__ import annotations

from typing import Any, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from services.magic.contract import (
    Availability, EvidenceItem, EvidenceType, MagicOutput,
    availability_from,
)
from services.magic.model_market import evaluate_model_market_convergence
from services.magic.contradictions import detect_contradictions
from services.magic.magic_score import compute_magic_score
from services.magic.exact_threshold import compute_exact_threshold_evidence


# ── Market → stat_key mappings ────────────────────────────────────
_MLB_STAT_MAP: dict[str, str] = {
    "hits":              "hits",
    "total bases":       "total_bases",
    "home runs":         "home_runs",
    "hr":                "home_runs",
    "rbi":               "rbi",
    "runs":              "runs",
    "strikeouts":        "strikeouts",
    "pitcher strikeouts": "strikeouts",
    "pitcher outs":      "pitcher_outs",
}
_NBA_STAT_MAP: dict[str, str] = {
    "points":     "points",
    "rebounds":   "rebounds",
    "assists":    "assists",
    "3pm":        "three_pointers_made",
    "threes":     "three_pointers_made",
    "3-pointers": "three_pointers_made",
}
_NBA_COMPOSITES: dict[str, tuple[str, ...]] = {
    "pra": ("points", "rebounds", "assists"),
    "pr":  ("points", "rebounds"),
    "pa":  ("points", "assists"),
    "ra":  ("rebounds", "assists"),
}
_NFL_STAT_MAP: dict[str, str] = {
    "passing yards":    "passing_yards",
    "rushing yards":    "rushing_yards",
    "receiving yards":  "receiving_yards",
    "receptions":       "receptions",
    "passing touchdowns": "passing_tds",
    "rushing tds":      "rushing_tds",
    "receiving tds":    "receiving_tds",
}


def _resolve_stat_key(sport: str, market: str) -> Optional[str]:
    m = (market or "").lower()
    if sport == "MLB":
        for k, v in _MLB_STAT_MAP.items():
            if k in m:
                return v
    if sport == "NBA":
        for c_key in _NBA_COMPOSITES:
            if c_key in m.replace(" ", ""):
                return c_key
        for k, v in _NBA_STAT_MAP.items():
            if k in m:
                return v
    if sport == "NFL":
        for k, v in _NFL_STAT_MAP.items():
            if k in m:
                return v
    return None


async def _composite_evidence(
    db: AsyncIOMotorDatabase, *, canonical_player_id: str, identity_class: str,
    parts: tuple[str, ...], threshold: float, market: Optional[str],
    selection: Optional[str],
) -> Optional[EvidenceItem]:
    """NBA composites: sum parts within THE SAME event row.  Never
    combines across events."""
    if identity_class not in ("AUTHORITATIVE", "MAPPED"):
        return EvidenceItem(
            evidence_type=EvidenceType.HISTORICAL_EXACT_THRESHOLD,
            availability=Availability.UNAVAILABLE,
            sport="NBA", market=market, selection=selection,
            line=threshold,
            canonical_player_id=canonical_player_id,
            notes="identity_class not verified — Session-D safety.",
        )
    limit = 20
    rows = await db.player_game_actuals.find(
        {"canonical_player_id": canonical_player_id,
         **{f"actuals.{p}": {"$exists": True} for p in parts}},
        projection={"_id": 0, "event_time": 1, "actuals": 1},
        sort=[("event_time", -1)],
    ).limit(limit).to_list(length=limit)
    if not rows:
        return EvidenceItem(
            evidence_type=EvidenceType.HISTORICAL_EXACT_THRESHOLD,
            availability=Availability.UNAVAILABLE,
            sport="NBA", market=market, selection=selection,
            line=threshold,
            canonical_player_id=canonical_player_id,
            notes=("no player_game_actuals rows with all component "
                    f"parts {parts}."),
        )
    obs: list[float] = []
    for r in rows:
        act = r.get("actuals") or {}
        vals = []
        ok = True
        for p in parts:
            v = act.get(p)
            if v is None:
                ok = False
                break
            try:
                vals.append(float(v))
            except (TypeError, ValueError):
                ok = False
                break
        if ok:
            obs.append(sum(vals))
    if not obs:
        return EvidenceItem(
            evidence_type=EvidenceType.HISTORICAL_EXACT_THRESHOLD,
            availability=Availability.UNAVAILABLE,
            sport="NBA", market=market, selection=selection,
            line=threshold,
            canonical_player_id=canonical_player_id,
            notes="component atoms found but none passed value coercion.",
        )
    hits = sum(1 for v in obs if v > threshold)
    rate = hits / len(obs)
    return EvidenceItem(
        evidence_type=EvidenceType.HISTORICAL_EXACT_THRESHOLD,
        availability=(Availability.AVAILABLE if len(obs) >= 5
                        else Availability.PARTIAL),
        sport="NBA", market=market, selection=selection, line=threshold,
        canonical_player_id=canonical_player_id,
        value=round(rate, 4),
        label=f"{hits}/{len(obs)} @ Over {threshold} (composite {'+'.join(parts)})",
        direction=("positive" if rate >= 0.6 else
                    "negative" if rate <= 0.4 else "neutral"),
        confidence=min(1.0, len(obs) / 20.0),
        sample_size=len(obs), time_window="last_20",
        source="player_game_actuals",
        source_class="authoritative",
        provenance={
            "composite_parts": list(parts),
            "same_event_rule": True,
        },
    )


async def build_playerprop_evidence(
    db: AsyncIOMotorDatabase, pick: dict, *, sport: str,
) -> MagicOutput:
    out = MagicOutput(
        pick_id=pick.get("id") or "",
        sport=sport,
        market=pick.get("market"),
        selection=pick.get("selection") or pick.get("player_name"),
        line=pick.get("line"),
        canonical_player_id=pick.get("canonical_player_id"),
        identity_class=pick.get("identity_class"),
    )
    ic = (pick.get("identity_class") or "").upper()
    stat_key = _resolve_stat_key(sport, pick.get("market") or "")
    line = pick.get("line")

    if stat_key and line is not None and pick.get("canonical_player_id"):
        if sport == "NBA" and stat_key in _NBA_COMPOSITES:
            comp = await _composite_evidence(
                db,
                canonical_player_id=pick.get("canonical_player_id") or "",
                identity_class=ic,
                parts=_NBA_COMPOSITES[stat_key], threshold=float(line),
                market=pick.get("market"), selection=pick.get("selection"),
            )
            if comp:
                out.add(comp)
        else:
            for it in await compute_exact_threshold_evidence(
                db,
                canonical_player_id=pick.get("canonical_player_id") or "",
                identity_class=ic,
                stat_key=stat_key, threshold=float(line),
                direction="over" if "over"
                    in (pick.get("selection") or "").lower() else "at_least",
                sport=sport, market=pick.get("market"),
                selection=pick.get("selection"),
                windows=("last_5", "last_10", "last_20", "season"),
            ):
                out.add(it)
    else:
        # Missing stat mapping or line — HISTORY unavailable, not zero.
        out.add(EvidenceItem(
            evidence_type=EvidenceType.HISTORICAL_EXACT_THRESHOLD,
            availability=Availability.UNAVAILABLE,
            sport=sport, market=pick.get("market"), line=line,
            canonical_player_id=pick.get("canonical_player_id"),
            notes=("no stat_key mapping or missing line — "
                    "history evidence unavailable."),
        ))

    # Model probability + market convergence + risk flags.
    mp = pick.get("model_probability")
    out.add(EvidenceItem(
        evidence_type=EvidenceType.MODEL_PROBABILITY,
        availability=availability_from(mp),
        sport=sport, market=pick.get("market"),
        value=(float(mp) if mp is not None else None),
        direction=("positive" if mp and float(mp) >= 0.55
                    else "negative" if mp and float(mp) <= 0.45
                    else "neutral"),
        source="pick.model_probability",
        source_class=(pick.get("model_probability_source") or "unknown"),
    ))
    conv = evaluate_model_market_convergence(
        model_probability=mp,
        book_odds=pick.get("book_odds"),
        no_real_book_line=bool(pick.get("no_real_book_line") is True),
        book_implied_prob=pick.get("book_implied_prob"),
    )
    out.model_market_state = conv["state"]
    out.add(EvidenceItem(
        evidence_type=EvidenceType.SPORTSBOOK_CONSENSUS,
        availability=(Availability.AVAILABLE
                        if conv["market_prob"] is not None
                        else Availability.UNAVAILABLE),
        sport=sport, market=pick.get("market"),
        value=conv["market_prob"], label=f"delta_pts={conv['delta_pts']}",
        direction=("positive" if conv["delta_pts"] and conv["delta_pts"] > 0
                    else "negative" if conv["delta_pts"] and conv["delta_pts"] < 0
                    else "neutral"),
        source="pick.book_odds",
        source_class="the_odds_api",
        provenance=conv,
    ))
    out.risk_flags = detect_contradictions(
        evidence=out.evidence,
        identity_class=ic,
        no_real_book_line=bool(pick.get("no_real_book_line") is True),
        model_probability=mp,
        model_market_state=conv["state"],
    )
    compute_magic_score(out, identity_class=ic)
    return out


__all__ = ["build_playerprop_evidence"]
