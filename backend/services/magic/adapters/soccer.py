"""Magic Layer 2.0 — Soccer adapter.

Goalscorer / Creator / Dual-Threat evidence chain built on top of the
existing scorer/creator/dual-threat engine + authoritative Soccer
history (soccer_player_form, player_identities, MLS matchup history).

The existing archetype engine (services/player_props) is NOT rebuilt
— Magic READS its output and enriches with role/opportunity + xG
context + matchup evidence + real market convergence.

Missing data
────────────
* xG/xA present only for Big-5 (Understat).  Small leagues degrade
  to UNAVAILABLE — never fabricated.
* Opponent-defense strength is UNAVAILABLE when the registry has no
  authoritative team meta for the fixture.
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


async def _fetch_soccer_form(
    db: AsyncIOMotorDatabase, *, canonical_player_id: str,
    player_name: str, league: Optional[str],
) -> Optional[dict]:
    """Look up soccer_player_form by canonical id OR name+league."""
    # No canonical_player_id column on soccer_player_form — use name
    # + league as a mapping aid (Session-D style guardrail is at the
    # identity gate; here we only enrich).
    q: dict[str, Any] = {}
    if player_name:
        from services.pick_identity_remediation_soccer_tennis import (
            normalize_name,
        )
        q["name_canonical"] = normalize_name(player_name)
    if not q:
        return None
    if league:
        q["league"] = league
    row = await db.soccer_player_form.find_one(q, projection={"_id": 0})
    if row:
        return row
    if league:
        # Fall back to league-less search.
        q.pop("league")
        return await db.soccer_player_form.find_one(q, projection={"_id": 0})
    return None


def _archetype_label(row: dict) -> str:
    """Derive a coarse archetype using authoritative form fields
    when available.  Delegates to the existing engine's shape:
    GOAL_SCORER / CREATOR / DUAL_THREAT / PLAYMAKER / LOW_INVOLVEMENT
    / UNKNOWN.
    """
    if not row:
        return "UNKNOWN"
    g90  = float(row.get("goals_per_90")   or 0.0)
    kp   = float(row.get("key_passes")     or 0.0)
    ast  = float(row.get("assists")        or 0.0)
    npxg = float(row.get("npxg_per_90")    or 0.0)
    if g90 >= 0.35 and (ast >= 5 or kp >= 20):
        return "DUAL_THREAT"
    if g90 >= 0.35 or npxg >= 0.30:
        return "GOAL_SCORER"
    if ast >= 5 or kp >= 20:
        return "CREATOR"
    if g90 == 0.0 and ast == 0.0 and kp == 0.0:
        return "LOW_INVOLVEMENT"
    return "PLAYMAKER"


async def build_soccer_evidence(
    db: AsyncIOMotorDatabase, pick: dict,
) -> MagicOutput:
    """Emit Magic evidence bundle for a Soccer pick."""
    out = MagicOutput(
        pick_id=pick.get("id") or "",
        sport="Soccer",
        market=pick.get("market"),
        selection=pick.get("selection") or pick.get("player_name"),
        line=pick.get("line"),
        canonical_player_id=pick.get("canonical_player_id"),
        canonical_team_id=pick.get("canonical_team_id"),
        identity_class=pick.get("identity_class"),
    )
    league = pick.get("league")
    player_name = pick.get("player_name") or ""
    is_player_market = bool(player_name)
    ic = (pick.get("identity_class") or "").upper()

    # ── 1. Historical exact-threshold (only for verified identity) ──
    if is_player_market and ic in ("AUTHORITATIVE", "MAPPED"):
        # For soccer, exact-threshold history lives in
        # player_game_actuals when backfilled.  When missing we
        # emit an UNAVAILABLE evidence item — never zero.
        from services.magic.exact_threshold import (
            compute_exact_threshold_evidence,
        )
        stat_key = (
            "goals"   if "goal" in (pick.get("market") or "").lower()
            else "shots_on_target" if "shots on target"
            in (pick.get("market") or "").lower()
            else "shots" if "shots" in (pick.get("market") or "").lower()
            else "assists" if "assist"
            in (pick.get("market") or "").lower()
            else "goals"
        )
        line = pick.get("line") or 0.5
        for it in await compute_exact_threshold_evidence(
            db,
            canonical_player_id=pick.get("canonical_player_id") or "",
            identity_class=ic,
            stat_key=stat_key, threshold=float(line),
            direction="over" if "over" in (pick.get("selection")
                                              or "").lower()
                       else "at_least",
            sport="Soccer", league=league,
            market=pick.get("market"), selection=pick.get("selection"),
            windows=("last_10", "season"),
        ):
            out.add(it)

    # ── 2. Form + archetype (soccer_player_form) ─────────────────
    form_row: Optional[dict] = None
    archetype = "UNKNOWN"
    if is_player_market:
        form_row = await _fetch_soccer_form(
            db, canonical_player_id=pick.get("canonical_player_id"),
            player_name=player_name, league=league,
        )
        archetype = _archetype_label(form_row or {})
    if form_row:
        # Recent form (form_score is a rolling 0..100 authoritative signal).
        fs = form_row.get("form_score")
        out.add(EvidenceItem(
            evidence_type=EvidenceType.RECENT_FORM,
            availability=availability_from(fs, sample_size=int(
                form_row.get("games") or 0)),
            sport="Soccer", league=league,
            market=pick.get("market"), selection=pick.get("selection"),
            canonical_player_id=pick.get("canonical_player_id"),
            value=(float(fs) if fs is not None else None),
            label=str(form_row.get("form_label") or "").upper(),
            direction=("positive" if fs and float(fs) >= 60
                        else "negative" if fs and float(fs) <= 40
                        else "neutral"),
            sample_size=int(form_row.get("games") or 0),
            time_window="season",
            source="soccer_player_form",
            source_class="authoritative",
            provenance={
                "goals_per_90":  form_row.get("goals_per_90"),
                "shots_per_90":  form_row.get("shots_per_90"),
                "npxg_per_90":   form_row.get("npxg_per_90"),
                "assists":       form_row.get("assists"),
                "key_passes":    form_row.get("key_passes"),
                "goals_over_xg": form_row.get("goals_over_xg"),
                "archetype":     archetype,
            },
        ))
        # Role / opportunity — minutes proxy from games column.
        games = int(form_row.get("games") or 0)
        minutes = int(form_row.get("minutes") or 0)
        role_av = (Availability.AVAILABLE if minutes > 0
                     else Availability.UNAVAILABLE)
        out.add(EvidenceItem(
            evidence_type=EvidenceType.ROLE_OPPORTUNITY,
            availability=role_av,
            sport="Soccer", league=league,
            market=pick.get("market"), selection=pick.get("selection"),
            canonical_player_id=pick.get("canonical_player_id"),
            value=float(minutes),
            label=f"{minutes} min over {games} games",
            direction=("positive" if minutes >= 1500
                        else "neutral" if minutes >= 900
                        else "negative"),
            sample_size=games,
            source="soccer_player_form",
            source_class="authoritative",
            provenance={"position": form_row.get("position"),
                         "archetype": archetype},
        ))
    else:
        if is_player_market:
            out.add(EvidenceItem(
                evidence_type=EvidenceType.RECENT_FORM,
                availability=Availability.UNAVAILABLE,
                sport="Soccer", league=league,
                market=pick.get("market"),
                canonical_player_id=pick.get("canonical_player_id"),
                notes="soccer_player_form not available for this player.",
            ))

    # ── 3. Matchup (opponent-strength) — often UNAVAILABLE ────────
    out.add(EvidenceItem(
        evidence_type=EvidenceType.MATCHUP,
        availability=Availability.UNAVAILABLE,
        sport="Soccer", league=league,
        market=pick.get("market"),
        notes="Opponent-defense strength adapter is a Session-Magic-3 target; "
              "current session does not synthesize a matchup value.",
    ))

    # ── 4. Model probability (existing engine output) ─────────────
    mp = pick.get("model_probability")
    out.add(EvidenceItem(
        evidence_type=EvidenceType.MODEL_PROBABILITY,
        availability=availability_from(mp),
        sport="Soccer", league=league,
        market=pick.get("market"), selection=pick.get("selection"),
        canonical_player_id=pick.get("canonical_player_id"),
        value=(float(mp) if mp is not None else None),
        direction=("positive" if mp and float(mp) >= 0.55
                    else "negative" if mp and float(mp) <= 0.45
                    else "neutral"),
        source="pick.model_probability",
        source_class=(pick.get("model_probability_source") or "unknown"),
    ))

    # ── 5. Model↔market convergence ──────────────────────────────
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
        sport="Soccer", league=league,
        market=pick.get("market"), selection=pick.get("selection"),
        value=conv["market_prob"],
        label=f"delta_pts={conv['delta_pts']}",
        direction=("positive" if conv["delta_pts"] and conv["delta_pts"] > 0
                    else "negative" if conv["delta_pts"] and conv["delta_pts"] < 0
                    else "neutral"),
        source="pick.book_odds",
        source_class="the_odds_api",
        provenance=conv,
    ))

    # ── 6. Contradiction / risk flags ────────────────────────────
    out.risk_flags = detect_contradictions(
        evidence=out.evidence,
        identity_class=ic,
        no_real_book_line=bool(pick.get("no_real_book_line") is True),
        goals_over_xg_ratio=(
            form_row.get("goals_over_xg") if form_row else None),
        model_probability=mp,
        model_market_state=conv["state"],
    )

    # ── 7. Aggregate ─────────────────────────────────────────────
    compute_magic_score(out, identity_class=ic)
    return out


__all__ = ["build_soccer_evidence"]
