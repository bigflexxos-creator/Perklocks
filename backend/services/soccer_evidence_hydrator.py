"""Soccer Evidence Hydrator — Session 10.4 History Reconnect.
──────────────────────────────────────────────────────────────────
THE SHARED ADAPTER.  Sits between the existing universal identity/
history resolver (`services.soccer_feature_resolver.
resolve_soccer_player_features`) and the newer authority state
machine (`services.soccer_player_authority.PlayerEvidence`).

Contract:
    * Reuses the universal identity contract — canonical_player_id +
      verified aliases + canonical name + normalized provider name.
    * NEVER queries `soccer_player_form` directly by lowercased
      sportsbook name; always routes through the resolver.
    * Maps the resolver's returned row (`goals`, `xg`, `npxg`,
      `shots`, `goals_per_90`, `assists`, `key_passes`, ...) onto
      `PlayerEvidence` fields.
    * Missing fields stay None — NEVER fabricated.
    * Historical Sirius logs (Robbie Ure) remain valid HISTORY —
      current-team invariant is a SEPARATE concern (Session 10.1).
"""
from __future__ import annotations

from typing import Any, Optional
from services.soccer_player_authority import (
    PlayerEvidence, MinutesState, PenaltyRole,
)


def _to_per_90(total: Optional[float],
               minutes: Optional[float]) -> Optional[float]:
    if total is None or minutes is None: return None
    try:
        m = float(minutes)
        if m <= 0: return None
        return round(float(total) * 90.0 / m, 4)
    except Exception:
        return None


def _match_count(row: dict) -> Optional[int]:
    for k in ("matches", "games", "appearances", "sample_matches",
               "n_matches"):
        v = row.get(k)
        if v is not None:
            try: return int(v)
            except Exception: pass
    return None


async def hydrate_soccer_player_evidence(
    db,
    *,
    # Identity (pass whatever the ingest resolved)
    player_name: str,
    league: str = "",
    canonical_player_id: Optional[str] = None,
    canonical_player_name: Optional[str] = None,
    aliases: Optional[list[str]] = None,
    provider_player_name: Optional[str] = None,
    # Event context (from the sportsbook row)
    team: Optional[str] = None,
    opponent: Optional[str] = None,
    event_id: Optional[str] = None,
    is_home: Optional[bool] = None,
    # Market
    book_odds: Optional[float] = None,
    market_implied: Optional[float] = None,
    devig_implied: Optional[float] = None,
    # Fixture context from soccer_game_model
    team_lambda: Optional[float] = None,
    opp_def_strength: Optional[float] = None,
    # Minutes/lineup context
    expected_minutes: Optional[float] = None,
    starter_prob: Optional[float] = None,
    minutes_state: MinutesState = MinutesState.UNKNOWN,
    lineup_confirmed: bool = False,
    penalty_role: PenaltyRole = PenaltyRole.UNKNOWN,
) -> tuple[PlayerEvidence, str, dict]:
    """Return ``(evidence, resolver_source, raw_row)``.

    ``resolver_source`` is one of the strings returned by
    `resolve_soccer_player_features` (e.g. ``"soccer_player_form"``,
    ``"player_game_actuals"``, ``"soccer_player_game_logs"``,
    ``"espn_stats_fallback"``, or ``""`` when nothing was found).

    ``raw_row`` is the underlying resolver dict — surfaced for the
    Historical Intelligence pipeline so it can reuse the exact
    normalization/identity path.
    """
    # ── Reuse the universal identity/history resolver (P0 core) ───
    from services.soccer_feature_resolver import resolve_soccer_player_features
    row, source = await resolve_soccer_player_features(
        db,
        player_name=player_name, league=league,
        canonical_player_id=canonical_player_id,
        canonical_player_name=canonical_player_name,
        aliases=aliases,
        provider_player_name=provider_player_name,
    )
    row = row or {}
    # ── Map real fields → PlayerEvidence ──────────────────────────
    minutes    = row.get("minutes") or row.get("mins")
    matches    = _match_count(row)
    goals      = row.get("goals")
    xg         = row.get("xg") or row.get("xG")
    npxg       = row.get("npxg") or row.get("npxG")
    shots      = row.get("shots")
    sot        = row.get("shots_on_target") or row.get("sot")
    assists    = row.get("assists")
    xa         = row.get("xa") or row.get("xA")
    kp         = row.get("key_passes") or row.get("kp")
    # If per-90 rates are pre-computed by the store, prefer them;
    # otherwise derive from totals + minutes.
    goals_p90  = row.get("goals_per_90")  or _to_per_90(goals, minutes)
    xg_p90     = row.get("xg_per_90")     or _to_per_90(xg,    minutes)
    npxg_p90   = row.get("npxg_per_90")   or _to_per_90(npxg,  minutes)
    shots_p90  = row.get("shots_per_90")  or _to_per_90(shots, minutes)
    sot_p90    = row.get("sot_per_90")    or _to_per_90(sot,   minutes)
    xa_p90     = row.get("xa_per_90")     or _to_per_90(xa,    minutes)
    assists_p90= row.get("assists_per_90")or _to_per_90(assists, minutes)
    kp_p90     = row.get("key_passes_per_90") or _to_per_90(kp, minutes)

    # ── P1.2 PRIOR-SEASON SHRINKAGE ─────────────────────────────────
    # When the resolver selected CURRENT-season evidence and attached the
    # player's own prior-season row, the current per-90 rates are shrunk
    # toward the player's PRIOR rates (minutes-weighted, prior capped at
    # 900 minutes of support) instead of toward a league-average scorer.
    # The prior SUPPORTS the estimate; it never replaces current evidence.
    prior = row.get("prior_season_form") if isinstance(row.get("prior_season_form"), dict) else None
    prior_shrink_meta = None
    if prior and row.get("form_freshness") == "CURRENT_SEASON":
        p_min = float(prior.get("minutes") or 0)
        cur_min = float(minutes or 0)
        if p_min >= 450 and cur_min > 0:
            w_prior = min(p_min, 900.0)
            w_cur = cur_min
            def _blend(cur_rate, prior_total):
                pr = _to_per_90(prior_total, p_min)
                if cur_rate is None and pr is None:
                    return None
                if cur_rate is None:
                    return pr
                if pr is None:
                    return cur_rate
                return (cur_rate * w_cur + pr * w_prior) / (w_cur + w_prior)
            goals_p90 = _blend(goals_p90, prior.get("goals"))
            xg_p90    = _blend(xg_p90,    prior.get("xg"))
            shots_p90 = _blend(shots_p90, prior.get("shots"))
            xa_p90    = _blend(xa_p90,    prior.get("xa"))
            assists_p90 = _blend(assists_p90, prior.get("assists"))
            npxg_p90  = _blend(npxg_p90,  prior.get("xg")) if npxg_p90 is not None else npxg_p90
            # Combined evidence size = current matches + supported prior matches.
            prior_games = int(prior.get("games") or 0)
            matches = int(matches or 0) + min(prior_games, 10)
            prior_shrink_meta = {"prior_season": prior.get("season"), "prior_minutes": p_min,
                                 "prior_games": prior_games, "w_prior": w_prior, "w_cur": w_cur}

    # Provenance families — tag every family that had SOMETHING real.
    families = ["market_context"] if book_odds is not None else []
    if any(v is not None for v in (xg_p90, npxg_p90, goals_p90, shots_p90)):
        families.append("opportunity")
    if minutes_state != MinutesState.UNKNOWN or expected_minutes is not None:
        families.append("minutes")
    if team_lambda is not None:      families.append("team_env")
    if opp_def_strength is not None: families.append("opp_env")
    if matches is not None:          families.append("distribution")

    ev = PlayerEvidence(
        player_name=canonical_player_name or player_name,
        player_id=canonical_player_id or (canonical_player_name or player_name or "").lower(),
        team=team, opponent=opponent, event_id=event_id,
        league=league, is_home=is_home,
        book_odds=book_odds, market_implied=market_implied,
        devig_implied=devig_implied,
        minutes_state=minutes_state,
        expected_minutes=expected_minutes, starter_prob=starter_prob,
        lineup_confirmed=lineup_confirmed,
        goals_per_90=goals_p90, npxg_per_90=npxg_p90, xg_per_90=xg_p90,
        shots_per_90=shots_p90, sot_per_90=sot_p90,
        touches_in_box_p90=None,
        sample_matches=matches,
        xa_per_90=xa_p90, assists_per_90=assists_p90,
        key_passes_per_90=kp_p90,
        penalty_role=penalty_role, role_stability=None,
        team_lambda=team_lambda, opp_def_strength=opp_def_strength,
        league_reliability=None,
        evidence_families=families,
        missing_flags=[k for k in ("xg","npxg","assists","xa","shots","sot")
                        if row.get(k) is None and row.get(f"{k}_per_90") is None],
    )
    if prior_shrink_meta:
        row = dict(row); row["prior_season_shrinkage"] = prior_shrink_meta
    return ev, source, row


__all__ = ["hydrate_soccer_player_evidence"]
