"""Canonical Locks-eligibility authority — Root Closure 2026-06.

ONE eligibility answer per prediction.  Every consumer (Locks
board, Pick Breakdown, Rollover, Parlay, My Bets, History)
must read from this authority instead of independently re-scoring
the pick.

Contract (Root Closure §1-§4):

    IF   publication_state == "PUBLISHED"
    AND  active_revision == True
    AND  published_lock_score >= 85
    AND  event still pregame
    AND  off_board != True
    AND  no_bet != True
    AND  hide_from_main_board != True
    AND  excluded_from_history != True
    THEN locks_eligibility.eligible == True
         → canonical_prediction_id MUST appear on the Locks board.

Every ≥85 published prediction that is NOT eligible must carry an
explicit canonical `reason_code` from the fixed enum below —
NEVER "unknown" / "filtered" / "top_n" / "ranked_out".
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional

# ── Canonical reason codes (fixed enum — forbidden to invent new ones) ──
CANONICAL_LOCK_FLOOR = 85.0

# Eligibility states
CURRENT_PREGAME_LOCK      = "CURRENT_PREGAME_LOCK"
BLOCKED                   = "BLOCKED"
SUPERSEDED                = "SUPERSEDED"
EXPIRED                   = "EXPIRED"

# Canonical exclusion reasons — the ONLY acceptable values.
REASON_EVENT_STARTED           = "EVENT_STARTED"
REASON_EVENT_FINAL             = "EVENT_FINAL"
REASON_MARKET_WITHDRAWN        = "MARKET_WITHDRAWN"
REASON_REAL_LINE_INVALID       = "REAL_LINE_INVALID"
REASON_MODEL_UNAVAILABLE       = "MODEL_UNAVAILABLE"
REASON_EVIDENCE_INSUFFICIENT   = "EVIDENCE_INSUFFICIENT"
REASON_SETTLEMENT_UNSUPPORTED  = "SETTLEMENT_UNSUPPORTED"
REASON_SUPERSEDED              = "SUPERSEDED"
REASON_PUBLICATION_REVOKED     = "PUBLICATION_REVOKED"
REASON_BELOW_85                = "BELOW_85"
REASON_OFF_BOARD               = "OFF_BOARD"
REASON_NO_BET                  = "NO_BET"
REASON_HIDDEN_MAIN_BOARD       = "HIDDEN_MAIN_BOARD"
REASON_NOT_PUBLISHED           = "NOT_PUBLISHED"

_CANONICAL_REASONS = frozenset({
    REASON_EVENT_STARTED, REASON_EVENT_FINAL, REASON_MARKET_WITHDRAWN,
    REASON_REAL_LINE_INVALID, REASON_MODEL_UNAVAILABLE,
    REASON_EVIDENCE_INSUFFICIENT, REASON_SETTLEMENT_UNSUPPORTED,
    REASON_SUPERSEDED, REASON_PUBLICATION_REVOKED,
    REASON_BELOW_85, REASON_OFF_BOARD, REASON_NO_BET,
    REASON_HIDDEN_MAIN_BOARD, REASON_NOT_PUBLISHED,
})


def _parse_event_time(pick: dict) -> Optional[datetime]:
    et = pick.get("event_time")
    if et is None:
        return None
    if isinstance(et, datetime):
        return et if et.tzinfo else et.replace(tzinfo=timezone.utc)
    if isinstance(et, str):
        try:
            dt = datetime.fromisoformat(et.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None
    return None


def compute_locks_eligibility(pick: dict, *, now: Optional[datetime] = None) -> dict:
    """Compute the ONE canonical eligibility answer for a pick.

    Returns a stable dict shape:
        {
          "eligible": bool,
          "state":    CURRENT_PREGAME_LOCK | BLOCKED | SUPERSEDED | EXPIRED,
          "reason_code": <canonical enum> | None,
          "evaluated_at": ISO-8601 UTC,
          "publication_revision": <int|None>,
        }
    """
    now = now or datetime.now(timezone.utc)

    # 1. Publication authority
    pub_state = pick.get("publication_state")
    if pub_state != "PUBLISHED":
        return {
            "eligible": False, "state": BLOCKED,
            "reason_code": REASON_NOT_PUBLISHED,
            "evaluated_at": now.isoformat().replace("+00:00", "Z"),
            "publication_revision": pick.get("publication_revision"),
        }

    # 2. Superseded revisions never on the current board
    rev_state = (pick.get("revision_state") or "").upper()
    if rev_state in ("SUPERSEDED", "SUPERSEDED_IN_RUN"):
        return {
            "eligible": False, "state": SUPERSEDED,
            "reason_code": REASON_SUPERSEDED,
            "evaluated_at": now.isoformat().replace("+00:00", "Z"),
            "publication_revision": pick.get("publication_revision"),
        }

    # 3. Explicit off-board / no-bet / hidden
    if pick.get("off_board") is True:
        return _blocked(pick, REASON_OFF_BOARD, now)
    if pick.get("no_bet") is True:
        return _blocked(pick, REASON_NO_BET, now)
    if pick.get("hide_from_main_board") is True:
        return _blocked(pick, REASON_HIDDEN_MAIN_BOARD, now)

    # 4. Canonical Lock floor
    lock = pick.get("published_lock_score")
    if lock is None:
        lock = pick.get("lock_score")
    try:
        lockf = float(lock) if lock is not None else 0.0
    except Exception:
        lockf = 0.0
    if lockf < CANONICAL_LOCK_FLOOR:
        return _blocked(pick, REASON_BELOW_85, now)

    # 5. Event lifecycle — pregame only
    et = _parse_event_time(pick)
    if et is not None and et < now:
        # Distinguish EVENT_STARTED (recent) vs EVENT_FINAL (>6h past)
        reason = REASON_EVENT_FINAL if (now - et).total_seconds() > 6 * 3600 else REASON_EVENT_STARTED
        return {
            "eligible": False, "state": EXPIRED,
            "reason_code": reason,
            "evaluated_at": now.isoformat().replace("+00:00", "Z"),
            "publication_revision": pick.get("publication_revision"),
        }

    # ── ELIGIBLE ────
    return {
        "eligible": True, "state": CURRENT_PREGAME_LOCK,
        "reason_code": None,
        "evaluated_at": now.isoformat().replace("+00:00", "Z"),
        "publication_revision": pick.get("publication_revision"),
    }


def _blocked(pick: dict, reason: str, now: datetime) -> dict:
    assert reason in _CANONICAL_REASONS, f"non-canonical reason: {reason}"
    return {
        "eligible": False,
        "state":    BLOCKED,
        "reason_code": reason,
        "evaluated_at": now.isoformat().replace("+00:00", "Z"),
        "publication_revision": pick.get("publication_revision"),
    }


def _has_valid_settlement_capability(pick: dict) -> bool:
    """Every eligible current Lock must be settle-able canonically.

    We approximate this cheaply with attribute presence — the same
    signals the settlement service consumes:
      * a market string
      * a line OR selection identity
      * a valid sport in the canonical registry
    """
    if not (pick.get("market") or "").strip():
        return False
    if pick.get("line") is None and not (pick.get("selection") or "").strip():
        return False
    return bool((pick.get("sport") or "").strip())


def _has_real_book_offering(pick: dict) -> bool:
    """No synthetic actionable line may reach the board.

    A real sportsbook offering has BOTH `book_odds` (American price)
    AND a non-model-only source flag.  Rows written by model-only
    scorers carry explicit markers.
    """
    if pick.get("model_only") is True:
        return False
    if pick.get("no_real_book_line") is True:
        return False
    if pick.get("book_odds") in (None, ""):
        return False
    return True


def _is_contradiction_free(pick: dict) -> bool:
    """Reject rows explicitly flagged as contradicted upstream."""
    if pick.get("contradiction_rejected") is True:
        return False
    if pick.get("wager_contradiction") is True:
        return False
    return True


def rescue_validity_reason(pick: dict) -> Optional[str]:
    """Return a canonical rejection reason if the pick MUST NOT be
    rescued, else None.  This is a *tightening* on top of
    `compute_locks_eligibility.eligible=True` — the rescue layer is a
    fail-safe, NOT a second authority: it may only re-inject picks
    that already prove ALL safety conditions.
    """
    if not _has_real_book_offering(pick):
        return REASON_REAL_LINE_INVALID
    if not _has_valid_settlement_capability(pick):
        return REASON_SETTLEMENT_UNSUPPORTED
    if not _is_contradiction_free(pick):
        return "CONTRADICTION_REJECTED"
    if pick.get("synthetic") is True or pick.get("synthetic_market") is True:
        return "SYNTHETIC_MARKET"
    if pick.get("duplicate_revision") is True:
        return "DUPLICATE_REVISION"
    return None


async def rescue_missing_eligible(db, served_ids: set, rescue_query: dict):
    """Return (rescued_picks, ebm_ids, rejected_counts) — every eligible
    pick from `rescue_query` that is NOT in `served_ids` AND passes the
    full validity gate.  Never fabricates; never resurrects invalid rows."""
    rescued: list[dict] = []
    ebm_ids: list[str] = []
    rejected: dict[str, int] = {}
    async for p in db.picks.find(rescue_query, projection={"_id": 0}).limit(5000):
        pid = p.get("id")
        if not pid or pid in served_ids:
            continue
        if not compute_locks_eligibility(p)["eligible"]:
            continue
        rej = rescue_validity_reason(p)
        if rej is not None:
            rejected[rej] = rejected.get(rej, 0) + 1
            continue
        ebm_ids.append(pid)
        for _bk in ("prediction_snapshot_id", "settlement_event_id"):
            v = p.get(_bk)
            if v is not None and not isinstance(v, (str, int, float, bool)):
                p[_bk] = str(v)
        rescued.append(p)
    return rescued, ebm_ids, rejected


__all__ = [
    "compute_locks_eligibility",
    "rescue_missing_eligible",
    "CANONICAL_LOCK_FLOOR",
    "CURRENT_PREGAME_LOCK", "BLOCKED", "SUPERSEDED", "EXPIRED",
    "REASON_EVENT_STARTED", "REASON_EVENT_FINAL",
    "REASON_MARKET_WITHDRAWN", "REASON_REAL_LINE_INVALID",
    "REASON_MODEL_UNAVAILABLE", "REASON_EVIDENCE_INSUFFICIENT",
    "REASON_SETTLEMENT_UNSUPPORTED", "REASON_SUPERSEDED",
    "REASON_PUBLICATION_REVOKED", "REASON_BELOW_85",
    "REASON_OFF_BOARD", "REASON_NO_BET",
    "REASON_HIDDEN_MAIN_BOARD", "REASON_NOT_PUBLISHED",
]
