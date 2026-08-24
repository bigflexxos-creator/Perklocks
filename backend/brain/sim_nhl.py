"""NHL Prop + Game Simulator — real Monte Carlo distribution authority.

Pass 2 (2026-06) — brand-new NHL simulator built from existing/
backfilled team + player history.

Supported markets:

  **Game markets (Poisson-based team-score model):**
      * Moneyline          — P(home_score > away_score)
      * Puck Line ±1.5      — P(cover)
      * Total Over/Under   — P(total goals ≷ line)

  **Player markets (per-game stat distribution):**
      * Goals             — Poisson on player goals/60 × TOI
      * Assists           — Poisson on player assists/60 × TOI
      * Points            — Goals + Assists per game (sum-of-Poissons)
      * Shots on Goal     — Normal on SOG mean/std
      * Saves             — Normal on goalie saves mean/std

Any other NHL player family lacks a game plan → returns
``{ran: False, reason: "UNSUPPORTED_MARKET"}``.  Any market where the
required real inputs are missing → ``{ran: False, reason:
"DATA_INSUFFICIENT"}``.  Never fabricates a book-implied fallback.

Input contract:
    ``pick["nhl_sim_context"]`` (populated upstream by the NHL
    feature engine / precompute) is the primary source.  Legacy
    ``pick["player_intel"]`` and ``pick["team_context"]`` are also
    honoured so the sim degrades cleanly.

    Recognized keys::

        # Game markets
        home_team          str
        away_team          str
        home_lambda        float   — expected home goals (Poisson mean)
        away_lambda        float   — expected away goals
        total_line         float   — the book total (only informational)

        # Player markets
        stat_key           str     — "goals"/"assists"/"points"/
                                       "shots_on_goal"/"saves"
        recent_mean        float   — L5/L10 blended per-game mean
        season_mean        float   — season-to-date per-game mean
        recent_n           int
        season_n           int
        recent_std         float   — sample stddev (SOG/Saves only)
        is_home            bool
        opp_defense_mult   float   — opponent GAA / SV% adjustment
        role_volume_mult   float   — line role modifier
        goalie             bool    — True for saves (auto for saves market)
"""
from __future__ import annotations

import math
import random
from typing import Optional

from brain.sim_distribution import compute_percentiles

RUNS = 20_000

# ── Stat family classification ────────────────────────────────────
_POISSON_STATS = {"goals", "assists", "points"}
_NORMAL_STATS  = {"shots_on_goal", "saves", "shots"}

# Coefficient of variation for the Normal-family stats.  Empirical
# NHL API 2019-2024 season variance.
_CV = {
    "shots_on_goal": 0.55,
    "shots":         0.55,
    "saves":         0.30,
}

# Market string → stat_key.
_MARKET_STAT_KEY: dict[str, str] = {
    "player_goals":              "goals",
    "player_goal_scorer":        "goals",
    "player_anytime_goal":       "goals",
    "goals":                     "goals",
    "player_assists":            "assists",
    "assists":                   "assists",
    "player_points":             "points",
    "points":                    "points",
    "player_shots_on_goal":      "shots_on_goal",
    "shots_on_goal":             "shots_on_goal",
    "player_shots":              "shots_on_goal",
    "shots":                     "shots_on_goal",
    "player_total_saves":        "saves",
    "player_saves":              "saves",
    "saves":                     "saves",
}

_GAME_MARKET_TOKENS_MONEY = ("moneyline", "money line")
_GAME_MARKET_TOKENS_PUCK  = ("puck line", "puck_line", "pucklinemarket", "puckline")
_GAME_MARKET_TOKENS_TOTAL = ("total ", "totals", "over/under", "o/u")


# ── Shared helpers ────────────────────────────────────────────────
def _wilson_ci(p: float, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt((p * (1 - p) / n) + (z * z / (4 * n * n))) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def _sample_poisson(lam: float) -> int:
    if lam <= 0:
        return 0
    L = math.exp(-lam)
    k = 0
    p = 1.0
    while True:
        k += 1
        p *= random.random()
        if p < L:
            break
    return k - 1


def _classify_market(pick: dict) -> tuple[str, Optional[str]]:
    """Return ``(kind, stat_key_or_side)``.

    ``kind`` ∈ {"moneyline", "puck_line", "total", "player", "unsupported"}
    For "player", the second element is the stat_key.
    For "puck_line" / "total", the second element is the side ("over"/"under"
    for total; "home"/"away" for puck line).

    Game-market tokens (moneyline / puck line / total) take PRECEDENCE
    over player-market tokens so a display string like "Total Goals"
    is not mis-routed to the goals player-market simulator.
    """
    raw = str(pick.get("market") or "").strip().lower()
    if not raw:
        return "unsupported", None

    # ── Game markets FIRST (broader tokens, higher precedence) ──
    if any(tok in raw for tok in _GAME_MARKET_TOKENS_PUCK):
        return "puck_line", None
    if any(tok in raw for tok in _GAME_MARKET_TOKENS_MONEY):
        return "moneyline", None
    if raw.startswith("total ") or " total " in raw or raw.endswith(" total") \
       or any(tok in raw for tok in ("totals", "over/under", "o/u")):
        return "total", None

    # ── Player markets (exact key first, then loose match) ──
    mk = str(pick.get("market_key") or "").lower()
    for token, stat in _MARKET_STAT_KEY.items():
        if token == mk or token == raw:
            return "player", stat
    for token, stat in _MARKET_STAT_KEY.items():
        if token in mk or token in raw:
            return "player", stat
    return "unsupported", None


def _extract_line(pick: dict) -> Optional[float]:
    for k in ("line", "point", "threshold"):
        v = pick.get(k)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    import re
    m = re.search(r"(-?\d+(?:\.\d+)?)", str(pick.get("market") or ""))
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def _parse_side(pick: dict) -> Optional[str]:
    for k in ("side", "selection", "pick", "market"):
        v = str(pick.get(k) or "").lower()
        if "over" in v:
            return "over"
        if "under" in v:
            return "under"
    return None


def _team_side(pick: dict) -> Optional[str]:
    """For moneyline / puck-line picks, return "home" or "away"."""
    home = str(pick.get("home_team") or "").strip().lower()
    away = str(pick.get("away_team") or "").strip().lower()
    for k in ("side", "selection", "pick"):
        v = str(pick.get(k) or "").strip().lower()
        if not v:
            continue
        if home and home in v:
            return "home"
        if away and away in v:
            return "away"
    return None


# ── Game-market simulator ─────────────────────────────────────────
def _simulate_game(pick: dict, kind: str) -> dict:
    ctx = dict(pick.get("nhl_sim_context") or {})
    tc  = pick.get("team_context") or {}
    home_lam = ctx.get("home_lambda") or tc.get("home_lambda")
    away_lam = ctx.get("away_lambda") or tc.get("away_lambda")
    if not (isinstance(home_lam, (int, float))
             and isinstance(away_lam, (int, float))
             and home_lam > 0 and away_lam > 0):
        return {"ran": False, "reason": "DATA_INSUFFICIENT",
                "market": pick.get("market"),
                "game_market_kind": kind}

    n = RUNS
    home_scores: list[int] = []
    away_scores: list[int] = []
    for _ in range(n):
        h = _sample_poisson(float(home_lam))
        a = _sample_poisson(float(away_lam))
        # Regulation ties are broken 50/50 in the sim to approximate
        # OT/SO — the exact NHL rules for tie-breaks are not
        # material to the aggregate P(win).
        if h == a:
            if random.random() < 0.5:
                h += 1
            else:
                a += 1
        home_scores.append(h)
        away_scores.append(a)
    totals = [h + a for h, a in zip(home_scores, away_scores)]

    if kind == "moneyline":
        team = _team_side(pick)
        if team is None:
            return {"ran": False, "reason": "MISSING_SIDE",
                    "market": pick.get("market")}
        wins = sum(1 for h, a in zip(home_scores, away_scores)
                     if (h > a if team == "home" else a > h))
        p_win = wins / n
        distribution = home_scores if team == "home" else away_scores
        threshold = None
    elif kind == "puck_line":
        team = _team_side(pick)
        line = _extract_line(pick)
        if team is None or line is None:
            return {"ran": False, "reason": "MISSING_SIDE_OR_LINE",
                    "market": pick.get("market")}
        # NHL puck line typically ±1.5.  Cover contract:
        #   home -L covers iff home_score - away_score > L
        #   home +L covers iff home_score - away_score > -L  (rarely used)
        wins = 0
        for h, a in zip(home_scores, away_scores):
            margin = (h - a) if team == "home" else (a - h)
            if margin > line:
                wins += 1
        p_win = wins / n
        distribution = [(h - a) if team == "home" else (a - h)
                          for h, a in zip(home_scores, away_scores)]
        threshold = line
    elif kind == "total":
        side = _parse_side(pick)
        line = _extract_line(pick)
        if side is None or line is None:
            return {"ran": False, "reason": "MISSING_SIDE_OR_LINE",
                    "market": pick.get("market")}
        is_under = (side == "under")
        wins = sum(1 for t in totals
                     if (t < line if is_under else t > line))
        p_win = wins / n
        distribution = totals
        threshold = line
    else:
        return {"ran": False, "reason": "UNSUPPORTED_GAME_MARKET",
                "game_market_kind": kind}

    ci_lo, ci_hi = _wilson_ci(p_win, n)
    blended = float(pick.get("win_probability") or 0)
    sim_wp_pct = round(p_win * 100, 1)
    disagreement = round(sim_wp_pct - blended, 2)
    if disagreement > 5:
        signal = "stronger"
    elif disagreement < -5:
        signal = "weaker"
    else:
        signal = "neutral"

    payload = {
        "ran":                          True,
        "sim_win_probability":          sim_wp_pct,
        "sim_ci_lower":                 round(ci_lo * 100, 1),
        "sim_ci_upper":                 round(ci_hi * 100, 1),
        "sim_runs":                     n,
        "sim_threshold":                threshold,
        "sim_expected_stat":            round(sum(distribution) / n, 2),
        "sim_disagreement_with_model":  disagreement,
        "sim_signal":                   signal,
        "sim_game_market_kind":         kind,
        "sim_home_lambda":              float(home_lam),
        "sim_away_lambda":              float(away_lam),
        "simulator_type":               "distribution_monte_carlo",
        "simulator_name":               "nhl_simulator",
        "simulator_version":            "1.0.0",
        "independent_evidence":         True,
        "valid":                        True,
        **compute_percentiles(distribution, threshold=threshold),
    }
    _stamp_provenance(payload, pick, signals=2, p_win=p_win)
    return payload


# ── Player-market simulator ──────────────────────────────────────
def _simulate_player(pick: dict, stat_key: str) -> dict:
    line = _extract_line(pick)
    if line is None:
        return {"ran": False, "reason": "MISSING_LINE"}
    side = _parse_side(pick)
    # Milestone/binary markets (e.g. anytime goal) may not have Over/Under
    # in the market text — default to Over (≥1) semantics for goals.
    if side is None:
        side = "over"
    is_under = (side == "under")

    ctx = dict(pick.get("nhl_sim_context") or {})
    pi = pick.get("player_intel") or {}
    ctx.setdefault("recent_mean",
                    pi.get(f"recent_{stat_key}")
                    or (pi.get("recent") or {}).get(stat_key))
    ctx.setdefault("season_mean",
                    pi.get(f"season_{stat_key}")
                    or (pi.get("season") or {}).get(stat_key))
    ctx.setdefault("recent_n", pi.get("recent_n"))
    ctx.setdefault("season_n", pi.get("season_n"))
    ctx.setdefault("recent_std",
                    pi.get(f"std_{stat_key}")
                    or (pi.get("std") or {}).get(stat_key))
    ctx.setdefault("is_home", pick.get("is_home"))
    ctx.setdefault("opp_defense_mult", ctx.get("opp_defense_mult"))
    ctx.setdefault("role_volume_mult", ctx.get("role_volume_mult"))

    rec = ctx.get("recent_mean")
    ss  = ctx.get("season_mean")
    if not (isinstance(rec, (int, float)) or isinstance(ss, (int, float))):
        return {"ran": False, "reason": "DATA_INSUFFICIENT",
                "market": pick.get("market"),
                "stat_key": stat_key}

    parts: list[tuple[float, float, int]] = []
    if isinstance(rec, (int, float)):
        parts.append((float(rec), 0.60,
                       int(ctx.get("recent_n") or 5)))
    if isinstance(ss, (int, float)):
        parts.append((float(ss), 0.40,
                       int(ctx.get("season_n") or 10)))
    total_w = sum(w for _, w, _ in parts)
    mean = sum(m * w for m, w, _ in parts) / total_w
    sample = int(round(min(n for _, _, n in parts)))
    if sample < 3:
        return {"ran": False, "reason": "DATA_INSUFFICIENT",
                "market": pick.get("market"),
                "stat_key": stat_key, "sample": sample}

    # Apply context multipliers.
    ih = ctx.get("is_home")
    if ih is True:
        mean *= 1.03
    elif ih is False:
        mean *= 0.97
    odm = ctx.get("opp_defense_mult")
    if isinstance(odm, (int, float)):
        mean *= max(0.75, min(1.25, float(odm)))
    rvm = ctx.get("role_volume_mult")
    if isinstance(rvm, (int, float)):
        mean *= max(0.60, min(1.40, float(rvm)))

    # Draw distribution.
    if stat_key in _POISSON_STATS:
        # Points ≈ sum of goals + assists Poissons.  When only a
        # combined recent_mean is provided (typical), a single Poisson
        # with that mean is a valid approximation.
        distribution: list[float] = [
            float(_sample_poisson(max(0.01, mean))) for _ in range(RUNS)
        ]
    elif stat_key in _NORMAL_STATS:
        std = ctx.get("recent_std")
        if not isinstance(std, (int, float)) or std <= 0:
            std = max(0.5, mean * _CV.get(stat_key, 0.35))
        distribution = []
        for _ in range(RUNS):
            x = random.gauss(mean, float(std))
            distribution.append(float(max(0, int(round(x)))))
    else:
        return {"ran": False, "reason": "UNSUPPORTED_MARKET",
                "market": pick.get("market")}

    n = len(distribution)
    if is_under:
        wins = sum(1 for x in distribution if x < line)
    else:
        wins = sum(1 for x in distribution if x > line)
    p_win = wins / n
    ci_lo, ci_hi = _wilson_ci(p_win, n)
    blended = float(pick.get("win_probability") or 0)
    sim_wp_pct = round(p_win * 100, 1)
    disagreement = round(sim_wp_pct - blended, 2)
    if disagreement > 5:
        signal = "stronger"
    elif disagreement < -5:
        signal = "weaker"
    else:
        signal = "neutral"

    alt_lines: dict = {}
    for delta in (-2, -1.0, -0.5, 0.5, 1.0, 2.0):
        alt = round(line + delta, 1)
        if alt < 0:
            continue
        if is_under:
            hits = sum(1 for x in distribution if x < alt)
        else:
            hits = sum(1 for x in distribution if x > alt)
        alt_lines[str(alt)] = round(hits / n * 100, 1)

    payload = {
        "ran":                          True,
        "sim_win_probability":          sim_wp_pct,
        "sim_ci_lower":                 round(ci_lo * 100, 1),
        "sim_ci_upper":                 round(ci_hi * 100, 1),
        "sim_runs":                     n,
        "sim_threshold":                line,
        "sim_is_under":                 is_under,
        "sim_expected_stat":            round(sum(distribution) / n, 2),
        "sim_alt_lines":                alt_lines,
        "sim_disagreement_with_model":  disagreement,
        "sim_signal":                   signal,
        "sim_stat_key":                 stat_key,
        "sim_mean_adjusted":            round(mean, 2),
        "sim_sample_size":              sample,
        "simulator_type":               "distribution_monte_carlo",
        "simulator_name":               "nhl_simulator",
        "simulator_version":            "1.0.0",
        "independent_evidence":         True,
        "valid":                        True,
        **compute_percentiles(distribution, threshold=line),
    }
    _stamp_provenance(payload, pick, signals=_count_signals(ctx),
                       p_win=p_win)
    return payload


def _count_signals(ctx: dict) -> int:
    return sum(1 for k in (
        "recent_mean", "season_mean", "recent_std",
        "is_home", "opp_defense_mult", "role_volume_mult",
        "home_lambda", "away_lambda") if ctx.get(k) is not None)


def _stamp_provenance(payload: dict, pick: dict, *, signals: int,
                       p_win: float) -> None:
    try:
        from services.simulator_provenance import (
            stamp_sim_output, classify_input_quality,
        )
        if signals >= 4:
            provenance = "CAUSAL_INDEPENDENT"
        elif signals >= 2:
            provenance = "EMPIRICAL_INDEPENDENT"
        else:
            provenance = "PRIOR_ONLY"
        mp = pick.get("win_probability")
        if isinstance(mp, (int, float)) and mp > 1:
            mp = mp / 100.0
        stamp_sim_output(
            payload, provenance=provenance,
            input_quality=classify_input_quality(signals),
            sim_prob=p_win,
            model_prob=(float(mp) if isinstance(mp, (int, float)) else None),
        )
    except Exception:
        pass


# ── Public entry point ───────────────────────────────────────────
def simulate(pick: dict) -> dict:
    """Real Monte Carlo NHL simulator (game + player)."""
    sport = str(pick.get("sport") or "").upper()
    if sport != "NHL":
        return {"ran": False, "reason": "WRONG_SPORT",
                "sport": sport, "market": pick.get("market")}

    kind, stat = _classify_market(pick)
    if kind == "player" and stat:
        return _simulate_player(pick, stat)
    if kind in ("moneyline", "puck_line", "total"):
        return _simulate_game(pick, kind)
    return {"ran": False, "reason": "UNSUPPORTED_MARKET",
            "market": pick.get("market")}


def supports(pick: dict) -> bool:
    return str(pick.get("sport") or "").upper() == "NHL"


# Alias for import-consistency with the other sport sims.
simulate_nhl_pick = simulate


__all__ = ["simulate", "simulate_nhl_pick", "supports"]
