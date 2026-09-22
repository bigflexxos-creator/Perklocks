"""Universal NFL prop distribution + threshold + best-bet discovery engine.

Reuses:
  * services/player_history/service.get_player_history for L5/L10/L20 + quantiles
  * services/platinum_nfl/game_runtime.build_nfl_game_model_context for game context
  * services/alt_lines_feed for real sportsbook thresholds
  * services/espn_injury_notes for availability (via DefaultAvailabilityProvider)

Never manufactures a line.  Never suppresses a legitimate real-line prop.
Weather/injury enrichment degrades CONFIDENCE only, never admission.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Optional

from .adapters import (
    DefaultWeatherProvider, DefaultAvailabilityProvider,
    WeatherReport, AvailabilityReport,
    WEATHER_STATUS_AVAILABLE, WEATHER_STATUS_UNAVAILABLE,
    INJURY_STATUS_AVAILABLE, INJURY_STATUS_PARTIAL, INJURY_STATUS_UNAVAILABLE,
)


# Markets supported by this pass — the engine will discover the best
# threshold across every real sportsbook alt line found for these
# markets.  Adding a market later is a data-only change.
SUPPORTED_MARKETS = {
    "pass_yds":       {"direction": "over", "position_hint": "QB"},
    "pass_attempts":  {"direction": "over", "position_hint": "QB"},
    "pass_completions": {"direction": "over", "position_hint": "QB"},
    "rush_yds":       {"direction": "over", "position_hint": "RB"},
    "rush_attempts":  {"direction": "over", "position_hint": "RB"},
    "rec_yds":        {"direction": "over", "position_hint": "WR"},
    "receptions":     {"direction": "over", "position_hint": "WR"},
    "anytime_td":     {"direction": "over", "position_hint": "ANY"},
}


@dataclass
class Distribution:
    """Coherent per-player-per-market distribution."""
    market: str
    sample_size: int
    mean: Optional[float] = None
    variance: Optional[float] = None
    q10: Optional[float] = None
    q25: Optional[float] = None
    median: Optional[float] = None
    q75: Optional[float] = None
    q90: Optional[float] = None
    values: list = field(default_factory=list)
    provenance: str = ""

    def hit_probability_over(self, line: float) -> Optional[float]:
        """P(actual > line).  Empirical from stored values when sample>=5,
        else Gaussian fallback using mean+variance."""
        if line is None:
            return None
        if self.values and len(self.values) >= 5:
            n_hits = sum(1 for v in self.values if v is not None and float(v) > float(line))
            return n_hits / len(self.values)
        if self.mean is None or self.variance is None or self.variance <= 0:
            return None
        # Gaussian approximation
        sd = math.sqrt(self.variance)
        z = (float(line) - self.mean) / sd
        # 1 - Phi(z)
        return 0.5 * (1.0 - math.erf(z / math.sqrt(2)))

    def floor_distance(self, line: float) -> Optional[float]:
        """Q25 - line — positive means the sportsbook line sits BELOW
        the player's 25th percentile (a high-floor opportunity)."""
        if self.q25 is None or line is None:
            return None
        return round(float(self.q25) - float(line), 2)


@dataclass
class ThresholdEvaluation:
    market: str
    line: float
    odds: Optional[int]
    sportsbook: Optional[str]
    hit_probability_raw: Optional[float]
    hit_probability_monotonic: Optional[float]
    floor_distance: Optional[float]
    implied_probability: Optional[float]
    edge_pp: Optional[float]

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class PlayerEvaluation:
    player_name: str
    canonical_player_id: Optional[str]
    position: Optional[str]
    team: Optional[str]
    opponent: Optional[str]
    game_context: dict
    weather: dict
    availability: dict
    distributions_by_market: dict          # market -> Distribution.as_dict()
    thresholds: list                       # every real-line evaluation
    safest_bet: Optional[dict]
    best_value: Optional[dict]
    confidence: float                       # 0..1 — down-weights by data quality
    confidence_breakdown: dict
    frozen_at: str

    def as_dict(self) -> dict:
        d = asdict(self)
        d["distributions_by_market"] = {k: (v if isinstance(v, dict) else v.__dict__)
                                        for k, v in self.distributions_by_market.items()}
        return d


# ─────────────────────────────────────────────────────────────────────

async def build_distribution(db, *, player_name: str, canonical_player_id: Optional[str],
                              market: str, opponent: Optional[str] = None,
                              as_of: Optional[str] = None) -> Distribution:
    """Build a per-player, per-market distribution from PerkLocks'
    canonical NFL historical actuals (``player_game_actuals``).

    Root Closure — NFL Props 2.0 (2026-06-21):
      * Resolves the free-form sportsbook market string via the
        canonical NFL market→stat mapper.
      * Reads directly from ``db.player_game_actuals`` with sport="nfl".
      * As-of safe — only games with ``event_time < as_of`` are used.
      * Small-sample tolerant — a player with 6 games still gets a
        Distribution; sample_size reflects the truth.

    No fake values are ever manufactured; when the underlying stat is
    absent we return sample_size=0 (probabilities remain None
    downstream — never fabricated).
    """
    from .nfl_stat_mapping import resolve_market_to_stat, actuals_value

    stat_key = resolve_market_to_stat(market)
    if not stat_key:
        return Distribution(market=market, sample_size=0,
                             provenance=f"nfl_stat_mapping:unresolved:{market[:40]}")

    # Build the as-of filter.  Prefer canonical_player_id, fall back to
    # player_name.  Never fabricate a canonical id.
    query: dict = {"sport": "nfl"}
    if canonical_player_id:
        query["canonical_player_id"] = canonical_player_id
    elif player_name:
        query["player_name"] = {"$regex": f"^{player_name}$", "$options": "i"}
    else:
        return Distribution(market=market, sample_size=0,
                             provenance="nfl_actuals:no_player_key")
    if as_of:
        query["event_time"] = {"$lt": as_of}

    docs = await db.player_game_actuals.find(
        query, {"_id": 0, "event_time": 1, "actuals": 1, "season": 1, "week": 1},
    ).sort("event_time", -1).to_list(length=40)   # L20 window, buffered

    numeric_values: list[float] = []
    for d in docs:
        v = actuals_value(d.get("actuals") or {}, stat_key)
        if v is not None:
            numeric_values.append(v)

    n = len(numeric_values)
    if n == 0:
        return Distribution(market=market, sample_size=0,
                             provenance=f"nfl_actuals:{stat_key}:no_samples")

    # Prefer the most recent L20 window when we have >20 games; keep the
    # tail otherwise (small-sample tolerant per the spec).
    window = numeric_values[:20]
    slice_name = f"nfl_actuals:{stat_key}:l{len(window)}"

    mean = sum(window) / len(window)
    variance = None
    if len(window) >= 2:
        try:
            variance = statistics.variance(window)
        except statistics.StatisticsError:
            variance = None
    # Quantiles — use empirical percentiles on the window.
    sorted_w = sorted(window)
    def _p(pct: float) -> float:
        if not sorted_w:
            return 0.0
        idx = min(len(sorted_w) - 1,
                  max(0, int(round((pct / 100.0) * (len(sorted_w) - 1)))))
        return sorted_w[idx]

    return Distribution(
        market=market,
        sample_size=len(window),
        mean=mean,
        variance=variance,
        q10=_p(10),
        q25=_p(25),
        median=_p(50),
        q75=_p(75),
        q90=_p(90),
        values=window,
        provenance=slice_name,
    )


def enforce_monotonicity(evals: list[ThresholdEvaluation]) -> list[ThresholdEvaluation]:
    """For a set of ordered-line evaluations (same player, same market),
    enforce P(t1) >= P(t2) whenever t1 < t2.  Uses pool-adjacent-violators
    (isotonic) monotonicity — never manufactures a probability, only
    clips against neighbours."""
    if not evals:
        return evals
    # Sort by ascending line
    ordered = sorted(evals, key=lambda x: x.line)
    # Backwards pass: each threshold's probability must be >= next threshold's
    max_next = None
    for e in reversed(ordered):
        if e.hit_probability_raw is None:
            e.hit_probability_monotonic = None
            continue
        p = float(e.hit_probability_raw)
        if max_next is not None:
            p = max(p, max_next)
        e.hit_probability_monotonic = p
        max_next = p
    return ordered


def _implied_prob(american_odds: Optional[int]) -> Optional[float]:
    if american_odds is None:
        return None
    try:
        a = int(american_odds)
    except (TypeError, ValueError):
        return None
    if a == 0:
        return None
    if a > 0:
        return 100.0 / (a + 100.0)
    return abs(a) / (abs(a) + 100.0)


async def evaluate_player_across_markets(db, *, player_name: str,
                                          canonical_player_id: Optional[str],
                                          position: Optional[str],
                                          team: Optional[str],
                                          opponent: Optional[str],
                                          game: dict,
                                          weather_provider=None,
                                          availability_provider=None) -> PlayerEvaluation:
    """Universal per-player evaluator.

    Pipeline:
      GAME CONTEXT  → WEATHER  → AVAILABILITY  → per-market distribution
      → all real alt lines  → threshold evaluations (monotonic)
      → safest bet + best value  → frozen snapshot
    """
    weather_provider     = weather_provider or DefaultWeatherProvider()
    availability_provider = availability_provider or DefaultAvailabilityProvider()

    # ── GAME CONTEXT ─────────────────────────────────────────────
    game_ctx = {}
    try:
        from services.platinum_nfl.game_runtime import build_nfl_game_model_context
        game_ctx = await build_nfl_game_model_context(game or {})
    except Exception as e:
        game_ctx = {"nfl_model_available": False, "nfl_model_reason": f"error:{type(e).__name__}"}

    weather = await weather_provider.report_for_game(db, game or {})
    availability = await availability_provider.report_for_player(
        db, player_name=player_name, team=team or "", sport="nfl",
    )

    # ── CONFIDENCE DOWN-WEIGHTS ──────────────────────────────────
    # Root Closure (2026-06-21): missing WEATHER data does NOT
    # penalize confidence — the spec forbids suppressing NFL props
    # merely because PerkLocks does not yet ingest a weather feed.
    # Weather can only modify confidence when the provider actually
    # returned trustworthy weather DATA that suggests degraded
    # conditions (e.g. sustained 20+ mph wind).  A neutral / indoor /
    # UNAVAILABLE report contributes weather multiplier = 1.0.
    confidence_breakdown = {
        "game_context": 1.0 if game_ctx.get("nfl_model_available") else 0.9,
        "weather":      _weather_confidence_multiplier(weather),
        "availability": {
            INJURY_STATUS_AVAILABLE:   1.0,
            INJURY_STATUS_PARTIAL:     1.0,   # PARTIAL is honest, not penalised
            INJURY_STATUS_UNAVAILABLE: 1.0,   # feed offline ≠ player suppressed
        }.get(availability.status, 1.0),
    }
    # Explicit designations DO modify confidence — QUESTIONABLE 0.65 etc.
    if availability.availability_probability is not None:
        confidence_breakdown["availability_prob"] = availability.availability_probability
    else:
        confidence_breakdown["availability_prob"] = 1.0
    confidence = 1.0
    for v in confidence_breakdown.values():
        confidence *= float(v)
    confidence = round(min(1.0, max(0.0, confidence)), 4)

    # ── DISCOVER THIS PLAYER'S REAL MARKETS FROM THE BOARD ──────
    # Universal Closure rule: use REAL market strings from the current
    # slate, never a hard-coded list.  This keeps us aligned with
    # whatever taxonomy the ingestion writes into db.picks.
    real_markets_cursor = db.picks.find(
        {"sport": "NFL",
         "elite_player_name": {"$regex": f"^{player_name}$", "$options": "i"},
         "off_board": {"$ne": True}, "no_bet": {"$ne": True}},
        {"_id": 0, "market": 1, "line": 1, "book_odds": 1,
         "sportsbook": 1, "is_alt": 1, "id": 1, "selection": 1},
    )
    real_rows = await real_markets_cursor.to_list(length=200)
    # Group rows by market string.
    from collections import defaultdict
    rows_by_market: dict[str, list[dict]] = defaultdict(list)
    for r in real_rows:
        mk = str(r.get("market") or "").strip()
        if mk:
            rows_by_market[mk].append(r)

    # Compute as-of once so all distributions honour "no future leakage".
    # We use the game's event_time as the as-of horizon.
    _as_of = (game or {}).get("event_time")

    # ── DISTRIBUTIONS + THRESHOLDS (compute ONCE per market) ────
    dists: dict[str, Distribution] = {}
    thresholds: list[ThresholdEvaluation] = []
    for market_str, ladder in rows_by_market.items():
        dist = await build_distribution(
            db, player_name=player_name,
            canonical_player_id=canonical_player_id,
            market=market_str, opponent=opponent, as_of=_as_of,
        )
        dists[market_str] = dist
        raw_evals: list[ThresholdEvaluation] = []
        for row in ladder:
            line = _safe_float(row.get("line"))
            if line is None:
                continue
            odds = row.get("book_odds")
            book = row.get("sportsbook")
            p_raw = dist.hit_probability_over(line)
            implied = _implied_prob(int(odds) if odds is not None else None)
            edge_pp = None
            if p_raw is not None and implied is not None:
                edge_pp = round((p_raw - implied) * 100.0, 2)
            raw_evals.append(ThresholdEvaluation(
                market=market_str, line=line,
                odds=int(odds) if odds is not None else None,
                sportsbook=book,
                hit_probability_raw=p_raw, hit_probability_monotonic=None,
                floor_distance=dist.floor_distance(line),
                implied_probability=implied,
                edge_pp=edge_pp,
            ))
        thresholds.extend(enforce_monotonicity(raw_evals))

    # ── SAFEST BET + BEST VALUE ──────────────────────────────────
    def _safest_key(t: ThresholdEvaluation):
        return (-(t.hit_probability_monotonic or 0.0),
                -(t.floor_distance or 0.0),
                (t.implied_probability or 0.0))
    def _best_value_key(t: ThresholdEvaluation):
        return (-(t.edge_pp or -999.0),
                -(t.hit_probability_monotonic or 0.0))
    filtered = [t for t in thresholds if t.hit_probability_monotonic is not None]
    safest = None
    best_value = None
    if filtered:
        safest = min(filtered, key=_safest_key).as_dict()
        with_edge = [t for t in filtered if t.edge_pp is not None]
        if with_edge:
            best_value = min(with_edge, key=_best_value_key).as_dict()

    return PlayerEvaluation(
        player_name=player_name,
        canonical_player_id=canonical_player_id,
        position=position,
        team=team, opponent=opponent,
        game_context=game_ctx,
        weather=asdict(weather),
        availability=asdict(availability),
        distributions_by_market={
            m: asdict(d) for m, d in dists.items()
            if d.sample_size > 0 or d.mean is not None
        },
        thresholds=[t.as_dict() for t in thresholds],
        safest_bet=safest,
        best_value=best_value,
        confidence=confidence,
        confidence_breakdown=confidence_breakdown,
        frozen_at=datetime.now(timezone.utc).isoformat(),
    )


def _normalize_market(m: str) -> str:
    """Alias real sportsbook market_keys onto SUPPORTED_MARKETS keys."""
    m = (m or "").lower()
    if "pass" in m and ("yd" in m or "yard" in m): return "pass_yds"
    if "pass" in m and "attempt" in m:               return "pass_attempts"
    if "pass" in m and ("comp" in m or "completion" in m): return "pass_completions"
    if "rush" in m and ("yd" in m or "yard" in m):   return "rush_yds"
    if "rush" in m and "attempt" in m:               return "rush_attempts"
    if ("rec" in m or "reception" in m) and ("yd" in m or "yard" in m): return "rec_yds"
    if "reception" in m or (m.startswith("player_rec") and "yd" not in m): return "receptions"
    if "anytime" in m and "td" in m:                 return "anytime_td"
    if "atd" in m:                                    return "anytime_td"
    return m


def _safe_float(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _weather_confidence_multiplier(weather) -> float:
    """Return a confidence multiplier from a WeatherReport.

    Contract: missing data does NOT penalize.  Only real, trustworthy
    weather EVIDENCE that suggests degraded conditions reduces
    confidence.  Indoor / retractable-closed roofs are treated as
    neutral (multiplier = 1.0) regardless of outdoor conditions.
    """
    try:
        status = getattr(weather, "status", None)
        # UNAVAILABLE → we simply don't know.  Not a penalty.
        if status != WEATHER_STATUS_AVAILABLE:
            return 1.0
        # Indoor / roof closed → neutral.
        if bool(getattr(weather, "is_indoor", False)):
            return 1.0
        roof = (getattr(weather, "roof_status", None) or "").lower()
        if "closed" in roof or "dome" in roof:
            return 1.0
        # Real evidence of degraded conditions ONLY reduces confidence.
        wind = getattr(weather, "wind_mph", None)
        gust = getattr(weather, "wind_gust_mph", None)
        precip = getattr(weather, "precip_probability", None)
        # Sustained 20+ mph wind → small confidence haircut.
        w = 1.0
        if wind is not None and wind >= 20:
            w *= 0.95
        # Sustained gusts 30+ mph → additional haircut.
        if gust is not None and gust >= 30:
            w *= 0.95
        # Very high precip probability → small haircut.
        if precip is not None and precip >= 0.7:
            w *= 0.95
        return round(w, 4)
    except Exception:
        return 1.0


__all__ = [
    "SUPPORTED_MARKETS", "Distribution", "ThresholdEvaluation",
    "PlayerEvaluation", "build_distribution", "enforce_monotonicity",
    "evaluate_player_across_markets",
]
