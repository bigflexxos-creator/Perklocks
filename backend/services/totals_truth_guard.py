"""Universal Totals Truth — One-Active-Side + Supersession (§9-§11, §14).

Surgical guard around the existing per-sport totals models. Enforces:
  * Side-neutral canonical_market_key: `{sport}|{event_id}|{period}|
    TOTAL|{line}` — Over/Under is CURRENT RECOMMENDATION STATE, not part
    of identity.
  * Only ONE ACTIVE recommendation per canonical key at any time.
  * A legitimate side-flip promotes the new row to ACTIVE and marks
    the previous row `revision_state="SUPERSEDED"` with reason.
  * Signal preservation: SUPERSEDED rows remain immutable in history.
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("lockscore.totals_truth")


def _canonical_totals_key(pick: dict[str, Any]) -> str | None:
    """SIDE-NEUTRAL canonical identity for a Total market. Returns None
    when the pick is not a Total or identity is incomplete."""
    market = str(pick.get("market") or "").lower()
    if "total" not in market and pick.get("market_family") not in ("TOTAL", "GAME_TOTAL", "MATCH_TOTAL"):
        return None
    sport = (pick.get("sport") or "").upper()
    event_id = pick.get("event_id") or pick.get("canonical_event_id") or pick.get("event")
    period = pick.get("period") or "FULL_GAME"
    line = pick.get("line")
    if not (sport and event_id and line is not None):
        return None
    try:
        line_str = f"{float(line):.2f}"
    except Exception:
        line_str = str(line)
    return f"{sport}|{event_id}|{period}|TOTAL|{line_str}"


async def enforce_single_active_total(db, picks: list[dict]) -> dict[str, int]:
    """Enforce §9 / §11 / §14 across the picks list + existing DB rows.

    Pass-1 upgrade (2026-09-01) — FAIL CLOSED for affected totals:
      * side conflict → SUPERSEDED_IN_RUN + off_board (unchanged)
      * conservation fail on emitted pair → off_board + reason
      * caller-provided ladder failure stamped upstream — never deleted.

    Never fails the whole board. Unrelated markets keep flowing.
    """
    stats = {"totals_seen": 0, "superseded": 0, "duplicates_dedup": 0,
             "keys_stamped": 0, "conservation_failed": 0}
    if not picks:
        return stats
    # 1) Stamp key on all in-flight totals picks + attach canonical edge
    # payload where paired-market odds are available on the pick record.
    for p in picks:
        k = _canonical_totals_key(p)
        if not k:
            continue
        p["canonical_market_key"] = k
        p["revision_state"] = p.get("revision_state") or "ACTIVE"
        stats["keys_stamped"] += 1
        stats["totals_seen"] += 1
        # ── §5 conservation enforcement at publication path ──────────
        o = p.get("total_over_prob")
        u = p.get("total_under_prob")
        pu = p.get("total_push_prob") or 0
        if o is not None and u is not None:
            ok, reason = check_over_under_conservation(o, u, pu)
            if not ok:
                p["off_board"] = True
                p.setdefault("off_board_reasons", [])
                if "TOTALS_CONSERVATION_FAILED" not in p["off_board_reasons"]:
                    p["off_board_reasons"].append("TOTALS_CONSERVATION_FAILED")
                p["totals_integrity"] = {"ok": False, "reason": reason}
                stats["conservation_failed"] += 1
        # ── §8 attach canonical Edge (paired de-vig) when both odds
        # are present on the pick's stored sportsbook snapshot.
        try:
            from services.totals_devig import canonical_totals_edge
            oo = p.get("over_odds") or p.get("book_over_odds")
            uo = p.get("under_odds") or p.get("book_under_odds")
            side = p.get("selection") or p.get("side") or p.get("market")
            mp = p.get("model_probability") or p.get("win_probability")
            if mp and mp > 1: mp = mp / 100.0  # normalize percent
            edge = canonical_totals_edge(mp, side, oo, uo)
            if edge.get("available"):
                p["canonical_totals_edge"] = edge
        except Exception:
            pass
    # 2) In-run de-dupe (unchanged).
    by_key: dict[str, list[dict]] = {}
    for p in picks:
        k = p.get("canonical_market_key")
        if not k: continue
        by_key.setdefault(k, []).append(p)
    for k, rows in by_key.items():
        if len(rows) < 2:
            continue
        rows.sort(key=lambda r: float(r.get("lock_score") or 0), reverse=True)
        winner = rows[0]
        for loser in rows[1:]:
            loser["revision_state"] = "SUPERSEDED_IN_RUN"
            loser["superseded_by_selection"] = winner.get("selection")
            loser["superseded_reason"] = "TOTALS_SIDE_CONFLICT"
            loser["off_board"] = True
            loser["off_board_reasons"] = list(loser.get("off_board_reasons") or []) + ["TOTALS_SIDE_CONFLICT"]
            stats["duplicates_dedup"] += 1
    # 3) Cross-refresh supersession (unchanged).
    now = datetime.now(timezone.utc).isoformat()
    for p in picks:
        if p.get("revision_state") != "ACTIVE":
            continue
        k = p.get("canonical_market_key")
        if not k:
            continue
        pick_date = p.get("pick_date")
        selection = (p.get("selection") or "").strip().lower()
        if not pick_date:
            continue
        try:
            r = await db.picks.update_many(
                {
                    "canonical_market_key": k,
                    "pick_date": pick_date,
                    "revision_state": {"$in": ["ACTIVE", None]},
                    "$expr": {"$ne": [{"$toLower": {"$ifNull": ["$selection", ""]}}, selection]},
                },
                {"$set": {
                    "revision_state": "SUPERSEDED",
                    "superseded_at": now,
                    "superseded_reason": "side_flip_by_newer_active_revision",
                    "off_board": True,
                }, "$addToSet": {"off_board_reasons": "superseded_revision"}},
            )
            if r.modified_count:
                stats["superseded"] += r.modified_count
        except Exception as e:  # pragma: no cover
            log.debug("supersession update fail-open %s: %s", k, e)
    if stats["totals_seen"]:
        log.info("Totals truth guard: seen=%d keys=%d dedup=%d superseded=%d conservation_failed=%d",
                 stats["totals_seen"], stats["keys_stamped"],
                 stats["duplicates_dedup"], stats["superseded"],
                 stats["conservation_failed"])
    return stats


def check_over_under_conservation(over_prob: float | None,
                                    under_prob: float | None,
                                    push_prob: float | None = None,
                                    tol: float = 0.02) -> tuple[bool, str]:
    """§5 conservation: P(over)+P(under)[+P(push)] ≈ 1.0.

    Returns (ok, reason). `ok=False` means the pair is not derived from
    ONE distribution — the caller should stamp an integrity failure
    rather than publish.
    """
    o = float(over_prob or 0)
    u = float(under_prob or 0)
    p = float(push_prob or 0)
    total = o + u + p
    if not (0.98 <= total <= 1.02):
        return False, f"conservation_fail:sum={total:.3f} (o={o:.3f}, u={u:.3f}, p={p:.3f})"
    return True, "conservation_ok"
