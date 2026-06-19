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


def to_picks_collection_doc(pred: dict) -> dict:
    """Convert a soccer prediction into the existing `picks` schema."""
    conf = float(pred.get("confidence") or 0)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return {
        "id":               pred["id"],
        "sport":            "Soccer",
        "league":           pred.get("league") or "Soccer",
        "event":            pred["event"],
        "event_time":       pred.get("event_time") or "",
        "market":           pred["market"],
        "selection":        pred["selection"],
        "win_probability":  round(conf / 100.0, 4),
        "implied_probability": 0.5,
        "book_odds":        100,
        "edge_percent":     round((conf - 50.0) / 5.0, 2),
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
    }


def _grade_from_conf(c: float) -> str:
    if c >= 90: return "ELITE"
    if c >= 85: return "A+"
    if c >= 80: return "A"
    if c >= 75: return "B+"
    if c >= 70: return "B"
    return "C"
