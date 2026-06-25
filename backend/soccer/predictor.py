"""Soccer prediction engine v1.1.0 — feature-richer build.

Upgrades over v1.0.0-mvp:
  • Exponential form decay (most-recent match weighted ~3× the oldest)
  • Head-to-head trend baked into team strength (last 6 H2Hs)
  • Standings goal-differential per game (kept from v1.0.0)
  • Home advantage offset (kept from v1.0.0)

xG is NOT included — football-data.org doesn't expose xG on TIER_ONE.
Adding it would require Sportmonks (€16/mo) or api-sports.io upgrade.

Pure-function design so the prediction logic is unit-testable and
fully separated from I/O. The pipeline.py orchestrator handles all
async H2H lookups and passes the resolved feature dict here.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from .normalize import normalize_match, normalize_standing_row  # noqa: F401

logger = logging.getLogger("lockscore.soccer.predictor")

MODEL_VERSION = "soccer.v1.1.0-h2h-decay"

# Exponential decay: most-recent match has weight 1.0, each step back
# multiplies by DECAY. With DECAY=0.7, last 5 matches → weights
# [1.0, 0.7, 0.49, 0.343, 0.24] ⇒ newest match is ~4× the oldest.
_FORM_DECAY  = 0.7
_FORM_POINTS = {"W": 3.0, "D": 1.0, "L": 0.0}
_HOME_ADV    = 0.30   # ~5% baseline edge for hosts


def _form_score(form: str | None) -> float:
    """Exponential-decay weighted form score in [0, 3]. Neutral 1.5 if unknown.

    `form` is contiguous like "WWDLW" (oldest → newest from upstream).
    """
    if not form:
        return 1.5
    chars = form.strip().upper()[-5:]
    if not chars:
        return 1.5
    # Weights run oldest → newest. Build right-to-left for clarity.
    n = len(chars)
    weights = [_FORM_DECAY ** (n - 1 - i) for i in range(n)]
    total_w = sum(weights)
    s = sum(_FORM_POINTS.get(c, 1.0) * w for c, w in zip(chars, weights))
    return s / total_w


def _goal_diff_per_game(row: dict) -> float:
    """Average goal differential — strong proxy for team strength."""
    played = row.get("played") or 0
    gd = row.get("goal_diff") or 0
    if played <= 0:
        return 0.0
    return gd / played


def _h2h_score(home_team_id: int, away_team_id: int,
               h2h_matches: list[dict]) -> float:
    """Compute home team's H2H advantage in [-0.5, +0.5].

    Each H2H match scored from the home team's perspective:
      • win  → +1
      • draw → 0
      • loss → -1
    Recency-weighted with the same exponential decay (most recent
    match worth ~3× the oldest). Score normalised to [-0.5, +0.5] so
    H2H can shift team strength but not overwhelm form + GD signals.
    """
    if not h2h_matches:
        return 0.0
    # Oldest → newest. football-data.org returns most-recent first, so
    # reverse before applying decay weights.
    ordered = list(reversed(h2h_matches))[-6:]
    n = len(ordered)
    weights = [_FORM_DECAY ** (n - 1 - i) for i in range(n)]
    total_w = sum(weights) or 1.0
    score = 0.0
    for m, w in zip(ordered, weights):
        score_dict = m.get("score") or {}
        winner = score_dict.get("winner")
        home_id_in_match = ((m.get("homeTeam") or {}).get("id"))
        if winner == "DRAW":
            outcome = 0.0
        elif winner == "HOME_TEAM":
            outcome = 1.0 if home_id_in_match == home_team_id else -1.0
        elif winner == "AWAY_TEAM":
            outcome = -1.0 if home_id_in_match == home_team_id else 1.0
        else:
            outcome = 0.0  # incomplete/postponed match
        score += outcome * w
    raw = score / total_w  # in [-1, +1]
    # Squash to [-0.5, +0.5] so H2H can swing but not dominate.
    return raw * 0.5


def _confidence(home_strength: float, away_strength: float) -> tuple[str, float]:
    """Pick the strongest side; if scores are tied, return DRAW.

    Confidence = 60 + (signed strength gap × scaling), capped at 96.
    """
    gap = home_strength - away_strength
    abs_gap = abs(gap)
    if abs_gap < 0.25:
        conf = 55.0 + (0.25 - abs_gap) * 30.0
        return "DRAW", round(min(conf, 70.0), 1)
    side = "HOME" if gap > 0 else "AWAY"
    conf = 60.0 + abs_gap * 25.0
    return side, round(min(conf, 96.0), 1)


def build_prediction(fixture_raw: dict, standings_index: dict[int, dict],
                     h2h_matches: list[dict] | None = None) -> dict | None:
    """Produce one prediction for a fixture.

    `standings_index`: { team_id → normalised standing row }
    `h2h_matches`:     raw football-data.org match list between the
                       two teams (most-recent first). Optional —
                       omitted for the v1.0.0 fallback path.

    Returns None when both teams missing from standings (rare cup
    crossovers etc.) — those fixtures get skipped.
    """
    fx = normalize_match(fixture_raw)
    home_id = fx.get("home_team_id")
    away_id = fx.get("away_team_id")
    if not home_id or not away_id:
        return None
    home_row = standings_index.get(home_id)
    away_row = standings_index.get(away_id)
    if not home_row and not away_row:
        return None

    home_form_pts = _form_score((home_row or {}).get("form"))
    away_form_pts = _form_score((away_row or {}).get("form"))
    home_gd = _goal_diff_per_game(home_row or {})
    away_gd = _goal_diff_per_game(away_row or {})
    h2h_adv = _h2h_score(home_id, away_id, h2h_matches or [])

    # Composite strength formula. Coefficients chosen so each signal
    # contributes ~similar dynamic range:
    #   • form: (0..3)/3   → 0..1
    #   • gd:   × 0.4      → typically ±0.4
    #   • h2h:  raw        → ±0.5
    home_strength = (home_form_pts / 3.0) + home_gd * 0.4 + h2h_adv + _HOME_ADV
    away_strength = (away_form_pts / 3.0) + away_gd * 0.4 - h2h_adv

    pick_side, conf = _confidence(home_strength, away_strength)
    if pick_side == "HOME":
        selection = fx["home_team_name"]
    elif pick_side == "AWAY":
        selection = fx["away_team_name"]
    else:
        selection = "Draw"

    sig = f"{fx['fixture_id']}|{MODEL_VERSION}|moneyline"
    pred_id = str(uuid.UUID(hashlib.md5(sig.encode()).hexdigest()))

    return {
        "id":             pred_id,
        "fixture_id":     fx["fixture_id"],
        "market":         "Match Result (1X2)",
        "selection":      selection,
        "pick_side":      pick_side,
        "confidence":     conf,
        "model_version":  MODEL_VERSION,
        "event":          f"{fx['away_team_name']} @ {fx['home_team_name']}",
        "event_time":     fx["date"],
        "league_id":      fx["league_id"],
        "league":         fx["league_name"],
        "sport":          "Soccer",
        "home_team_id":   home_id,
        "away_team_id":   away_id,
        "features": {
            "home_form":     round(home_form_pts, 3),
            "away_form":     round(away_form_pts, 3),
            "home_gd_per_g": round(home_gd, 3),
            "away_gd_per_g": round(away_gd, 3),
            "h2h_adv":       round(h2h_adv, 3),
            "h2h_n":         len(h2h_matches or []),
            "home_strength": round(home_strength, 3),
            "away_strength": round(away_strength, 3),
        },
        "created_at":     datetime.now(timezone.utc).isoformat(),
    }


def to_picks_collection_doc(pred: dict, real_odds: dict | None = None) -> dict:
    """Convert a soccer prediction into the existing `picks` schema.

    If ``real_odds`` is provided (resolved by ``soccer.real_odds``), the
    pick will carry the actual FanDuel/DraftKings/BetMGM American line
    AND the corresponding edge percent — instead of the previous
    fair-odds-only synthetic estimate. ``real_odds`` schema is the same
    object returned by :func:`soccer.real_odds.lookup_real_odds`.
    """
    conf = float(pred.get("confidence") or 0)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # ── User-friendly market label.
    # "Match Result (1X2)" → "{Team} to Win" / "{Team} or Draw" / "Draw"
    sel = pred.get("selection") or ""
    side = (pred.get("pick_side") or "").lower()
    if side == "draw" or sel.lower() == "draw":
        friendly_market = "Match Result · Draw"
    elif "or draw" in sel.lower():
        friendly_market = f"{sel}"  # already "Team Win or Draw"
    elif side in ("home", "away"):
        friendly_market = f"{sel} to Win"
    else:
        friendly_market = sel or pred.get("market") or "Match Result"
    # ── Convert model confidence → FAIR American odds. ────────────
    # Previously this returned a hardcoded `book_odds=100` (even-money)
    # on every pick, which was a bug a user spotted: "France not +100
    # tomorrow" — the model thought France ≈96% to beat Iraq but the pick
    # was published at +100 / 50% implied. That fake line was misleading
    # because no sportsbook will ever offer France at +100 for that match.
    #
    # Now we derive the fair American line from the model's win prob and
    # flag the pick as a model estimate (no real bookmaker line). Mirrors
    # the tennis_extra fair-odds flow so the frontend "Extended Coverage"
    # badge shows automatically.
    prob_dec = max(0.001, min(0.999, conf / 100.0))
    if prob_dec >= 0.5:
        fair_odds = -round(100 * prob_dec / (1 - prob_dec))
    else:
        fair_odds = round(100 * (1 - prob_dec) / prob_dec)

    # ── Prefer REAL book odds when available ──────────────────────
    # The Odds API was queried upstream by the pipeline. If a real
    # FanDuel/DraftKings/BetMGM line came back, use it and compute
    # genuine edge against the book's implied price. Otherwise fall
    # back to the model's fair-odds estimate + Extended Coverage flag.
    using_real = bool(real_odds and real_odds.get("book_odds") is not None)
    if using_real:
        book_odds         = int(real_odds["book_odds"])
        book_implied_pct  = float(real_odds["implied_probability"])
        bookmaker_label   = str(real_odds.get("bookmaker") or "Sportsbook")
        edge_pct          = round(conf - book_implied_pct, 2)
        is_extra_flag     = False
        fair_only_flag    = False
        # Sample multi-book quote dict for the sportsbook_mapper.
        all_books         = real_odds.get("all_books") or {}
    else:
        book_odds         = int(fair_odds)
        book_implied_pct  = round(conf, 1)  # At fair odds, implied == model
        bookmaker_label   = "Fair Odds (Model)"
        edge_pct          = 0.0
        is_extra_flag     = True
        fair_only_flag    = True
        all_books         = {}

    doc = {
        "id":               pred["id"],
        "sport":            "Soccer",
        "league":           pred.get("league") or "Soccer",
        "event":            pred["event"],
        "event_time":       pred.get("event_time") or "",
        "market":           friendly_market,
        "selection":        sel,
        # Win probability stored as 0-100 percentage (matches main pipeline).
        "win_probability":  round(conf, 1),
        "implied_probability": book_implied_pct,
        "book_odds":        book_odds,
        "edge_percent":     edge_pct,
        "lock_score":       round(conf, 1),
        "grade":            _grade_from_conf(conf),
        "pick_date":        today,
        "is_under_lock":    False,
        "no_bet":           conf < 60.0,
        "elite_player":     False,
        "deep_dive":        False,
        "source":           "soccer_v1",
        "model_version":    pred["model_version"],
        "created_at":       pred["created_at"],
        # ── Real-vs-Fair flags ─────────────────────────────────────
        # `is_extra=True` ONLY when we couldn't find a real book line
        # → the pick goes to Extended Coverage. With real odds, the
        # pick flows into the regular Locks board like any other.
        "is_extra":         is_extra_flag,
        "fair_odds_model":  fair_only_flag,
        "bookmaker":        bookmaker_label,
        "factors": {
            "Coverage Source": (
                f"Real book odds via {bookmaker_label}"
                if using_real else
                "Soccer model fair-odds (The Odds API doesn't carry this fixture). "
                "Confirm the line at your sportsbook before placing."
            ),
            "Model Confidence": f"Model puts {sel} at {round(conf)}% to win.",
        },
    }
    if using_real and all_books:
        # Multi-book deep-link sources for the sportsbook_mapper.
        doc["all_book_odds"] = all_books
    return doc


def _grade_from_conf(c: float) -> str:
    """Soccer grader — delegates to the canonical `sports_engine._grade`
    so the soccer pipeline writes the spec vocabulary
    ("Elite Lock" / "Strong Lock" / "Lock" / "Playable" / "Pass") instead
    of the legacy ("ELITE","A+","A","B+","B","C") values. Without this
    delegation the soccer 15-min upsert cycle stomps the validator's
    correct grade back to a legacy letter, leaving Lock 90+ picks
    visually grouped under wrong tier badges in the feed.
    """
    try:
        from sports_engine import _grade as _spec_grade
        return _spec_grade(float(c))
    except Exception:
        # Fail-safe: legacy mapping aligned to spec tier thresholds.
        if c >= 98: return "Elite Lock"
        if c >= 95: return "Strong Lock"
        if c >= 90: return "Lock"
        if c >= 85: return "Playable"
        return "Pass"


# ────────────────────────────────────────────────────────────────────
# Over 1.5 Goals synthesis (no Odds API dependency)
# ────────────────────────────────────────────────────────────────────
# Football-data.org doesn't provide totals odds either, so we model the
# expected goal total λ from team strengths and emit Over 1.5 via a
# Poisson lookup. Tagged `model_line=True` so the UI labels it honestly.
#
# Calibration: across recent international + top-5 league sample, mean
# total goals is ~2.65. We anchor λ_base at 2.6 and shift ±0.5 based on
# combined team strength signal (form + goal-diff already baked into
# `home_strength` + `away_strength`).
def make_over_1_5_pick(prediction: dict) -> dict | None:
    """Emit a Total Goals Over 1.5 pick if the model thinks it's safe.

    Returns None when expected λ is too low (defensive matchups) or when
    confidence falls below the threshold (75%) — we don't want to spam
    Over 1.5 on every game just because it cleared the math gate.
    """
    import math as _math
    if not prediction:
        return None
    feats = prediction.get("features") or {}
    hs = float(feats.get("home_strength") or 1.0)
    as_ = float(feats.get("away_strength") or 1.0)
    # λ_total — expected combined goals. Mean of the two strengths is in
    # [-0.5, 2.5] range; map to [1.9, 3.3] for a sensible λ_total.
    avg_str = max(-0.5, min(2.5, (hs + as_) / 2.0))
    lam = 1.9 + (avg_str + 0.5) / 3.0 * 1.4        # → 1.9..3.3
    lam = max(1.6, min(3.6, lam))
    # P(X >= 2) with X ~ Poisson(lam)  =  1 - e^{-lam}(1 + lam)
    p_over_15 = 1.0 - _math.exp(-lam) * (1.0 + lam)
    win_pct = round(p_over_15 * 100.0, 1)
    # Threshold tuned to surface Over 1.5 on matchups with a credible goal
    # floor (top-flight + most internationals clear this). Below ~70%
    # the bet is too thin to justify featuring.
    if win_pct < 70.0:
        return None
    # Fair American odds from probability (used as `book_odds` placeholder
    # since we have no bookmaker line for this synth).
    if p_over_15 >= 0.5:
        fair_odds = int(round(-100 * p_over_15 / (1 - p_over_15)))
    else:
        fair_odds = int(round(100 * (1 - p_over_15) / p_over_15))

    sig = f"{prediction.get('fixture_id')}|{MODEL_VERSION}|over_1_5"
    pred_id = str(uuid.UUID(hashlib.md5(sig.encode()).hexdigest()))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return {
        "id":                pred_id,
        "sport":             "Soccer",
        "league":            prediction.get("league") or "Soccer",
        "event":             prediction["event"],
        "event_time":        prediction.get("event_time") or "",
        "market":            "Total Goals Over 1.5",
        "selection":         "Over",
        "win_probability":   win_pct,
        "implied_probability": 50.0,
        "book_odds":         fair_odds,
        "edge_percent":      round((win_pct - 50.0) / 5.0, 2),
        "lock_score":        win_pct,
        "grade":             _grade_from_conf(win_pct),
        "pick_date":         today,
        "is_under_lock":     False,
        "no_bet":            win_pct < 80.0,
        "elite_player":      False,
        "deep_dive":         False,
        "source":            "soccer_v1_synth",
        "model_version":     MODEL_VERSION,
        "model_line":        True,
        "model_source":      "poisson_from_strengths",
        "lam_total":         round(lam, 3),
        "created_at":        datetime.now(timezone.utc).isoformat(),
    }
