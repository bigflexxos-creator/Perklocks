"""Tennis dynamic strength — REAL evidence layer (Session 6, 2026-09-17).

Purpose: provide the authoritative, data-driven replacement for the
deterministic `_player_hash` heuristics that historically bled synthetic
evidence into `tennis_engine.py`.  Everything here is derived from raw
historical matches in ``tennis_matches_history`` (~38k rows).

Contract:
    * REAL DATA → AVAILABLE, computed on the fly with recency decay.
    * REAL DATA ABSENT → returns ``None`` (MISSING).  Never a synthetic
      hash.  Callers MUST treat ``None`` as "no evidence" — not zero.
    * Small samples are shrunk toward a tour/surface prior and their
      reliability is explicitly reported so the calibration layer can
      down-weight them.

Key exposures:
    ``get_player_snapshot(name, surface, as_of_iso)``
        → :class:`PlayerSnapshot`   (fields: elo, surface_elo, form_l5,
          form_l10, form_l20, sample_size, coverage_pct, reliability,
          recency, missing_flags)
    ``get_h2h_edge(a, b, surface, as_of_iso)``
        → :class:`H2HResult`         (weighted / recency-decayed / shrunk)
    ``compose_matchup(a, b, surface, tier, as_of_iso)``
        → :class:`MatchupEvidence`   used by tennis_engine to replace
          the hash-based helpers.

The module is intentionally read-only: it never mutates the DB or the
running pick documents.  It is safe to call from any request path.

Simple in-process cache keyed by (name_norm, surface, as_of_day) —
snapshots are computed at most once per day per player-surface, which
keeps request latency low even when many picks land in the same slate.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Iterable, Optional


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------

# Elo constants — pinned so behaviour is stable and reproducible.
BASE_ELO             = 1500.0
K_ATP                = 24.0
K_WTA                = 26.0
K_CHALLENGER         = 20.0
K_ITF                = 16.0
SURFACE_ELO_WEIGHT   = 0.35    # blend surface Elo into overall Elo signal
RECENCY_HALF_LIFE_DAYS = 365.0
FORM_HALF_LIFE_DAYS  = 90.0
MIN_MATCHES_STABLE   = 12       # below → high-shrinkage snapshot
SURFACE_MIN_STABLE   = 8

# Missing-data sentinel objects for callers that pattern-match.
MISSING = None


@dataclass
class PlayerSnapshot:
    name:            str
    as_of:           str          # ISO-8601
    surface:         Optional[str]
    elo:             Optional[float] = None
    surface_elo:     Optional[float] = None
    win_pct_52w:     Optional[float] = None
    surface_win_pct: Optional[float] = None
    form_l5:         Optional[float] = None   # weighted 0..1
    form_l10:        Optional[float] = None
    form_l20:        Optional[float] = None
    sample_size:     int = 0
    surface_sample:  int = 0
    recency_days:    Optional[int] = None      # days since last match
    coverage_pct:    float = 0.0               # 0..1 self-reported
    reliability:     float = 0.0               # 0..1 shrinkage-aware
    missing_flags:   list[str] = field(default_factory=list)
    tour:            Optional[str] = None      # ATP / WTA / CH / ITF

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class H2HResult:
    n:                int = 0
    n_same_surface:   int = 0
    weighted_wins:    float = 0.0   # sum of recency weights on wins
    weighted_total:   float = 0.0   # sum of recency weights on all obs
    raw_wins:         int = 0
    win_pct_shrunk:   Optional[float] = None
    win_pct_raw:      Optional[float] = None
    last_date:        Optional[str] = None
    surface_present:  bool = False
    recency_days:     Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MatchupEvidence:
    """Composed evidence that the tennis engine consumes.  Each field is
    strictly derived from :class:`PlayerSnapshot` + :class:`H2HResult`
    — the tennis engine no longer contributes hash-derived numbers."""
    player:           PlayerSnapshot
    opponent:         PlayerSnapshot
    h2h:              H2HResult
    strength_delta:   Optional[float] = None   # player_elo - opponent_elo
    surface_delta:    Optional[float] = None
    form_delta:       Optional[float] = None
    matchup_prob:     Optional[float] = None   # 0..1 pure Elo-derived
    reliability:      float = 0.0
    provenance:       list[str] = field(default_factory=list)
    missing_flags:    list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _iso_day(s: Optional[str]) -> str:
    if not s: return datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return str(s)[:10]


def _parse_date(iso: Optional[str]) -> Optional[datetime]:
    if not iso: return None
    try: return datetime.fromisoformat(str(iso)[:10])
    except Exception: return None


def _days_between(a: Optional[str], b: Optional[str]) -> Optional[int]:
    da = _parse_date(a); db = _parse_date(b)
    if not da or not db: return None
    return abs((db - da).days)


def _recency_weight(days: Optional[int], half_life: float) -> float:
    if days is None: return 0.0
    return 0.5 ** (days / max(1.0, half_life))


def _norm_name(s: Optional[str]) -> str:
    return (s or "").strip().lower().replace(".", "")


def _tour_k(tour: Optional[str]) -> float:
    t = (tour or "").upper()
    if "ATP" in t: return K_ATP
    if "WTA" in t: return K_WTA
    if "CH" in t:  return K_CHALLENGER
    if "ITF" in t or "FUT" in t or t.startswith("M15") or t.startswith("W15"): return K_ITF
    return K_ATP


def _elo_prob(delta: float) -> float:
    return 1.0 / (1.0 + 10 ** (-delta / 400.0))


def _shrunk_rate(hits: float, total: float, prior: float, prior_weight: float) -> float:
    if total <= 0: return prior
    return (hits + prior * prior_weight) / (total + prior_weight)


# ---------------------------------------------------------------------------
# In-process cache
# ---------------------------------------------------------------------------

_SNAPSHOT_CACHE: dict[tuple[str, str, str], tuple[float, PlayerSnapshot]] = {}
_H2H_CACHE:      dict[tuple[str, str, str, str], tuple[float, H2HResult]] = {}
_CACHE_TTL_SEC   = 3600.0


def _cache_get(store: dict, key: tuple) -> Any:
    v = store.get(key)
    if not v: return None
    ts, val = v
    if time.monotonic() - ts > _CACHE_TTL_SEC:
        store.pop(key, None); return None
    return val


def _cache_put(store: dict, key: tuple, val: Any) -> Any:
    store[key] = (time.monotonic(), val)
    return val


# ---------------------------------------------------------------------------
# Core: per-player match feed
# ---------------------------------------------------------------------------

async def _fetch_player_matches(db, name: str, as_of: str) -> list[dict]:
    """Return raw match rows for a player up to (exclusive) as_of.  Both
    winner_name and loser_name are queried.  Sorted by date ascending —
    this order matters for Elo accumulation."""
    if not name: return []
    cursor = db.tennis_matches_history.find(
        {"$or": [{"winner_name": name}, {"loser_name": name}],
         "date": {"$lt": as_of}}
    ).sort("date", 1).limit(400)
    return [m async for m in cursor]


async def _fetch_h2h_matches(db, a: str, b: str, as_of: str) -> list[dict]:
    if not a or not b: return []
    cursor = db.tennis_matches_history.find(
        {"$or": [
            {"winner_name": a, "loser_name": b},
            {"winner_name": b, "loser_name": a},
        ], "date": {"$lt": as_of}}
    ).sort("date", 1)
    return [m async for m in cursor]


# ---------------------------------------------------------------------------
# Player snapshot
# ---------------------------------------------------------------------------

async def get_player_snapshot(db, name: str, surface: Optional[str] = None,
                              as_of_iso: Optional[str] = None
                              ) -> Optional[PlayerSnapshot]:
    """Compute (or cache-hit) the player's dynamic strength snapshot.
    Returns ``None`` only if the DB call fails; if the player is truly
    unknown we still return a snapshot with ``missing_flags`` set and
    ``reliability=0``."""
    as_of = _iso_day(as_of_iso)
    surf  = (surface or "").capitalize() if surface else None
    key   = (_norm_name(name), surf or "-", as_of)
    hit = _cache_get(_SNAPSHOT_CACHE, key)
    if hit is not None:
        return hit

    matches = await _fetch_player_matches(db, name, as_of)
    snap = PlayerSnapshot(name=name, as_of=as_of, surface=surf)
    if not matches:
        snap.missing_flags.append("no_match_history")
        snap.reliability = 0.0
        snap.coverage_pct = 0.0
        return _cache_put(_SNAPSHOT_CACHE, key, snap)

    # Roll Elo forward chronologically.
    elo_o: dict[str, float] = {}          # overall Elo per opponent
    elo_p = BASE_ELO
    surf_elo_p = BASE_ELO
    surf_elo_o: dict[str, float] = {}
    n = 0
    surf_n = 0
    wins_52w = 0; total_52w = 0
    wins_surf = 0; total_surf = 0
    form_w_l5  = [0.0, 0.0]  # [wins, total] with recency weight
    form_w_l10 = [0.0, 0.0]
    form_w_l20 = [0.0, 0.0]
    last_date: Optional[str] = None
    tour_counter: dict[str, int] = {}

    # Preload last 20 dates so we know their L5/L10/L20 windows.
    recent = matches[-20:]

    for i, m in enumerate(matches):
        w_name = m.get("winner_name"); l_name = m.get("loser_name")
        m_date = str(m.get("date") or "")[:10]
        last_date = m_date
        is_win = (w_name == name)
        opp    = l_name if is_win else w_name
        m_surface = (m.get("surface") or "").capitalize() or None
        m_tour = (m.get("tourney_level") or "").upper()
        tour_counter[m_tour] = tour_counter.get(m_tour, 0) + 1
        k = _tour_k(m_tour)

        # Opponent Elo (with default) — we don't recompute opponent's
        # history; we just track a pooled default so the delta is
        # meaningful (this is the classical Elo shortcut).
        opp_e = elo_o.setdefault(opp or "_unknown_", BASE_ELO)
        expected = _elo_prob(elo_p - opp_e)
        actual   = 1.0 if is_win else 0.0
        elo_p    += k * (actual - expected)
        elo_o[opp or "_unknown_"] = opp_e + k * ((1 - actual) - (1 - expected))

        # Surface Elo bookkeeping
        if surf and m_surface == surf:
            opp_se = surf_elo_o.setdefault(opp or "_unknown_", BASE_ELO)
            expected_s = _elo_prob(surf_elo_p - opp_se)
            surf_elo_p += k * (actual - expected_s)
            surf_elo_o[opp or "_unknown_"] = opp_se + k * ((1 - actual) - (1 - expected_s))
            surf_n += 1
            total_surf += 1
            if is_win: wins_surf += 1

        n += 1
        # 52-week rolling
        rec_days = _days_between(m_date, as_of)
        if rec_days is not None and rec_days <= 365:
            total_52w += 1
            if is_win: wins_52w += 1

        # Recency-weighted form buckets — last 5/10/20 matches by index
        if i >= len(matches) - 20:
            w = _recency_weight(rec_days, FORM_HALF_LIFE_DAYS)
            form_w_l20[1] += w
            if is_win: form_w_l20[0] += w
            if i >= len(matches) - 10:
                form_w_l10[1] += w
                if is_win: form_w_l10[0] += w
            if i >= len(matches) - 5:
                form_w_l5[1] += w
                if is_win: form_w_l5[0] += w

    # Tour classification — pick modal tour.
    tour = None
    if tour_counter:
        modal = max(tour_counter.items(), key=lambda kv: kv[1])[0]
        if modal.startswith("A") or "ATP" in modal:   tour = "ATP"
        elif modal.startswith("W") or "WTA" in modal: tour = "WTA"
        elif modal.startswith("C") or "CH" in modal:  tour = "CH"
        elif modal.startswith("F") or "IT" in modal or modal.startswith("M15") or modal.startswith("W15"):
            tour = "ITF"

    def _pct(pair): return pair[0] / pair[1] if pair[1] > 0 else None

    snap.elo             = round(elo_p, 1)
    snap.surface_elo     = round(surf_elo_p, 1) if surf_n > 0 else None
    snap.win_pct_52w     = round(wins_52w / total_52w, 3) if total_52w else None
    snap.surface_win_pct = round(wins_surf / total_surf, 3) if total_surf else None
    snap.form_l5         = round(_pct(form_w_l5),  3) if form_w_l5[1]  else None
    snap.form_l10        = round(_pct(form_w_l10), 3) if form_w_l10[1] else None
    snap.form_l20        = round(_pct(form_w_l20), 3) if form_w_l20[1] else None
    snap.sample_size     = n
    snap.surface_sample  = surf_n
    snap.recency_days    = _days_between(last_date, as_of)
    snap.tour            = tour

    # Reliability: shrunk toward stability threshold.
    r_overall = min(1.0, n / MIN_MATCHES_STABLE)
    r_surface = min(1.0, surf_n / SURFACE_MIN_STABLE) if surf else 1.0
    r_recency = 1.0 if (snap.recency_days is not None and snap.recency_days <= 120) else \
                (0.6 if (snap.recency_days is not None and snap.recency_days <= 365) else 0.3)
    snap.reliability = round(r_overall * r_surface * r_recency, 3)
    snap.coverage_pct = round(min(1.0, n / (MIN_MATCHES_STABLE * 4)), 3)

    # Missing flags — honest disclosure of what wasn't derivable.
    if snap.surface_elo is None:                      snap.missing_flags.append("no_surface_elo")
    if snap.surface_win_pct is None and surf:         snap.missing_flags.append(f"no_surface_wp:{surf}")
    if snap.form_l5 is None:                          snap.missing_flags.append("no_form_l5")
    if snap.form_l10 is None:                         snap.missing_flags.append("no_form_l10")

    return _cache_put(_SNAPSHOT_CACHE, key, snap)


# ---------------------------------------------------------------------------
# H2H
# ---------------------------------------------------------------------------

async def get_h2h_edge(db, a: str, b: str, surface: Optional[str] = None,
                        as_of_iso: Optional[str] = None) -> H2HResult:
    as_of = _iso_day(as_of_iso)
    surf  = (surface or "").capitalize() if surface else None
    key = (_norm_name(a), _norm_name(b), surf or "-", as_of)
    hit = _cache_get(_H2H_CACHE, key)
    if hit is not None: return hit

    matches = await _fetch_h2h_matches(db, a, b, as_of)
    r = H2HResult()
    if not matches:
        return _cache_put(_H2H_CACHE, key, r)

    last: Optional[str] = None
    for m in matches:
        winner = m.get("winner_name")
        m_surface = (m.get("surface") or "").capitalize() or None
        m_date = str(m.get("date") or "")[:10]
        last = m_date
        rec_days = _days_between(m_date, as_of)
        # Recency + same-surface uplift.
        w = _recency_weight(rec_days, RECENCY_HALF_LIFE_DAYS)
        if surf and m_surface == surf:
            w *= 1.5
            r.n_same_surface += 1
            r.surface_present = True
        r.n += 1
        r.weighted_total += w
        if winner == a:
            r.weighted_wins += w
            r.raw_wins += 1

    prior = 0.5       # neutral prior
    prior_weight = 2.5  # 2.5 "phantom" matches on 50/50
    if r.weighted_total > 0:
        r.win_pct_raw = round(r.raw_wins / r.n, 3)
        r.win_pct_shrunk = round(_shrunk_rate(r.weighted_wins, r.weighted_total,
                                              prior, prior_weight), 3)
    r.last_date = last
    r.recency_days = _days_between(last, as_of)
    return _cache_put(_H2H_CACHE, key, r)


# ---------------------------------------------------------------------------
# Composite matchup evidence  ← consumed by tennis_engine
# ---------------------------------------------------------------------------

async def compose_matchup(db, player: str, opponent: str,
                          surface: Optional[str] = None,
                          tier: Optional[int] = None,
                          as_of_iso: Optional[str] = None
                          ) -> MatchupEvidence:
    p_snap = await get_player_snapshot(db, player, surface, as_of_iso)
    o_snap = await get_player_snapshot(db, opponent, surface, as_of_iso)
    h2h = await get_h2h_edge(db, player, opponent, surface, as_of_iso)
    evidence = MatchupEvidence(player=p_snap, opponent=o_snap, h2h=h2h)
    evidence.provenance.append("tennis_matches_history")

    if p_snap and o_snap and p_snap.elo is not None and o_snap.elo is not None:
        blended_p = p_snap.elo
        blended_o = o_snap.elo
        if surface and p_snap.surface_elo is not None and o_snap.surface_elo is not None:
            blended_p = (1 - SURFACE_ELO_WEIGHT) * p_snap.elo + SURFACE_ELO_WEIGHT * p_snap.surface_elo
            blended_o = (1 - SURFACE_ELO_WEIGHT) * o_snap.elo + SURFACE_ELO_WEIGHT * o_snap.surface_elo
        evidence.strength_delta = round(blended_p - blended_o, 1)
        evidence.matchup_prob   = round(_elo_prob(blended_p - blended_o), 4)
        if p_snap.surface_elo is not None and o_snap.surface_elo is not None:
            evidence.surface_delta = round(p_snap.surface_elo - o_snap.surface_elo, 1)
        if p_snap.form_l10 is not None and o_snap.form_l10 is not None:
            evidence.form_delta = round(p_snap.form_l10 - o_snap.form_l10, 3)
    else:
        evidence.missing_flags.append("insufficient_elo_history")

    # Composite reliability is the joint minimum — the weaker side dominates.
    r = min(p_snap.reliability if p_snap else 0.0,
            o_snap.reliability if o_snap else 0.0)
    # H2H boost only when meaningful sample AND recent
    if h2h.n >= 3 and h2h.recency_days is not None and h2h.recency_days <= 730:
        r = min(1.0, r + 0.05)
    evidence.reliability = round(r, 3)
    return evidence


# ---------------------------------------------------------------------------
# Public helpers used by tennis_engine for the 5 component scores
# ---------------------------------------------------------------------------

def evidence_to_surface_score(ev: MatchupEvidence) -> Optional[float]:
    """0..100 surface fit score derived from real surface Elo delta.
    Returns None (MISSING) when either side lacks surface history.
    """
    if not ev or ev.surface_delta is None: return None
    # +200 delta → ~76%, +400 → ~91%. Cap at 40..99 to preserve
    # differentiation without inflating certainty.
    prob = _elo_prob(ev.surface_delta)     # 0..1 relative to opponent
    return round(40.0 + prob * 55.0, 1)


def evidence_to_form_score(ev: MatchupEvidence) -> Optional[float]:
    """0..100 opponent-adjusted form score.  Uses recency-weighted L10
    win rate DIFFERENCE against a shrunken prior.  MISSING if neither
    player has enough form."""
    if not ev or ev.form_delta is None: return None
    # form_delta ∈ roughly [-1, +1].  Map to 40..95.
    return round(max(40.0, min(95.0, 60.0 + ev.form_delta * 45.0)), 1)


def evidence_to_matchup_score(ev: MatchupEvidence) -> Optional[float]:
    """0..100 raw Elo-strength score (not price-anchored)."""
    if not ev or ev.matchup_prob is None: return None
    return round(40.0 + ev.matchup_prob * 55.0, 1)


def evidence_to_h2h_score(ev: MatchupEvidence) -> Optional[float]:
    """0..100 H2H edge with heavy shrinkage on small/old samples.
    MISSING when there are no prior meetings."""
    h = ev.h2h if ev else None
    if not h or h.n == 0 or h.win_pct_shrunk is None: return None
    # Small sample → shrink toward 50; large sample → allow to stray.
    dampen = min(1.0, h.n / 8.0)
    signal = 0.5 + (h.win_pct_shrunk - 0.5) * dampen
    return round(40.0 + signal * 55.0, 1)


def evidence_reliability(ev: MatchupEvidence) -> float:
    return ev.reliability if ev else 0.0
