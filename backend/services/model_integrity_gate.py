"""Universal Model-Integrity Gate — Pass 5 (2026-06).

Pre-publication contract every model-backed pick MUST satisfy.

Rules (fail closed at CANDIDATE level — never starves other sports):

    1. Canonical event identity           (event / event_id / event_time)
    2. Canonical participant identity     (player_name OR team_name
                                             matching one of home/away)
    3. Real current sportsbook line       (book_odds present)
    4. Canonical market_key               (market OR market_key non-empty)
    5. Exact selected side                (side / selection present)
    6. Supported model authority          (win_probability from a
                                             sport-specific model, NOT
                                             book-implied)
    7. Real model probability             (in (0, 1) — not None / not 0)
    8. Probability provenance             (CAUSAL / EMPIRICAL only —
                                             MODEL_CONDITIONED /
                                             PRIOR_ONLY / INVALID rejected)
    9. Current universal Lock authority   (stamped by
                                             ``universal_lock_authority``)
   10. Sufficient required evidence       (≥1 real factor OR at least a
                                             specialized-engine marker)

Forbidden signals (any presence trips REJECT):

    * wrong-stat leakage
    * opposite-side positive evidence
    * book implied masquerading as independent probability
    * generic factor mean posing as final model probability
    * hash / synthetic / placeholder predictive evidence
    * conditioned simulator presented as independent

Contract:

    result = evaluate(pick)
    → { "allowed": True }          — pick may publish
    → { "allowed": False,
        "reason": "MODEL_UNAVAILABLE" | "DATA_INSUFFICIENT",
        "detail": "..." }

Caller responsibilities:
    * When ``allowed=False``, tag the CANDIDATE with the reason and
      DROP THE CANDIDATE ONLY.  Never crash safe_picks; never abort
      the orchestrator; never starve any other market or sport.
"""
from __future__ import annotations

from typing import Any, Optional

GATE_NAME = "model_integrity_gate"
GATE_VERSION = "1.0.0"

REJECT_MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
REJECT_DATA_INSUFFICIENT = "DATA_INSUFFICIENT"

_INDEPENDENT_PROVENANCES = {"CAUSAL_INDEPENDENT", "EMPIRICAL_INDEPENDENT"}
_CONDITIONED_PROVENANCES = {"MODEL_CONDITIONED", "PRIOR_ONLY", "INVALID"}

# Specialized engine markers — a candidate may lack a "factors" block
# entirely when it was produced by one of these authoritative engines.
_SPECIALIZED_ENGINE_MARKERS = (
    "k_math_expected_k",                # MLB K probability engine
    "_atd_evidence_block",              # NFL ATD engine
    "atd_model_override",               # NFL ATD engine (legacy key)
    "nfl_yardage_engine_output",        # NFL yardage engine
    "soccer_scorer_probability",        # Soccer scorer engine
    "platinum_challenger",              # Platinum NFL challenger
    "awaiting_sim_distribution",        # NBA/NHL — sim_runner will
                                          # promote sim_win_probability
                                          # AFTER this gate; treat the
                                          # pick as having a specialized
                                          # engine authority in-flight.
)


def _has_specialized_engine(pick: dict) -> bool:
    return any(pick.get(m) is not None for m in _SPECIALIZED_ENGINE_MARKERS)


def _fair_probability(pick: dict) -> Optional[float]:
    """Return the fair (no-vig) book probability in [0,1]."""
    for k in ("no_vig_implied_pct", "no_vig_book_probability",
              "devig_market_probability", "implied_probability"):
        v = pick.get(k)
        if isinstance(v, (int, float)):
            fv = float(v)
            if 1.0 < fv <= 100.0:
                return fv / 100.0
            if 0.0 <= fv <= 1.0:
                return fv
    return None


def evaluate(pick: dict) -> dict:
    """Return the gate decision for one candidate pick."""
    p = pick or {}

    # 1. Canonical event identity.
    if not (p.get("event") or p.get("event_id") or p.get("event_time")
             or p.get("commence_time")):
        return _reject(REJECT_DATA_INSUFFICIENT,
                        "missing_canonical_event_identity")

    # 2. Canonical participant identity.
    home = str(p.get("home_team") or "").strip().lower()
    away = str(p.get("away_team") or "").strip().lower()
    player = str(p.get("player_name") or p.get("player") or "").strip().lower()
    sel = str(p.get("side") or p.get("selection") or p.get("pick") or "").strip().lower()
    event_str = str(p.get("event") or "").strip().lower()
    is_player_market = bool(player)
    if not is_player_market:
        # Team-market: selected side must resolve to home OR away.
        # 2026-06 MLB game-market fix — accept ``event`` string
        # (e.g. "San Diego Padres @ Pittsburgh Pirates") as a valid
        # canonical team identity when home_team / away_team are
        # attached later in the pipeline (canonical publish enricher).
        # Also accept a non-empty ``selection`` team name.  This
        # unblocks MLB ML / Run Line / Total candidates whose team
        # fields are stamped AFTER compute_lock_score.
        has_event_identity = bool(
            event_str and (" vs " in event_str or " @ " in event_str or " v " in event_str)
        )
        has_selection = bool(sel)
        if not (home and away) and not (has_event_identity and has_selection):
            return _reject(REJECT_DATA_INSUFFICIENT,
                            "missing_canonical_team_identity")

    # 3. Real current sportsbook line.
    book_odds = p.get("book_odds")
    if not isinstance(book_odds, (int, float)) or int(book_odds) == 0:
        # A pick may honestly publish with book_odds=None + MODEL_ONLY
        # source (existing contract for models emitted without a
        # real line).  In that case require the explicit MODEL_ONLY
        # marker so we do not accept an accidental missing line.
        if str(p.get("odds_source") or "").upper() != "MODEL_ONLY":
            return _reject(REJECT_DATA_INSUFFICIENT,
                            "missing_real_sportsbook_line")

    # 4. Canonical market_key.
    if not (p.get("market") or p.get("market_key")):
        return _reject(REJECT_DATA_INSUFFICIENT,
                        "missing_canonical_market_key")

    # 5. Exact selected side.
    if not sel:
        return _reject(REJECT_DATA_INSUFFICIENT,
                        "missing_selected_side")

    # 6+7. Supported model authority + real model probability.
    win_prob_pct = p.get("sim_win_probability") \
                    or p.get("model_win_probability") \
                    or p.get("win_probability")
    win_prob_frac = p.get("model_win_prob")
    if isinstance(win_prob_pct, (int, float)) and 0 < float(win_prob_pct) <= 100:
        wp = float(win_prob_pct) / 100.0
    elif isinstance(win_prob_frac, (int, float)) and 0 < float(win_prob_frac) < 1:
        wp = float(win_prob_frac)
    else:
        return _reject(REJECT_MODEL_UNAVAILABLE,
                        "missing_real_model_probability")

    # Forbidden — book-implied masquerading as independent probability.
    fair = _fair_probability(p)
    prov = (p.get("simulator_provenance")
             or p.get("probability_provenance")
             or p.get("provenance"))
    if isinstance(fair, (int, float)):
        # If model probability EQUALS book implied within 0.001, and
        # provenance is NOT CAUSAL/EMPIRICAL independent, we reject.
        if abs(wp - fair) < 0.001 and prov not in _INDEPENDENT_PROVENANCES:
            # Specialized-engine outputs may coincidentally equal the
            # market when the engine agrees, so exempt those.
            if not _has_specialized_engine(p):
                return _reject(REJECT_MODEL_UNAVAILABLE,
                                "book_implied_masquerading_as_model")

    # 8. Probability provenance.  When present, must be independent.
    if prov is not None and prov in _CONDITIONED_PROVENANCES:
        return _reject(REJECT_MODEL_UNAVAILABLE,
                        f"conditioned_provenance:{prov}")

    # 9. Universal Lock authority stamp (Pass 4).  Missing stamp is
    # allowed pre-migration (many legacy callers have not adopted the
    # authority yet), but a stamped ``universal_lock=None`` from the
    # authority means the pick already failed the authority contract
    # and must not publish.
    ul = p.get("universal_lock")
    if "universal_lock" in p and ul is None:
        return _reject(REJECT_MODEL_UNAVAILABLE,
                        "universal_lock_authority_rejected")

    # 10. Sufficient evidence.
    factors = p.get("factors")
    real_factors = 0
    if isinstance(factors, dict):
        for v in factors.values():
            if isinstance(v, (int, float)):
                real_factors += 1
    if real_factors < 1 and not _has_specialized_engine(p):
        return _reject(REJECT_DATA_INSUFFICIENT,
                        "no_real_factors_and_no_specialized_engine")

    # Forbidden — factor mean posing as probability.  When
    # ``probability_source`` is explicitly ``factor_mean`` (legacy
    # placeholder), reject.
    if str(p.get("probability_source") or "").lower() in (
        "factor_mean", "factor_average", "generic_factor_mean",
    ):
        return _reject(REJECT_MODEL_UNAVAILABLE,
                        "generic_factor_mean_probability")

    # Forbidden — hash/synthetic/placeholder predictive evidence.
    for k in ("hash_evidence", "synthetic_evidence",
              "placeholder_evidence"):
        if p.get(k):
            return _reject(REJECT_MODEL_UNAVAILABLE,
                            f"forbidden_evidence:{k}")

    return {
        "allowed":       True,
        "gate":          GATE_NAME,
        "gate_version":  GATE_VERSION,
        "win_expected":  round(wp, 6),
        "fair_market":   fair,
        "provenance":    prov,
    }


def _reject(reason: str, detail: str) -> dict:
    return {
        "allowed":       False,
        "gate":          GATE_NAME,
        "gate_version":  GATE_VERSION,
        "reason":        reason,
        "detail":        detail,
    }


__all__ = [
    "GATE_NAME", "GATE_VERSION",
    "REJECT_MODEL_UNAVAILABLE", "REJECT_DATA_INSUFFICIENT",
    "evaluate",
]
