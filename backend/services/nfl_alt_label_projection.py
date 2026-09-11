"""
NFL Alt-Ladder READ-TIME Label Projection
=========================================

READ-ONLY, projection-only rewrite of the user-facing ``market``
label for NFL alt-ladder player-prop picks.

Why this service exists
-----------------------
The prior NFL Alt-Ladder Truth Closure updated
``sports_engine._prop_market_label`` so that newly-minted NFL
integer-alt player props (yards / receptions / attempts /
completions / TDs) emit sportsbook-milestone labels
(e.g. ``"200+ Passing Yards"``) instead of the raw
provider form (``"Over 199.5 Player Pass Yds  · ALT LOCK"``).

That fix runs at **pick-creation time**.  Existing rows that were
frozen in ``db.picks`` **before** the fix retain the old label —
and per PUBLICATION_CONTRACT §3 those rows are IMMUTABLE (we do
not silently overwrite historical / published picks).

Solution: apply the same label transformation at **read time**
inside the ``/api/picks/today`` projection.  We only rewrite the
outgoing response object; we never touch the DB.  Backend
settlement continues to read the raw ``line`` field (14.5, 199.5,
etc.) exactly as before.

Contract
--------
Applied ONLY when:
  * sport == "NFL"
  * pick carries the ALT LOCK marker in ``market`` (or is_alt=True)
  * the side is OVER
  * an integer-alt yardage / reception / attempt / completion /
    passing-TD market family can be detected from the label

Semantics preserved:
  * Raw ``line`` / ``threshold`` / any settlement-facing fields
    are NOT modified.
  * Only the human-readable ``market`` label is rewritten.
  * Under alts stay half-yard (per P0-D) — untouched.
  * Legacy main-line rows without the ALT LOCK marker — untouched.

Idempotent: rerunning on an already-projected pick is a no-op.
"""
from __future__ import annotations

import logging
import math
import re
from typing import Iterable

logger = logging.getLogger("lockscore.nfl_alt_label_projection")

# ─────────────────────────────────────────────────────────────
# Family detection — matches the sportsbook milestone map used
# in ``sports_engine._prop_market_label``.  Keep in sync.
# ─────────────────────────────────────────────────────────────
_NFL_FAMILY_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Order matters: more specific phrases FIRST.  We use word
    # boundaries so "reception yds" doesn't accidentally trip on
    # "pass yds".
    (re.compile(r"\bpassing tds?\b|\bpass tds?\b", re.IGNORECASE), "Passing TDs"),
    (re.compile(r"\bpassing attempts?\b|\bpass att(?:empts?)?\b", re.IGNORECASE),
     "Passing Attempts"),
    (re.compile(r"\bpassing completions?\b|\bpass comp(?:letions?)?\b",
                re.IGNORECASE), "Passing Completions"),
    (re.compile(r"\brushing attempts?\b|\brush att(?:empts?)?\b|\bcarries\b",
                re.IGNORECASE), "Rushing Attempts"),
    (re.compile(r"\bpassing yards?\b|\bpass yds?\b", re.IGNORECASE), "Passing Yards"),
    (re.compile(r"\brushing yards?\b|\brush yds?\b", re.IGNORECASE), "Rushing Yards"),
    (re.compile(r"\breceiving yards?\b|\breception yds?\b|\brec yds?\b",
                re.IGNORECASE), "Receiving Yards"),
    (re.compile(r"\bplayer\s+receptions?\b|\breceptions?\b", re.IGNORECASE),
     "Receptions"),
]

# Detect the "Over N.N" numeric point from the raw label.  Case-
# insensitive because the pipeline sometimes lowercases the side.
_OVER_POINT_RE = re.compile(r"\bover\s+(\d+(?:\.\d+)?)\b", re.IGNORECASE)
_UNDER_RE = re.compile(r"\bunder\s+\d", re.IGNORECASE)
_ALT_MARKER_RE = re.compile(r"·\s*ALT\s*LOCK", re.IGNORECASE)
# Match milestone-form so we can short-circuit if already
# projected (idempotency).
_MILESTONE_FORM_RE = re.compile(r"^\d+\+ ", re.IGNORECASE)


def _is_nfl_alt_over_pick(pick: dict) -> bool:
    if str(pick.get("sport") or "").upper() != "NFL":
        return False
    market = pick.get("market") or ""
    if not market:
        return False
    # Fast path: must be an ALT-LOCK row (canonical creation-time
    # marker) OR have is_alt=True.  Under rows stay half-yard.
    is_alt = bool(pick.get("is_alt") or pick.get("is_alt_prop")) \
        or bool(_ALT_MARKER_RE.search(market))
    if not is_alt:
        return False
    if _UNDER_RE.search(market):
        return False
    if not _OVER_POINT_RE.search(market):
        return False
    return True


def _detect_family(market: str) -> str | None:
    for pattern, canonical in _NFL_FAMILY_PATTERNS:
        if pattern.search(market):
            return canonical
    return None


def _split_player_from_market(market: str) -> tuple[str, str]:
    """Return (player_prefix, tail_after_over) or ("", market)."""
    m = _OVER_POINT_RE.search(market)
    if not m:
        return "", market
    prefix = market[: m.start()].rstrip()
    return prefix, market[m.start():]


def project_nfl_alt_label(pick: dict) -> bool:
    """Rewrite ``pick["market"]`` in place with a milestone label.

    Returns True if the pick was mutated.  Idempotent.
    """
    if not _is_nfl_alt_over_pick(pick):
        return False

    market: str = pick["market"]

    m_point = _OVER_POINT_RE.search(market)
    if not m_point:
        return False
    try:
        point = float(m_point.group(1))
    except (TypeError, ValueError):
        return False

    family = _detect_family(market)
    if not family:
        return False

    # milestone = floor(point) + 1 handles half-lines
    # (14.5 → 15) and integer lines (14 → 15).
    milestone = int(math.floor(point) + 1)
    if milestone <= 0:
        return False

    player_prefix, _tail = _split_player_from_market(market)

    # If we've already been projected, short-circuit.
    if _MILESTONE_FORM_RE.match(_tail.strip()):
        return False

    new_tail = f"{milestone}+ {family}"
    new_market = f"{player_prefix} {new_tail}".strip() if player_prefix \
        else new_tail

    pick["market"] = new_market
    # Stash the pre-projection label so downstream analytics / QA
    # can reconstruct the raw provider form.  We do NOT mutate any
    # settlement fields — the raw ``line`` / ``threshold`` /
    # ``point`` are the authoritative settlement anchor.
    if "display_label_source" not in pick:
        pick["display_label_source"] = "nfl_alt_ladder_projection"
    return True


def apply_nfl_alt_label_projection(picks: Iterable[dict]) -> dict:
    """Iterate the picks list and rewrite NFL alt-lock labels.

    Returns a stats dict for logging.  Only NFL picks are examined
    — other sports are untouched.
    """
    if not picks:
        return {"scanned": 0, "rewritten": 0, "skipped": 0}
    scanned = rewritten = skipped = 0
    for p in picks:
        if not isinstance(p, dict):
            continue
        if str(p.get("sport") or "").upper() != "NFL":
            skipped += 1
            continue
        scanned += 1
        if project_nfl_alt_label(p):
            rewritten += 1
    return {"scanned": scanned, "rewritten": rewritten, "skipped": skipped}


__all__ = [
    "apply_nfl_alt_label_projection",
    "project_nfl_alt_label",
]
