"""Canonical-truth-only Analytics computations.

2026-08-23 PASS 2 — Rewire main Analytics + Learning to canonical
published-results truth (see ``services.published_results_truth``).

* Population: comes ONLY from ``PublishedResultsTruthService.load()``.
  Never reads raw ``db.picks`` with self-reconstructed eligibility
  rules (no "lock >= 89" / Soccer-alt exceptions / current off_board
  reconstruction).
* Frozen values: uses ``project_publication_time_view()`` so
  historical Lock Score / probability / sportsbook odds are the
  publication-time values, not mutable current fields.
* Zero external calls, zero DB mutation on read, no CLV requested.
"""
from __future__ import annotations

from typing import Any, Iterable


# ── Performance math ─────────────────────────────────────────────
def units_for_win(american: int | float) -> float:
    """Units returned for a 1-unit stake at ``american`` odds on WIN."""
    try:
        o = int(american)
    except (TypeError, ValueError):
        return 0.0
    if o == 0:
        return 0.0
    return o / 100.0 if o > 0 else 100.0 / abs(o)


def units_for_pick(american, status: str) -> float:
    """Net units for a 1-unit stake given the settled status."""
    s = (status or "").lower()
    if s == "won":
        return units_for_win(american)
    if s == "lost":
        return -1.0
    return 0.0  # push / void / pending / unknown


def hit_rate(wins: int, losses: int) -> float:
    """wins / (wins + losses).  Push/void/pending excluded by
    construction — callers only pass decisive counts."""
    dec = wins + losses
    if dec <= 0:
        return 0.0
    return round(wins * 100.0 / dec, 1)


def roi(net_units: float, units_risked: float) -> float:
    if units_risked <= 0:
        return 0.0
    return round(net_units * 100.0 / units_risked, 2)


# ── Bucket helpers ────────────────────────────────────────────────
_PROB_BUCKETS = [(50, 60), (60, 70), (70, 80), (80, 90), (90, 101)]
_PROB_LABELS = ["50-59%", "60-69%", "70-79%", "80-89%", "90%+"]

_LOCK_BUCKETS = [(85, 90), (90, 95), (95, 99), (99, 100), (100, 101)]
_LOCK_LABELS = ["85-89", "90-94", "95-98", "99", "100 Apex"]


def _bucket_index(value: float, buckets):
    for i, (lo, hi) in enumerate(buckets):
        if lo <= value < hi:
            return i
    return None


def _frozen_prob(pick: dict) -> float | None:
    """Prefer the frozen published probability; fall back to
    win_probability captured at publication.  Never a current mutable
    field like ``model_win_prob``."""
    for k in ("published_win_probability", "win_probability_at_pick",
              "frozen_win_probability", "win_probability"):
        v = pick.get(k)
        if v is not None:
            try:
                fv = float(v)
                if 0 <= fv <= 1:
                    return fv * 100.0
                return fv
            except (TypeError, ValueError):
                continue
    return None


def _frozen_lock(pick: dict) -> float | None:
    for k in ("published_lock_score", "lock_score_at_pick",
              "frozen_lock_score", "lock_score"):
        v = pick.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def _frozen_odds(pick: dict):
    for k in ("published_odds", "odds_at_pick", "frozen_odds",
              "book_odds"):
        v = pick.get(k)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
    return None


_MIN_N = 10


def _sample_flag(n: int) -> str | None:
    return "INSUFFICIENT_SAMPLE" if n < _MIN_N else None


# ── Aggregators ──────────────────────────────────────────────────
def compute_calibration(records: Iterable[dict]) -> list[dict]:
    """Frozen-probability calibration buckets.  Push/void/pending
    excluded from the numerator; N tracked."""
    bins = [{"band": lbl, "n": 0, "wins": 0, "losses": 0, "p_sum": 0.0}
            for lbl in _PROB_LABELS]
    for r in records:
        st = (r.get("status") or "").lower()
        if st not in ("won", "lost"):
            continue
        p = _frozen_prob(r)
        if p is None:
            continue
        idx = _bucket_index(p, _PROB_BUCKETS)
        if idx is None:
            continue
        b = bins[idx]
        b["n"] += 1
        b["p_sum"] += p
        if st == "won":
            b["wins"] += 1
        else:
            b["losses"] += 1
    out = []
    for b in bins:
        actual = hit_rate(b["wins"], b["losses"])
        expected = round(b["p_sum"] / b["n"], 1) if b["n"] else 0.0
        out.append({
            "band":         b["band"],
            "n":            b["n"],
            "expected":     expected,
            "actual":       actual,
            "gap":          round(actual - expected, 2),
            "sample_flag":  _sample_flag(b["n"]),
        })
    return out


def compute_brier(records: Iterable[dict]) -> dict:
    """Brier score across decisive canonical-published picks.
    p in [0,1]; outcome win=1 / loss=0.  Push/void/pending excluded."""
    n = 0
    sse = 0.0
    for r in records:
        st = (r.get("status") or "").lower()
        if st not in ("won", "lost"):
            continue
        p = _frozen_prob(r)
        if p is None:
            continue
        p01 = p / 100.0
        outcome = 1.0 if st == "won" else 0.0
        sse += (p01 - outcome) ** 2
        n += 1
    if n == 0:
        return {"n": 0, "brier": None, "sample_flag": "INSUFFICIENT_SAMPLE"}
    return {"n": n, "brier": round(sse / n, 4),
             "sample_flag": _sample_flag(n)}


def compute_lock_performance(records: Iterable[dict]) -> list[dict]:
    """Lock Score is NOT win probability — evaluate separately.
    Buckets: 85-89 / 90-94 / 95-98 / 99 / 100 Apex."""
    bins = [{"tier": lbl, "n": 0, "wins": 0, "losses": 0, "pushes": 0,
              "net_units": 0.0, "units_risked": 0.0}
            for lbl in _LOCK_LABELS]
    for r in records:
        lock = _frozen_lock(r)
        if lock is None:
            continue
        idx = _bucket_index(lock, _LOCK_BUCKETS)
        if idx is None:
            continue
        b = bins[idx]
        b["n"] += 1
        odds = _frozen_odds(r)
        st = (r.get("status") or "").lower()
        u = units_for_pick(odds, st)
        b["net_units"] += u
        if st == "won":
            b["wins"] += 1
            b["units_risked"] += 1.0
        elif st == "lost":
            b["losses"] += 1
            b["units_risked"] += 1.0
        elif st in ("push", "void"):
            b["pushes"] += 1
    out = []
    for b in bins:
        out.append({
            "tier":          b["tier"],
            "n":             b["n"],
            "wins":          b["wins"],
            "losses":        b["losses"],
            "pushes":        b["pushes"],
            "hit_rate":      hit_rate(b["wins"], b["losses"]),
            "net_units":     round(b["net_units"], 2),
            "roi":           roi(b["net_units"], b["units_risked"]),
            "sample_flag":   _sample_flag(b["n"]),
        })
    return out


def compute_overall(records: Iterable[dict]) -> dict:
    """Overall wins / losses / pushes / hit rate / net units / ROI
    from canonical-published + settled records only."""
    wins = losses = pushes = 0
    net = 0.0
    risked = 0.0
    for r in records:
        st = (r.get("status") or "").lower()
        if st == "won":
            wins += 1
            odds = _frozen_odds(r)
            net += units_for_win(odds)
            risked += 1.0
        elif st == "lost":
            losses += 1
            net -= 1.0
            risked += 1.0
        elif st in ("push", "void"):
            pushes += 1
    return {
        "n":             wins + losses + pushes,
        "wins":          wins,
        "losses":        losses,
        "pushes":        pushes,
        "hit_rate":      hit_rate(wins, losses),
        "net_units":     round(net, 2),
        "roi":           roi(net, risked),
        "sample_flag":   _sample_flag(wins + losses),
    }


async def compute_from_canonical_truth(db, *, days: int = 30) -> dict:
    """Single-source Analytics computation.

    Population: ``PublishedResultsTruthService.load()`` — canonical
    published-results truth ONLY.  Generated-but-unpublished picks
    cannot contaminate performance, calibration, Brier, Lock-tier, or
    learning outputs when this is the authoritative source.

    Returns a plain dict — no DB mutation, no provider calls, no CLV.
    """
    from services.published_results_truth import (
        PublishedResultsTruthService, project_publication_time_view,
    )
    svc = PublishedResultsTruthService(db)
    published = await svc.load(days=days)
    frozen = [project_publication_time_view(p) for p in published]
    # Attach frozen prob/lock/odds/status for the aggregators to read
    # without needing to remember which frozen key each caller used.
    for f, p in zip(frozen, published):
        f["published_win_probability"] = (
            p.get("published_win_probability")
            or p.get("win_probability_at_pick")
            or p.get("win_probability")
        )
        f["published_lock_score"] = (
            p.get("published_lock_score")
            or p.get("lock_score_at_pick")
            or p.get("lock_score")
        )
        # Frozen odds already inside project_publication_time_view via
        # published_odds — do not overwrite with mutable book_odds.
    return {
        "population_source":   "PublishedResultsTruthService",
        "days":                days,
        "n_published_settled": sum(
            1 for r in frozen
            if (r.get("status") or "").lower() in ("won", "lost", "push", "void")
        ),
        "overall":             compute_overall(frozen),
        "calibration":         compute_calibration(frozen),
        "brier":               compute_brier(frozen),
        "lock_performance":    compute_lock_performance(frozen),
    }


__all__ = [
    "units_for_win", "units_for_pick", "hit_rate", "roi",
    "compute_calibration", "compute_brier",
    "compute_lock_performance", "compute_overall",
    "compute_from_canonical_truth",
]
