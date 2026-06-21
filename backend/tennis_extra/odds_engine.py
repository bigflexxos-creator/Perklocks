"""Tennis Fair-Odds Engine — generate win probabilities for matches that
have no bookmaker odds.

Combines four signals into a single fair win probability:

  1. Elo rating  — overall strength of each player (standard chess-style Elo).
  2. Surface Elo — separate Elo per surface (grass/clay/hard) since e.g.
     Nadal on clay ≠ Nadal on grass.
  3. Form (L10)  — recent win rate over last 10 matches (small ±50 Elo bump).
  4. Fatigue     — matches in the last 7 days (each match shaves a tiny Elo
                   penalty; 3-set wars cost more).

Storage:
  Collection `tennis_players`:
    {
      "name":          "Borges N.",
      "name_norm":     "borges n",
      "elo_overall":   1620,
      "elo_grass":     1640,
      "elo_clay":      1580,
      "elo_hard":      1610,
      "form":          { "wins": 7, "losses": 3, "last_match_iso": "..." },
      "matches_7d":    [{ "iso": "...", "sets": 2 }, ...],  # only recent
      "last_updated":  ISO 8601 UTC,
    }

Bootstrap: when a match has book odds, we REVERSE-ENGINEER each player's
Elo (delta) from the implied probability. This seeds the player table
instantly without needing months of history.

Update: after a match settles (won/lost), apply standard Elo K-factor (32
for non-elite, 16 for high-rated) to overall + surface Elo.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger("lockscore.tennis_extra.odds_engine")

# ─────────────── Constants ───────────────────────────────────────────
BASE_ELO = 1500.0
ELO_K = 32.0                  # K-factor for sub-elite (most players)
ELO_K_ELITE = 16.0            # K-factor for ≥1800 Elo (more stable)
FORM_BUMP_MAX = 50.0          # max ±Elo from form
FATIGUE_PER_MATCH = 8.0       # Elo penalty per match in last 7d (per set played)


# ─────────────── Surface inference ──────────────────────────────────

_GRASS_KEYWORDS = ("mallorca", "halle", "queen", "eastbourne", "homburg",
                    "nottingham", "wimbledon", "stuttgart", "rosmalen",
                    "hertogenbosch", "newport", "birmingham")
_CLAY_KEYWORDS = ("madrid", "rome", "monte carlo", "barcelona", "hamburg",
                   "munich", "estoril", "geneva", "lyon", "umag", "gstaad",
                   "kitzbuhel", "bastad", "french open", "roland garros",
                   "rio open", "buenos aires", "santiago", "córdoba",
                   "marrakech", "houston", "charleston", "stuttgart wta",
                   "strasbourg", "rabat", "warsaw", "iasi", "palermo",
                   "lausanne", "prague", "bogota", "bucharest", "targu mures",
                   "piracicaba")  # most challengers in summer = clay
_HARD_KEYWORDS = ("indian wells", "miami", "cincinnati", "shanghai",
                   "paris masters", "tokyo", "beijing", "us open",
                   "australian open", "dubai", "doha", "qatar", "washington",
                   "winston-salem", "antwerp", "vienna", "basel", "metz",
                   "marseille", "sofia", "atlanta", "los cabos", "delray",
                   "san diego", "astana", "tel aviv", "auckland",
                   "adelaide", "brisbane", "atp finals")


def infer_surface(tournament: str) -> str:
    """Return 'grass' / 'clay' / 'hard'."""
    t = (tournament or "").lower()
    if any(kw in t for kw in _GRASS_KEYWORDS):
        return "grass"
    if any(kw in t for kw in _CLAY_KEYWORDS):
        return "clay"
    if any(kw in t for kw in _HARD_KEYWORDS):
        return "hard"
    # Default for unknown smaller events — most ITFs are clay/hard mix.
    return "hard"


# ─────────────── Helpers ────────────────────────────────────────────

def _norm_name(name: str) -> str:
    if not name:
        return ""
    n = name.lower().strip()
    n = re.sub(r"\s*\(\d+\)\s*$", "", n)
    n = re.sub(r"[^a-z0-9\s\.]", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def _implied_to_elo_delta(implied_prob: float) -> float:
    """Reverse-engineer Elo delta from book implied probability.

    P(A wins) = 1 / (1 + 10^(-Δ/400))   →   Δ = 400 * log10(P / (1-P))
    """
    p = max(0.001, min(0.999, implied_prob))
    return 400.0 * math.log10(p / (1.0 - p))


def _elo_to_prob(elo_a: float, elo_b: float) -> float:
    """Standard Elo formula: probability A beats B."""
    diff = elo_a - elo_b
    return 1.0 / (1.0 + 10.0 ** (-diff / 400.0))


# ─────────────── DB access ──────────────────────────────────────────

_db = None


def set_db(db) -> None:
    global _db
    _db = db


async def _get_player(name: str) -> dict:
    """Fetch or create a player row. Returns the doc."""
    norm = _norm_name(name)
    if _db is None or not norm:
        return _new_player_doc(name)
    doc = await _db.tennis_players.find_one({"name_norm": norm})
    if doc:
        return doc
    doc = _new_player_doc(name)
    await _db.tennis_players.insert_one(doc)
    return doc


def _new_player_doc(name: str) -> dict:
    return {
        "name": name,
        "name_norm": _norm_name(name),
        "elo_overall": BASE_ELO,
        "elo_grass": BASE_ELO,
        "elo_clay": BASE_ELO,
        "elo_hard": BASE_ELO,
        "form": {"wins": 0, "losses": 0, "last_match_iso": None},
        "matches_7d": [],
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }


# ─────────────── Bootstrap from book odds ───────────────────────────

async def bootstrap_from_market(player_a: str, player_b: str,
                                 implied_a: float, surface: str) -> None:
    """When a match has real book odds, use them to update both players'
    Elo so subsequent matches without odds get sensible predictions."""
    pa = await _get_player(player_a)
    pb = await _get_player(player_b)
    delta = _implied_to_elo_delta(implied_a)
    # The implied-Elo delta represents (A's surface Elo) - (B's surface Elo).
    # We don't know absolute values, so we anchor A at base + delta/2 and
    # B at base - delta/2 IF both are at default. Otherwise we soft-update
    # toward the implied delta with a small learning rate.
    surface_key = f"elo_{surface}"
    cur_delta_surface = pa.get(surface_key, BASE_ELO) - pb.get(surface_key, BASE_ELO)
    cur_delta_overall = pa.get("elo_overall", BASE_ELO) - pb.get("elo_overall", BASE_ELO)
    # Anchor faster when both at base, slower as players accumulate history.
    a_at_base = abs(pa.get(surface_key, BASE_ELO) - BASE_ELO) < 1
    b_at_base = abs(pb.get(surface_key, BASE_ELO) - BASE_ELO) < 1
    lr = 0.5 if (a_at_base and b_at_base) else 0.10
    new_delta_surface = cur_delta_surface + (delta - cur_delta_surface) * lr
    new_delta_overall = cur_delta_overall + (delta - cur_delta_overall) * lr
    # Distribute the delta change symmetrically.
    half_s = (new_delta_surface - cur_delta_surface) / 2
    half_o = (new_delta_overall - cur_delta_overall) / 2
    if _db is None:
        return
    await _db.tennis_players.update_one(
        {"name_norm": pa["name_norm"]},
        {"$inc": {surface_key: half_s, "elo_overall": half_o},
         "$set": {"last_updated": datetime.now(timezone.utc).isoformat()}},
    )
    await _db.tennis_players.update_one(
        {"name_norm": pb["name_norm"]},
        {"$inc": {surface_key: -half_s, "elo_overall": -half_o},
         "$set": {"last_updated": datetime.now(timezone.utc).isoformat()}},
    )


# ─────────────── Fair odds computation ──────────────────────────────

def _form_bump(player: dict) -> float:
    """Convert recent W/L into ±50 Elo bump."""
    form = player.get("form") or {}
    w = int(form.get("wins") or 0)
    l = int(form.get("losses") or 0)
    if w + l == 0:
        return 0.0
    rate = w / (w + l)
    # 50% → 0; 90% → +40; 10% → -40
    return (rate - 0.5) * 2.0 * FORM_BUMP_MAX


def _fatigue_penalty(player: dict, now: datetime) -> float:
    """Sum of fatigue penalties from matches in last 7 days."""
    matches = player.get("matches_7d") or []
    penalty = 0.0
    cutoff = now - timedelta(days=7)
    for m in matches:
        try:
            mtime = datetime.fromisoformat((m.get("iso") or "").replace("Z", "+00:00"))
        except Exception:
            continue
        if mtime < cutoff:
            continue
        sets = int(m.get("sets") or 2)
        penalty += FATIGUE_PER_MATCH * sets
    return penalty


async def fair_win_probability(player_a: str, player_b: str, *,
                                 tournament: str,
                                 now: Optional[datetime] = None) -> dict:
    """Compute fair win probability for `player_a` vs `player_b`.

    Returns:
      {
        "prob_a":          float in [0.001, 0.999],
        "prob_b":          float (= 1 - prob_a),
        "fair_odds_a":     int (American moneyline),
        "fair_odds_b":     int,
        "surface":         "grass" | "clay" | "hard",
        "effective_elo_a": float (Elo after form + fatigue adjustments),
        "effective_elo_b": float,
        "components": {
            "elo_overall":    {"a": ..., "b": ...},
            "elo_surface":    {"a": ..., "b": ...},
            "form_bump":      {"a": ..., "b": ...},
            "fatigue_pen":    {"a": ..., "b": ...},
        }
      }
    """
    now = now or datetime.now(timezone.utc)
    surface = infer_surface(tournament)
    pa = await _get_player(player_a)
    pb = await _get_player(player_b)

    surface_key = f"elo_{surface}"
    # Blend: 60% surface Elo + 40% overall Elo. Surface dominates because
    # tennis is famously surface-specific.
    base_a = 0.6 * pa.get(surface_key, BASE_ELO) + 0.4 * pa.get("elo_overall", BASE_ELO)
    base_b = 0.6 * pb.get(surface_key, BASE_ELO) + 0.4 * pb.get("elo_overall", BASE_ELO)

    form_a = _form_bump(pa)
    form_b = _form_bump(pb)
    fatigue_a = _fatigue_penalty(pa, now)
    fatigue_b = _fatigue_penalty(pb, now)

    eff_a = base_a + form_a - fatigue_a
    eff_b = base_b + form_b - fatigue_b

    prob_a = _elo_to_prob(eff_a, eff_b)
    prob_a = max(0.001, min(0.999, prob_a))

    return {
        "prob_a": round(prob_a, 4),
        "prob_b": round(1 - prob_a, 4),
        "fair_odds_a": _prob_to_american(prob_a),
        "fair_odds_b": _prob_to_american(1 - prob_a),
        "surface": surface,
        "effective_elo_a": round(eff_a, 1),
        "effective_elo_b": round(eff_b, 1),
        "components": {
            "elo_overall": {"a": round(pa.get("elo_overall", BASE_ELO), 1),
                            "b": round(pb.get("elo_overall", BASE_ELO), 1)},
            "elo_surface": {"a": round(pa.get(surface_key, BASE_ELO), 1),
                            "b": round(pb.get(surface_key, BASE_ELO), 1)},
            "form_bump":   {"a": round(form_a, 1), "b": round(form_b, 1)},
            "fatigue_pen": {"a": round(fatigue_a, 1), "b": round(fatigue_b, 1)},
        },
    }


def _prob_to_american(p: float) -> int:
    """Probability → American moneyline."""
    p = max(0.001, min(0.999, p))
    if p >= 0.5:
        return -round(100 * p / (1 - p))
    return round(100 * (1 - p) / p)


# ─────────────── Update on settlement ───────────────────────────────

async def update_after_match(winner: str, loser: str, *,
                              tournament: str,
                              sets_played: int = 2,
                              when: Optional[datetime] = None) -> None:
    """Apply standard Elo K-factor update after a match settles.

    Also bumps form (W/L) and adds to matches_7d for fatigue tracking.
    """
    if _db is None:
        return
    when = when or datetime.now(timezone.utc)
    surface = infer_surface(tournament)
    surface_key = f"elo_{surface}"
    pw = await _get_player(winner)
    pl = await _get_player(loser)
    expected_w = _elo_to_prob(pw.get(surface_key, BASE_ELO),
                                pl.get(surface_key, BASE_ELO))
    # K-factor depends on rating tier.
    k_w = ELO_K_ELITE if pw.get(surface_key, BASE_ELO) >= 1800 else ELO_K
    k_l = ELO_K_ELITE if pl.get(surface_key, BASE_ELO) >= 1800 else ELO_K
    dw = k_w * (1.0 - expected_w)
    dl = k_l * (0.0 - (1.0 - expected_w))
    # Update overall Elo too with smaller K.
    dw_o = (k_w / 2.0) * (1.0 - expected_w)
    dl_o = (k_l / 2.0) * (0.0 - (1.0 - expected_w))
    iso = when.isoformat()
    await _db.tennis_players.update_one(
        {"name_norm": pw["name_norm"]},
        {"$inc": {surface_key: dw, "elo_overall": dw_o, "form.wins": 1},
         "$set": {"form.last_match_iso": iso,
                  "last_updated": iso},
         "$push": {"matches_7d": {"iso": iso, "sets": sets_played}}},
    )
    await _db.tennis_players.update_one(
        {"name_norm": pl["name_norm"]},
        {"$inc": {surface_key: dl, "elo_overall": dl_o, "form.losses": 1},
         "$set": {"form.last_match_iso": iso,
                  "last_updated": iso},
         "$push": {"matches_7d": {"iso": iso, "sets": sets_played}}},
    )
