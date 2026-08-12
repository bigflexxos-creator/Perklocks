"""Magic Layer 2.0 — Tennis adapter.

Surface-aware serve/return + ELO evidence chain.  Reads
``db.tennis_players`` (Sackmann mirror with surface splits + surface
ELOs) and ``db.tennis_matches_history`` for head-to-head context.
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


def _guess_surface(pick: dict) -> Optional[str]:
    """Best-effort surface extraction from league/tournament fields."""
    for k in ("surface", "court_surface", "tournament_surface"):
        v = pick.get(k)
        if v:
            return str(v).title()
    text = " ".join([
        str(pick.get("league") or ""),
        str(pick.get("event") or ""),
        str(pick.get("tournament") or ""),
    ]).lower()
    for kw, s in (("clay", "Clay"), ("grass", "Grass"),
                    ("hard", "Hard"), ("indoor", "Indoor"),
                    ("roland garros", "Clay"), ("french open", "Clay"),
                    ("wimbledon", "Grass"),
                    ("us open", "Hard"), ("australian open", "Hard")):
        if kw in text:
            return s
    return None


async def _get_tp(db: AsyncIOMotorDatabase, name: str) -> Optional[dict]:
    from services.pick_identity_remediation_soccer_tennis import (
        normalize_name,
    )
    nn = normalize_name(name)
    if not nn:
        return None
    return await db.tennis_players.find_one(
        {"name_norm": nn}, projection={"_id": 0},
    )


async def build_tennis_evidence(
    db: AsyncIOMotorDatabase, pick: dict,
) -> MagicOutput:
    out = MagicOutput(
        pick_id=pick.get("id") or "",
        sport="Tennis",
        market=pick.get("market"),
        selection=pick.get("selection") or pick.get("player_name"),
        line=pick.get("line"),
        canonical_player_id=pick.get("canonical_player_id"),
        identity_class=pick.get("identity_class"),
    )
    ic = (pick.get("identity_class") or "").upper()
    is_player_market = bool(pick.get("player_name")) or bool(
        pick.get("home_team_name") and pick.get("away_team_name"))
    surface = _guess_surface(pick)

    player_name = (pick.get("player_name")
                     or pick.get("home_team_name") or "")
    opponent = (pick.get("opponent_team")
                  or pick.get("opponent") or "")
    if not opponent and pick.get("home_team_name") and pick.get("away_team_name"):
        from services.pick_identity_remediation_soccer_tennis import (
            normalize_name,
        )
        if normalize_name(player_name) == normalize_name(
                pick.get("home_team_name") or ""):
            opponent = pick.get("away_team_name") or ""
        else:
            opponent = pick.get("home_team_name") or ""

    if is_player_market and ic not in ("AUTHORITATIVE", "MAPPED"):
        # Session-D safety — never consume history from provisional id.
        out.add(EvidenceItem(
            evidence_type=EvidenceType.HISTORICAL_EXACT_THRESHOLD,
            availability=Availability.UNAVAILABLE,
            sport="Tennis", market=pick.get("market"),
            notes="identity_class not verified — Tennis authoritative "
                  "history consumption blocked.",
        ))
    else:
        # ── 1. Player ELO (overall + surface) ────────────────────
        tp = await _get_tp(db, player_name) if player_name else None
        op = await _get_tp(db, opponent)     if opponent    else None

        if tp:
            elo = tp.get("elo_overall")
            surface_key = (f"elo_{surface.lower()}" if surface
                            else None)
            elo_surf = (tp.get(surface_key)
                          if surface_key else None)
            out.add(EvidenceItem(
                evidence_type=EvidenceType.RECENT_FORM,
                availability=availability_from(elo),
                sport="Tennis", league=pick.get("league"),
                market=pick.get("market"),
                selection=pick.get("selection"),
                canonical_player_id=pick.get("canonical_player_id"),
                value=(float(elo) if elo is not None else None),
                label=f"ELO overall={elo}",
                direction=("positive" if elo and elo >= 1600
                            else "negative" if elo and elo <= 1400
                            else "neutral"),
                source="tennis_players",
                source_class="authoritative",
                provenance={"elo_surface_key": surface_key,
                             "elo_surface":  elo_surf,
                             "form":         (tp.get("form") or {})},
            ))
            if elo_surf is not None:
                out.add(EvidenceItem(
                    evidence_type=EvidenceType.SURFACE_CONTEXT,
                    availability=Availability.AVAILABLE,
                    sport="Tennis", league=pick.get("league"),
                    market=pick.get("market"),
                    selection=pick.get("selection"),
                    canonical_player_id=pick.get("canonical_player_id"),
                    value=float(elo_surf),
                    label=f"{surface} ELO={elo_surf}",
                    direction=("positive" if elo_surf >= 1600
                                else "negative" if elo_surf <= 1400
                                else "neutral"),
                    source="tennis_players.surface_splits",
                    source_class="authoritative",
                    provenance={"surface": surface},
                ))
        else:
            out.add(EvidenceItem(
                evidence_type=EvidenceType.RECENT_FORM,
                availability=Availability.UNAVAILABLE,
                sport="Tennis", market=pick.get("market"),
                notes="tennis_players not found for selected player.",
            ))

        # ── 2. Opponent strength (relative ELO delta) ────────────
        if tp and op:
            e1 = tp.get("elo_overall")
            e2 = op.get("elo_overall")
            if e1 and e2:
                delta = float(e1) - float(e2)
                out.add(EvidenceItem(
                    evidence_type=EvidenceType.OPPONENT_STRENGTH,
                    availability=Availability.AVAILABLE,
                    sport="Tennis", market=pick.get("market"),
                    selection=pick.get("selection"),
                    canonical_player_id=pick.get("canonical_player_id"),
                    value=delta,
                    label=f"ELO delta vs opponent = {delta:+.0f}",
                    direction=("positive" if delta >= 50
                                else "negative" if delta <= -50
                                else "neutral"),
                    source="tennis_players.elo_overall",
                    source_class="authoritative",
                    provenance={"opponent": opponent},
                ))
        elif tp and not op:
            out.add(EvidenceItem(
                evidence_type=EvidenceType.OPPONENT_STRENGTH,
                availability=Availability.UNAVAILABLE,
                sport="Tennis", market=pick.get("market"),
                notes=("opponent not resolvable in tennis_players "
                        "(name_norm miss)."),
            ))

    # ── 3. Model probability ─────────────────────────────────────
    mp = pick.get("model_probability")
    out.add(EvidenceItem(
        evidence_type=EvidenceType.MODEL_PROBABILITY,
        availability=availability_from(mp),
        sport="Tennis", league=pick.get("league"),
        market=pick.get("market"),
        value=(float(mp) if mp is not None else None),
        direction=("positive" if mp and float(mp) >= 0.55
                    else "negative" if mp and float(mp) <= 0.45
                    else "neutral"),
        source="pick.model_probability",
        source_class=(pick.get("model_probability_source") or "unknown"),
    ))

    # ── 4. Model↔market convergence ──────────────────────────────
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
        sport="Tennis", league=pick.get("league"),
        market=pick.get("market"),
        value=conv["market_prob"],
        label=f"delta_pts={conv['delta_pts']}",
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


__all__ = ["build_tennis_evidence"]
