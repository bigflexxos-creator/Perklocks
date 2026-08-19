"""Soccer Game Model — Phase 2A.5B (2026-08).

DELTA CLOSURE — independent Soccer team/game probability core.

Purpose
-------
Before Phase 2A.5B, ``sports_engine`` line ~1335 executed:

    home_model = home_implied

for every Soccer match — the "model" was the sportsbook implied
probability.  That contradicts Phase 1B's independent-model contract.
This module replaces the probability CORE only, reusing existing
``home_form`` / ``away_form`` / ``home_xg_rolling`` / ``away_xg_rolling``
context populated upstream by ``services.game_context``.

Architecture
------------
1. ATTACK / DEFENSE STRENGTH derived from either:
   * real xG rolling (``source != "form_proxy"``) — TIER_A
   * form-derived GF / GA (labeled as GF/GA not xG) — TIER_B
   * only one side available — TIER_C (higher uncertainty)
   * insufficient — TIER_D → MODEL_UNAVAILABLE

2. Home advantage constant: ``+0.20 goals`` (approx league average).
   Regularized toward the league mean of 2.6 goals/match with a small
   K-prior when sample size is thin.

3. Poisson score matrix 0..7 × 0..7 with Dixon-Coles low-score correction
   for (0,0), (0,1), (1,0), (1,1) cells.

4. 1X2 / totals / BTTS / DC probabilities derived from the same matrix —
   downstream markets reuse this ONE distribution instead of each
   inventing their own probability.

Contracts
---------
* Sportsbook odds are NEVER read into λ. Book is MARKET INFORMATION only.
* GF / GA are NEVER labeled xG. When only form_proxy is available,
  ``sources`` carries ``TEAM_STRENGTH`` (not ``EXPECTED_GOALS``).
* Missing xG does not automatically MODEL_UNAVAILABLE — form-derived
  team strength is a legitimate tier.
* Same input → same output (deterministic).
"""
from __future__ import annotations

import logging
import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("lockscore.soccer_game_model")


# ── PERKLOCKS UNIVERSAL SOCCER (2026-06) ─────────────────────────────
# Shared team-name normalization helpers used by ``build_soccer_team_ctx``
# to resolve the Odds API canonical form (``Atlético Madrid``,
# ``Málaga``, ``Deportivo La Coruña``, ``Borussia Mönchengladbach``,
# ``Olympique Lyonnais``) against the ASCII short forms stored in
# ``soccer_matches`` (``Ath Madrid``, ``Malaga``, ``RC Deportivo La
# Coruña``, ``M'gladbach``, ``Lyon``). No fabrication — these are
# strictly retry variants; the primary exact match is still tried
# first.

_TEAM_PREFIX_ALIASES = (
    "AS ", "AC ", "AFC ", "SS ", "SSC ", "SC ", "FC ", "CF ", "CD ",
    "CA ", "CR ", "RC ", "RCD ", "RCA ", "SD ", "UD ", "US ", "UC ",
    "SV ", "SG ", "1. FC ", "1.FC ", "1. FSV ", "TSG ", "VfB ",
    "VfL ", "Real ", "Deportivo ", "Athletic ", "Atlético ",
    "Atletico ", "Ath ", "Club ", "Olympique ", "Stade ",
    "Racing ", "Sporting ",
)
_TEAM_SUFFIX_ALIASES = (
    " FC", " CF", " AFC", " SC", " AC", " Vigo", " Bilbao",
    " Madrid", " City", " Town", " United", " de Barcelona",
    " Lyonnais", " Marseille", " Berlin", " München",
)


def _strip_accents(s: str) -> str:
    """Return ``s`` with combining marks removed (Unicode NFKD → ASCII)."""
    if not s:
        return ""
    nk = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nk if not unicodedata.combining(c))


def _regex_escape(s: str) -> str:
    """Escape user-supplied strings before dropping them into a Mongo
    ``$regex``. Prevents parentheses / dots / apostrophes in team
    names (``M'gladbach``, ``1. FC Köln``) from being interpreted as
    regex metacharacters."""
    return re.escape(s or "")


def _team_name_variants(name: str) -> list[str]:
    """Return ordered team-name retry variants.

    Order is strict — exact first, then accent-stripped, then common
    alias forms. Duplicates are removed while preserving order so
    the primary hit still wins on identical databases.
    """
    if not name:
        return []
    base = name.strip()
    ascii_ = _strip_accents(base)
    variants: list[str] = [base]
    if ascii_ and ascii_ != base:
        variants.append(ascii_)
    # Alias stripping — only apply if the prefix/suffix is actually there.
    for src in list(variants):
        for pfx in _TEAM_PREFIX_ALIASES:
            if src.lower().startswith(pfx.lower()):
                stripped = src[len(pfx):].strip()
                if stripped and stripped not in variants:
                    variants.append(stripped)
        for sfx in _TEAM_SUFFIX_ALIASES:
            if src.lower().endswith(sfx.lower()):
                stripped = src[: -len(sfx)].strip()
                if stripped and stripped not in variants:
                    variants.append(stripped)
    # Named-alias contractions (Odds API long-form → soccer_matches short form).
    # Only applied to the accent-stripped copy of the input so the
    # primary exact match is never skipped.
    _CONTRACTIONS = (
        ("Atletico ",  "Ath "),
        ("Athletic ",  "Ath "),
        ("Olympique Lyonnais", "Lyon"),
        ("Olympique de Marseille", "Marseille"),
        ("Olympique ", ""),
        ("Espanyol",   "Espanol"),
        ("RCD Espanyol de Barcelona", "Espanol"),
        ("Deportivo Alavés", "Alaves"),
        ("Deportivo Alaves", "Alaves"),
        ("Alavés",     "Alaves"),
        ("Cádiz",      "Cadiz"),
        ("Almería",    "Almeria"),
        ("Leganés",    "Leganes"),
        ("RC Deportivo La Coruña", "Deportivo La Coruna"),
        ("Borussia Mönchengladbach", "M'gladbach"),
        ("Bayer Leverkusen",  "Bayer 04 Leverkusen"),
        ("FC Bayern München", "Bayern Munich"),
        ("FC Bayern",  "Bayern Munich"),
    )
    for src, dst in _CONTRACTIONS:
        for i, v in enumerate(list(variants)):
            if src.lower() in v.lower():
                new = re.sub(re.escape(src), dst, v, flags=re.IGNORECASE).strip()
                if new and new not in variants:
                    variants.append(new)
    return variants


def _team_core_token(name: str) -> str:
    """Return the longest single alpha token from ``name`` (used as a
    last-chance ``$regex contains`` seed). Skips generic short tokens
    like ``FC`` / ``CF`` / ``SC`` / ``AC`` / ``AS``."""
    if not name:
        return ""
    ascii_ = _strip_accents(name)
    tokens = re.findall(r"[A-Za-z]+", ascii_)
    _skip = {"fc", "cf", "sc", "ac", "as", "sd", "cd", "ca", "cr",
             "rc", "rcd", "afc", "us", "uc", "sv", "sg", "ss", "ssc",
             "vfb", "vfl", "tsg", "de", "la", "el", "1"}
    candidates = [t for t in tokens if t.lower() not in _skip and len(t) >= 4]
    if not candidates:
        return ""
    # Longest token — typically the distinctive part of the name
    # (``Málaga`` → ``Malaga``, ``Deportivo La Coruña`` → ``Deportivo``,
    # ``Borussia Mönchengladbach`` → ``Monchengladbach``).
    return sorted(candidates, key=len, reverse=True)[0]

# ------------------------------------------------------------------ #
# Constants
# ------------------------------------------------------------------ #
LEAGUE_AVG_GOALS_PER_MATCH = 2.65
LEAGUE_AVG_TEAM_GOALS       = LEAGUE_AVG_GOALS_PER_MATCH / 2.0  # ~1.325 / side
HOME_ADVANTAGE_GOALS        = 0.20     # +0.20 goals extra for home
DIXON_COLES_RHO             = -0.10    # low-score correlation

# Shrinkage priors (regularise thin-sample rate estimates).
STRENGTH_PRIOR_MATCHES      = 6        # phantom matches worth of league avg

# Score matrix bounds.
MAX_GOALS                   = 7

# Evidence categories — used to prevent correlated features from being
# counted as multiple independent confirmations by downstream systems.
EV_TEAM_STRENGTH   = "TEAM_STRENGTH"
EV_EXPECTED_GOALS  = "EXPECTED_GOALS"
EV_RECENT_FORM     = "RECENT_FORM"
EV_LINEUP          = "LINEUP_AVAILABILITY"
EV_REST            = "REST_SCHEDULE"
EV_H2H             = "H2H"
EV_SCORE_MODEL     = "SCORE_MODEL"


# ------------------------------------------------------------------ #
# Types
# ------------------------------------------------------------------ #
@dataclass
class SoccerGameOutputs:
    available: bool
    reason: Optional[str] = None
    tier: str = "D"
    p_home: float = 0.0
    p_draw: float = 0.0
    p_away: float = 0.0
    lambda_home: float = 0.0
    lambda_away: float = 0.0
    uncertainty: float = 0.5
    sources: list[str] = field(default_factory=list)
    evidence_categories: list[str] = field(default_factory=list)
    xg_available: bool = False
    home_strength: dict[str, float] = field(default_factory=dict)
    away_strength: dict[str, float] = field(default_factory=dict)
    # Full 8x8 Poisson matrix — downstream markets consume from here.
    score_matrix: list[list[float]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "reason": self.reason,
            "tier": self.tier,
            "p_home": self.p_home,
            "p_draw": self.p_draw,
            "p_away": self.p_away,
            "lambda_home": self.lambda_home,
            "lambda_away": self.lambda_away,
            "uncertainty": self.uncertainty,
            "sources": list(self.sources),
            "evidence_categories": list(self.evidence_categories),
            "xg_available": self.xg_available,
            "home_strength": dict(self.home_strength),
            "away_strength": dict(self.away_strength),
            "score_matrix_shape": [len(self.score_matrix),
                                   len(self.score_matrix[0]) if self.score_matrix else 0],
        }


# ------------------------------------------------------------------ #
# Team-strength extraction
# ------------------------------------------------------------------ #
def _extract_strength(side: str, ctx: dict) -> dict[str, Any]:
    """Return {'gf':, 'ga':, 'matches':, 'xg_source':, 'has_real_xg': bool}.

    * `side` is "home" or "away".
    * Prefer real xG (from `<side>_xg_rolling` when source != form_proxy).
    * Otherwise fall back to `<side>_form` GF/GA — TAG AS GF/GA, NOT xG.
    """
    xg_key = f"{side}_xg_rolling"
    form_key = f"{side}_form"
    xg_doc = ctx.get(xg_key) or {}
    form_doc = ctx.get(form_key) or {}
    xg_source = str(xg_doc.get("source") or "")
    has_real_xg = bool(xg_doc) and xg_source != "form_proxy" and (
        xg_doc.get("xg_avg") is not None
    )

    if has_real_xg:
        return {
            "gf":       float(xg_doc.get("xg_avg") or 0.0),
            "ga":       float(xg_doc.get("xga_avg") or 0.0),
            "matches":  int(xg_doc.get("matches") or 0),
            "xg_source": xg_source or "real_xg",
            "has_real_xg": True,
            "provenance": EV_EXPECTED_GOALS,
        }
    # ── form_proxy xg_rolling — this IS team strength, NOT xG ────────
    # `services.game_context` populates `<side>_xg_rolling` with
    # `source=form_proxy` when only GF/GA form data is available.  The
    # `xg_avg` / `xga_avg` keys are aliases of `gf_avg` / `ga_avg` in
    # that case — clearly labeled by `xg_available=False`.  We reuse
    # the numeric values but categorise the evidence as TEAM_STRENGTH.
    if (xg_doc and xg_doc.get("xg_avg") is not None
            and xg_doc.get("xga_avg") is not None):
        return {
            "gf":       float(xg_doc.get("gf_avg", xg_doc.get("xg_avg")) or 0.0),
            "ga":       float(xg_doc.get("ga_avg", xg_doc.get("xga_avg")) or 0.0),
            "matches":  int(xg_doc.get("matches") or 0),
            "xg_source": "form_proxy",
            "has_real_xg": False,
            "provenance": EV_TEAM_STRENGTH,
        }
    # Fall back to form-derived GF/GA — clearly labeled as GF/GA.
    if form_doc:
        gf = form_doc.get("gf_avg")
        ga = form_doc.get("ga_avg")
        if isinstance(gf, (int, float)) and isinstance(ga, (int, float)):
            return {
                "gf":       float(gf),
                "ga":       float(ga),
                "matches":  int(form_doc.get("n_matches") or 0),
                "xg_source": "form_gf_ga",
                "has_real_xg": False,
                "provenance": EV_TEAM_STRENGTH,
            }
    return {
        "gf": None, "ga": None, "matches": 0,
        "xg_source": "unavailable",
        "has_real_xg": False,
        "provenance": None,
    }


def _shrink_rate(observed: float, matches: int, prior: float,
                 k: int = STRENGTH_PRIOR_MATCHES) -> float:
    """Blend observed per-match rate toward the league prior."""
    if matches is None or matches <= 0:
        return prior
    w = matches / (matches + k)
    return w * observed + (1.0 - w) * prior


def _dixon_coles_tau(x: int, y: int, lambda_h: float, lambda_a: float,
                     rho: float = DIXON_COLES_RHO) -> float:
    """Dixon-Coles low-score correlation factor."""
    if x == 0 and y == 0:
        return 1.0 - lambda_h * lambda_a * rho
    if x == 0 and y == 1:
        return 1.0 + lambda_h * rho
    if x == 1 and y == 0:
        return 1.0 + lambda_a * rho
    if x == 1 and y == 1:
        return 1.0 - rho
    return 1.0


def _poisson_pmf(k: int, lam: float) -> float:
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def _build_score_matrix(lambda_h: float, lambda_a: float,
                        max_goals: int = MAX_GOALS,
                        rho: float = DIXON_COLES_RHO) -> list[list[float]]:
    """8×8 Poisson score matrix with Dixon-Coles low-score correction."""
    mat: list[list[float]] = []
    total = 0.0
    for x in range(max_goals + 1):
        row = []
        for y in range(max_goals + 1):
            base = _poisson_pmf(x, lambda_h) * _poisson_pmf(y, lambda_a)
            tau  = _dixon_coles_tau(x, y, lambda_h, lambda_a, rho)
            cell = max(0.0, base * tau)
            row.append(cell)
            total += cell
        mat.append(row)
    # Normalise so probabilities sum to exactly 1.
    if total > 0:
        for x in range(max_goals + 1):
            for y in range(max_goals + 1):
                mat[x][y] /= total
    return mat


# ------------------------------------------------------------------ #
# Public API
# ------------------------------------------------------------------ #
def estimate_soccer_game_probabilities(
    ctx: Optional[dict],
    home: str,
    away: str,
    *,
    home_advantage: float = HOME_ADVANTAGE_GOALS,
    league_avg_team_goals: float = LEAGUE_AVG_TEAM_GOALS,
) -> SoccerGameOutputs:
    """Return authoritative independent Soccer game probabilities.

    Never reads sportsbook odds.  Never fabricates xG from GF/GA.
    """
    ctx = ctx or {}
    h = _extract_strength("home", ctx)
    a = _extract_strength("away", ctx)

    # ── Tier determination ─────────────────────────────────────────
    both_missing = (h["gf"] is None and a["gf"] is None)
    one_missing  = (h["gf"] is None) ^ (a["gf"] is None)
    if both_missing:
        return SoccerGameOutputs(
            available=False, reason="INSUFFICIENT_HISTORY", tier="D",
            uncertainty=0.90,
        )

    if h["has_real_xg"] and a["has_real_xg"]:
        tier = "A"
        uncertainty_base = 0.10
    elif (h["has_real_xg"] or a["has_real_xg"]) and not one_missing:
        tier = "B"
        uncertainty_base = 0.20
    elif not one_missing:
        tier = "B"
        uncertainty_base = 0.22
    else:
        tier = "C"
        uncertainty_base = 0.35

    # ── Fill missing side with league prior when only one side has data.
    for row in (h, a):
        if row["gf"] is None:
            row["gf"] = league_avg_team_goals
            row["ga"] = league_avg_team_goals
            row["matches"] = 0

    # ── Sample-size shrinkage ─────────────────────────────────────
    h_gf = _shrink_rate(h["gf"], h["matches"], league_avg_team_goals)
    h_ga = _shrink_rate(h["ga"], h["matches"], league_avg_team_goals)
    a_gf = _shrink_rate(a["gf"], a["matches"], league_avg_team_goals)
    a_ga = _shrink_rate(a["ga"], a["matches"], league_avg_team_goals)

    # ── Attack / defense multipliers (relative to league average) ─
    ha_attack = h_gf / max(0.20, league_avg_team_goals)
    ha_def    = h_ga / max(0.20, league_avg_team_goals)
    aw_attack = a_gf / max(0.20, league_avg_team_goals)
    aw_def    = a_ga / max(0.20, league_avg_team_goals)

    # λ_home = league_avg * home_attack * away_defensive_weakness + home_advantage
    lambda_home = league_avg_team_goals * ha_attack * aw_def + home_advantage
    lambda_away = league_avg_team_goals * aw_attack * ha_def
    lambda_home = max(0.05, min(6.0, lambda_home))
    lambda_away = max(0.05, min(6.0, lambda_away))

    # ── Score matrix ─────────────────────────────────────────────
    mat = _build_score_matrix(lambda_home, lambda_away)

    # ── 1X2 probabilities from the matrix ────────────────────────
    p_home = 0.0
    p_away = 0.0
    p_draw = 0.0
    for x in range(len(mat)):
        for y in range(len(mat[x])):
            cell = mat[x][y]
            if x > y:  p_home += cell
            elif x < y: p_away += cell
            else:       p_draw += cell
    # Normalise (should already be 1.0 by construction).
    total = p_home + p_draw + p_away
    if total > 0:
        p_home /= total; p_draw /= total; p_away /= total

    # ── Provenance / evidence categories ─────────────────────────
    ev_cats: set[str] = {EV_SCORE_MODEL}
    sources: list[str] = ["soccer_game_model_v1"]
    if h["has_real_xg"] or a["has_real_xg"]:
        ev_cats.add(EV_EXPECTED_GOALS)
        sources.append(f"xg_source:home={h['xg_source']}")
        sources.append(f"xg_source:away={a['xg_source']}")
    else:
        ev_cats.add(EV_TEAM_STRENGTH)
        sources.append("gf_ga_form_proxy")

    # Uncertainty grows with sample size gap.
    matches_gap = abs((h["matches"] or 0) - (a["matches"] or 0))
    unc_bonus = 0.10 if matches_gap >= 10 else (0.05 if matches_gap >= 5 else 0.0)

    return SoccerGameOutputs(
        available=True,
        tier=tier,
        p_home=round(p_home, 4),
        p_draw=round(p_draw, 4),
        p_away=round(p_away, 4),
        lambda_home=round(lambda_home, 4),
        lambda_away=round(lambda_away, 4),
        uncertainty=round(min(0.75, uncertainty_base + unc_bonus), 3),
        sources=sources,
        evidence_categories=sorted(ev_cats),
        xg_available=(h["has_real_xg"] or a["has_real_xg"]),
        home_strength={
            "gf": round(h_gf, 3), "ga": round(h_ga, 3),
            "matches": h["matches"],
        },
        away_strength={
            "gf": round(a_gf, 3), "ga": round(a_ga, 3),
            "matches": a["matches"],
        },
        score_matrix=mat,
    )


# ------------------------------------------------------------------ #
# Derived market probabilities (reuse the ONE distribution)
# ------------------------------------------------------------------ #
def totals_from_matrix(mat: list[list[float]], line: float) -> tuple[float, float]:
    """Return (P(Over line), P(Under line)) from score matrix.
    Push (X + Y == line) is treated as neutral (0.5 each) for integer
    lines, but Soccer lines are always half-integer in practice.
    """
    p_over = 0.0
    p_under = 0.0
    p_push = 0.0
    for x in range(len(mat)):
        for y in range(len(mat[x])):
            tot = x + y
            cell = mat[x][y]
            if tot > line:      p_over += cell
            elif tot < line:    p_under += cell
            else:               p_push += cell
    if p_push > 0:
        p_over  += p_push * 0.5
        p_under += p_push * 0.5
    return round(p_over, 4), round(p_under, 4)


def btts_from_matrix(mat: list[list[float]]) -> tuple[float, float]:
    """Return (P(BTTS Yes), P(BTTS No))."""
    p_yes = 0.0
    for x in range(1, len(mat)):
        for y in range(1, len(mat[x])):
            p_yes += mat[x][y]
    p_yes = max(0.0, min(1.0, p_yes))
    return round(p_yes, 4), round(1.0 - p_yes, 4)


def double_chance_from_1x2(p_home: float, p_draw: float,
                            p_away: float) -> dict[str, float]:
    """Return P(1X), P(X2), P(12) from 1X2 probabilities."""
    return {
        "1X": round(p_home + p_draw, 4),
        "X2": round(p_draw + p_away, 4),
        "12": round(p_home + p_away, 4),
    }


# ------------------------------------------------------------------ #
# Universal DB-backed entry point — Phase 2A.5 UNIVERSAL
# ------------------------------------------------------------------ #
async def build_soccer_team_ctx(
    db, *, home_team: str, away_team: str, league: str = "",
) -> dict[str, Any]:
    """Assemble the ``ctx`` dict expected by
    :func:`estimate_soccer_game_probabilities` from the existing
    Perklocks collections.

    Resolution order (never fabricates):
        1. ``soccer_team_form`` — pre-aggregated rolling form.
        2. ``team_form`` (multi-sport) filtered to Soccer.
        3. ``soccer_matches`` — 25k+ real historical matches with GF/GA
           per team.  Derive rolling form from the last 20 matches on
           the fly when no pre-agg row exists.  This is the fallback
           that unlocks Phase 2A.5 UNIVERSAL end-to-end game-market
           coverage without any team_form backfill job.

    A team with no rolling data yields None GF/GA which the estimator
    handles via league-average priors.  League name is passed through
    so future extensions can filter by competition.

    Per-process cache: bulk_odds ingest generates 20-40 game-market
    outcomes per fixture, all sharing the same two teams.  A 60s TTL
    memoisation collapses the 3-4 DB round-trips per team into ONE
    per fixture per minute — this is the difference between a 30-90s
    startup ingest and a 3-5s one.

    PERKLOCKS UNIVERSAL SOCCER (2026-06):
      Team-name resolution now retries with an accent-stripped form
      and a small set of shared prefix/suffix aliases before giving
      up. This closes the La Liga / Ligue 1 / Bundesliga / Serie A
      NO_TEAM_CONTEXT bleed where the Odds API sends the accented
      canonical form (``Atlético Madrid``, ``Málaga``,
      ``Borussia Mönchengladbach``, ``Deportivo La Coruña``) while
      ``soccer_matches`` stores the ASCII short form
      (``Ath Madrid``, ``Malaga``, ``M'gladbach``, ``RC Deportivo
      La Coruña``). The variants are tried in strict order and only
      as fallbacks — never as overrides — so legitimate hits keep
      their exact match.
    """
    import time
    global _CTX_CACHE, _CTX_CACHE_TS
    _now = time.monotonic()
    # Purge whole cache every 60s (cheap; keeps memory bounded).
    if _now - _CTX_CACHE_TS > 60:
        _CTX_CACHE.clear()
        _CTX_CACHE_TS = _now
    _key = ((home_team or "").strip().lower(),
            (away_team or "").strip().lower(),
            (league or "").strip().lower())
    if _key in _CTX_CACHE:
        return _CTX_CACHE[_key]

    ctx: dict[str, Any] = {}

    for side, team_name in (("home", home_team), ("away", away_team)):
        if not team_name:
            continue
        row = None
        variants = _team_name_variants(team_name)
        for variant in variants:
            try:
                row = await db.soccer_team_form.find_one({
                    "team_canonical": variant.lower(),
                })
            except Exception:
                row = None
            if row:
                break
        if not row:
            for variant in variants:
                try:
                    row = await db.team_form.find_one({
                        "sport": "Soccer",
                        "team": {"$regex": f"^{_regex_escape(variant)}$", "$options": "i"},
                    })
                except Exception:
                    row = None
                if row:
                    break
        if row:
            gf = row.get("gf_per_match") or row.get("gf_avg") or row.get("gf") or row.get("goals_for_per_game")
            ga = row.get("ga_per_match") or row.get("ga_avg") or row.get("ga") or row.get("goals_against_per_game")
            matches = int(row.get("matches") or row.get("n_matches") or row.get("games") or 0)
            ctx[f"{side}_form"] = {
                "gf_avg":    float(gf) if gf is not None else None,
                "ga_avg":    float(ga) if ga is not None else None,
                "n_matches": matches,
                "matches":   matches,
                "source":    row.get("source") or "team_form",
            }
        else:
            # ── Fallback: derive from raw historical matches ──────
            # Try every team-name variant (exact, then accent-stripped,
            # then contains-match) before giving up.
            docs = None
            resolved_variant = None
            for variant in variants:
                try:
                    match_filter: dict[str, Any] = {
                        "$or": [
                            {"home_team": {"$regex": f"^{_regex_escape(variant)}$", "$options": "i"}},
                            {"away_team": {"$regex": f"^{_regex_escape(variant)}$", "$options": "i"}},
                        ],
                        "home_score": {"$exists": True, "$ne": None},
                        "away_score": {"$exists": True, "$ne": None},
                    }
                    d = await db.soccer_matches.find(match_filter).sort(
                        [("date", -1)]
                    ).limit(20).to_list(20)
                    if d:
                        docs = d
                        resolved_variant = variant
                        break
                except Exception as _mm_err:
                    logger.debug(
                        "soccer_matches rollup failed for %s (variant=%r): %s",
                        team_name, variant, _mm_err,
                    )
            # Last-chance contains match for compound names
            # (e.g. "Bayern Munich" ~ "FC Bayern München").
            if not docs:
                try:
                    core = _team_core_token(team_name)
                    if core and len(core) >= 4:
                        d = await db.soccer_matches.find({
                            "$or": [
                                {"home_team": {"$regex": _regex_escape(core), "$options": "i"}},
                                {"away_team": {"$regex": _regex_escape(core), "$options": "i"}},
                            ],
                            "home_score": {"$exists": True, "$ne": None},
                            "away_score": {"$exists": True, "$ne": None},
                        }).sort([("date", -1)]).limit(20).to_list(20)
                        if d:
                            docs = d
                            resolved_variant = f"~contains:{core}"
                except Exception:
                    pass
            if docs:
                tot_gf = 0.0
                tot_ga = 0.0
                n = 0
                # Resolve which side each doc puts the team on by
                # checking against the resolved variant (or the
                # original team name if we matched via contains).
                match_target = (resolved_variant or team_name).strip().lower()
                # Strip leading "~contains:" marker for token compares.
                if match_target.startswith("~contains:"):
                    match_target = match_target[len("~contains:"):]
                for d in docs:
                    hs = d.get("home_score")
                    as_ = d.get("away_score")
                    if hs is None or as_ is None:
                        continue
                    try:
                        hs = float(hs); as_ = float(as_)
                    except Exception:
                        continue
                    ht = (d.get("home_team") or "").strip().lower()
                    at = (d.get("away_team") or "").strip().lower()
                    if match_target in ht or ht in match_target:
                        tot_gf += hs; tot_ga += as_
                    elif match_target in at or at in match_target:
                        tot_gf += as_; tot_ga += hs
                    else:
                        # Ambiguous match — skip this row rather than
                        # attribute goals to the wrong side.
                        continue
                    n += 1
                if n:
                    ctx[f"{side}_form"] = {
                        "gf_avg":     tot_gf / n,
                        "ga_avg":     tot_ga / n,
                        "n_matches":  n,
                        "matches":    n,
                        "source":     "soccer_matches_rolling20",
                    }
        # xG rolling window (optional).
        try:
            xg = None
            for variant in variants:
                xg = await db.soccer_team_xg_rolling.find_one({
                    "team_canonical": variant.lower(),
                })
                if xg:
                    break
            if xg:
                ctx[f"{side}_xg_rolling"] = {
                    "xg_for":     float(xg.get("xg_for") or 0),
                    "xg_against": float(xg.get("xg_against") or 0),
                    "matches":    int(xg.get("matches") or 0),
                    "source":     xg.get("source") or "xg_rolling",
                }
        except Exception:
            pass

    if league:
        ctx["league"] = league

    # ─── MLS ADAPTER (SOCCER_UNIVERSAL_RUNTIME 2026-09) ───────────
    # For any team where the primary resolution chain
    # (`soccer_team_form` → `team_form` → `soccer_matches` rolling
    # 20) yielded no `<side>_form`, fall through to an MLS-specific
    # adapter that derives per-team GF/GA from data already present
    # in production stores:
    #   • GF: espn_mls_stats top-scorer aggregation (goals ÷ max
    #     games).  Top scorers reliably capture ≥ 55 % of team
    #     goals — used as a relative-strength signal, not fabricated.
    #   • GA: player_game_actuals grouped by opponent_name+date
    #     (goals conceded per match), aggregated over the season.
    # ESPN identity remains ENRICHMENT ONLY.  The canonical event /
    # team identity from the odds provider is preserved on the pick
    # doc — this adapter only supplies team-form NUMERICS so the
    # existing Soccer engine can evaluate.
    for side, team_name in (("home", home_team), ("away", away_team)):
        if ctx.get(f"{side}_form") or not team_name:
            continue
        try:
            mls_row = await _mls_form_adapter(db, team_name)
        except Exception as _mls_err:
            logger.debug(
                "MLS adapter failed for %s: %s", team_name, _mls_err,
            )
            mls_row = None
        if mls_row:
            ctx[f"{side}_form"] = mls_row

    _CTX_CACHE[_key] = ctx
    return ctx


# ------------------------------------------------------------------ #
# MLS team-form adapter — SOCCER_UNIVERSAL_RUNTIME
# ------------------------------------------------------------------ #
# Per-process cache (60-s TTL) so the 20-40 game-market outcomes for
# one MLS fixture share ONE lookup per team.
_MLS_FORM_CACHE: dict[str, dict[str, Any]] = {}
_MLS_FORM_CACHE_TS: float = 0.0


def _mls_alias_match(a: str, b: str) -> bool:
    """Wrapper around ``services.mls_direct_inject._team_match`` that
    returns False on any import error (adapter must never explode)."""
    if not a or not b:
        return False
    try:
        from services.mls_direct_inject import _team_match
        return bool(_team_match(a, b))
    except Exception:
        return a.strip().lower() == b.strip().lower()


async def _mls_form_adapter(db, team_name: str) -> Optional[dict[str, Any]]:
    """Return an MLS team-form dict derived from existing production
    stores.  Never fabricates statistics.

    * GF: ``espn_mls_stats`` top-scorer aggregation.  Real season
      goals divided by the maximum player-games count for the team
      (best available proxy for team-games in MLS).  This is a
      relative-strength signal; the estimator's league-mean shrinkage
      handles absolute-scale distortion.
    * GA: ``player_game_actuals`` opponent-view aggregation.  Every
      row where ``opponent_name`` matches this team is a goal an
      opposing player scored AGAINST the team; grouping by (event
      date × opponent-name) yields per-match totals.  Distinct dates
      = matches played.
    """
    import time
    global _MLS_FORM_CACHE, _MLS_FORM_CACHE_TS
    _now = time.monotonic()
    if _now - _MLS_FORM_CACHE_TS > 60:
        _MLS_FORM_CACHE.clear()
        _MLS_FORM_CACHE_TS = _now
    key = team_name.strip().lower()
    if key in _MLS_FORM_CACHE:
        return _MLS_FORM_CACHE[key]

    # ── GF from espn_mls_stats (top-scorer aggregation) ──────────
    gf_avg: Optional[float] = None
    max_games: int = 0
    total_goals: int = 0
    try:
        espn_docs = await db.espn_mls_stats.find(
            {}, {"team": 1, "goals": 1, "games": 1},
        ).to_list(500)
    except Exception:
        espn_docs = []
    matching = [d for d in espn_docs
                if _mls_alias_match(d.get("team") or "", team_name)]
    if matching:
        for d in matching:
            try:
                total_goals += int(d.get("goals") or 0)
            except Exception:
                pass
            try:
                g = int(d.get("games") or 0)
                if g > max_games:
                    max_games = g
            except Exception:
                pass
        if max_games > 0:
            # Raw top-scorer rate as team-strength proxy.  Do NOT
            # scale to a league average — that would introduce
            # fabricated absolute numbers.  Downstream Poisson
            # shrinkage regularises this against the league prior.
            gf_avg = total_goals / max_games

    # ── GA from player_game_actuals opponent-view ───────────────
    ga_avg: Optional[float] = None
    n_matches: int = 0
    try:
        opp_names = await db.player_game_actuals.distinct(
            "opponent_name",
            {"sport": "soccer", "competition": "MLS"},
        )
    except Exception:
        opp_names = []
    matching_opps = [o for o in opp_names
                      if _mls_alias_match(o, team_name)]
    if matching_opps:
        try:
            pipeline = [
                {"$match": {
                    "sport": "soccer",
                    "competition": "MLS",
                    "opponent_name": {"$in": matching_opps},
                }},
                # Group by (opponent_name × date-substring-of-event_id).
                # event_id format: "mls-{pid}-{oppid}-YYYY-MM-DD"
                {"$group": {
                    "_id": {
                        "opp":  "$opponent_name",
                        "date": {"$substrBytes": [
                            "$event_id",
                            {"$subtract": [{"$strLenBytes": "$event_id"}, 10]},
                            10,
                        ]},
                    },
                    "conceded": {"$sum": {"$ifNull": ["$actuals.goals", 0]}},
                }},
            ]
            match_rows = await db.player_game_actuals.aggregate(
                pipeline
            ).to_list(1000)
        except Exception:
            match_rows = []
        if match_rows:
            n_matches = len(match_rows)
            total_ga = sum(float(r.get("conceded") or 0) for r in match_rows)
            ga_avg = total_ga / n_matches if n_matches else None

    # Emit a form row only when at least one signal is present.
    if gf_avg is None and ga_avg is None:
        _MLS_FORM_CACHE[key] = None  # type: ignore
        return None

    # SOCCER_FINAL_RUNTIME_INTEGRITY (2026-09) §15 — do NOT mirror
    # missing attacking evidence from defensive evidence (or vice
    # versa).  Missing GF is not equivalent to GA.  Leave the side
    # None and let the existing engine's league-prior shrinkage
    # handle the gap explicitly.  Prior behavior (mirror-fill) is
    # removed.
    row = {
        "gf_avg":    float(gf_avg) if gf_avg is not None else None,
        "ga_avg":    float(ga_avg) if ga_avg is not None else None,
        "n_matches": max(max_games, n_matches),
        "matches":   max(max_games, n_matches),
        "source":    "mls_espn_stats+player_game_actuals",
    }
    _MLS_FORM_CACHE[key] = row
    return row


# Module-level per-process ctx cache (60s TTL, cleared on read).
_CTX_CACHE: dict[tuple[str, str, str], Any] = {}
_CTX_CACHE_TS: float = 0.0


async def compute_game_market_prob(
    db, *,
    home_team: str,
    away_team: str,
    league: str,
    market_key: str,
    selection: str,
    line: Optional[float] = None,
) -> Optional[float]:
    """Universal per-market probability entry point used by the
    real-line Soccer ingester.

    Returns the model probability the given (market, selection, line)
    hits — never the sportsbook implied.  Returns None when the
    model has insufficient inputs (caller tags NO_MODEL_PROBABILITY).
    """
    if not (home_team and away_team):
        return None
    ctx = await build_soccer_team_ctx(
        db, home_team=home_team, away_team=away_team, league=league,
    )
    out = estimate_soccer_game_probabilities(ctx, home_team, away_team)
    if not out.available or not out.score_matrix:
        return None

    mk = (market_key or "").lower()
    sel = (selection or "").strip().lower()

    # ── 1X2 / Match Winner ────────────────────────────────────────
    if mk == "h2h":
        if sel in ("draw", "tie", "x"):
            return out.p_draw
        # Match against team names — best effort.
        if home_team and sel == home_team.strip().lower():
            return out.p_home
        if away_team and sel == away_team.strip().lower():
            return out.p_away
        # Bookmaker may return "1"/"2" or side labels.
        if sel in ("home", "1"):
            return out.p_home
        if sel in ("away", "2"):
            return out.p_away
        return None

    # ── Double Chance ──────────────────────────────────────────────
    if mk == "double_chance":
        dc = double_chance_from_1x2(out.p_home, out.p_draw, out.p_away)
        # Sportsbooks label them "Home/Draw" ("1X"), "Draw/Away" ("X2"),
        # "Home/Away" ("12").
        aliases = {
            "1x": "1X", "home/draw": "1X", "home or draw": "1X",
            "x2": "X2", "draw/away": "X2", "draw or away": "X2",
            "12": "12", "home/away": "12", "home or away": "12",
        }
        key = aliases.get(sel)
        if key:
            return dc[key]
        return None

    # ── Totals (Over / Under) ─────────────────────────────────────
    if mk in ("totals", "alternate_totals"):
        if line is None:
            return None
        p_over, p_under = totals_from_matrix(out.score_matrix, float(line))
        if sel.startswith("over") or sel == "o":
            return p_over
        if sel.startswith("under") or sel == "u":
            return p_under
        return None

    # ── BTTS ──────────────────────────────────────────────────────
    if mk in ("btts", "both_teams_to_score"):
        p_yes, p_no = btts_from_matrix(out.score_matrix)
        if sel in ("yes", "y"):
            return p_yes
        if sel in ("no", "n"):
            return p_no
        return None

    # ── Spread / Asian Handicap ────────────────────────────────────
    if mk in ("spreads", "alternate_spreads"):
        if line is None:
            return None
        # Handicap applies to the *selection* side.  Positive handicap
        # → selection can lose by |line| goals; negative → must win
        # by more than |line|.
        # Determine which team the selection references.
        sel_is_home = (home_team and sel == home_team.strip().lower())
        sel_is_away = (away_team and sel == away_team.strip().lower())
        if not (sel_is_home or sel_is_away):
            return None
        mat = out.score_matrix
        p = 0.0
        for x in range(len(mat)):
            for y in range(len(mat[x])):
                cell = mat[x][y]
                if sel_is_home:
                    diff = (x + line) - y  # home + handicap - away
                else:
                    diff = (y + line) - x
                if diff > 0:
                    p += cell
                elif diff == 0:
                    p += cell * 0.5   # push credit for asian half
        return round(max(0.0, min(1.0, p)), 4)

    return None



__all__ = [
    "estimate_soccer_game_probabilities",
    "totals_from_matrix",
    "btts_from_matrix",
    "double_chance_from_1x2",
    "compute_game_market_prob",
    "build_soccer_team_ctx",
    "SoccerGameOutputs",
    "EV_TEAM_STRENGTH",
    "EV_EXPECTED_GOALS",
    "EV_SCORE_MODEL",
    "EV_RECENT_FORM",
    "EV_H2H",
]
