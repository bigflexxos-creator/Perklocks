"""Soccer Final Root Closure — Diagnostic Endpoints (Session 10).

READ-ONLY endpoints proving the coherent Soccer game distribution +
goalscorer authority state machine.

    GET /api/soccer-model/fixture
        Produces the ONE authoritative fixture score distribution
        and every derived market (1X2, BTTS, Totals, Handicap,
        Double Chance, DNB) priced from that same matrix.

    GET /api/soccer-model/goalscorer
        Applies the evidence-first authority state machine to a
        player + fixture context.  Returns authority state, lambda,
        ATG probability, Score-or-Assist probability, and Lock
        Score ceiling.

    GET /api/soccer-model/reachability
        Deterministic synthetic evidence fixtures proving that
        each Lock Score tier (85, 90, 93, 95, 96, 98, 99) is
        mathematically reachable when supported by convergent
        independent evidence.

    GET /api/soccer-model/publication-recon
        Reconciles raw Soccer market rows on the canonical picks
        collection against publication outcomes.  Returns terminal
        reason counts by family.

    GET /api/soccer-model/invariants
        Confirms mathematical invariants (Home+Draw+Away=1,
        BTTS Yes+No=1, DC = 1X2 sums, totals monotonic).
"""
from __future__ import annotations

from typing import Annotated, Optional
from fastapi import APIRouter, Depends, HTTPException, Query

from deps import current_user
from auth import UserPublic


def _get_db():
    from server import db as _db
    return _db


router = APIRouter(prefix="/api/soccer-model", tags=["soccer-final-closure"])


# ═══════════════════════════════════════════════════════════════════
# Fixture — coherent game distribution
# ═══════════════════════════════════════════════════════════════════
@router.get("/fixture")
async def fixture_distribution(
    user: Annotated[UserPublic, Depends(current_user)],
    home: str = Query(..., description="Home team canonical name"),
    away: str = Query(..., description="Away team canonical name"),
    league: str = Query("", description="League name (optional, informational)"),
):
    """Return the coherent Soccer fixture distribution + derived markets."""
    from services.soccer_game_model import (
        build_soccer_team_ctx, estimate_soccer_game_probabilities,
        price_soccer_game_markets,
    )
    db = _get_db()
    ctx = await build_soccer_team_ctx(db, home_team=home, away_team=away, league=league)
    out = estimate_soccer_game_probabilities(ctx, home, away)
    return {
        "home": home, "away": away, "league": league,
        "ctx_summary": {
            "home_form": ctx.get("home_form"),
            "away_form": ctx.get("away_form"),
        },
        "distribution": price_soccer_game_markets(out),
    }


# ═══════════════════════════════════════════════════════════════════
# Goalscorer — evidence authority
# ═══════════════════════════════════════════════════════════════════
@router.get("/goalscorer")
async def goalscorer_authority(
    user: Annotated[UserPublic, Depends(current_user)],
    # Identity
    player: str = Query(..., description="Player display name"),
    team: str = Query(...),
    opponent: str = Query(...),
    event_id: str = Query(...),
    is_home: bool = Query(True),
    league: str = Query(""),
    # Market
    book_odds: Optional[float] = Query(None, description="American odds"),
    market_implied: Optional[float] = Query(None),
    devig_implied: Optional[float] = Query(None),
    # Minutes
    minutes_state: str = Query("UNKNOWN",
        description="CONFIRMED_STARTER / PROJECTED_STARTER / ROTATION_RISK / BENCH_EXPECTED / UNKNOWN"),
    expected_minutes: Optional[float] = Query(None),
    starter_prob: Optional[float] = Query(None),
    lineup_confirmed: bool = Query(False),
    # Scoring
    goals_per_90: Optional[float] = Query(None),
    npxg_per_90: Optional[float] = Query(None),
    xg_per_90: Optional[float] = Query(None),
    shots_per_90: Optional[float] = Query(None),
    sot_per_90: Optional[float] = Query(None),
    touches_in_box_p90: Optional[float] = Query(None),
    sample_matches: Optional[int] = Query(None),
    # Assists (for Score-or-Assist)
    xa_per_90: Optional[float] = Query(None),
    assists_per_90: Optional[float] = Query(None),
    key_passes_per_90: Optional[float] = Query(None),
    # Role / env
    penalty_role: str = Query("UNKNOWN"),
    role_stability: Optional[float] = Query(None),
    team_lambda: Optional[float] = Query(None),
    opp_def_strength: Optional[float] = Query(None),
    league_reliability: Optional[float] = Query(None),
    # Provenance
    evidence_families: str = Query("", description="CSV: opportunity,minutes,team_env,opp_env,market_context,distribution"),
):
    """Return authority + λ_player + ATG + SGA + lock authority."""
    from services.soccer_player_authority import (
        PlayerEvidence, MinutesState, PenaltyRole,
        classify_authority, estimate_player_lambda,
        price_score_or_assist, player_lock_authority,
        enumerate_terminal_reason,
    )
    try:
        m_state = MinutesState(minutes_state)
    except ValueError:
        m_state = MinutesState.UNKNOWN
    try:
        p_role = PenaltyRole(penalty_role)
    except ValueError:
        p_role = PenaltyRole.UNKNOWN
    families = [s.strip() for s in (evidence_families or "").split(",") if s.strip()]
    ev = PlayerEvidence(
        player_name=player, player_id=player.lower().replace(" ", "-"),
        team=team, opponent=opponent, event_id=event_id, league=league, is_home=is_home,
        book_odds=book_odds, market_implied=market_implied, devig_implied=devig_implied,
        minutes_state=m_state, expected_minutes=expected_minutes,
        starter_prob=starter_prob, lineup_confirmed=lineup_confirmed,
        goals_per_90=goals_per_90, npxg_per_90=npxg_per_90, xg_per_90=xg_per_90,
        shots_per_90=shots_per_90, sot_per_90=sot_per_90,
        touches_in_box_p90=touches_in_box_p90, sample_matches=sample_matches,
        xa_per_90=xa_per_90, assists_per_90=assists_per_90,
        key_passes_per_90=key_passes_per_90,
        penalty_role=p_role, role_stability=role_stability,
        team_lambda=team_lambda, opp_def_strength=opp_def_strength,
        league_reliability=league_reliability,
        evidence_families=families,
    )
    auth = classify_authority(ev)
    lam = estimate_player_lambda(ev)
    sga = price_score_or_assist(ev)
    la = player_lock_authority(
        ev,
        model_prob=lam.get("atg_prob"),
        devig_prob=devig_implied,
    )
    reason = enumerate_terminal_reason(
        ev, model_prob=lam.get("atg_prob"),
        devig_prob=devig_implied, lock_score=la.reachable_max,
    )
    return {
        "authority":         auth.value,
        "reachable_max":     la.reachable_max,
        "ceiling_reasons":   la.ceiling_reasons,
        "terminal_reason":   reason.value,
        "atg":               lam,
        "score_or_assist":   sga,
        "evidence":          ev.to_dict(),
    }


# ═══════════════════════════════════════════════════════════════════
# Reachability proof — deterministic synthetic fixtures
# ═══════════════════════════════════════════════════════════════════
@router.get("/reachability")
async def reachability_proof(
    user: Annotated[UserPublic, Depends(current_user)],
):
    """Prove each Lock Score tier is mathematically reachable.

    Construct deterministic PlayerEvidence fixtures spanning weak →
    elite convergence and show the Lock Score ceiling each fixture
    can attain.  100 is intentionally NOT reachable here (Apex-only).
    """
    from services.soccer_player_authority import (
        PlayerEvidence, MinutesState, PenaltyRole,
        classify_authority, estimate_player_lambda,
        player_lock_authority,
    )
    fixtures = []

    def build(name, minutes_state, expected_minutes, samples, xg90,
              npxg90, shots90, sot90, team_lam, opp_def, model_prob,
              devig, families, pen_role=PenaltyRole.NONE, league_rel=0.9):
        ev = PlayerEvidence(
            player_name=name, player_id=name.lower(),
            team="TeamX", opponent="TeamY", event_id="e-reach", league="Test League",
            book_odds=-140, market_implied=devig, devig_implied=devig,
            minutes_state=minutes_state, expected_minutes=expected_minutes,
            starter_prob=1.0 if minutes_state == MinutesState.CONFIRMED_STARTER else None,
            goals_per_90=xg90, npxg_per_90=npxg90, xg_per_90=xg90,
            shots_per_90=shots90, sot_per_90=sot90,
            sample_matches=samples,
            penalty_role=pen_role,
            team_lambda=team_lam, opp_def_strength=opp_def,
            league_reliability=league_rel,
            evidence_families=families,
        )
        auth = classify_authority(ev)
        lam = estimate_player_lambda(ev)
        la  = player_lock_authority(ev, model_prob=lam.get("atg_prob"),
                                     devig_prob=devig)
        return {
            "case":              name,
            "authority":         auth.value,
            "reachable_max":     la.reachable_max,
            "ceiling_reasons":   la.ceiling_reasons,
            "atg_prob":          lam.get("atg_prob"),
            "lambda_player":     lam.get("lambda_player"),
        }

    _ALL_FAMILIES = ["opportunity", "minutes", "team_env", "opp_env",
                     "market_context", "distribution"]

    fixtures.append(build(
        name="apex-full-99",
        minutes_state=MinutesState.CONFIRMED_STARTER, expected_minutes=90,
        samples=25, xg90=0.85, npxg90=0.72, shots90=4.1, sot90=2.0,
        team_lam=2.4, opp_def=1.9, model_prob=0.58, devig=0.55,
        families=_ALL_FAMILIES, pen_role=PenaltyRole.PRIMARY,
    ))
    fixtures.append(build(
        name="elite-full-98",
        minutes_state=MinutesState.CONFIRMED_STARTER, expected_minutes=90,
        samples=20, xg90=0.65, npxg90=0.55, shots90=3.5, sot90=1.6,
        team_lam=2.0, opp_def=1.6, model_prob=0.52, devig=0.48,
        families=_ALL_FAMILIES, pen_role=PenaltyRole.PRIMARY,
    ))
    fixtures.append(build(
        name="strong-96",
        minutes_state=MinutesState.PROJECTED_STARTER, expected_minutes=80,
        samples=18, xg90=0.55, npxg90=0.48, shots90=3.0, sot90=1.3,
        team_lam=1.8, opp_def=1.5, model_prob=0.44, devig=0.42,
        families=_ALL_FAMILIES,
    ))
    fixtures.append(build(
        name="strong-95",
        minutes_state=MinutesState.PROJECTED_STARTER, expected_minutes=75,
        samples=14, xg90=0.42, npxg90=0.38, shots90=2.6, sot90=1.1,
        team_lam=1.6, opp_def=1.4, model_prob=0.36, devig=0.34,
        families=["opportunity", "minutes", "team_env", "opp_env",
                  "market_context"],
    ))
    fixtures.append(build(
        name="strong-93",
        minutes_state=MinutesState.PROJECTED_STARTER, expected_minutes=70,
        samples=10, xg90=0.35, npxg90=0.30, shots90=2.2, sot90=0.9,
        team_lam=1.5, opp_def=1.35, model_prob=0.30, devig=0.28,
        families=["opportunity", "minutes", "team_env", "opp_env"],
    ))
    fixtures.append(build(
        name="limited-88",
        minutes_state=MinutesState.ROTATION_RISK, expected_minutes=45,
        samples=6, xg90=0.28, npxg90=None, shots90=1.5, sot90=0.6,
        team_lam=1.4, opp_def=1.3, model_prob=0.16, devig=0.20,
        families=["opportunity", "minutes"],
    ))
    fixtures.append(build(
        name="limited-85-boundary",
        minutes_state=MinutesState.ROTATION_RISK, expected_minutes=30,
        samples=5, xg90=0.22, npxg90=None, shots90=1.2, sot90=0.5,
        team_lam=1.2, opp_def=1.2, model_prob=0.11, devig=0.14,
        families=["opportunity", "minutes"],
    ))
    fixtures.append(build(
        name="insufficient-noreach",
        minutes_state=MinutesState.UNKNOWN, expected_minutes=None,
        samples=None, xg90=None, npxg90=None, shots90=None, sot90=None,
        team_lam=None, opp_def=None, model_prob=None, devig=None,
        families=[],
    ))

    tiers = {85: False, 90: False, 93: False, 95: False, 96: False, 98: False, 99: False}
    for f in fixtures:
        rm = f["reachable_max"]
        for t in tiers:
            if rm >= t: tiers[t] = True

    return {
        "fixtures": fixtures,
        "tiers_reachable": {str(k): v for k, v in tiers.items()},
        "all_tiers_pass":  all(tiers.values()),
        "note": "100 remains Apex-only via a separate gate — not asserted here.",
    }


# ═══════════════════════════════════════════════════════════════════
# Publication reconciliation — terminal reason counts
# ═══════════════════════════════════════════════════════════════════
@router.get("/publication-recon")
async def publication_recon(
    user: Annotated[UserPublic, Depends(current_user)],
):
    """Reconcile canonical Soccer picks against publication outcomes.

    Returns raw counts by market family + terminal reason for
    non-published rows.  Data source is the live `picks` collection.
    """
    db = _get_db()
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    q = {"sport": "Soccer", "pick_date": today}
    total = await db.picks.count_documents(q)
    published = await db.picks.count_documents({
        **q,
        "$or": [
            {"publication_state": "PUBLISHED"},
            {"canonical_id": {"$exists": True}},
        ],
    })
    # ── Break out by market family ──────────────────────────────
    def _fam(m: str) -> str:
        ml = (m or "").lower()
        if "anytime" in ml and "assist" in ml:       return "score_or_assist"
        if "anytime" in ml or "goal scorer" in ml:   return "anytime_scorer"
        if "moneyline" in ml or "match winner" in ml or "match result" in ml:
            return "one_x_two"
        if "double chance" in ml:                    return "double_chance"
        if "draw no bet" in ml:                      return "dnb"
        if "btts" in ml or "both teams" in ml:       return "btts"
        if "handicap" in ml or "spread" in ml or "asian" in ml:
            return "handicap"
        if "total" in ml or "over/under" in ml or "over " in ml or "under " in ml:
            return "totals"
        return "other"
    fam_counts = {"one_x_two": 0, "handicap": 0, "totals": 0, "btts": 0,
                  "double_chance": 0, "dnb": 0, "anytime_scorer": 0,
                  "score_or_assist": 0, "other": 0}
    fam_published = dict(fam_counts)
    cursor = db.picks.find(q, {"market": 1, "publication_state": 1, "canonical_id": 1,
                                "lock_score": 1, "off_board_reason": 1})
    async for p in cursor:
        f = _fam(p.get("market") or "")
        fam_counts[f] = fam_counts.get(f, 0) + 1
        if p.get("publication_state") == "PUBLISHED" or p.get("canonical_id"):
            fam_published[f] = fam_published.get(f, 0) + 1

    # ── Off-board reason breakdown ─────────────────────────────
    reason_counts: dict[str, int] = {}
    async for p in db.picks.find({**q, "off_board_reason": {"$exists": True}},
                                  {"off_board_reason": 1, "market": 1}):
        r = p.get("off_board_reason") or "OTHER"
        reason_counts[r] = reason_counts.get(r, 0) + 1

    return {
        "as_of": today,
        "total_soccer_picks": total,
        "published_soccer_picks": published,
        "by_family": {
            k: {"total": fam_counts[k], "published": fam_published[k]}
            for k in fam_counts
        },
        "off_board_reasons": reason_counts,
        "reconciliation_check": {
            "sum_of_family_totals_matches_total": sum(fam_counts.values()) == total,
        },
    }


# ═══════════════════════════════════════════════════════════════════
# Session 10.1 · Live-Truth Closure endpoints
# ═══════════════════════════════════════════════════════════════════
@router.post("/transfer-registry/seed")
async def seed_transfer_registry(
    user: Annotated[UserPublic, Depends(current_user)],
):
    """Seed the transfer registry with manually-verified transfers.
    Idempotent — safe to call repeatedly."""
    from services.soccer_transfer_registry import seed_known_transfers
    n = await seed_known_transfers(_get_db())
    return {"seeded_or_updated": n}


@router.get("/transfer-registry/lookup")
async def transfer_registry_lookup(
    user: Annotated[UserPublic, Depends(current_user)],
    player: str = Query(...),
):
    """Return canonical current-team registry row for a player."""
    from services.soccer_transfer_registry import get_current_team
    row = await get_current_team(_get_db(), player)
    return {"player": player, "row": row}


@router.get("/stale-transfer-scan")
async def stale_transfer_scan(
    user: Annotated[UserPublic, Depends(current_user)],
    pick_date: Optional[str] = None,
    quarantine: bool = Query(False, description="If true, off-board offending picks"),
):
    """Scan today's Soccer player-prop candidates for canonical
    current-team violations.  Emits CURRENT_TEAM_MISMATCH and
    STALE_PLAYER_TEAM terminal-reason counts.  Optionally
    quarantines offending picks with off_board_reason set."""
    from services.soccer_transfer_registry import (
        scan_stale_transfer_attachments, quarantine_stale_picks,
    )
    db = _get_db()
    report = await scan_stale_transfer_attachments(db, pick_date=pick_date)
    quarantined = 0
    if quarantine and report["affected_pick_ids"]:
        # Rescan the full affected list — the diagnostic only returned first 100.
        # For safety, only quarantine the sampled offenders in this call.
        quarantined = await quarantine_stale_picks(
            db, report["affected_pick_ids"],
            reason="STALE_TRANSFER_AUTOCORRECTION",
        )
    return {**report, "quarantined_now": quarantined}


@router.get("/live-player-trace")
async def live_player_trace(
    user: Annotated[UserPublic, Depends(current_user)],
    player: str = Query(..., description="Player display name"),
    pick_date: Optional[str] = None,
):
    """End-to-end live trace for a player.  Walks:
       canonical picks → publication_state → off_board_reason →
       transfer registry → current-team invariant → /api/picks/today
       parity check.
    """
    from services.soccer_transfer_registry import (
        get_current_team, _parse_player_from_market, _parse_event_sides,
    )
    from services.soccer_player_authority import verify_current_team
    from datetime import datetime, timezone
    db = _get_db()
    if pick_date is None:
        pick_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Registry lookup
    registry = await get_current_team(db, player)
    canonical_current = registry.get("current_team") if registry else None
    # Candidates in canonical picks today
    candidates = []
    async for p in db.picks.find({
        "sport": "Soccer",
        "pick_date": pick_date,
        "$or": [
            {"market":    {"$regex": rf"\b{player}\b", "$options": "i"}},
            {"selection": {"$regex": rf"\b{player}\b", "$options": "i"}},
        ],
    }).limit(20):
        home, away = _parse_event_sides(p.get("event") or "")
        is_cur, reason, note = verify_current_team(
            player_name=player,
            canonical_current_team=canonical_current,
            event_home_team=home,
            event_away_team=away,
            pick_team_hint=p.get("team"),
        )
        candidates.append({
            "id":                p.get("id"),
            "event":             p.get("event"),
            "event_time":        p.get("event_time"),
            "market":            p.get("market"),
            "selection":         p.get("selection"),
            "team":              p.get("team"),
            "league":            p.get("league"),
            "source":            p.get("source"),
            "lock_score":        p.get("lock_score"),
            "publication_state": p.get("publication_state"),
            "off_board_reason":  p.get("off_board_reason"),
            "canonical_id":      p.get("canonical_id"),
            "current_team_verdict": {
                "is_current":       is_cur,
                "terminal_reason":  reason.value if reason else None,
                "note":             note,
            },
        })
    return {
        "player":                    player,
        "pick_date":                 pick_date,
        "registry":                  registry,
        "canonical_current_team":    canonical_current,
        "candidates_found":          len(candidates),
        "candidates":                candidates,
        "note": (
            "For a truly live proof, `candidates` should be non-empty AND every "
            "entry should carry a current_team_verdict.is_current = True. "
            "Any entry with a terminal_reason is a stale transfer / mismatch "
            "that must be quarantined."
        ),
    }


# ═══════════════════════════════════════════════════════════════════
# Invariants — mathematical sanity check on the derived markets
# ═══════════════════════════════════════════════════════════════════
@router.get("/invariants")
async def invariants(
    user: Annotated[UserPublic, Depends(current_user)],
    lambda_home: float = 1.6,
    lambda_away: float = 1.2,
):
    """Confirm all mathematical invariants hold for a Poisson/DC
    fixture with the supplied λ."""
    from services.soccer_game_model import (
        _build_score_matrix, totals_exact, handicap_from_matrix,
        btts_from_matrix, double_chance_from_1x2, dnb_from_1x2,
    )
    mat = _build_score_matrix(lambda_home, lambda_away)
    p_home = p_draw = p_away = 0.0
    for x in range(len(mat)):
        for y in range(len(mat[x])):
            c = mat[x][y]
            if x > y:   p_home += c
            elif x < y: p_away += c
            else:       p_draw += c
    btts_yes, btts_no = btts_from_matrix(mat)
    dc = double_chance_from_1x2(p_home, p_draw, p_away)
    dnb_home = dnb_from_1x2(p_home, p_draw, p_away, "home")

    # Totals ladder monotonicity
    totals = [totals_exact(mat, ln, "over")["win"] for ln in
              [0.5, 1.5, 2.5, 3.5, 4.5, 5.5, 6.5]]
    monotonic = all(totals[i] <= totals[i - 1] + 1e-6 for i in range(1, len(totals)))

    # Handicap ladder monotonicity
    hcs = [handicap_from_matrix(mat, ln, "home")["win"] for ln in
           [-2.5, -1.5, -0.5, 0.5, 1.5, 2.5]]
    hc_monotonic = all(hcs[i] >= hcs[i - 1] - 1e-6 for i in range(1, len(hcs)))

    return {
        "lambda_home": lambda_home, "lambda_away": lambda_away,
        "one_x_two_sum":     round(p_home + p_draw + p_away, 6),
        "btts_sum":          round(btts_yes + btts_no, 6),
        "dc_1x_equals_home_plus_draw":  round(dc["1X"] - (p_home + p_draw), 8),
        "dc_x2_equals_draw_plus_away":  round(dc["X2"] - (p_draw + p_away), 8),
        "dc_12_equals_home_plus_away":  round(dc["12"] - (p_home + p_away), 8),
        "dnb_home_win_plus_push_plus_lose":
            round(dnb_home["win"] + dnb_home["push"] + dnb_home["lose"], 6),
        "totals_over_monotonic_desc":   monotonic,
        "handicap_home_monotonic_asc":  hc_monotonic,
        "totals_ladder":                totals,
        "handicap_home_ladder":         hcs,
        "invariants_pass":
            abs(p_home + p_draw + p_away - 1.0) < 1e-6
            and abs(btts_yes + btts_no - 1.0) < 1e-6
            and monotonic and hc_monotonic,
    }
