"""ROLLOVER SHARED SELECTOR (PERKLOCKS MAIN 36 · P1 · 2026-06-30).

One pure selector/ranker function called by BOTH:
  • the live ``/api/picks/rollover`` endpoint (production membership).
  • historical replay / analytics / backtests (candidate universe).

No duplicated scoring formula — live and replay always agree.

Contract:
  • Only ``LIVE_FROZEN_SELECTION`` rows count toward Rollover membership
    / prospective performance.  Candidate-only rows are RESEARCH.
  • Dedupe by ``canonical_event_id`` (never the display-string event).
  • Selector may legitimately return 3, 2, 1, or 0 picks — no forced
    weak third leg.
  • Fail-closed on the canonical-eligibility, market-identity,
    settlement-capability, model-integrity, provenance and
    PublishedPickContract checks — no ``except Exception: pass``.

Version: bump when the ranker changes so DB-stamped selections are
auditable across selector generations.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional


SELECTOR_VERSION = "rollover_selector.v2.p36"


# ─────────────────────────────────────────────────────────────────
# V5 FILTERS (retired: H+R+RBI blacklist + -140/-110 dead-zone).
# ─────────────────────────────────────────────────────────────────
LOCK_FLOOR       = 89
LOCK_DEAD_LO     = 80
LOCK_DEAD_HI     = 85
WP_FLOOR         = 0.60
EDGE_FLOOR       = 0.0
EDGE_CAP         = 12.0
CHALK_CAP        = -350
MAX_LEGS         = 3

# NOTE (P1.4): The stale ``-140 to -110`` odds dead-zone rule and the
# MLB H+R+RBI blacklist have been RETIRED — they contradicted current
# model research.  Market performance is now contextual only via
# ``MARKET_BOOSTS`` below; nothing is hard-banned outside genuine
# non-settleable families.  Do NOT re-introduce new arbitrary bans.
MARKET_BOOSTS = [
    (re.compile(r"win or draw|double chance",              re.I), 1.15),
    (re.compile(r"\bstrikeouts?\b",                        re.I), 1.10),
    (re.compile(r"total goals",                            re.I), 1.05),
    (re.compile(r"tennis moneyline|match winner",          re.I), 1.05),
    (re.compile(r"run line|spread|handicap",               re.I), 1.02),
    (re.compile(r"\bhits\b(?!.*runs.*rbi)",                re.I), 1.00),
]

# Non-settleable / product-banned families ONLY (goalscorer family
# cannot settle reliably in our provider mix).
NON_SETTLEABLE_MARKET_RE = re.compile(
    r"goal scorer|to score or assist|score or assist|score and assist|"
    r"score & assist|to score 2|to score 3|hat.?trick|first goal|"
    r"last goal|winning goal|to assist",
    re.I,
)


def _norm_prob(v) -> float:
    if v is None:
        return 0.0
    try:
        f = float(v)
    except Exception:
        return 0.0
    return f / 100.0 if f > 1.0 else f


def _market_multiplier(market: str) -> float:
    m = market or ""
    for pat, boost in MARKET_BOOSTS:
        if pat.search(m):
            return boost
    return 1.0


def passes_v5(p: dict) -> tuple[bool, str]:
    """Return (accept, reject_reason)."""
    lock = float(
        p.get("published_lock_score")
        if p.get("published_lock_score") is not None
        else (p.get("lock_score") or 0)
    )
    odds = float(p.get("book_odds") or -9999)
    edge = float(p.get("edge_percent") or 0)
    wp   = _norm_prob(p.get("win_probability"))
    if NON_SETTLEABLE_MARKET_RE.search(p.get("market") or ""):
        return False, "non_settleable_market"
    if lock < LOCK_FLOOR:
        return False, "lock<89"
    if LOCK_DEAD_LO <= lock < LOCK_DEAD_HI:
        return False, "lock_dead_zone_80-84"
    if wp < WP_FLOOR:
        return False, "wp<0.60"
    if edge < EDGE_FLOOR:
        return False, "edge_negative"
    if edge > EDGE_CAP:
        return False, "edge>12_inverted"
    if odds < CHALK_CAP:
        return False, "odds<-350_chalk"
    return True, ""


def ev_score(p: dict) -> float:
    """Composite ranker — identical for live + replay."""
    wp = _norm_prob(p.get("win_probability"))
    sim = _norm_prob(p.get("sim_win_probability")) or wp
    edge = float(p.get("edge_percent") or 0)
    odds = float(p.get("book_odds") or -100)
    edge_norm = max(0.0, min(1.0, edge / 8.0))
    alt_bonus = 1.0 if p.get("is_alt") else 0.0
    base = 0.55 * wp + 0.20 * sim + 0.15 * edge_norm + 0.10 * alt_bonus
    mkt_mult = _market_multiplier(p.get("market") or "")
    chalk_pen = min(0.30, (abs(odds) - 200) / 500.0) if odds <= -200 else 0.0
    sig = p.get("historical_signal") or {}
    if sig.get("label") == "hot" and float(sig.get("consistency") or 0) >= 0.7:
        hist_mult = 1.05
    elif sig.get("label") == "cold":
        hist_mult = 0.95
    else:
        hist_mult = 1.0
    ss = p.get("signal_score")
    try:
        sig_mult = (1.0 + ((float(ss) - 50.0) / 50.0) * 0.08) if ss is not None else 1.0
    except (TypeError, ValueError):
        sig_mult = 1.0
    return base * mkt_mult * (1.0 - chalk_pen) * hist_mult * sig_mult


def canonical_event_key(p: dict) -> str:
    """Return the canonical event key for uniqueness dedupe.

    Prefer ``canonical_event_id`` (immutable, provider-stable).  Fall
    back to ``event_id`` and only last to the display ``event`` string
    when neither is present.  Never use the display string when a
    canonical id exists.
    """
    return (
        str(p.get("canonical_event_id")
            or p.get("event_id")
            or p.get("event")
            or "")
    )


def select_rollover_top(
    candidates: Iterable[dict],
    *,
    max_legs: int = MAX_LEGS,
) -> tuple[list[dict], dict]:
    """Pure selector: apply filter, rank, dedupe by canonical_event_id,
    keep top ``max_legs``.  Returns (picks, reject_reasons_summary).

    Legitimately returns [] when no candidate passes the gate — no
    forced weak third leg (P1.6).
    """
    reject: dict[str, int] = {}
    accepted: list[dict] = []
    for p in candidates:
        ok, reason = passes_v5(p)
        if ok:
            accepted.append(p)
        else:
            reject[reason] = reject.get(reason, 0) + 1
    accepted.sort(key=ev_score, reverse=True)
    # P1.3 — canonical event uniqueness.
    seen: set[str] = set()
    top: list[dict] = []
    for p in accepted:
        key = canonical_event_key(p)
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        top.append(p)
        if len(top) >= max_legs:
            break
    return top, reject


def is_live_frozen_selection(pick: dict) -> bool:
    """Return True IFF the pick was frozen as an actual live Rollover
    selection (P1.1).  Generation-time-only candidates must return False
    so they never contaminate prospective performance.
    """
    return (
        bool(pick.get("on_rollover_at"))
        and pick.get("rollover_frozen_source") == "picks_route_live"
        and pick.get("rollover_selection_rank") in (1, 2, 3)
    )


def freeze_metadata(*, rank: int, stamped_at: str,
                     canonical_event_id: Optional[str] = None) -> dict:
    """The metadata every LIVE_FROZEN_SELECTION row carries (P1.7).

    Settlement appends outcome only — never rewrites these fields.
    """
    return {
        "on_rollover_at":            stamped_at,
        "rollover_frozen_source":    "picks_route_live",
        "rollover_selection_rank":   rank,
        "rollover_selector_version": SELECTOR_VERSION,
        "rollover_canonical_event_id": canonical_event_id,
    }


__all__ = [
    "SELECTOR_VERSION",
    "LOCK_FLOOR", "LOCK_DEAD_LO", "LOCK_DEAD_HI",
    "WP_FLOOR", "EDGE_FLOOR", "EDGE_CAP", "CHALK_CAP", "MAX_LEGS",
    "MARKET_BOOSTS",
    "passes_v5", "ev_score", "canonical_event_key",
    "select_rollover_top", "is_live_frozen_selection",
    "freeze_metadata",
]
