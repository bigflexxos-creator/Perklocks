"""Strategy Lab — Continuous Surgical Research Extensions (§5-§14).

Additive, read-only. Every helper returns a small dict tagged with
`provenance` (FACTUAL / DERIVED_FACT / SHADOW_SIGNAL) so the workstation
never blurs data-quality lines.

Nothing here mutates production Lock math or settlement.
"""
from __future__ import annotations

import math
import statistics
from datetime import datetime, timezone
from typing import Any

from deps import db


# ── §5 ROLE / OPPORTUNITY CHANGE RESEARCH ─────────────────────────────
async def role_change(sport: str, subject: str) -> dict[str, Any]:
    """Compare baseline (season) vs current (last window) opportunity."""
    sport = (sport or "").upper()
    baseline: dict[str, float] = {}
    current: dict[str, float] = {}
    reason: str | None = None
    if sport == "MLB":
        try:
            row = await db.mlb_players_intel.find_one(
                {"name_canonical": subject.lower()},
                {"_id": 0, "season_pa": 1, "recent_pa": 1, "lineup_slot": 1,
                 "recent_lineup_slot": 1, "platoon": 1, "recent_platoon": 1,
                 "season_contact_pct": 1, "recent_contact_pct": 1},
            )
        except Exception: row = None
        if row:
            baseline = {"pa_pg": row.get("season_pa") or 0,
                        "lineup_slot": row.get("lineup_slot") or 0,
                        "contact_pct": row.get("season_contact_pct") or 0}
            current = {"pa_pg": row.get("recent_pa") or 0,
                       "lineup_slot": row.get("recent_lineup_slot") or 0,
                       "contact_pct": row.get("recent_contact_pct") or 0}
    elif sport == "NFL":
        try:
            row = await db.nfl_players_intel.find_one(
                {"name_canonical": subject.lower()},
                {"_id": 0, "season_snap_pct": 1, "snap_pct": 1,
                 "season_target_share": 1, "target_share": 1,
                 "season_carry_share": 1, "carry_share": 1,
                 "season_rz_touches_pg": 1, "rz_touches_pg": 1},
            )
        except Exception: row = None
        if row:
            baseline = {"snap_pct": row.get("season_snap_pct") or 0,
                        "target_share": row.get("season_target_share") or 0,
                        "carry_share": row.get("season_carry_share") or 0,
                        "rz_touches_pg": row.get("season_rz_touches_pg") or 0}
            current = {"snap_pct": row.get("snap_pct") or 0,
                       "target_share": row.get("target_share") or 0,
                       "carry_share": row.get("carry_share") or 0,
                       "rz_touches_pg": row.get("rz_touches_pg") or 0}
    elif sport == "NBA":
        try:
            row = await db.nba_players_intel.find_one(
                {"name_canonical": subject.lower()},
                {"_id": 0, "season_min": 1, "recent_min": 1,
                 "season_usg": 1, "recent_usg": 1,
                 "season_role": 1, "recent_role": 1,
                 "season_fga": 1, "recent_fga": 1},
            )
        except Exception: row = None
        if row:
            baseline = {"minutes": row.get("season_min") or 0,
                        "usage": row.get("season_usg") or 0,
                        "fga_pg": row.get("season_fga") or 0}
            current = {"minutes": row.get("recent_min") or 0,
                       "usage": row.get("recent_usg") or 0,
                       "fga_pg": row.get("recent_fga") or 0}
    if not baseline or not current or all(v == 0 for v in baseline.values()):
        return {"available": False, "reason": "INSUFFICIENT_DATA",
                "provenance": "FACTUAL"}
    deltas = {}
    for k, base in baseline.items():
        cur = current.get(k, 0)
        abs_d = cur - base
        pct_d = (abs_d / base * 100.0) if base else None
        deltas[k] = {"baseline": base, "current": cur,
                     "abs_delta": round(abs_d, 3),
                     "pct_delta": (round(pct_d, 1) if pct_d is not None else None)}
    # Classify overall change
    max_abs_pct = max((abs(v["pct_delta"]) for v in deltas.values()
                       if v["pct_delta"] is not None), default=0)
    if max_abs_pct >= 25:
        cls = "OPPORTUNITY_CHANGE"
    elif max_abs_pct >= 12:
        cls = "ROLE_CHANGE"
    elif max_abs_pct >= 5:
        cls = "UNDERLYING_SKILL_CHANGE"
    else:
        cls = "RESULT_TREND"
    return {"available": True, "sport": sport, "subject": subject,
            "classification": cls, "deltas": deltas,
            "max_pct_delta": round(max_abs_pct, 1),
            "provenance": "DERIVED_FACT"}


# ── §6 REGRESSION RESEARCH ────────────────────────────────────────────
async def regression(sport: str, subject: str) -> dict[str, Any]:
    """AVG vs xBA / SLG vs xSLG (MLB); production vs opportunity (NFL);
    PTS vs MIN/USG/FGA (NBA)."""
    sport = (sport or "").upper()
    if sport == "MLB":
        try:
            row = await db.mlb_statcast.find_one(
                {"name_canonical": subject.lower()},
                {"_id": 0, "avg": 1, "xba": 1, "slg": 1, "xslg": 1,
                 "woba": 1, "xwoba": 1, "barrel_pct": 1, "hard_hit_pct": 1,
                 "hr": 1},
            )
        except Exception: row = None
        if not row:
            return {"available": False, "reason": "no_statcast", "provenance": "FACTUAL"}
        gaps = {}
        for actual, expected in [("avg", "xba"), ("slg", "xslg"), ("woba", "xwoba")]:
            a = row.get(actual); e = row.get(expected)
            if a is not None and e is not None:
                gaps[actual] = {"actual": a, "expected": e,
                                "gap": round(a - e, 3)}
        # Classify from wOBA gap primarily
        w = gaps.get("woba")
        if not w:
            cls = "UNDERLYING_STABLE"
        elif w["gap"] <= -0.020:
            cls = "POSITIVE_REGRESSION"
        elif w["gap"] >= 0.020:
            cls = "NEGATIVE_REGRESSION"
        elif abs(w["gap"]) >= 0.010:
            cls = "OVERPERFORMING" if w["gap"] > 0 else "UNDERPERFORMING"
        else:
            cls = "UNDERLYING_STABLE"
        return {"available": True, "sport": "MLB", "subject": subject,
                "gaps": gaps, "classification": cls,
                "barrel_pct": row.get("barrel_pct"),
                "hard_hit_pct": row.get("hard_hit_pct"),
                "provenance": "DERIVED_FACT"}
    if sport == "NFL":
        try:
            row = await db.nfl_players_intel.find_one(
                {"name_canonical": subject.lower()},
                {"_id": 0, "target_share": 1, "receiving_yards_pg": 1,
                 "carry_share": 1, "rushing_yards_pg": 1,
                 "rz_touches_pg": 1, "receiving_tds_pg": 1,
                 "expected_yards_pg": 1},
            )
        except Exception: row = None
        if not row:
            return {"available": False, "reason": "no_data", "provenance": "FACTUAL"}
        exp = row.get("expected_yards_pg") or 0
        got_r = row.get("receiving_yards_pg") or 0
        got_u = row.get("rushing_yards_pg") or 0
        got = got_r + got_u
        gap = got - exp if exp else None
        if exp <= 0:
            cls = "UNDERLYING_STABLE"
        else:
            g = gap or 0
            if g <= -8: cls = "POSITIVE_REGRESSION"
            elif g >= 8: cls = "NEGATIVE_REGRESSION"
            elif abs(g) >= 4: cls = "OVERPERFORMING" if g > 0 else "UNDERPERFORMING"
            else: cls = "UNDERLYING_STABLE"
        return {"available": True, "sport": "NFL", "subject": subject,
                "expected_yards_pg": exp, "actual_yards_pg": got,
                "gap": gap, "classification": cls,
                "provenance": "DERIVED_FACT"}
    if sport == "NBA":
        try:
            cursor = db.player_game_logs.find(
                {"sport": "nba", "player_name": subject},
                {"_id": 0, "stats": 1, "minutes": 1},
            ).sort("date", -1).limit(15)
            rows = await cursor.to_list(length=15)
        except Exception:
            rows = []
        if not rows:
            return {"available": False, "reason": "no_data", "provenance": "FACTUAL"}
        pts = []; mins = []; fga = []
        for r in rows:
            s = r.get("stats") or {}
            m = s.get("minutes") or r.get("minutes") or 0
            if m <= 0: continue
            pts.append(s.get("points") or 0)
            mins.append(m)
            fga.append(s.get("field_goal_attempts") or 0)
        if not pts:
            return {"available": False, "reason": "no_data", "provenance": "FACTUAL"}
        pts_per_min = (sum(pts) / sum(mins)) if sum(mins) else 0
        pts_per_fga = (sum(pts) / sum(fga)) if sum(fga) else 0
        # Compare per-minute scoring against league-typical 0.55 pts/min baseline
        gap = pts_per_min - 0.55
        if gap >= 0.10: cls = "OVERPERFORMING"
        elif gap <= -0.10: cls = "UNDERPERFORMING"
        elif abs(gap) >= 0.05: cls = ("POSITIVE_REGRESSION" if gap < 0
                                      else "NEGATIVE_REGRESSION")
        else: cls = "UNDERLYING_STABLE"
        return {"available": True, "sport": "NBA", "subject": subject,
                "pts_per_min": round(pts_per_min, 3),
                "pts_per_fga": round(pts_per_fga, 3),
                "classification": cls, "sample_size": len(pts),
                "provenance": "DERIVED_FACT"}
    return {"available": False, "reason": "unsupported_sport"}


# ── §7 MARKET DISAGREEMENT ────────────────────────────────────────────
async def market_disagreement(pick_id: str | None,
                              model_prob: float | None,
                              book_odds: int | None) -> dict[str, Any]:
    """Compare model P vs raw market implied P. Zero extra polling — only
    reads already-acquired odds already stored on the pick."""
    if model_prob is None or book_odds is None:
        return {"available": False, "reason": "insufficient_pricing"}
    # Raw implied P from book odds
    if book_odds >= 0:
        implied = 100.0 / (book_odds + 100.0)
    else:
        implied = -book_odds / (-book_odds + 100.0)
    diff = model_prob - implied
    if diff >= 0.05: cls = "MODEL_ABOVE_MARKET"
    elif diff <= -0.05: cls = "MODEL_BELOW_MARKET"
    else: cls = "MARKET_ALIGNED"
    # Look up dispersion across bookmaker snapshot if present.
    dispersion = None
    book_count = None
    if pick_id:
        try:
            p = await db.picks.find_one(
                {"pick_id": pick_id},
                {"_id": 0, "sportsbook_mapping": 1, "sportsbook_prices": 1},
            )
        except Exception: p = None
        prices = (p or {}).get("sportsbook_prices") or (p or {}).get("sportsbook_mapping") or {}
        vals = []
        if isinstance(prices, dict):
            for v in prices.values():
                if isinstance(v, dict) and "odds" in v: vals.append(v["odds"])
                elif isinstance(v, (int, float)): vals.append(v)
        elif isinstance(prices, list):
            for v in prices:
                if isinstance(v, dict) and "odds" in v: vals.append(v["odds"])
        if len(vals) >= 2:
            try:
                dispersion = round(statistics.pstdev(vals), 1)
                book_count = len(vals)
            except statistics.StatisticsError:
                pass
    if book_count and dispersion and dispersion >= 15:
        cls = "MARKET_FRAGMENTED"
    return {"available": True, "model_prob": round(model_prob, 3),
            "market_prob": round(implied, 3),
            "difference": round(diff, 3),
            "classification": cls,
            "book_count": book_count, "book_dispersion": dispersion,
            "provenance": "DERIVED_FACT"}


# ── §8 LINE SENSITIVITY ───────────────────────────────────────────────
def line_sensitivity(values: list[float], line: float,
                     step: float = 5.0) -> dict[str, Any]:
    """Evaluate empirical P(over) at line ± step / ± 2·step. Uses ONE
    underlying distribution — never fabricates alternate market lines."""
    if not values:
        return {"available": False}
    n = len(values)
    thresholds = [line - 2 * step, line - step, line,
                  line + step, line + 2 * step]
    curve = []
    for t in thresholds:
        p = sum(1 for v in values if v > t) / n
        curve.append({"model_threshold": round(t, 2),
                      "empirical_over": round(p, 3)})
    # Slope from -1step to +1step
    p_lo = curve[1]["empirical_over"]
    p_hi = curve[3]["empirical_over"]
    slope = abs(p_hi - p_lo)
    if slope >= 0.25:
        cls = "LINE_FRAGILE"
    elif slope >= 0.10:
        cls = "LINE_SENSITIVE"
    else:
        cls = "LINE_ROBUST"
    return {"available": True, "line": line, "step": step,
            "curve": curve, "slope": round(slope, 3),
            "classification": cls,
            "note": "model_threshold entries are Lab thresholds — NOT sportsbook lines",
            "provenance": "DERIVED_FACT"}


# ── §9 PRICE QUALITY ──────────────────────────────────────────────────
async def price_quality(pick_id: str, model_prob: float | None) -> dict[str, Any]:
    """Compare book odds vs model fair price. Zero new polling."""
    if model_prob is None:
        return {"available": False, "reason": "no_model_prob"}
    try:
        p = await db.picks.find_one(
            {"pick_id": pick_id},
            {"_id": 0, "book_odds": 1, "sportsbook_prices": 1,
             "sportsbook_mapping": 1},
        )
    except Exception: p = None
    if not p:
        return {"available": False, "reason": "no_pick"}
    book_odds = p.get("book_odds")
    if book_odds is None:
        return {"available": False, "reason": "NO_CURRENT_PRICE"}
    # Fair American odds from model prob
    if model_prob <= 0 or model_prob >= 1:
        return {"available": False, "reason": "invalid_prob"}
    if model_prob >= 0.5:
        fair = int(round(-100 * model_prob / (1 - model_prob)))
    else:
        fair = int(round(100 * (1 - model_prob) / model_prob))
    raw_implied = (100.0 / (book_odds + 100.0) if book_odds >= 0
                   else -book_odds / (-book_odds + 100.0))
    edge = model_prob - raw_implied
    if edge >= 0.05: cls = "GOOD_PRICE"
    elif edge >= 0.01: cls = "FAIR_PRICE"
    else: cls = "POOR_PRICE"
    # Best/worst/consensus/dispersion from existing snapshot
    prices = p.get("sportsbook_prices") or p.get("sportsbook_mapping") or {}
    vals = []
    if isinstance(prices, dict):
        for v in prices.values():
            if isinstance(v, dict) and "odds" in v: vals.append(int(v["odds"]))
            elif isinstance(v, (int, float)): vals.append(int(v))
    consensus = round(sum(vals) / len(vals)) if vals else None
    return {"available": True, "book_odds": book_odds, "fair_odds": fair,
            "raw_implied_prob": round(raw_implied, 3),
            "model_prob": round(model_prob, 3),
            "edge_pp": round(edge * 100, 1),
            "best_price": max(vals) if vals else book_odds,
            "worst_price": min(vals) if vals else book_odds,
            "consensus_price": consensus,
            "book_count": len(vals) if vals else 1,
            "classification": cls,
            "provenance": "DERIVED_FACT"}


# ── §10 SAMPLE STABILITY ──────────────────────────────────────────────
async def sample_stability(sport: str, subject: str,
                            stat_field: str = "hits",
                            line: float | None = None) -> dict[str, Any]:
    """Compute L5/L10/L20/SEASON summaries so 2/2 doesn't masquerade as
    18/25. Never surfaces high stability on tiny samples."""
    try:
        cursor = db.player_game_logs.find(
            {"sport": sport.lower(), "player_name": subject},
            {"_id": 0, "stats": 1, "date": 1},
        ).sort("date", -1).limit(200)
        rows = await cursor.to_list(length=200)
    except Exception:
        rows = []
    if not rows:
        return {"available": False, "reason": "no_data", "provenance": "FACTUAL"}
    values: list[float] = []
    for r in rows:
        s = r.get("stats") or {}
        v = s.get(stat_field)
        if v is None and stat_field == "pra":
            v = ((s.get("points") or 0) + (s.get("rebounds") or 0)
                 + (s.get("assists") or 0))
        if v is None: continue
        try:
            values.append(float(v))
        except Exception: continue
    if not values:
        return {"available": False, "reason": "no_data", "provenance": "FACTUAL"}
    def _slice(k: int) -> dict[str, Any] | None:
        if len(values) < 1: return None
        s = values[:min(k, len(values))]
        m = sum(s) / len(s)
        med = sorted(s)[len(s) // 2]
        try: std = statistics.pstdev(s)
        except Exception: std = 0
        hr = None
        if line is not None:
            hr = sum(1 for x in s if x > line) / len(s)
        return {"n": len(s), "mean": round(m, 2),
                "median": round(med, 2), "std": round(std, 2),
                "hit_rate": (round(hr, 3) if hr is not None else None)}
    windows = {"L5": _slice(5), "L10": _slice(10),
               "L20": _slice(20), "SEASON": _slice(len(values))}
    windows = {k: v for k, v in windows.items() if v is not None}
    n_full = windows["SEASON"]["n"]
    # Classification via mean drift + volatility
    m5 = (windows.get("L5") or {}).get("mean")
    m20 = (windows.get("L20") or {}).get("mean")
    std5 = (windows.get("L5") or {}).get("std") or 0
    if n_full < 5: cls = "SMALL_SAMPLE"
    elif m5 is not None and m20 is not None and m5 - m20 >= 0.5:
        cls = "IMPROVING"
    elif m5 is not None and m20 is not None and m5 - m20 <= -0.5:
        cls = "DECLINING"
    elif std5 >= (max((windows.get("SEASON") or {}).get("std") or 0, 1.0)):
        cls = "VOLATILE"
    else:
        cls = "STABLE"
    return {"available": True, "sport": sport.upper(), "subject": subject,
            "stat_field": stat_field, "line": line,
            "windows": windows, "classification": cls,
            "provenance": "DERIVED_FACT"}


# ── §11 OPPONENT CONTEXT ──────────────────────────────────────────────
async def opponent_context(sport: str, subject: str,
                           opponent: str | None) -> dict[str, Any]:
    sport = (sport or "").upper()
    if not opponent:
        return {"available": False, "reason": "no_opponent",
                "provenance": "FACTUAL"}
    if sport == "MLB":
        try:
            row = await db.mlb_pitchers_intel.find_one(
                {"name_canonical": opponent.lower()},
                {"_id": 0, "handedness": 1, "season_k_pct": 1,
                 "hard_hit_pct_allowed": 1, "l5_k_avg": 1,
                 "stuff_plus": 1},
            )
        except Exception: row = None
        if not row:
            return {"available": False, "reason": "INSUFFICIENT_DATA",
                    "provenance": "FACTUAL"}
        k = row.get("season_k_pct") or 0
        stuff = row.get("stuff_plus") or 100
        if k >= 28 or stuff >= 110: cls = "RISK"
        elif k <= 20 and stuff <= 95: cls = "ADVANTAGE"
        else: cls = "NEUTRAL"
        return {"available": True, "sport": "MLB", "subject": subject,
                "opponent": opponent, "context": row,
                "classification": cls, "provenance": "FACTUAL"}
    if sport == "NFL":
        try:
            row = await db.nfl_defense_intel.find_one(
                {"team_canonical": opponent.lower()},
                {"_id": 0, "vs_qb_rank": 1, "vs_rb_rank": 1,
                 "vs_wr_rank": 1, "vs_te_rank": 1,
                 "vs_pass_ypg": 1, "vs_rush_ypg": 1,
                 "vs_yprr_wr": 1},
            )
        except Exception: row = None
        if not row:
            return {"available": False, "reason": "INSUFFICIENT_DATA",
                    "provenance": "FACTUAL"}
        # Higher rank number = worse defense = advantage for offense
        ranks = [v for k, v in row.items() if "rank" in k and v is not None]
        avg_rank = sum(ranks) / len(ranks) if ranks else 16
        if avg_rank >= 22: cls = "ADVANTAGE"
        elif avg_rank <= 10: cls = "RISK"
        else: cls = "NEUTRAL"
        return {"available": True, "sport": "NFL", "subject": subject,
                "opponent": opponent, "context": row,
                "classification": cls, "provenance": "FACTUAL"}
    if sport == "NBA":
        try:
            row = await db.team_form.find_one(
                {"sport": "nba", "team_canonical": opponent.lower()},
                {"_id": 0, "pace": 1, "def_rating": 1,
                 "opp_pts_pg": 1, "opp_reb_pg": 1, "opp_ast_pg": 1},
            )
        except Exception: row = None
        if not row:
            return {"available": False, "reason": "INSUFFICIENT_DATA",
                    "provenance": "FACTUAL"}
        pace = row.get("pace") or 100
        drtg = row.get("def_rating") or 110
        if pace >= 103 and drtg >= 115: cls = "ADVANTAGE"
        elif pace <= 96 and drtg <= 108: cls = "RISK"
        else: cls = "NEUTRAL"
        return {"available": True, "sport": "NBA", "subject": subject,
                "opponent": opponent, "context": row,
                "classification": cls, "provenance": "FACTUAL"}
    return {"available": False, "reason": "unsupported_sport"}


# ── §12 H2H QUALITY ──────────────────────────────────────────────────
def h2h_quality(sample_size: int, threshold_meetings: int | None = None) -> dict[str, Any]:
    """Credibility layer for H2H — 2/2 must not look like 18/25."""
    if sample_size <= 0:
        cls = "NOT_MEANINGFUL"
    elif sample_size >= 15:
        cls = "HIGH_VALUE_H2H"
    elif sample_size >= 6:
        cls = "MODERATE_H2H"
    else:
        cls = "LOW_SAMPLE_H2H"
    return {"sample_size": sample_size,
            "threshold_meetings": threshold_meetings,
            "classification": cls,
            "provenance": "FACTUAL"}


# ── §13 MODEL DRIFT MONITOR ──────────────────────────────────────────
async def model_drift(sport: str, days_recent: int = 14) -> dict[str, Any]:
    """Compare recent settled-pick calibration against a longer baseline
    window. Read-only — never auto-recalibrates production."""
    from datetime import timedelta
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days_recent)).isoformat()
        cursor = db.picks.find(
            {"sport": sport.upper(), "status": {"$in": ["won", "lost"]}},
            {"_id": 0, "win_probability": 1, "status": 1,
             "settled_at": 1},
        ).limit(30000)
        rows = await cursor.to_list(length=30000)
    except Exception:
        rows = []
    if not rows:
        return {"available": False}
    recent = []; baseline = []
    for r in rows:
        wp = r.get("win_probability")
        if wp is None: continue
        try: wpf = float(wp) / 100.0
        except Exception: continue
        st = 1 if r.get("status") == "won" else 0
        (recent if r.get("settled_at", "") >= cutoff else baseline).append((wpf, st))
    def _calib(pairs):
        if not pairs: return None
        avg_p = sum(p for p, _ in pairs) / len(pairs)
        hr = sum(s for _, s in pairs) / len(pairs)
        brier = sum((p - s) ** 2 for p, s in pairs) / len(pairs)
        try:
            log_loss = -sum(
                s * math.log(max(p, 1e-6)) + (1 - s) * math.log(max(1 - p, 1e-6))
                for p, s in pairs
            ) / len(pairs)
        except Exception:
            log_loss = None
        return {"n": len(pairs), "avg_pred": round(avg_p, 3),
                "actual_hr": round(hr, 3),
                "gap_pp": round((avg_p - hr) * 100, 1),
                "brier": round(brier, 3),
                "log_loss": round(log_loss, 3) if log_loss is not None else None}
    r = _calib(recent); b = _calib(baseline)
    if not r or not b:
        return {"available": True, "recent": r, "baseline": b,
                "classification": "STABLE", "provenance": "DERIVED_FACT"}
    gap_diff = abs(r["gap_pp"] - b["gap_pp"])
    if gap_diff >= 5.0: cls = "DEGRADED"
    elif gap_diff >= 2.5: cls = "WATCH"
    else: cls = "STABLE"
    return {"available": True, "sport": sport.upper(),
            "recent": r, "baseline": b,
            "gap_delta_pp": round(gap_diff, 1),
            "classification": cls,
            "provenance": "DERIVED_FACT"}


# ── §14 RESEARCH SCORECARD ──────────────────────────────────────────
def research_scorecard(
    role: dict | None = None,
    matchup: dict | None = None,
    regression_r: dict | None = None,
    stability: dict | None = None,
    price: dict | None = None,
) -> dict[str, Any]:
    """Compact 6-dimension research scorecard. NOT a Lock."""
    def _grade_role(d):
        if not d or not d.get("available"): return "LOW"
        cls = d.get("classification")
        if cls == "OPPORTUNITY_CHANGE": return "HIGH"
        if cls == "ROLE_CHANGE": return "MEDIUM"
        return "LOW"
    def _grade_matchup(d):
        if not d or not d.get("available"): return "LOW"
        cls = d.get("classification")
        return {"ADVANTAGE": "HIGH", "NEUTRAL": "MEDIUM", "RISK": "LOW"}.get(cls, "LOW")
    def _grade_skill(d):
        if not d or not d.get("available"): return "LOW"
        cls = d.get("classification")
        if cls == "POSITIVE_REGRESSION": return "HIGH"
        if cls in ("OVERPERFORMING", "NEGATIVE_REGRESSION", "UNDERPERFORMING"): return "LOW"
        return "MEDIUM"
    def _grade_form(d):
        if not d or not d.get("available"): return "LOW"
        cls = d.get("classification")
        return {"STABLE": "HIGH", "IMPROVING": "HIGH",
                "VOLATILE": "MEDIUM", "SMALL_SAMPLE": "LOW",
                "DECLINING": "LOW"}.get(cls, "MEDIUM")
    def _grade_price(d):
        if not d or not d.get("available"): return "LOW"
        cls = d.get("classification")
        return {"GOOD_PRICE": "HIGH", "FAIR_PRICE": "MEDIUM",
                "POOR_PRICE": "LOW", "NO_CURRENT_PRICE": "LOW"}.get(cls, "LOW")
    def _grade_data(*parts):
        strong = 0; total = 0
        for p in parts:
            if not p: continue
            total += 1
            if p.get("available"): strong += 1
        if total == 0: return "LOW"
        ratio = strong / total
        if ratio >= 0.8: return "HIGH"
        if ratio >= 0.5: return "MEDIUM"
        return "LOW"
    grades = {
        "OPPORTUNITY": _grade_role(role),
        "MATCHUP": _grade_matchup(matchup),
        "UNDERLYING_SKILL": _grade_skill(regression_r),
        "FORM_STABILITY": _grade_form(stability),
        "PRICE_QUALITY": _grade_price(price),
        "DATA_QUALITY": _grade_data(role, matchup, regression_r, stability, price),
    }
    highs = sum(1 for v in grades.values() if v == "HIGH")
    lows  = sum(1 for v in grades.values() if v == "LOW")
    if highs >= 4 and lows <= 1: overall = "HIGH"
    elif lows >= 4: overall = "LOW"
    else: overall = "MEDIUM"
    return {
        "dimensions": grades,
        "research_quality": overall,
        "note": "Research scorecard is DIAGNOSTIC ONLY. Never converts to a Lock.",
        "provenance": "DERIVED_FACT",
    }
