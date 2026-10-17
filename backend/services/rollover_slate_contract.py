"""Rollover TODAY-only contract (B3 — items 48-57).

Rollover semantics:

    BEST QUALIFYING LOCKS OF THE CURRENT SLATE DAY

NOT:
    * next 24 hours
    * tomorrow
    * next weekend
    * future tournament round
    * weeks ahead

Current (pre-B3) defect class:

    rank broad candidate set
      → select Top 3
      → check "within 24h"
      → if none: pool = picks   (← this explicitly allows future picks)

B3 corrects the order:

    canonical published Locks
      → filter to CURRENT SLATE DAY (slate_day_start <= event_time < next_slate_day_start)
      → remove started / invalid events
      → canonical Locks eligibility
      → Rollover safety gates
      → rank TODAY's candidates
      → canonical-event dedupe
      → choose up to 3
      → freeze official slate

If today's qualifying pool has 0 → return 0.  NEVER backfill from a
future day.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Any, Iterable, Optional

# Product timezone is a Perklocks-wide product rule.  Keep it adjacent
# to the Rollover contract so slate-day semantics never drift from the
# product's definition of "today".
PRODUCT_TZ_UTC_OFFSET_HOURS: int = -4   # ET

ROLLOVER_FUTURE_EVENT_REJECTED = "ROLLOVER_FUTURE_EVENT_REJECTED"
SLATE_DATE_MISMATCH             = "SLATE_DATE_MISMATCH"


def _as_dt(iso: str) -> Optional[datetime]:
    if not iso: return None
    s = str(iso)[:19]
    try:
        return datetime.fromisoformat(s.replace("Z", ""))
    except Exception:
        return None


def product_tz() -> timezone:
    return timezone(timedelta(hours=PRODUCT_TZ_UTC_OFFSET_HOURS))


def slate_day_bounds(as_of_iso: str) -> tuple[str, str]:
    """Return (slate_day_start, next_slate_day_start) as ISO strings
    in UTC — the EXACT bounds Rollover uses to decide "today"."""
    tz = product_tz()
    now = _as_dt(as_of_iso) or datetime.now(timezone.utc).replace(tzinfo=None)
    now_product = now.replace(tzinfo=timezone.utc).astimezone(tz)
    start_product = datetime.combine(now_product.date(), time(0, 0),
                                        tzinfo=tz)
    end_product = start_product + timedelta(days=1)
    start_utc = start_product.astimezone(timezone.utc)
    end_utc = end_product.astimezone(timezone.utc)
    return (start_utc.isoformat().replace("+00:00", "Z"),
            end_utc.isoformat().replace("+00:00", "Z"))


def is_today_slate(event_time_iso: str, as_of_iso: str) -> bool:
    """Return True iff ``event_time_iso`` falls within the current
    product-day slate (``slate_day_start <= event_time <
    next_slate_day_start``)."""
    start, end = slate_day_bounds(as_of_iso)
    if not event_time_iso:
        return False
    s = str(event_time_iso)[:19]
    try:
        et = datetime.fromisoformat(s.replace("Z", ""))
    except Exception:
        return False
    ds = _as_dt(start); de = _as_dt(end)
    if ds is None or de is None:
        return False
    return ds <= et < de


def filter_today_only(picks: Iterable[dict], *, as_of_iso: str,
                      drop_started: bool = True) -> list[dict]:
    """Return the subset of ``picks`` whose ``event_time`` belongs to
    the current product slate day.

    * pick_date is NEVER consulted — only ``event_time`` (item 51).
    * Started events are dropped by default (``drop_started``).
    """
    today: list[dict] = []
    now_dt = _as_dt(as_of_iso)
    for p in picks or []:
        et = p.get("event_time") or p.get("game_date") or p.get("date")
        if not is_today_slate(et, as_of_iso):
            continue
        if drop_started and now_dt:
            edt = _as_dt(et)
            if edt is not None and edt <= now_dt:
                continue
        today.append(p)
    return today


def choose_rollover_locks(picks: Iterable[dict],
                           *,
                           as_of_iso: str,
                           safety_filter=None,
                           dedupe_key=None,
                           max_picks: int = 3,
                           min_lock_score: float = 85.0,
                           ) -> dict[str, Any]:
    """Return the chosen Rollover slate (up to ``max_picks``) + full
    counter telemetry for item 57.

    Pipeline (strict, item 49):

      1. Canonical published Locks (``picks`` must already be the
         canonical-published pool — this function does NOT re-rank
         future picks).
      2. filter_today_only(...)
      3. drop started / invalid
      4. ``safety_filter`` (optional, Rollover-specific gates;
         caller-provided so this module stays decoupled from any
         Rollover safety rule).
      5. rank by lock_score DESC
      6. ``dedupe_key(pick)`` dedupe (caller-provided).
      7. choose up to ``max_picks``.

    Returns ``{selected, rollover_telemetry}``.  ``rollover_telemetry``
    carries item-57 counters.
    """
    canonical = list(picks or [])
    today = filter_today_only(canonical, as_of_iso=as_of_iso)
    # Future-day rejections surface in telemetry (item 48).
    rollover_rejected_future_day = max(0, len(canonical) - len(today))
    # Lock score floor + Rollover safety.
    eligible = [p for p in today
                 if (p.get("lock_score") or 0) >= min_lock_score]
    safety_eligible = (safety_filter(eligible) if callable(safety_filter)
                        else list(eligible))
    # Rank: lock_score DESC, then edge_percent DESC.
    def _rank(p):
        return (-(p.get("lock_score") or 0),
                -(p.get("edge_percent") or 0))
    safety_eligible.sort(key=_rank)
    # Canonical-event dedupe.
    if callable(dedupe_key):
        seen = set()
        deduped = []
        for p in safety_eligible:
            k = dedupe_key(p)
            if k in seen:
                continue
            seen.add(k)
            deduped.append(p)
        safety_eligible = deduped
    selected = safety_eligible[:max_picks]
    telemetry = {
        "rollover_canonical_candidates": len(canonical),
        "rollover_today_candidates":     len(today),
        "rollover_rejected_future_day":  rollover_rejected_future_day,
        "rollover_safety_eligible":      len(safety_eligible),
        "rollover_selected":             len(selected),
    }
    return {"selected": selected, "rollover_telemetry": telemetry}


# ─────────────────────────────────────────────────────────────────────
# Official-freeze guard (items 53-54)
# ─────────────────────────────────────────────────────────────────────
def validate_freeze_leg(leg: dict, *, slate_date_iso: str) -> tuple[bool, Optional[str]]:
    """Return (ok, reason).  Fails fast with
    ``ROLLOVER_FUTURE_EVENT_REJECTED`` / ``SLATE_DATE_MISMATCH`` if
    the leg's event_time doesn't belong to the slate day."""
    et = leg.get("event_time") or leg.get("game_date") or leg.get("date")
    if not et:
        return False, SLATE_DATE_MISMATCH
    if not is_today_slate(et, as_of_iso=slate_date_iso):
        return False, ROLLOVER_FUTURE_EVENT_REJECTED
    return True, None


def validate_freeze_slate(legs: Iterable[dict], *, slate_date_iso: str
                           ) -> tuple[bool, list[str]]:
    """Return (ok, failures).  ``ok`` is True iff EVERY leg passes
    ``validate_freeze_leg``.  Caller MUST reject the freeze on False
    rather than silently storing a bad slate (item 53)."""
    failures: list[str] = []
    for leg in legs or []:
        ok, reason = validate_freeze_leg(leg, slate_date_iso=slate_date_iso)
        if not ok:
            failures.append(reason or SLATE_DATE_MISMATCH)
    return (not failures), failures


# ─────────────────────────────────────────────────────────────────────
# Sticky cache / DB-frozen restore guard (item 55)
# ─────────────────────────────────────────────────────────────────────
def filter_sticky_cache_entries(entries: Iterable[dict],
                                 *, as_of_iso: str) -> list[dict]:
    """Return only the cache entries whose ``event_time`` belongs to
    the current product slate day.  TTL is irrelevant — wrong-day
    membership can NEVER be served (item 55)."""
    return filter_today_only(entries, as_of_iso=as_of_iso,
                              drop_started=False)


__all__ = [
    "PRODUCT_TZ_UTC_OFFSET_HOURS",
    "ROLLOVER_FUTURE_EVENT_REJECTED", "SLATE_DATE_MISMATCH",
    "slate_day_bounds", "is_today_slate",
    "filter_today_only", "choose_rollover_locks",
    "validate_freeze_leg", "validate_freeze_slate",
    "filter_sticky_cache_entries",
]
