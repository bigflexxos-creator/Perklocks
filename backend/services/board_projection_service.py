"""P0.2d — Canonical Board Projection Service.

Single canonical boundary between production Locks-board consumers and
the canonical pick/prediction truth.  READ-ONLY.

Architecture:

    canonical published picks (`db.picks` post-ingestion)
              +
    canonical eligibility  (services.main_board_eligibility)
              +
    canonical lifecycle    (event_time / status window)
              ↓
    BoardProjectionService
              ↓
    /api/picks/today  •  /api/picks/all  •  /api/picks/markets/{sport}
              ↓
    web preview  •  mobile app  •  sport tabs

This service does NOT:

  * generate picks
  * score picks (Lock Score / Magic / APEX)
  * fabricate market lines
  * settle picks
  * mutate canonical records

It DOES:

  * consume canonical eligibility via `is_main_board_eligible`
  * apply canonical filters (sport / market / league / lifecycle)
  * canonical deduplication by `id` (canonical_pick_id fallback)
  * deterministic sort with stable tie-breaker
  * preserve frozen pregame values verbatim

Design invariants (P0.2d spec §5, §7, §8, §14):

  §5  Board consumes ``is_main_board_eligible`` — never re-scores.
  §7  Dedupe by canonical id; distinct alt-lines with different
      (market, line, side) tuples remain distinct.
  §8  Pipeline order:
        load pool → normalize → dedupe → filter → lifecycle → sort
  §14 Deterministic sort with stable tie-breakers
      (canonical_pick_id, event_time, event_id).
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, Optional


# ─── Canonical projection identity ─────────────────────────────────
def _canonical_pick_id(pick: dict) -> str:
    """Best-available canonical id for dedupe / sort tie-break."""
    return (pick.get("id")
            or pick.get("canonical_pick_id")
            or pick.get("prediction_id")
            or pick.get("_id")
            or "")


def _canonical_market_identity(pick: dict) -> tuple:
    """Tuple of (market, side, line) — the canonical alt-line key.

    Distinct legitimate alt-lines have distinct (market, side, line);
    exact canonical duplicates share the same tuple + canonical id.
    """
    return (
        (pick.get("market") or "").strip().lower(),
        (pick.get("side") or pick.get("selection") or "").strip().lower(),
        pick.get("line"),
    )


# ─── Canonical dedupe (§7) ─────────────────────────────────────────
def dedupe_canonical(picks: Iterable[dict]) -> list[dict]:
    """Collapse exact canonical duplicates while preserving distinct
    alt-lines.  A duplicate is defined as SAME canonical_pick_id, or
    SAME (event_id + market + side + line) tuple when the id is
    missing.  Order-preserving on first occurrence."""
    seen_ids: set[str] = set()
    seen_identities: set[tuple] = set()
    out: list[dict] = []
    for p in picks:
        pid = _canonical_pick_id(p)
        if pid and pid in seen_ids:
            continue
        ev = (p.get("event_id") or p.get("fanduel_event_id") or "").strip()
        identity = (ev,) + _canonical_market_identity(p)
        if not pid and identity in seen_identities:
            continue
        if pid:
            seen_ids.add(pid)
        seen_identities.add(identity)
        out.append(p)
    return out


# ─── Deterministic sort (§14) ──────────────────────────────────────
def _sort_key_lock_desc(pick: dict) -> tuple:
    """Primary: canonical Lock Score desc.  Tie-break: event_time,
    canonical_pick_id, event_id — all deterministic."""
    pls = pick.get("published_lock_score")
    ls  = pick.get("lock_score")
    lock = pls if pls is not None else (ls if ls is not None else 0.0)
    try:
        lock = float(lock)
    except (TypeError, ValueError):
        lock = 0.0
    return (
        -lock,                                    # highest lock first
        pick.get("event_time") or "",             # earlier event first
        _canonical_pick_id(pick),                 # stable pk tie-break
        pick.get("event_id") or "",
    )


def deterministic_sort(picks: Iterable[dict]) -> list[dict]:
    return sorted(picks, key=_sort_key_lock_desc)


# ─── Sport filter (§6) ─────────────────────────────────────────────
def filter_sport(picks: Iterable[dict], sport: Optional[str]) -> list[dict]:
    """Sport-tab projections narrow the canonical pool.  ``None`` or
    ``"all"`` returns the pool unchanged."""
    if not sport or sport.lower() == "all":
        return list(picks)
    s = sport.strip().lower()
    return [p for p in picks
            if (p.get("sport") or "").strip().lower() == s]


# ─── The projection service ────────────────────────────────────────
class BoardProjectionService:
    """P0.2d canonical Locks-board projection.

    Consumers pass a set of already-loaded canonical picks (typically
    the day's slate) plus the requested filters; the service returns
    the deterministic board projection.  Loading itself is delegated
    to callers so this service stays isolated from the ingestion path
    and easy to test with in-memory fixtures.
    """

    def __init__(self,
                 eligibility_fn: Optional[Callable[[dict], bool]] = None):
        """`eligibility_fn` defaults to
        ``services.main_board_eligibility.is_main_board_eligible``
        so we consume canonical eligibility (§5) without re-scoring."""
        if eligibility_fn is None:
            from services.main_board_eligibility import is_main_board_eligible
            eligibility_fn = is_main_board_eligible
        self._eligible = eligibility_fn

    def filter_eligible(self, picks: Iterable[dict]) -> list[dict]:
        """§5 — consume canonical publication/eligibility.  Also
        excludes explicit off_board / no_bet / hide_from_main_board
        picks (per production `is_main_board_eligible` contract)."""
        out: list[dict] = []
        for p in picks:
            if p.get("no_bet") is True:
                continue
            if p.get("off_board") is True:
                continue
            if p.get("hide_from_main_board") is True:
                continue
            if not self._eligible(p):
                continue
            out.append(p)
        return out

    def project(self,
                picks: Iterable[dict],
                *,
                sport: Optional[str] = None,
                lifecycle_filter: Optional[Callable[[list[dict]],
                                                     list[dict]]] = None,
                ) -> list[dict]:
        """Canonical projection pipeline (§8):

            eligible → dedupe → sport filter → lifecycle → sort
        """
        pool = self.filter_eligible(list(picks))
        pool = dedupe_canonical(pool)
        pool = filter_sport(pool, sport)
        if lifecycle_filter is not None:
            pool = lifecycle_filter(pool)
        pool = deterministic_sort(pool)
        return pool

    def project_ids(self,
                     picks: Iterable[dict],
                     *,
                     sport: Optional[str] = None,
                     lifecycle_filter: Optional[Callable[[list[dict]],
                                                          list[dict]]] = None,
                     ) -> list[str]:
        """Canonical membership only — for cross-surface parity tests."""
        return [_canonical_pick_id(p) for p in self.project(
                picks, sport=sport, lifecycle_filter=lifecycle_filter)]


__all__ = [
    "BoardProjectionService",
    "dedupe_canonical",
    "deterministic_sort",
    "filter_sport",
    "_canonical_pick_id",
]
