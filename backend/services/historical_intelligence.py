"""Universal Historical Intelligence — v2.0 backend contract.

Session 4 (2026-09-16) — ONE reusable contract with sport-specific
adapters.  Consumed by ``GET /api/picks/{id}/historical-intelligence``
so Pick Breakdown can lazily load deep history without ever hitting
the mobile Locks lite hot path.

Design tenets (from the master directive):
    * MARKET-AWARE actuals: passing-yards market pulls historical
      passing yards, not team final scores.
    * MISSING stays MISSING.  Zero-imputation is banned.
    * VS OPP is real: same entity vs today's opponent, `n` always
      exposed, tiny samples don't visually beat large ones.
    * Exact-CURRENT-line hit/miss classification. Historical
      sportsbook lines can be shown but never substitute for the
      current threshold.
    * Home/Away toggles are real filters, not decorative labels.
    * Distribution / quantiles are computed from raw actuals, not
      reconstructed from hit-rate.
    * Descriptive only — Lock Score / UEA / Apex are NEVER modified
      by this module.  Read-only truth.
"""
from __future__ import annotations

import logging
import re
import statistics
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable, Optional

logger = logging.getLogger("lockscore.historical_intelligence")

# ---------------------------------------------------------------------------
# Shared contract
# ---------------------------------------------------------------------------

_SPORT_ADAPTERS: dict[str, "HistoricalAdapter"] = {}


@dataclass
class HistoricalQuery:
    sport:              str                # "NFL" | "MLB" | "Soccer" | "Tennis" | "CFB"
    entity_type:        str                # "player" | "team"
    entity_id:          str                # canonical player_id or team_id
    entity_name:        Optional[str] = None
    opponent_id:        Optional[str] = None
    opponent_name:      Optional[str] = None
    market_family:      str = ""           # "pass_yds" / "hits" / "1x2" / …
    current_threshold:  Optional[float] = None    # today's exact line
    sample_scope:       str = "L10"        # "L5" | "L10" | "L20" | "SEASON" | "ALL"
    venue_scope:        str = "ALL"        # "ALL" | "HOME" | "AWAY"
    season:             Optional[int] = None
    context_scope:      Optional[str] = None   # sport-specific (surface, RHP/LHP, …)
    side:               str = "over"       # "over" | "under" | "cover" | "ml"


@dataclass
class HistoricalObservation:
    date:            str        # ISO
    opponent_id:     Optional[str] = None
    opponent_name:   Optional[str] = None
    home_away:       Optional[str] = None    # "home" | "away" | None
    actual:          Optional[float] = None  # the raw stat under the market
    result:          Optional[str] = None    # "HIT" | "MISS" | "PUSH" | None
    context:         dict[str, Any] = field(default_factory=dict)
    provenance:      Optional[str] = None
    event_id:        Optional[str] = None


@dataclass
class HistoricalResponse:
    sport:          str
    entity_id:      str
    entity_name:    Optional[str]
    market_family:  str
    current_threshold: Optional[float]
    scope:          dict[str, Any]                # {sample_scope, venue_scope, context_scope}
    games:          list[HistoricalObservation]
    sample_size:    int
    hits:           int
    misses:         int
    pushes:         int
    hit_rate:       Optional[float]
    mean:           Optional[float]
    median:         Optional[float]
    q25:            Optional[float]
    q75:            Optional[float]
    stddev:         Optional[float]
    trend:          Optional[str]
    home_summary:   Optional[dict[str, Any]]
    away_summary:   Optional[dict[str, Any]]
    opponent_summary: Optional[dict[str, Any]]   # VS OPP
    context_summary:  Optional[dict[str, Any]]
    data_coverage:  dict[str, Any]
    provenance:     list[str]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # HistoricalObservation dataclass → dict via asdict recurses; JSON-safe.
        return d


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class HistoricalAdapter:
    """Base class every sport adapter subclasses.  Subclasses only
    implement ``fetch_observations`` — the shared reducer computes
    every derived statistic uniformly."""
    sport: str = ""

    async def fetch_observations(self, db, query: HistoricalQuery) -> list[HistoricalObservation]:
        raise NotImplementedError


def register_adapter(sport: str, adapter: HistoricalAdapter) -> None:
    _SPORT_ADAPTERS[sport.upper()] = adapter


def get_adapter(sport: str) -> Optional[HistoricalAdapter]:
    return _SPORT_ADAPTERS.get((sport or "").upper())


# ---------------------------------------------------------------------------
# Universal reducer
# ---------------------------------------------------------------------------

def _classify(observation_actual: Optional[float],
              threshold: Optional[float],
              side: str) -> Optional[str]:
    """Compare a raw actual against the CURRENT sportsbook threshold."""
    if observation_actual is None or threshold is None:
        return None
    side = (side or "over").lower()
    # Whole-number thresholds may PUSH; half-numbers never push.
    is_whole = float(threshold).is_integer()
    if side in ("over", "cover"):
        if observation_actual > threshold:
            return "HIT"
        if observation_actual < threshold:
            return "MISS"
        return "PUSH" if is_whole else "HIT"   # half-line: exact-equal is impossible
    if side == "under":
        if observation_actual < threshold:
            return "HIT"
        if observation_actual > threshold:
            return "MISS"
        return "PUSH" if is_whole else "HIT"
    # Moneyline / 1X2 / binary outcomes handled by adapter.
    return None


def _apply_venue_scope(obs: list[HistoricalObservation],
                       venue_scope: str) -> list[HistoricalObservation]:
    v = (venue_scope or "ALL").upper()
    if v == "ALL":
        return obs
    want = "home" if v == "HOME" else "away"
    return [o for o in obs if (o.home_away or "").lower() == want]


def _apply_sample_scope(obs: list[HistoricalObservation],
                        sample_scope: str) -> list[HistoricalObservation]:
    """Sort by date desc, then trim to L5/L10/L20/SEASON/ALL."""
    sorted_obs = sorted(obs, key=lambda o: o.date or "", reverse=True)
    s = (sample_scope or "L10").upper()
    if s == "L5":     return sorted_obs[:5]
    if s == "L10":    return sorted_obs[:10]
    if s == "L20":    return sorted_obs[:20]
    if s == "SEASON":
        # First observation's season year — trust the caller to have
        # filtered to a season if they need a specific one.  The
        # reducer just returns everything sorted here.
        return sorted_obs
    return sorted_obs   # ALL


def _quantile(sorted_vals: list[float], q: float) -> Optional[float]:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def _summarize(obs: list[HistoricalObservation],
               threshold: Optional[float],
               side: str) -> dict[str, Any]:
    n = len(obs)
    if n == 0:
        return {"n": 0, "hits": 0, "misses": 0, "pushes": 0,
                "hit_rate": None, "mean": None, "median": None,
                "q25": None, "q75": None, "stddev": None}
    actuals = [o.actual for o in obs if o.actual is not None]
    hits = misses = pushes = 0
    for o in obs:
        r = _classify(o.actual, threshold, side) if o.result is None else o.result
        if r == "HIT":    hits += 1
        elif r == "MISS": misses += 1
        elif r == "PUSH": pushes += 1
    denom = hits + misses
    hit_rate = round(hits / denom, 4) if denom > 0 else None
    if actuals:
        srt = sorted(actuals)
        mean = round(statistics.fmean(srt), 3)
        median = round(statistics.median(srt), 3)
        q25 = round(_quantile(srt, 0.25) or 0, 3)
        q75 = round(_quantile(srt, 0.75) or 0, 3)
        stddev = round(statistics.pstdev(srt), 3) if len(srt) > 1 else 0.0
    else:
        mean = median = q25 = q75 = stddev = None
    return {"n": n, "hits": hits, "misses": misses, "pushes": pushes,
            "hit_rate": hit_rate,
            "mean": mean, "median": median, "q25": q25, "q75": q75,
            "stddev": stddev}


async def query_historical(db, q: HistoricalQuery) -> HistoricalResponse:
    """Universal entry point.  Dispatches to the sport adapter,
    then runs the shared reducer.  Empty responses are honest —
    missing data is never zero-imputed."""
    adapter = get_adapter(q.sport)
    provenance: list[str] = []
    all_obs: list[HistoricalObservation] = []
    if adapter is None:
        provenance.append(f"no_adapter_for_sport:{q.sport}")
    else:
        try:
            all_obs = await adapter.fetch_observations(db, q) or []
            provenance.append(f"adapter:{type(adapter).__name__}")
        except Exception as _err:
            logger.warning("adapter %s failed: %s", q.sport, _err)
            provenance.append(f"adapter_error:{_err}")

    # SCOPE MATRIX ─────────────────────────────────────────────────
    #  - venue_scope filter
    #  - sample_scope trim (L5/L10/L20/SEASON/ALL)
    venue_filtered = _apply_venue_scope(all_obs, q.venue_scope)
    scoped = _apply_sample_scope(venue_filtered, q.sample_scope)

    main = _summarize(scoped, q.current_threshold, q.side)

    # SPLIT SUMMARIES — only produced when the sport supports them.
    home_only = [o for o in venue_filtered if (o.home_away or "").lower() == "home"]
    away_only = [o for o in venue_filtered if (o.home_away or "").lower() == "away"]
    home_summary = _summarize(home_only, q.current_threshold, q.side) if home_only else None
    away_summary = _summarize(away_only, q.current_threshold, q.side) if away_only else None

    # VS OPP — always sourced from the FULL history (never venue-restricted
    # unless the user also asked for a venue filter downstream in the UI).
    vs_opp: Optional[list[HistoricalObservation]] = None
    opp_summary: Optional[dict[str, Any]] = None
    if q.opponent_id:
        vs_opp = [o for o in all_obs if (o.opponent_id or "") == q.opponent_id]
    if q.opponent_name and not vs_opp:
        norm = q.opponent_name.strip().lower()
        vs_opp = [o for o in all_obs
                  if (o.opponent_name or "").strip().lower() == norm]
    if vs_opp:
        opp_summary = _summarize(vs_opp, q.current_threshold, q.side)
        opp_summary["games"] = [asdict(o) for o in vs_opp[:10]]
    elif q.opponent_id or q.opponent_name:
        opp_summary = {"n": 0, "note": "NO PRIOR MATCHUPS"}   # missing ≠ zero

    # Context summary — sport-specific splits (surface, RHP/LHP).  Adapter
    # provides them via `context` on each observation.  We emit per-key
    # micro-summaries when a context key appears.
    context_summary: Optional[dict[str, Any]] = None
    context_bucket: dict[str, list[HistoricalObservation]] = {}
    if q.context_scope:
        for o in venue_filtered:
            v = (o.context or {}).get(q.context_scope)
            if v is None:
                continue
            context_bucket.setdefault(str(v), []).append(o)
        if context_bucket:
            context_summary = {
                k: _summarize(v, q.current_threshold, q.side)
                for k, v in context_bucket.items()
            }

    # Trend — sign of L5 vs L10 mean, if enough data.
    trend = None
    if len(scoped) >= 5:
        recent5 = [o.actual for o in scoped[:5] if o.actual is not None]
        older = [o.actual for o in scoped[5:15] if o.actual is not None]
        if recent5 and older:
            r5 = statistics.fmean(recent5)
            r_old = statistics.fmean(older)
            if r_old > 0:
                delta = (r5 - r_old) / max(0.5, abs(r_old))
                trend = "up" if delta > 0.08 else "down" if delta < -0.08 else "flat"

    # Data coverage — every field must survive as MISSING if it truly is.
    data_coverage = {
        "raw_actual_pct": round(
            sum(1 for o in scoped if o.actual is not None) /
            max(1, len(scoped)), 3),
        "opponent_id_pct": round(
            sum(1 for o in scoped if o.opponent_id) /
            max(1, len(scoped)), 3),
        "home_away_pct": round(
            sum(1 for o in scoped if o.home_away in ("home", "away")) /
            max(1, len(scoped)), 3),
    }

    # Materialize response.  Every observation is preserved raw.
    return HistoricalResponse(
        sport=q.sport,
        entity_id=q.entity_id,
        entity_name=q.entity_name,
        market_family=q.market_family,
        current_threshold=q.current_threshold,
        scope={
            "sample_scope": q.sample_scope,
            "venue_scope":  q.venue_scope,
            "context_scope": q.context_scope,
            "side":         q.side,
        },
        games=scoped,
        sample_size=main["n"],
        hits=main["hits"],
        misses=main["misses"],
        pushes=main["pushes"],
        hit_rate=main["hit_rate"],
        mean=main["mean"],
        median=main["median"],
        q25=main["q25"],
        q75=main["q75"],
        stddev=main["stddev"],
        trend=trend,
        home_summary=home_summary,
        away_summary=away_summary,
        opponent_summary=opp_summary,
        context_summary=context_summary,
        data_coverage=data_coverage,
        provenance=provenance,
    )


# ---------------------------------------------------------------------------
# NFL adapter — reads player_game_actuals (nfl_player_weekly + friends).
# ---------------------------------------------------------------------------

# Market family → primary actuals key(s) inside `actuals` payload.
_NFL_MARKET_MAP = {
    "pass_yds":     "pass_yds",
    "pass_tds":     "pass_tds",
    "interceptions":"interceptions",
    "completions":  "completions",
    "attempts":     "attempts",
    "rush_yds":     "rush_yds",
    "rush_attempts":"rush_attempts",
    "rush_tds":     "rush_tds",
    "rec_yds":      "rec_yds",
    "receptions":   "receptions",
    "rec_tds":      "rec_tds",
    "targets":      "targets",
    "atd":          "__total_tds__",   # synthesized below
}


def _nfl_market_family_from_market(market: str) -> Optional[str]:
    if not market:
        return None
    m = market.lower()
    if "passing yard" in m or "pass yard" in m:                          return "pass_yds"
    if "passing td" in m or "passing tds" in m or "pass tds" in m:       return "pass_tds"
    if "interception" in m:                                              return "interceptions"
    if "completion" in m:                                                return "completions"
    if "pass attempt" in m or ("attempt" in m and "pass" in m):          return "attempts"
    if "rushing yard" in m or "rush yard" in m:                          return "rush_yds"
    if "carr" in m or "rushing attempt" in m or "rush attempt" in m:     return "rush_attempts"
    if "rushing td" in m or "rush td" in m:                              return "rush_tds"
    if "receiving yard" in m or "rec yard" in m:                         return "rec_yds"
    if "reception" in m and "yard" not in m:                             return "receptions"
    if "receiving td" in m or "rec td" in m:                             return "rec_tds"
    if "target" in m:                                                    return "targets"
    if "anytime td" in m or "anytime touchdown" in m:                    return "atd"
    return None


class NFLHistoricalAdapter(HistoricalAdapter):
    sport = "NFL"

    async def fetch_observations(self, db, q: HistoricalQuery
                                  ) -> list[HistoricalObservation]:
        family = q.market_family
        actuals_key = _NFL_MARKET_MAP.get(family)
        if not actuals_key and family != "atd":
            return []

        # Primary lookup: canonical_player_id.  Fallback to player_name
        # for legacy rows that missed identity resolution.
        or_clauses: list[dict] = []
        if q.entity_id:
            or_clauses.append({"canonical_player_id": q.entity_id})
            or_clauses.append({"player_id": q.entity_id})
        if q.entity_name:
            or_clauses.append({"player_name": q.entity_name})
        if not or_clauses:
            return []

        base = {
            "$or": or_clauses,
            "$and": [{"$or": [
                {"source": {"$regex": "nfl", "$options": "i"}},
                {"sport": "NFL"},
            ]}],
        }

        obs: list[HistoricalObservation] = []
        async for doc in db.player_game_actuals.find(base).sort("event_time", -1).limit(60):
            actuals = doc.get("actuals") or {}
            if actuals_key == "__total_tds__":
                # ATD = rush TD + rec TD + return TDs (if surfaced).
                a = 0.0
                got_any = False
                for k in ("rush_tds", "rec_tds"):
                    v = actuals.get(k)
                    if v is not None:
                        try:
                            a += float(v); got_any = True
                        except Exception:
                            pass
                actual = a if got_any else None
                # 1+ TD is the industry ATD convention.  Threshold
                # defaults to 0.5 if caller didn't supply one.
            else:
                v = actuals.get(actuals_key)
                actual = None
                if v is not None:
                    try: actual = float(v)
                    except Exception: actual = None
            obs.append(HistoricalObservation(
                date=str(doc.get("event_time") or "")[:10],
                opponent_id=doc.get("canonical_opponent_id"),
                opponent_name=doc.get("opponent"),
                home_away=doc.get("home_away"),
                actual=actual,
                context={"season": doc.get("season")},
                provenance=doc.get("source"),
                event_id=doc.get("canonical_event_id") or doc.get("event_id"),
            ))
        return obs


register_adapter("NFL", NFLHistoricalAdapter())


# ---------------------------------------------------------------------------
# MLB / Soccer / Tennis / CFB adapters — SKELETONS registered so the
# endpoint dispatches to a real (empty-until-implemented) result rather
# than a 500.  Filling these is the Session 5 continuation.
# ---------------------------------------------------------------------------

class _StubAdapter(HistoricalAdapter):
    """Returns [] with an honest provenance note so downstream callers
    see empty-vs-broken clearly.  Sport-specific stat mappings are the
    Session 5 item — data is present (player_game_logs, tennis_matches_history,
    soccer_player_game_logs) but the market-family → actuals-key wiring
    per sport is deferred with the frontend rebuild."""
    sport = "STUB"

    def __init__(self, sport: str) -> None:
        self.sport = sport

    async def fetch_observations(self, db, q: HistoricalQuery
                                  ) -> list[HistoricalObservation]:
        logger.info(
            "historical adapter stub returning [] for sport=%s market=%s "
            "(Session 5 wire-up).  Data stores available: "
            "MLB→player_game_logs (348k), Soccer→soccer_player_game_logs "
            "(50k), Tennis→tennis_matches_history (38k), "
            "CFB→team_game_actuals (60k universal team log).",
            self.sport, q.market_family,
        )
        return []


register_adapter("MLB",    _StubAdapter("MLB"))
register_adapter("Soccer", _StubAdapter("Soccer"))
register_adapter("Tennis", _StubAdapter("Tennis"))
register_adapter("CFB",    _StubAdapter("CFB"))
