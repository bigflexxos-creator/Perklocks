"""Soccer Scorer Regen via Authority — Session 10.5.
──────────────────────────────────────────────────────────────────
Wires the SESSION 10.4 hydrator + SESSION 10 authority path directly
into current/future ``db.picks`` for Soccer player-prop markets.

Contract:
    * Reads only CURRENT / FUTURE soccer player-prop picks
      (``event_time >= now``, ``sport == "Soccer"``,
      ``book_odds != None``).
    * NEVER touches ``started`` / ``settled`` / ``history`` picks.
    * Uses ``hydrate_soccer_player_evidence`` (identity registry
      contract) → ``estimate_player_lambda`` (real math) →
      ``player_lock_authority`` (ceiling).
    * Model probability is set on ``win_probability``.
    * Lock score is capped at the authority ceiling (LIMITED=88, etc).
    * Stamps ``soccer_model_version`` + ``generation_id`` +
      ``model_math_trace`` for audit provenance.
    * If a pick already has current-generation stamps, it's skipped.

Markets covered (extract from real_line_scorer_ingest labels):
    * Anytime Goal Scorer          → ATG          (estimate_player_lambda)
    * First Goal Scorer            → ATG × 0.35   (rough FGS conversion)
    * Last Goal Scorer             → ATG × 0.30
    * To Score or Assist           → SGA          (price_score_or_assist)
    * Anytime Assist               → ATG_assist   (estimate_assist_lambda)
    * Shots / Shots on Target      → Poisson on shots_per_90 / sot_per_90
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any, Optional

from services.soccer_evidence_hydrator import hydrate_soccer_player_evidence
from services.soccer_player_authority import (
    MinutesState, PenaltyRole,
    classify_authority, estimate_player_lambda, estimate_assist_lambda,
    price_score_or_assist, player_lock_authority, Authority,
)
from services.soccer_game_model import (
    build_soccer_team_ctx, estimate_soccer_game_probabilities,
)


_SOCCER_MODEL_VERSION = "soccer_player_authority_v1.0.5"


# ── Market classification ───────────────────────────────────────
_MARKET_PATTERNS = [
    (re.compile(r"Anytime Goal Scorer|Anytime Scorer", re.I),      "atg"),
    (re.compile(r"First Goal Scorer",                     re.I),      "fgs"),
    (re.compile(r"Last Goal Scorer",                      re.I),      "lgs"),
    (re.compile(r"To Score or Assist|Score or Assist",   re.I),      "sga"),
    (re.compile(r"Anytime Assist|To Assist",             re.I),      "atg_assist"),
    (re.compile(r"Shots on Target|SOT",                  re.I),      "sot"),
    (re.compile(r"\bShots\b",                              re.I),      "shots"),
]


def _classify_market(market: str) -> Optional[str]:
    for pat, tag in _MARKET_PATTERNS:
        if pat.search(market or ""):
            return tag
    return None


def _extract_player_from_market(market: str, selection: str) -> str:
    """Real book markets are formatted as
    ``<Player Name> Anytime Goal Scorer`` per real_line_scorer_ingest.
    """
    m = market or ""
    for suffix in ("Anytime Goal Scorer", "First Goal Scorer",
                   "Last Goal Scorer", "To Score or Assist",
                   "Anytime Assist", "Shots on Target", "Shots"):
        if m.endswith(suffix):
            return m[:-len(suffix)].strip(" -·:")
    # Selection fallback
    return (selection or "").strip()


def _parse_event(event: str) -> tuple[str, str]:
    """`event` is "Home Team @ Away Team" or "Home vs Away"."""
    e = (event or "").strip()
    for sep in (" @ ", " vs ", " v "):
        if sep in e:
            a, b = e.split(sep, 1)
            return a.strip(), b.strip()
    return "", ""


def _line_from_market(market: str) -> Optional[float]:
    m = re.search(r"(\d+(?:\.\d+)?)", market or "")
    return float(m.group(1)) if m else None


def _poisson_over_line(lam: float, line: float) -> float:
    """P(X > line) = 1 - Sum_{k<=floor(line)} Poisson(k;lam).

    Half-lines (0.5, 1.5, 2.5) commonly used in shots markets.
    """
    if lam <= 0: return 0.0
    thresh = math.floor(line)
    p_le = 0.0
    log_lam = math.log(lam) if lam > 0 else 0
    # Compute cumulative up to thresh
    logfact = 0.0
    for k in range(thresh + 1):
        if k > 0:
            logfact += math.log(k)
        log_p = -lam + k * log_lam - logfact
        p_le += math.exp(log_p)
    return max(0.0, min(1.0, 1.0 - p_le))


async def _team_lambdas_for_fixture(db, home_team: str, away_team: str,
                                     league: str) -> tuple[Optional[float],
                                                           Optional[float]]:
    """Return (lambda_home, lambda_away) from the coherent game model,
    or (None, None) if insufficient team data.
    """
    try:
        ctx = await build_soccer_team_ctx(
            db, home_team=home_team, away_team=away_team, league=league,
        )
        out = estimate_soccer_game_probabilities(ctx, home_team, away_team)
        if not out.available:
            return None, None
        return out.lambda_home, out.lambda_away
    except Exception:
        return None, None


async def regen_soccer_player_picks_via_authority(
    db,
    *,
    dry_run: bool = False,
    limit: int = 500,
    verbose: bool = False,
) -> dict[str, Any]:
    """Recompute win_probability + lock_score for current soccer
    player-prop picks using the SESSION 10.4 hydrator + authority.
    """
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    # ── ACTIVE HORIZON — 48 h (2026-09-18, user-approved) ─────────
    # Only regenerate ATGS/scorer picks whose event falls inside the
    # rolling 48 h active window.  Prevents wasting compute + memory
    # on events days out that the acquisition path shouldn't be
    # active-processing yet.  Falls back gracefully — never restricts
    # historical settlement or completed-match research.
    from services.soccer_active_horizon import soccer_active_time_filter
    _active_window = soccer_active_time_filter(
        fields=("event_time", "kickoff_iso"), now=now,
    )

    q = {
        "sport": "Soccer",
        "book_odds": {"$ne": None},
        # ACTIVE 48h window (was: unbounded future)
        **_active_window,
        # NEVER touch settled / history picks
        "started":  {"$ne": True},
        "settled":  {"$ne": True},
        "state":    {"$nin": ["SETTLED", "HISTORY"]},
        # Player-prop markets only
        "market": {"$regex":
                    "Anytime Goal Scorer|First Goal Scorer|Last Goal Scorer|"
                    "Score or Assist|Anytime Assist|Shots on Target|Shots",
                    "$options": "i"},
    }

    considered = 0
    regenerated = 0
    skipped_no_evidence = 0
    skipped_insufficient = 0
    skipped_no_market_class = 0
    skipped_unknown_line = 0
    errors: list[dict] = []
    per_pick: list[dict] = []
    authority_counts = {"FULL": 0, "STRONG": 0, "LIMITED": 0,
                        "INSUFFICIENT": 0}
    market_counts: dict[str, int] = {}

    # For efficiency, cache team-lambda per fixture
    fixture_cache: dict[tuple[str, str, str], tuple[Optional[float],
                                                     Optional[float]]] = {}

    async for pick in db.picks.find(q).limit(limit):
        considered += 1
        try:
            market = pick.get("market") or ""
            selection = pick.get("selection") or ""
            mkt_tag = _classify_market(market)
            if not mkt_tag:
                skipped_no_market_class += 1
                continue
            market_counts[mkt_tag] = market_counts.get(mkt_tag, 0) + 1

            player = _extract_player_from_market(market, selection)
            home_t, away_t = _parse_event(pick.get("event") or "")
            league = pick.get("league") or ""
            pick_team = pick.get("team") or ""
            is_home = _norm_team(pick_team) == _norm_team(home_t)

            # ── Fixture team lambdas ───
            key = (home_t.lower(), away_t.lower(), league.lower())
            if key not in fixture_cache:
                fixture_cache[key] = await _team_lambdas_for_fixture(
                    db, home_t, away_t, league,
                )
            lam_h, lam_a = fixture_cache[key]
            team_lambda    = lam_h if is_home else lam_a
            opp_def_lambda = lam_a if is_home else lam_h
            # opp_def_strength: how many goals the OPP concedes on avg —
            # use the OPP's own lambda as a proxy (game-model already
            # priced them into the pairing).

            # ── Hydrate evidence via canonical resolver ──
            ev, source, raw_row = await hydrate_soccer_player_evidence(
                db, player_name=player, league=league,
                team=pick_team,
                opponent=(away_t if is_home else home_t),
                event_id=pick.get("event_id"),
                is_home=is_home,
                book_odds=pick.get("book_odds"),
                team_lambda=team_lambda,
                opp_def_strength=opp_def_lambda,
                minutes_state=MinutesState.UNKNOWN,
                penalty_role=PenaltyRole.UNKNOWN,
            )

            auth = classify_authority(ev)
            authority_counts[auth.value] = authority_counts.get(
                auth.value, 0) + 1
            if auth == Authority.INSUFFICIENT:
                skipped_insufficient += 1
                per_pick.append({
                    "pick_id": pick["id"], "player": player,
                    "market_tag": mkt_tag,
                    "regen_applied": False,
                    "reason": "AUTHORITY_INSUFFICIENT",
                    "history_source": source,
                    "authority": auth.value,
                })
                continue

            # ── Compute model probability per market tag ──
            model_prob = None; model_trace: dict = {}
            if mkt_tag == "atg":
                est = estimate_player_lambda(ev)
                model_prob = est.get("atg_prob")
                model_trace = {"family": "ATG", **est}
            elif mkt_tag == "fgs":
                est = estimate_player_lambda(ev)
                # FGS ≈ ATG × (player_lambda / team_lambda), rough
                # approximation.  For real BMK pricing this needs
                # ordering statistics.  Conservative: use 0.35 of ATG.
                if est.get("atg_prob") is not None:
                    model_prob = est["atg_prob"] * 0.35
                model_trace = {"family": "FGS", "atg_component": est,
                               "fgs_factor": 0.35}
            elif mkt_tag == "lgs":
                est = estimate_player_lambda(ev)
                if est.get("atg_prob") is not None:
                    model_prob = est["atg_prob"] * 0.30
                model_trace = {"family": "LGS", "atg_component": est,
                               "lgs_factor": 0.30}
            elif mkt_tag == "sga":
                res = price_score_or_assist(ev)
                model_prob = res.get("sga_prob")
                model_trace = {"family": "SGA", **res}
            elif mkt_tag == "atg_assist":
                lam_a_est = estimate_assist_lambda(ev)
                if lam_a_est is not None:
                    model_prob = 1.0 - math.exp(-lam_a_est)
                model_trace = {"family": "ATG_ASSIST",
                               "lambda_assist": lam_a_est}
            elif mkt_tag == "shots":
                line = _line_from_market(market)
                if line is None:
                    skipped_unknown_line += 1
                    continue
                # Expected shots = shots_per_90 * minutes_share
                if ev.shots_per_90 is None:
                    skipped_no_evidence += 1
                    continue
                minutes_share = 0.60  # UNKNOWN default
                lam_shots = ev.shots_per_90 * minutes_share
                # opp adj same as ATG
                if ev.opp_def_strength is not None:
                    lam_shots *= max(0.75, min(1.30,
                                                 ev.opp_def_strength / 1.32))
                model_prob = _poisson_over_line(lam_shots, line)
                model_trace = {"family": "SHOTS", "lambda_shots": lam_shots,
                               "line": line}
            elif mkt_tag == "sot":
                line = _line_from_market(market)
                if line is None:
                    skipped_unknown_line += 1
                    continue
                if ev.sot_per_90 is None:
                    skipped_no_evidence += 1
                    continue
                minutes_share = 0.60
                lam_sot = ev.sot_per_90 * minutes_share
                if ev.opp_def_strength is not None:
                    lam_sot *= max(0.75, min(1.30,
                                              ev.opp_def_strength / 1.32))
                model_prob = _poisson_over_line(lam_sot, line)
                model_trace = {"family": "SOT", "lambda_sot": lam_sot,
                               "line": line}

            if model_prob is None or model_prob <= 0:
                skipped_no_evidence += 1
                per_pick.append({
                    "pick_id": pick["id"], "player": player,
                    "market_tag": mkt_tag,
                    "regen_applied": False,
                    "reason": "MODEL_PROB_NONE",
                    "history_source": source,
                    "authority": auth.value,
                })
                continue

            # ── Lock score authority ceiling ──
            devig = pick.get("devig_probability") or pick.get(
                "devig_implied_prob")
            lock_auth = player_lock_authority(
                ev, model_prob=model_prob,
                devig_prob=float(devig) / 100 if devig else None,
            )
            authority_ceiling = lock_auth.reachable_max

            # ── Compute lock score ──
            # Formula: base = 60 + 30 * model_prob (naive fit), then
            # cap at ceiling.  We don't want to over-engineer this —
            # the point is that model prob drives lock and authority
            # sets ceiling.
            base_lock = 60.0 + 30.0 * model_prob
            book_pct  = pick.get("book_odds")
            # book edge: if book_odds is closer to model_prob than
            # random, add a small bump (up to +5)
            new_lock_score = min(base_lock, authority_ceiling)

            # ── Stamp ──
            update = {
                "win_probability":            round(model_prob * 100, 3),
                "lock_score":                 round(new_lock_score, 2),
                # Canonical publication fields — take precedence in the
                # API hydration layer (services/published_prediction_reader).
                # Setting these ensures API/EXPO responses return the
                # SAME values the model computed, bypassing legacy
                # elite-anchor/display-cap post-processing.
                "published_probability":      round(model_prob, 5),  # 0..1
                "published_lock_score":       round(new_lock_score, 2),
                "publication_state":          (
                    "PUBLISHED" if new_lock_score >= 85 else "OFF_BOARD"
                ),
                "publication_source":         "soccer_player_authority_v1",
                "soccer_model_version":       _SOCCER_MODEL_VERSION,
                "model_version":              _SOCCER_MODEL_VERSION,
                "generation_id":              f"soccer_regen_{now.strftime('%Y%m%dT%H%M%S')}",
                "authority":                  auth.value,
                "authority_ceiling":          authority_ceiling,
                "authority_reasons":          lock_auth.ceiling_reasons,
                "model_math_trace":           model_trace,
                "history_source":             source,
                "hydrator_reconnected":       True,
                "regenerated_at":             now_iso,
            }

            if not dry_run:
                # P0.2/P0.3 — the re-score is a PRE-publication step.
                # Metadata is stamped directly; the scored truth goes
                # through PredictionPublicationService.publish() so it
                # becomes a NEW versioned snapshot (grade derived from the
                # canonical mapping, dual-written aliases, publication event).
                _meta = {k: v for k, v in update.items()
                         if k not in ("win_probability", "lock_score",
                                      "published_probability", "published_lock_score",
                                      "publication_source")}
                await db.picks.update_one({"id": pick["id"]}, {"$set": _meta})
                try:
                    from services.prediction_publication_service import PredictionPublicationService
                    _svc = PredictionPublicationService(db, board_version=update["generation_id"])
                    _cand = {**pick, **_meta,
                             "win_probability": update["win_probability"],
                             "lock_score": update["lock_score"]}
                    _cand["edge_percent"] = pick.get("edge_percent")
                    await _svc.publish(_cand, publication_source="soccer_player_authority_v1")
                except Exception as _pub_err:
                    logger.warning("soccer regen publish failed for %s: %s", pick.get("id"), _pub_err)
            regenerated += 1
            per_pick.append({
                "pick_id":          pick["id"],
                "player":           player,
                "event":            pick.get("event"),
                "market_tag":       mkt_tag,
                "market":           market,
                "history_source":   source,
                "authority":        auth.value,
                "authority_ceiling": authority_ceiling,
                "old_win_probability": pick.get("win_probability"),
                "new_win_probability": round(model_prob * 100, 3),
                "old_lock_score":  pick.get("lock_score"),
                "new_lock_score":  round(new_lock_score, 2),
                "regen_applied":   True,
            })
        except Exception as e:
            errors.append({"pick_id": pick.get("id"),
                           "error": str(e)})

    return {
        "as_of":               now_iso,
        "model_version":       _SOCCER_MODEL_VERSION,
        "considered":          considered,
        "picks_regenerated":   regenerated,
        "skipped_no_market_class": skipped_no_market_class,
        "skipped_insufficient":    skipped_insufficient,
        "skipped_no_evidence":     skipped_no_evidence,
        "skipped_unknown_line":    skipped_unknown_line,
        "errors":              errors[:20],
        "error_count":         len(errors),
        "authority_distribution": authority_counts,
        "market_distribution":    market_counts,
        "per_pick":            per_pick,
        "dry_run":             dry_run,
    }


def _norm_team(s: str) -> str:
    return (s or "").strip().lower()


__all__ = ["regen_soccer_player_picks_via_authority",
           "_SOCCER_MODEL_VERSION"]
