"""Correlation Engine (Phase 5, 2026-06-30).

Extends `correlation_guard.analyze_parlay` with:
  • positive correlation detection (QB pass yards + WR receiving yards
    same team → game-script tailwind for both)
  • negative correlation (two players needing opposite game scripts)
  • same-game dependency (already scored, now also numeric)
  • usage conflict (two RBs same team, two WRs receptions same team)

Returns pairwise correlation scores in [-1, 1] plus a parlay-level
report. Never modifies the underlying picks.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional

try:
    from correlation_guard import analyze_parlay as _guard_analyze
except Exception:  # pragma: no cover
    def _guard_analyze(_legs):
        return {"warnings": [], "blocked_pairs": [],
                "downweight_factor": 1.0, "correlation_tier": "none"}


# ═════════════════════════════════════════════════════════════════════
# Pick-shape helpers
# ═════════════════════════════════════════════════════════════════════
def _norm(v) -> str:
    return (v or "").strip().lower() if isinstance(v, str) else ""


def _event_id(leg: dict) -> str:
    for k in ("event_id", "game_id", "external_event_id"):
        v = leg.get(k)
        if v:
            return str(v).lower()
    home, away = leg.get("home_team"), leg.get("away_team")
    if home and away:
        return f"{_norm(home)}__{_norm(away)}"
    return _norm(leg.get("event"))


def _sport(leg: dict) -> str:
    return _norm(leg.get("sport"))


def _team(leg: dict) -> str:
    """Best-effort player-team extraction."""
    for k in ("player_team", "team", "team_abbrev"):
        v = leg.get(k)
        if v:
            return _norm(v)
    # Try to parse (ABBR) from the market
    market = leg.get("market") or ""
    if isinstance(market, str) and "(" in market and ")" in market:
        try:
            token = market.split("(")[1].split(")")[0]
            if 2 <= len(token) <= 5 and token.isalnum():
                return _norm(token)
        except (IndexError, ValueError):
            pass
    return ""


def _market_family(market: str) -> str:
    m = _norm(market)
    if not m:
        return "other"
    if "pass" in m and ("yards" in m or "yds" in m): return "qb_pass_yards"
    if "pass" in m and ("tds" in m or "td" in m):    return "qb_pass_tds"
    if "rush" in m and ("yards" in m or "yds" in m): return "rush_yards"
    if "rush" in m and ("tds" in m or "td" in m):    return "rush_tds"
    if "receiving yards" in m or "rec yards" in m or "rec yds" in m:
        return "rec_yards"
    if "receptions" in m:                             return "receptions"
    if "receiving tds" in m or "rec tds" in m:       return "rec_tds"
    if "anytime" in m and "goal" in m:                return "goal_scorer"
    if "first goal scorer" in m:                      return "first_goal"
    if "to score or assist" in m:                     return "score_or_assist"
    if "win or draw" in m or "double chance" in m:    return "win_or_draw"
    if "moneyline" in m:                              return "moneyline"
    if "spread" in m or "run line" in m or "puck line" in m: return "spread"
    if "total" in m and (" over " in m or " under " in m):
        return "team_total" if "team" in m else "game_total"
    if " over " in m or "over " in m or " under " in m or "under " in m:
        return "player_over_under"
    if "hits" in m or "total bases" in m:             return "batter_over"
    if "strikeouts" in m or "ks " in m or " k's" in m: return "pitcher_ks"
    return "other"


def _pick_side_direction(leg: dict) -> Optional[str]:
    """OVER/UNDER direction (returns 'over' or 'under' or None)."""
    sel = _norm(leg.get("selection"))
    market = _norm(leg.get("market"))
    for src in (sel, market):
        if not src:
            continue
        if src.startswith("over") or " over " in src or src.startswith("o "):
            return "over"
        if src.startswith("under") or " under " in src or src.startswith("u "):
            return "under"
    return None


# ═════════════════════════════════════════════════════════════════════
# Pairwise correlation rules
# ═════════════════════════════════════════════════════════════════════
# Rule: (family_a, family_b, same_team?, direction_a, direction_b) → corr
# Positive correlations reinforce (both need the same game script).
# Negative correlations conflict (one leg winning implies the other loses).
_POSITIVE_SAME_TEAM = {
    ("qb_pass_yards", "rec_yards"):  0.55,
    ("qb_pass_yards", "receptions"): 0.45,
    ("qb_pass_yards", "rec_tds"):    0.35,
    ("qb_pass_tds",   "rec_tds"):    0.55,
    ("rec_yards",     "receptions"): 0.65,
    ("rush_yards",    "rush_tds"):   0.55,
    ("team_total",    "qb_pass_yards"): 0.35,
    ("team_total",    "rush_yards"):    0.30,
    ("team_total",    "rec_yards"):     0.30,
    ("game_total",    "qb_pass_yards"): 0.25,
    ("game_total",    "rec_yards"):     0.20,
}

# Two RBs same team, or two same-role WRs → usage conflict (they split a
# fixed pie: one over means less pie for the other).
_USAGE_CONFLICT_SAME_TEAM = {
    ("rush_yards", "rush_yards"):  -0.45,
    ("rush_tds",   "rush_tds"):    -0.40,
    ("receptions", "receptions"): -0.35,
    ("rec_yards",  "rec_yards"):  -0.30,
    ("pitcher_ks", "pitcher_ks"): -0.80,  # can't have two starters same team
}


def _lookup_rule(a: str, b: str, table: dict) -> Optional[float]:
    if (a, b) in table:
        return table[(a, b)]
    if (b, a) in table:
        return table[(b, a)]
    return None


def _direction_correlation(dir_a: Optional[str], dir_b: Optional[str],
                           same_team: bool) -> float:
    """Adjust correlation by OVER/UNDER direction alignment.

    Two OVERs on the same team's game-script picks reinforce. OVER + UNDER
    on same team = the picks require opposite outcomes."""
    if dir_a is None or dir_b is None:
        return 1.0
    if same_team and dir_a == dir_b:
        return 1.0
    if same_team and dir_a != dir_b:
        return -1.0
    # opposite team: OVER + OVER can still coexist (high-scoring game).
    return 0.75


# ═════════════════════════════════════════════════════════════════════
# Public API
# ═════════════════════════════════════════════════════════════════════
@dataclass
class CorrelationPair:
    idx_a: int
    idx_b: int
    correlation: float                # in [-1, 1]
    kind: str                         # "positive", "negative", "same_game",
                                      # "usage_conflict", "same_player",
                                      # "opposite_script", "neutral"
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CorrelationReport:
    pairs: list                       # list[CorrelationPair]
    blocked_pairs: list               # list[(i, j)] (same-player, opp-script)
    positive_pairs: list              # list[(i, j)]
    negative_pairs: list              # list[(i, j)]
    correlation_score: float          # -1..1 average signed magnitude
    downweight_factor: float          # 0..1 multiplier for edge display
    warnings: list                    # human-readable strings
    tier: str                         # "none"/"low"/"medium"/"high"

    def to_dict(self) -> dict:
        return {
            **asdict(self),
            "pairs": [p.to_dict() if isinstance(p, CorrelationPair)
                      else p for p in self.pairs],
        }


def pairwise_correlation(leg_a: dict, leg_b: dict) -> CorrelationPair:
    """Compute correlation between two legs. Returns CorrelationPair with
    idx_a=0, idx_b=1 as sentinels — caller sets real indices."""
    if not isinstance(leg_a, dict) or not isinstance(leg_b, dict):
        return CorrelationPair(0, 1, 0.0, "neutral", "invalid input")

    fam_a = _market_family(leg_a.get("market") or "")
    fam_b = _market_family(leg_b.get("market") or "")
    team_a, team_b = _team(leg_a), _team(leg_b)
    event_a, event_b = _event_id(leg_a), _event_id(leg_b)
    same_event = bool(event_a and event_b and event_a == event_b)
    same_team = bool(team_a and team_b and team_a == team_b)
    dir_a, dir_b = _pick_side_direction(leg_a), _pick_side_direction(leg_b)

    # Same player detection — ONLY trust explicit player_name/player
    # fields. Do NOT try to sniff a name out of the market string; too
    # noisy (matches e.g. "Rushing Yards" as the "name" and every RB in
    # the parlay collides).
    _GENERIC_MARKET_PREFIXES = (
        "passing", "rushing", "receiving", "receptions", "moneyline",
        "spread", "total", "run line", "puck line", "team total",
    )

    def _player(leg):
        for k in ("player_name", "player"):
            v = leg.get(k)
            if isinstance(v, str) and v.strip():
                # Guard against callers stuffing the market family into
                # `player`. Generic prefixes are not real names.
                name = v.strip().lower()
                if any(name.startswith(p) for p in _GENERIC_MARKET_PREFIXES):
                    return None
                return name
        return None

    pa, pb = _player(leg_a), _player(leg_b)
    if pa and pb and pa == pb:
        return CorrelationPair(0, 1, 0.95, "same_player",
                               f"Both legs on {pa.title()}")

    # Usage conflict (same-team, same role)
    if same_team:
        conflict = _lookup_rule(fam_a, fam_b, _USAGE_CONFLICT_SAME_TEAM)
        if conflict is not None:
            return CorrelationPair(0, 1, conflict, "usage_conflict",
                                   f"Same-team usage split ({fam_a} × {fam_b})")

    # Positive correlations (same team, complementary market families)
    if same_team:
        pos = _lookup_rule(fam_a, fam_b, _POSITIVE_SAME_TEAM)
        if pos is not None:
            adj = pos * _direction_correlation(dir_a, dir_b, True)
            kind = "positive" if adj > 0 else "opposite_script"
            reason = (f"Same-team game-script link ({fam_a} × {fam_b})"
                      if adj > 0 else
                      f"Same-team opposite direction ({dir_a}/{dir_b})")
            return CorrelationPair(0, 1, round(adj, 3), kind, reason)

    # Opposite team, both OVER same market family = correlated game total
    if event_a and event_b and not same_event and fam_a == fam_b \
            and dir_a == "over" and dir_b == "over" \
            and fam_a in ("qb_pass_yards", "rec_yards", "rush_yards", "team_total"):
        # Different games — trivially independent unless we've inferred a
        # slate-wide macro. Neutral for now.
        return CorrelationPair(0, 1, 0.0, "neutral",
                               "Different games, independent")

    # Same game, different teams = negative game-script (RB Team A over +
    # RB Team B over both want to run out the clock).
    if same_event and not same_team and fam_a == fam_b and fam_a in (
            "rush_yards", "rush_tds") and dir_a == "over" and dir_b == "over":
        return CorrelationPair(0, 1, -0.40, "opposite_script",
                               "Both RBs need clock-eating positive game "
                               "script — mutually exclusive")

    # Same game, generic mild same-game dependency
    if same_event:
        # Same-side (both backing same team via ML / spread / etc.):
        side_a = _norm(leg_a.get("pick_side") or leg_a.get("selection"))
        side_b = _norm(leg_b.get("pick_side") or leg_b.get("selection"))
        if side_a and side_b and side_a == side_b:
            return CorrelationPair(0, 1, 0.45, "same_game",
                                   "Same team backed twice in same game")
        return CorrelationPair(0, 1, 0.15, "same_game",
                               "Legs share a game (mild dependency)")

    return CorrelationPair(0, 1, 0.0, "neutral", "")


def analyze_correlations(legs: list) -> CorrelationReport:
    """Compute the full correlation report for a parlay."""
    if not isinstance(legs, list) or len(legs) < 2:
        return CorrelationReport(pairs=[], blocked_pairs=[],
                                 positive_pairs=[], negative_pairs=[],
                                 correlation_score=0.0, downweight_factor=1.0,
                                 warnings=[], tier="none")

    pairs: list = []
    blocked: list = []
    positives: list = []
    negatives: list = []
    warnings: list = []
    total_signed = 0.0
    strong_positive = 0
    strong_negative = 0

    for i in range(len(legs)):
        for j in range(i + 1, len(legs)):
            cp = pairwise_correlation(legs[i], legs[j])
            cp.idx_a, cp.idx_b = i, j
            pairs.append(cp)
            total_signed += cp.correlation
            if cp.kind == "same_player" or cp.correlation <= -0.75:
                blocked.append((i, j))
                warnings.append(f"BLOCK: {cp.reason}")
            elif cp.correlation >= 0.35:
                positives.append((i, j))
                if cp.correlation >= 0.5:
                    strong_positive += 1
            elif cp.correlation <= -0.30:
                negatives.append((i, j))
                if cp.correlation <= -0.5:
                    strong_negative += 1

    n_pairs = max(1, len(pairs))
    avg_signed = total_signed / n_pairs

    # Downweight factor: shave 15 % per strong positive pair (fake edge
    # inflation) and 8 % per strong negative pair (mutual exclusion).
    downweight = 1.0
    downweight *= 0.85 ** strong_positive
    downweight *= 0.92 ** strong_negative

    # Merge with heuristic guard (backward compatibility)
    try:
        guard = _guard_analyze(legs) or {}
        for gp in guard.get("blocked_pairs") or []:
            if tuple(gp) not in blocked:
                blocked.append(tuple(gp))
        downweight *= float(guard.get("downweight_factor") or 1.0)
        for w in guard.get("warnings") or []:
            if w not in warnings:
                warnings.append(w)
    except Exception:
        pass

    downweight = max(0.30, min(1.0, downweight))

    if strong_positive >= 2 or blocked:
        tier = "high"
    elif strong_positive >= 1 or strong_negative >= 1:
        tier = "medium"
    elif positives or negatives:
        tier = "low"
    else:
        tier = "none"

    if positives:
        warnings.append(f"{len(positives)} positively correlated leg "
                        f"pair(s) — edge attenuated {int((1-downweight)*100)}%.")
    if negatives:
        warnings.append(f"{len(negatives)} conflicting/negative pair(s) — "
                        "one may cancel the other.")

    return CorrelationReport(
        pairs=pairs, blocked_pairs=blocked,
        positive_pairs=positives, negative_pairs=negatives,
        correlation_score=round(avg_signed, 3),
        downweight_factor=round(downweight, 3),
        warnings=warnings, tier=tier,
    )


def combine_with_guard(legs: list) -> dict:
    """Legacy-compatible shape: guard-report merged with new analyzer.

    Returns a dict — same top-level keys as `correlation_guard.analyze_parlay`
    plus new `pairs`, `positive_pairs`, `negative_pairs`, `correlation_score`.
    """
    report = analyze_correlations(legs)
    return {
        "warnings": report.warnings,
        "blocked_pairs": [list(p) for p in report.blocked_pairs],
        "downweight_factor": report.downweight_factor,
        "correlation_tier": report.tier,
        "pairs": [p.to_dict() for p in report.pairs],
        "positive_pairs": [list(p) for p in report.positive_pairs],
        "negative_pairs": [list(p) for p in report.negative_pairs],
        "correlation_score": report.correlation_score,
    }
