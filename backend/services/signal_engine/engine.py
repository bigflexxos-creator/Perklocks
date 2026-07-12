"""Signal Engine orchestrator — Phase A.

Runs the six universal calculators, combines them into a 0-100 Signal
Score, rewrites the signal-driven "Why This Pick" bullets, and (bulk
path) persists the block back to `db.picks` so the ranking engine can
read `signal_score` on subsequent queries without re-decoration.

Score model:
    score = clamp(50 + Σ component points, 0, 100)
    component budgets: form ±12 · matchup ±8 · volume ±7
                       · injury ±8 · market ±7 · value ±8

Freshness: the market signal moves with live odds, so a stored block
older than 30 minutes is recomputed on the next read. Recompute is
cheap — the only I/O is `get_player_form` which is TTL-cached in
memory.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from pymongo import UpdateOne

from .calculators import (
    form_signal, injury_signal, market_signal, matchup_signal,
    value_signal, volume_signal,
)
from .rationale import build_why, signal_breakdown_line

logger = logging.getLogger("lockscore.services.signal_engine")

SIGNAL_VERSION = 1
_REFRESH_SECS = 1800  # 30 min — market signal tracks live line movement


def _grade(score: int) -> str:
    if score >= 80:
        return "Elite"
    if score >= 65:
        return "Strong"
    if score >= 50:
        return "Moderate"
    if score >= 35:
        return "Weak"
    return "Fade"


def _is_fresh(block: dict) -> bool:
    if block.get("version") != SIGNAL_VERSION:
        return False
    ts = block.get("computed_at")
    if not ts:
        return False
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds() < _REFRESH_SECS
    except Exception:
        return False


async def compute_signals(db, pick: dict) -> dict:
    """Mutate `pick` in place: adds `signal_engine` + `signal_score` and
    injects the top signal bullets into `pick_rationale.evidence`.
    No-op when a fresh same-version block already exists."""
    if not pick:
        return pick
    existing = pick.get("signal_engine")
    if isinstance(existing, dict) and _is_fresh(existing):
        pick.setdefault("signal_score", existing.get("score"))
        return pick

    components = [
        await form_signal(db, pick),
        await matchup_signal(db, pick),
        volume_signal(pick),
        injury_signal(pick),
        market_signal(pick),
        value_signal(pick),
    ]

    total = sum(c["points"] for c in components)
    score = int(round(max(0.0, min(100.0, 50.0 + total))))
    why = build_why(pick, score, components)

    pick["signal_engine"] = {
        "version": SIGNAL_VERSION,
        "score": score,
        "grade": _grade(score),
        "breakdown": signal_breakdown_line(components),
        "components": components,
        "why": why,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }
    pick["signal_score"] = score

    _inject_rationale(pick, score, components)
    return pick


def _inject_rationale(pick: dict, score: int, components: list[dict]) -> None:
    """Push the two strongest signal bullets into `pick_rationale.evidence`
    so the card's expandable "Why This Pick?" panel surfaces them.
    Deduped case-insensitively against existing lines."""
    strongest = sorted(
        (c for c in components if abs(c["points"]) >= 2 and c["details"]),
        key=lambda c: abs(c["points"]), reverse=True,
    )[:2]
    if not strongest:
        return
    rationale = pick.setdefault("pick_rationale", {})
    if not isinstance(rationale, dict):
        return
    evidence = rationale.setdefault("evidence", [])
    if not isinstance(evidence, list):
        return
    seen = {str(line).lower() for line in evidence}
    for c in strongest:
        line = (f"📡 {c['label']} signal "
                f"{'+' if c['points'] > 0 else ''}{c['points']:g}: {c['details'][0]}")
        if line.lower() not in seen:
            evidence.append(line)
            seen.add(line.lower())


async def decorate_signals_bulk(db, picks: list[dict], persist: bool = True) -> list[dict]:
    """Bulk entry-point used by /picks/today and detail endpoints.
    Persists changed blocks best-effort so the Rollover ranker (which
    queries raw docs) can read `signal_score` without re-decoration."""
    if not picks:
        return picks
    ops: list[UpdateOne] = []
    for p in picks:
        try:
            before = (p.get("signal_engine") or {}).get("computed_at")
            await compute_signals(db, p)
            after = (p.get("signal_engine") or {}).get("computed_at")
            if persist and p.get("id") and after and after != before:
                ops.append(UpdateOne(
                    {"id": p["id"]},
                    {"$set": {
                        "signal_engine": p["signal_engine"],
                        "signal_score": p["signal_score"],
                    }},
                ))
        except Exception as e:
            logger.warning("signal engine failed for pick %s: %s", p.get("id"), e)
    if ops:
        try:
            await db.picks.bulk_write(ops, ordered=False)
        except Exception as e:
            logger.warning("signal engine persist failed: %s", e)
    return picks
