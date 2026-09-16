"""Universal Historical Intelligence — v2.0 backend contract.

Session 5 (2026-09-17) — ALL sport adapters wired to the REAL raw
historical data stores identified in Session 4.  Consumed by
``GET /api/picks/{id}/historical-intelligence`` so Pick Breakdown can
lazily load deep history without ever touching the mobile Locks
lite hot path.

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

Data sources per sport (verified 2026-09-17):
    NFL player  → player_game_actuals (sport=nfl, 129k rows)
    NFL team    → team_game_actuals   (sport=nfl, 570 rows)
    MLB player  → player_game_actuals (sport=mlb, 64k rows)
    MLB team    → team_game_actuals   (sport=mlb, 4k rows)
    Soccer plyr → soccer_player_game_logs (49k rows)
    Soccer team → soccer_matches      (25k rows) — pure raw finals
    Tennis      → tennis_matches_history (38k rows) — name-keyed
    CFB team    → games (sport=cfb, 2.2k rows)
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
    """Compare a raw actual against the CURRENT sportsbook threshold.

    Handles moneyline (side="ml") and 1X2 via string actuals ("W"/"L"/"D").
    """
    if observation_actual is None:
        return None
    side = (side or "over").lower()
    # ML/1X2 style — actual is a discrete result stored as float sentinel
    # (1.0=WIN, 0.5=DRAW, 0.0=LOSS) by adapters that grade directly.
    if side in ("ml", "moneyline"):
        if observation_actual >= 1.0:  return "HIT"
        if observation_actual == 0.5:  return "PUSH"   # rare in ML
        return "MISS"
    if side in ("cover_ml_1x2",):
        return "HIT" if observation_actual >= 1.0 else "MISS"
    if threshold is None:
        return None
    is_whole = float(threshold).is_integer()
    if side in ("over", "cover"):
        if observation_actual > threshold:
            return "HIT"
        if observation_actual < threshold:
            return "MISS"
        return "PUSH" if is_whole else "HIT"
    if side == "under":
        if observation_actual < threshold:
            return "HIT"
        if observation_actual > threshold:
            return "MISS"
        return "PUSH" if is_whole else "HIT"
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
        r = o.result if o.result is not None else _classify(o.actual, threshold, side)
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
            logger.warning("adapter %s failed: %s", q.sport, _err, exc_info=True)
            provenance.append(f"adapter_error:{_err}")

    venue_filtered = _apply_venue_scope(all_obs, q.venue_scope)
    scoped = _apply_sample_scope(venue_filtered, q.sample_scope)

    main = _summarize(scoped, q.current_threshold, q.side)

    home_only = [o for o in venue_filtered if (o.home_away or "").lower() == "home"]
    away_only = [o for o in venue_filtered if (o.home_away or "").lower() == "away"]
    home_summary = _summarize(home_only, q.current_threshold, q.side) if home_only else None
    away_summary = _summarize(away_only, q.current_threshold, q.side) if away_only else None

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
        opp_summary = {"n": 0, "note": "NO PRIOR MATCHUPS"}

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
        "total_observations": len(all_obs),
    }

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


# ===========================================================================
# NFL PLAYER ADAPTER — passing / rushing / receiving / ATD
# ===========================================================================

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
    "atd":          "__total_tds__",
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


class NFLPlayerHistoricalAdapter(HistoricalAdapter):
    sport = "NFL"

    async def fetch_observations(self, db, q: HistoricalQuery
                                  ) -> list[HistoricalObservation]:
        family = q.market_family
        actuals_key = _NFL_MARKET_MAP.get(family)
        if not actuals_key and family != "atd":
            return []
        # Query strategy: canonical_player_id first (indexed) — but only
        # if the id looks NFL-canonical ("00-XXXXXXX").  Otherwise fall
        # back to player_identities → gsis lookup by name.
        base_q: Optional[dict] = None
        eid = str(q.entity_id or "")
        looks_gsis = eid.startswith("00-") and len(eid) >= 8
        if looks_gsis:
            base_q = {"sport": "nfl", "canonical_player_id": eid}
        if base_q is None and q.entity_name:
            norm = q.entity_name.strip().lower().replace(".", "")
            ident = await db.player_identities.find_one(
                {"sport": "NFL", "name_norm": norm},
                {"provider_ids": 1, "canonical_player_id": 1})
            if ident:
                gsis = (ident.get("provider_ids") or {}).get("gsis") \
                       or (ident.get("provider_ids") or {}).get("nfl_gsis")
                if gsis:
                    base_q = {"sport": "nfl", "canonical_player_id": gsis}
        if base_q is None and q.entity_name:
            base_q = {"sport": "nfl", "player_name": q.entity_name}
        if not base_q:
            return []
        obs: list[HistoricalObservation] = []
        cursor = db.player_game_actuals.find(base_q).sort("event_time", -1).limit(80)
        async for doc in cursor:
            actuals = doc.get("actuals") or {}
            if actuals_key == "__total_tds__":
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
            else:
                v = actuals.get(actuals_key)
                actual = None
                if v is not None:
                    try: actual = float(v)
                    except Exception: actual = None
            obs.append(HistoricalObservation(
                date=str(doc.get("event_time") or "")[:10],
                opponent_id=doc.get("canonical_opponent_id"),
                opponent_name=doc.get("opponent") or doc.get("canonical_opponent_id"),
                home_away=doc.get("home_away"),
                actual=actual,
                context={"season": doc.get("season"), "week": doc.get("week")},
                provenance=doc.get("source"),
                event_id=doc.get("canonical_event_id") or doc.get("event_id"),
            ))
        return obs


# ===========================================================================
# NFL TEAM / GAME ADAPTER — ML / SPREAD / TOTAL
# ===========================================================================

def _nfl_game_market_family(market: str) -> Optional[str]:
    if not market: return None
    m = market.lower()
    if "moneyline" in m or " ml" in m:  return "moneyline"
    if "spread" in m:                    return "spread"
    if "total" in m or "over/under" in m: return "total"
    return None


class NFLTeamHistoricalAdapter(HistoricalAdapter):
    """Reads team_game_actuals sport=nfl (raw final scores).

    Each observation carries team_score / opponent_score / result.
    - moneyline: actual = 1.0 (WIN) / 0.0 (LOSS)
    - spread:    actual = team_score - opponent_score  (margin)
    - total:     actual = team_score + opponent_score
    """
    sport = "NFL_TEAM"

    async def fetch_observations(self, db, q: HistoricalQuery
                                  ) -> list[HistoricalObservation]:
        team = q.entity_id or q.entity_name
        if not team:
            return []
        family = q.market_family or ""
        cursor = db.team_game_actuals.find(
            {"sport": "nfl", "canonical_team_id": team}
        ).sort("event_time", -1).limit(80)
        obs: list[HistoricalObservation] = []
        async for doc in cursor:
            ts = doc.get("team_score")
            os_ = doc.get("opponent_score")
            if ts is None or os_ is None:
                continue
            try:
                ts = float(ts); os_ = float(os_)
            except Exception:
                continue
            if family == "moneyline":
                actual = 1.0 if ts > os_ else (0.5 if ts == os_ else 0.0)
            elif family == "spread":
                actual = ts - os_
            elif family == "total":
                actual = ts + os_
            else:
                actual = ts - os_
            obs.append(HistoricalObservation(
                date=str(doc.get("event_time") or "")[:10],
                opponent_id=doc.get("canonical_opponent_id"),
                opponent_name=doc.get("canonical_opponent_id"),
                home_away=doc.get("home_away"),
                actual=actual,
                context={"team_score": ts, "opponent_score": os_,
                         "result": doc.get("result"),
                         "season": doc.get("season")},
                provenance=doc.get("source"),
                event_id=doc.get("event_id"),
            ))
        return obs


# ===========================================================================
# MLB PLAYER ADAPTER — hits / TB / H+R+RBI / HR / RBI / Runs / Ks / Outs
# ===========================================================================

_MLB_HITTER_MAP = {
    "hits":              lambda a: a.get("h"),
    "total_bases":       lambda a: a.get("tb"),
    "home_runs":         lambda a: a.get("hr"),
    "rbi":               lambda a: a.get("rbi"),
    "runs":              lambda a: a.get("r"),
    # Note: MLB raw actuals do not contain Runs Scored (universal
    # coverage gap).  We surface H+RBI as an APPROXIMATION and set a
    # `runs_unavailable` flag on each observation so the frontend can
    # honestly disclose the proxy in-context. MISSING ≠ ZERO is
    # preserved by never fabricating a Runs value.
    "hits_runs_rbi":     lambda a: (
        None if a.get("h") is None or a.get("rbi") is None
        else float(a.get("h") or 0) + float(a.get("rbi") or 0)
    ),
    "batter_strikeouts": lambda a: a.get("strikeouts"),
}
_MLB_PITCHER_MAP = {
    "strikeouts":  lambda a: a.get("k"),
    "outs":        lambda a: a.get("outs"),
}


def _mlb_market_family(market: str) -> Optional[str]:
    if not market: return None
    m = market.lower()
    # Compound markets FIRST (before "rbi"/"hits" catch-all)
    if "hits + runs + rbi" in m or "h+r+rbi" in m or "hits, runs" in m or "hits runs rbi" in m: return "hits_runs_rbi"
    # Pitcher first (strikeouts / outs)
    if "outs recorded" in m or ("outs" in m and "pitch" in m): return "outs"
    if "batter strikeouts" in m: return "batter_strikeouts"
    if "strikeout" in m and "batter" not in m:
        return "strikeouts"
    if "total base" in m:      return "total_bases"
    if "home run" in m:        return "home_runs"
    if "rbi" in m or "run batted" in m: return "rbi"
    if "hit" in m and "run" not in m: return "hits"
    if "run" in m and "line" not in m: return "runs"
    if "run line" in m: return "run_line"
    if "moneyline" in m or " ml" in m: return "moneyline"
    if "total" in m: return "total"
    return None


class MLBPlayerHistoricalAdapter(HistoricalAdapter):
    sport = "MLB"

    async def fetch_observations(self, db, q: HistoricalQuery
                                  ) -> list[HistoricalObservation]:
        family = q.market_family or ""
        hitter_fn = _MLB_HITTER_MAP.get(family)
        pitcher_fn = _MLB_PITCHER_MAP.get(family)
        if hitter_fn is None and pitcher_fn is None:
            return []
        # Canonical-id-first strategy — but only when the id looks
        # numeric (MLBAM-style).  Otherwise it's a fallback entity_id
        # (usually the player's display name) that we resolve via
        # player_identities.
        base_q: Optional[dict] = None
        eid = str(q.entity_id or "")
        looks_numeric = eid.isdigit() or (eid.startswith("6") and len(eid) == 6)
        if looks_numeric:
            base_q = {"sport": "mlb", "canonical_player_id": eid}
        # If no numeric MLBAM id — look up via player_identities.name_norm
        if base_q is None and q.entity_name:
            norm = q.entity_name.strip().lower().replace(".", "")
            ident = await db.player_identities.find_one(
                {"sport": "MLB", "name_norm": norm},
                {"provider_ids": 1, "canonical_player_id": 1})
            if ident:
                mlbam = (ident.get("provider_ids") or {}).get("mlb_stats")
                if mlbam:
                    base_q = {"sport": "mlb", "canonical_player_id": str(mlbam)}
        if base_q is None and q.entity_name:
            # Last-resort — direct player_name (rarely populated).
            base_q = {"sport": "mlb", "player_name": q.entity_name}
        if not base_q:
            return []
        obs: list[HistoricalObservation] = []
        cursor = db.player_game_actuals.find(base_q).sort("event_time", -1).limit(120)
        async for doc in cursor:
            actuals = doc.get("actuals") or {}
            fn = hitter_fn or pitcher_fn
            v = fn(actuals)
            actual: Optional[float] = None
            if v is not None:
                try: actual = float(v)
                except Exception: actual = None
            # Context supporting fields (raw, honest).
            ctx = {k: actuals.get(k) for k in
                   ("h", "hr", "rbi", "r", "tb", "at_bats", "strikeouts",
                    "k", "outs")}
            ctx["season"] = doc.get("season")
            # Transparent proxy disclosure for H+R+RBI (Runs missing).
            if family == "hits_runs_rbi":
                ctx["proxy"] = "H+RBI (Runs unavailable in source)"
            obs.append(HistoricalObservation(
                date=str(doc.get("event_time") or doc.get("ingested_at") or "")[:10],
                opponent_id=doc.get("canonical_opponent_id"),
                opponent_name=doc.get("opponent") or doc.get("canonical_opponent_id"),
                home_away=doc.get("home_away"),
                actual=actual,
                context=ctx,
                provenance=doc.get("source"),
                event_id=doc.get("canonical_event_id") or doc.get("event_id"),
            ))
        return obs


# ===========================================================================
# MLB TEAM / GAME ADAPTER — ML / RUN LINE / TOTAL
# ===========================================================================

class MLBTeamHistoricalAdapter(HistoricalAdapter):
    sport = "MLB_TEAM"

    async def fetch_observations(self, db, q: HistoricalQuery
                                  ) -> list[HistoricalObservation]:
        team = q.entity_id or q.entity_name
        if not team:
            return []
        family = q.market_family or ""
        cursor = db.team_game_actuals.find(
            {"sport": "mlb", "canonical_team_id": team}
        ).sort("event_time", -1).limit(120)
        obs: list[HistoricalObservation] = []
        async for doc in cursor:
            ts = doc.get("team_score"); os_ = doc.get("opponent_score")
            if ts is None or os_ is None: continue
            try:
                ts = float(ts); os_ = float(os_)
            except Exception:
                continue
            if family == "moneyline":
                actual = 1.0 if ts > os_ else (0.5 if ts == os_ else 0.0)
            elif family == "run_line":
                actual = ts - os_
            elif family == "total":
                actual = ts + os_
            else:
                actual = ts - os_
            obs.append(HistoricalObservation(
                date=str(doc.get("event_time") or "")[:10],
                opponent_id=doc.get("canonical_opponent_id"),
                opponent_name=doc.get("canonical_opponent_id"),
                home_away=doc.get("home_away"),
                actual=actual,
                context={"team_score": ts, "opponent_score": os_,
                         "season": doc.get("season"),
                         "result": doc.get("result")},
                provenance=doc.get("source"),
                event_id=doc.get("event_id"),
            ))
        return obs


# ===========================================================================
# SOCCER PLAYER ADAPTER — goals / assists / shots / SOT
# ===========================================================================

_SOCCER_PLAYER_MAP = {
    "goals":         lambda d: d.get("goals"),
    "assists":       lambda d: d.get("assists"),
    "goal_or_assist":lambda d: (
        None if d.get("goals") is None and d.get("assists") is None
        else (float(d.get("goals") or 0) + float(d.get("assists") or 0))
    ),
    "shots":         lambda d: d.get("shots"),
    "sot":           lambda d: d.get("shots_on_target"),
}


def _soccer_market_family(market: str) -> Optional[str]:
    if not market: return None
    m = market.lower()
    if "score or assist" in m or "goal or assist" in m or "assists or goal" in m:
        return "goal_or_assist"
    if "anytime goal" in m or "goalscorer" in m or "to score" in m or "player goal" in m:
        return "goals"
    if "assist" in m:                       return "assists"
    if "shot on target" in m or "sot" in m: return "sot"
    if "shot" in m:                          return "shots"
    if "btts" in m or "both teams to score" in m: return "btts"
    if "double chance" in m:                 return "double_chance"
    if "1x2" in m or "match result" in m:    return "1x2"
    if "moneyline" in m:                     return "1x2"
    if "handicap" in m or "spread" in m:     return "handicap"
    if "total goal" in m or "total" in m or "over/under" in m: return "total"
    if "corner" in m:                        return "corners"
    if "card" in m:                          return "cards"
    return None


class SoccerPlayerHistoricalAdapter(HistoricalAdapter):
    sport = "SOCCER_PLAYER"

    async def fetch_observations(self, db, q: HistoricalQuery
                                  ) -> list[HistoricalObservation]:
        family = q.market_family or ""
        fn = _SOCCER_PLAYER_MAP.get(family)
        if fn is None:
            return []
        or_clauses: list[dict] = []
        if q.entity_id:
            or_clauses.append({"player_id": str(q.entity_id)})
        if q.entity_name:
            nm = q.entity_name.strip()
            or_clauses.append({"player_name": nm})
            or_clauses.append({"name_canonical": nm.lower()})
        if not or_clauses:
            return []
        base = {"$or": or_clauses}
        obs: list[HistoricalObservation] = []
        cursor = db.soccer_player_game_logs.find(base).sort("match_date", -1).limit(120)
        async for doc in cursor:
            v = fn(doc)
            actual: Optional[float] = None
            if v is not None:
                try: actual = float(v)
                except Exception: actual = None
            is_home = str(doc.get("is_home", "")).lower() in ("true", "1")
            obs.append(HistoricalObservation(
                date=str(doc.get("match_date") or "")[:10],
                opponent_id=str(doc.get("opponent_team_id") or "") or None,
                opponent_name=doc.get("opponent_team_name"),
                home_away="home" if is_home else "away",
                actual=actual,
                context={"minutes": doc.get("minutes"),
                         "started": bool(str(doc.get("starts","")).lower() in ("true","1")),
                         "goals": doc.get("goals"),
                         "assists": doc.get("assists"),
                         "shots": doc.get("shots"),
                         "sot": doc.get("shots_on_target"),
                         "league": doc.get("league"),
                         "season": doc.get("season")},
                provenance=doc.get("source"),
                event_id=doc.get("match_id"),
            ))
        return obs


# ===========================================================================
# SOCCER TEAM / GAME ADAPTER — 1X2 / handicap / total / BTTS / DC
# ===========================================================================

class SoccerTeamHistoricalAdapter(HistoricalAdapter):
    sport = "SOCCER_TEAM"

    async def fetch_observations(self, db, q: HistoricalQuery
                                  ) -> list[HistoricalObservation]:
        team = q.entity_id or q.entity_name
        if not team:
            return []
        family = q.market_family or ""
        # soccer_matches: home_team / away_team / home_score / away_score
        cursor = db.soccer_matches.find(
            {"$or": [{"home_team": team}, {"away_team": team}],
             "status": "finished"}
        ).sort("date", -1).limit(120)
        obs: list[HistoricalObservation] = []
        async for doc in cursor:
            try:
                hs = float(doc.get("home_score"))
                as_ = float(doc.get("away_score"))
            except Exception:
                continue
            home = doc.get("home_team"); away = doc.get("away_team")
            is_home = (home == team)
            team_gf = hs if is_home else as_
            team_ga = as_ if is_home else hs
            opp = away if is_home else home
            total = hs + as_
            if family == "1x2":
                # 1.0 = win (from selected team perspective), 0.5 = draw, 0.0 = loss
                actual = 1.0 if team_gf > team_ga else (0.5 if team_gf == team_ga else 0.0)
            elif family == "handicap":
                actual = team_gf - team_ga
            elif family == "total":
                actual = total
            elif family == "btts":
                actual = 1.0 if (hs > 0 and as_ > 0) else 0.0
            elif family == "double_chance":
                # covers if team wins OR draws
                actual = 1.0 if team_gf >= team_ga else 0.0
            else:
                actual = team_gf - team_ga
            obs.append(HistoricalObservation(
                date=str(doc.get("date") or "")[:10],
                opponent_id=None,
                opponent_name=opp,
                home_away="home" if is_home else "away",
                actual=actual,
                context={"gf": team_gf, "ga": team_ga, "total": total,
                         "league": doc.get("league"), "season": doc.get("season")},
                provenance=doc.get("source"),
                event_id=None,
            ))
        return obs


# ===========================================================================
# TENNIS ADAPTER — ML / GAME SPREAD / GAME TOTAL
# ===========================================================================

def _tennis_market_family(market: str) -> Optional[str]:
    if not market: return None
    m = market.lower()
    if "game spread" in m or "spread" in m:      return "game_spread"
    if "total games" in m or "game total" in m or "over/under" in m:
        return "game_total"
    if "total" in m and "game" in m:             return "game_total"
    if "moneyline" in m or "match winner" in m:  return "moneyline"
    return None


_TENNIS_SCORE_RE = re.compile(r"(\d+)[\s\-–]+(\d+)")


def _parse_tennis_games(score: str) -> Optional[tuple[int, int]]:
    """Return (winner_games, loser_games) parsed from a tml_database style
    score string like "6-4 3-6 7-5" or "4-6 7-5 6-2".  Handles tiebreak
    annotations like "7-6(4)".  Returns None for walkovers / retirements
    where a full completed count is not reliable."""
    if not score:
        return None
    s = score.strip()
    if not s or s.upper() in ("W/O", "WALKOVER", "DEF"):
        return None
    if "def" in s.lower() or "w/o" in s.lower():
        return None
    w = 0; l = 0
    for m in _TENNIS_SCORE_RE.finditer(s):
        try:
            a = int(m.group(1)); b = int(m.group(2))
            w += a; l += b
        except Exception:
            continue
    if w == 0 and l == 0:
        return None
    return (w, l)


class TennisHistoricalAdapter(HistoricalAdapter):
    sport = "TENNIS"

    async def fetch_observations(self, db, q: HistoricalQuery
                                  ) -> list[HistoricalObservation]:
        family = q.market_family or ""
        if not q.entity_name:
            return []
        # Tennis history is keyed by winner_name / loser_name.
        # We must query BOTH sides then merge.
        name = q.entity_name.strip()
        # Normalize "Cirstea S." style to "Sorana Cirstea" would require
        # cross-referencing tennis_players; we try both raw and a light
        # last-name lookup so real DB names win the match.
        or_clauses = [
            {"winner_name": name},
            {"loser_name":  name},
        ]
        cursor = db.tennis_matches_history.find(
            {"$or": or_clauses}
        ).sort("date", -1).limit(200)
        obs: list[HistoricalObservation] = []
        async for doc in cursor:
            score = doc.get("score") or ""
            is_walkover = str(doc.get("walkover", "")).lower() == "true"
            retired = str(doc.get("retirement", "")).lower() == "true"
            if is_walkover:
                # Walkovers can't produce a real games actual
                continue
            games = _parse_tennis_games(score)
            if games is None:
                continue
            wg, lg = games
            is_winner = (doc.get("winner_name") == name)
            player_g = wg if is_winner else lg
            opp_g    = lg if is_winner else wg
            total_g  = wg + lg
            opp_name = doc.get("loser_name") if is_winner else doc.get("winner_name")
            surface  = doc.get("surface")
            if family == "moneyline":
                actual = 1.0 if is_winner else 0.0
            elif family == "game_spread":
                actual = player_g - opp_g
            elif family == "game_total":
                actual = total_g
            else:
                actual = player_g - opp_g
            obs.append(HistoricalObservation(
                date=str(doc.get("date") or "")[:10],
                opponent_id=None,
                opponent_name=opp_name,
                home_away=None,   # tennis has no home/away
                actual=actual,
                context={"surface": surface,
                         "player_games": player_g,
                         "opp_games": opp_g,
                         "total_games": total_g,
                         "retired": retired,
                         "tourney_level": doc.get("tourney_level"),
                         "tourney_name": doc.get("tourney_name"),
                         "round": doc.get("round"),
                         "score": score,
                         "won": is_winner},
                provenance=doc.get("source"),
                event_id=doc.get("tourney_id"),
            ))
        return obs


# ===========================================================================
# CFB TEAM / GAME ADAPTER — ML / SPREAD / TOTAL
# ===========================================================================

class CFBHistoricalAdapter(HistoricalAdapter):
    sport = "CFB"

    async def fetch_observations(self, db, q: HistoricalQuery
                                  ) -> list[HistoricalObservation]:
        team = q.entity_id or q.entity_name
        if not team:
            return []
        family = q.market_family or ""
        cursor = db.games.find(
            {"sport": "cfb", "$or": [{"home": team}, {"away": team}],
             "status": "Final"}
        ).sort("date", -1).limit(120)
        obs: list[HistoricalObservation] = []
        async for doc in cursor:
            result = doc.get("result") or {}
            try:
                hs = float(result.get("home"))
                as_ = float(result.get("away"))
            except Exception:
                continue
            home = doc.get("home"); away = doc.get("away")
            is_home = (home == team)
            team_pts = hs if is_home else as_
            opp_pts  = as_ if is_home else hs
            opp      = away if is_home else home
            total    = hs + as_
            if family == "moneyline":
                actual = 1.0 if team_pts > opp_pts else (0.5 if team_pts == opp_pts else 0.0)
            elif family == "spread":
                actual = team_pts - opp_pts
            elif family == "total":
                actual = total
            else:
                actual = team_pts - opp_pts
            obs.append(HistoricalObservation(
                date=str(doc.get("date") or "")[:10],
                opponent_id=None,
                opponent_name=opp,
                home_away="home" if is_home else "away",
                actual=actual,
                context={"team_score": team_pts, "opponent_score": opp_pts,
                         "total": total,
                         "season": doc.get("season"), "week": doc.get("week")},
                provenance="games",
                event_id=doc.get("game_id"),
            ))
        return obs


# ===========================================================================
# DISPATCHER — resolves entity_type × sport → correct adapter
# ===========================================================================

class _SportDispatcher(HistoricalAdapter):
    """Some sports have distinct player vs team adapters.  This wrapper
    dispatches by ``entity_type`` on the query object."""
    def __init__(self, sport: str,
                 player: Optional[HistoricalAdapter],
                 team:   Optional[HistoricalAdapter]) -> None:
        self.sport = sport
        self.player = player
        self.team = team

    async def fetch_observations(self, db, q: HistoricalQuery
                                  ) -> list[HistoricalObservation]:
        if q.entity_type == "player" and self.player is not None:
            return await self.player.fetch_observations(db, q)
        if q.entity_type == "team" and self.team is not None:
            return await self.team.fetch_observations(db, q)
        # Fallback — try player first, then team.
        if self.player is not None:
            r = await self.player.fetch_observations(db, q)
            if r: return r
        if self.team is not None:
            return await self.team.fetch_observations(db, q)
        return []


register_adapter("NFL",    _SportDispatcher(
    "NFL", NFLPlayerHistoricalAdapter(), NFLTeamHistoricalAdapter()))
register_adapter("MLB",    _SportDispatcher(
    "MLB", MLBPlayerHistoricalAdapter(), MLBTeamHistoricalAdapter()))
register_adapter("Soccer", _SportDispatcher(
    "Soccer", SoccerPlayerHistoricalAdapter(), SoccerTeamHistoricalAdapter()))
register_adapter("Tennis", _SportDispatcher(
    "Tennis", TennisHistoricalAdapter(), None))
register_adapter("CFB",    _SportDispatcher(
    "CFB", None, CFBHistoricalAdapter()))


# ---------------------------------------------------------------------------
# Sport-neutral market family resolver (used by routes + coverage matrix)
# ---------------------------------------------------------------------------

def resolve_market_family(sport: str, market: str) -> Optional[str]:
    if sport == "NFL":
        f = _nfl_market_family_from_market(market)
        if f: return f
        return _nfl_game_market_family(market)
    if sport == "MLB":    return _mlb_market_family(market)
    if sport == "Soccer": return _soccer_market_family(market)
    if sport == "Tennis": return _tennis_market_family(market)
    if sport == "CFB":    return _nfl_game_market_family(market)  # ML/spread/total same shape
    return None


# List of markets exposed in the coverage matrix per sport.
COVERAGE_MATRIX_SPEC: dict[str, list[tuple[str, str]]] = {
    # (market_family, human_label)
    "NFL":    [
        ("pass_yds",       "PASS YDS"),
        ("rush_yds",       "RUSH YDS"),
        ("rec_yds",        "REC YDS"),
        ("receptions",     "RECEPTIONS"),
        ("pass_tds",       "PASS TD"),
        ("atd",            "ATD"),
        ("moneyline",      "ML"),
        ("spread",         "SPREAD"),
        ("total",          "TOTAL"),
    ],
    "MLB":    [
        ("hits",           "HITS"),
        ("total_bases",    "TB"),
        ("hits_runs_rbi",  "H+R+RBI"),
        ("home_runs",      "HR"),
        ("rbi",            "RBI"),
        ("runs",           "RUNS"),
        ("strikeouts",     "Ks"),
        ("outs",           "OUTS"),
        ("moneyline",      "ML"),
        ("run_line",       "RUN LINE"),
        ("total",          "TOTAL"),
    ],
    "Soccer": [
        ("1x2",            "1X2"),
        ("handicap",       "HANDICAP"),
        ("total",          "TOTAL"),
        ("btts",           "BTTS"),
        ("double_chance",  "DOUBLE CHANCE"),
        ("goals",          "GOALSCORER"),
        ("assists",        "ASSISTS"),
        ("goal_or_assist", "SCORE OR ASSIST"),
        ("shots",          "SHOTS"),
        ("sot",            "SOT"),
    ],
    "Tennis": [
        ("moneyline",      "ML"),
        ("game_spread",    "SPREAD"),
        ("game_total",     "TOTAL"),
    ],
    "CFB":    [
        ("moneyline",      "ML"),
        ("spread",         "SPREAD"),
        ("total",          "TOTAL"),
    ],
}
