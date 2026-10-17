"""Publication-owned rejection reasons lifecycle (B1 — item 13).

Addresses the sticky-rejection class where a pick enters:

    REJECTED → off_board=True → no_bet=True

… then is legitimately published later, but stale hidden flags carry
over because the single-string ``off_board_reason`` cannot distinguish
*who* set the flag.

This module introduces:

  * ``off_board_reasons: list[str]`` — array of REASON labels; each
    label is scoped by its owner (``PUBLICATION_REJECTED:*``,
    ``LINEUP_SCRATCHED``, ``SETTLED_FROZEN``, …).
  * ``no_bet_reasons: list[str]``   — same shape for ``no_bet``.
  * ``add_publication_rejection(pick, reason)`` — stamps a
    publication-owned reason onto the pick.
  * ``clear_publication_owned_reasons(pick)`` — removes ONLY the
    publication-owned labels (NEVER touches lineup / settlement /
    manual / MODEL_UNAVAILABLE / contradiction-loser / scratch /
    player-team-mismatch / first-TD-dormant / true-lineup-rejection).
    After removal, if no independent blockers remain,
    ``off_board=False`` and ``no_bet=False``.

Legacy singular ``off_board_reason`` (string) is preserved — writes
go to BOTH the array and the legacy string so no existing consumer
breaks.
"""
from __future__ import annotations

from typing import Any, Iterable


# ── Reason prefixes / exact labels ──────────────────────────────────
# Any label starting with ``PUBLICATION_REJECTED:`` is OWNED by the
# publication boundary and MAY be cleared when a later valid
# publication succeeds.  Other labels are owned by their respective
# lifecycle sources and MUST survive publication clearing.
PUBLICATION_PREFIX: str = "PUBLICATION_REJECTED:"

# Independent-blocker labels (NEVER cleared by this module).  Add
# future owners here when their logic needs survival across a
# publication retry.
PROTECTED_LABELS: frozenset[str] = frozenset({
    "SETTLED",             "SETTLED_FROZEN",
    "SCRATCH",             "LINEUP_SCRATCHED",
    "PLAYER_TEAM_MISMATCH",
    "CONTRADICTION_LOSER",
    "FIRST_TD_DORMANT",
    "MANUAL_BLOCK",
    "MODEL_UNAVAILABLE",
    "LINEUP_REJECTION",
})


def _as_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, (list, tuple, set)):
        return [str(x) for x in v if x]
    if isinstance(v, str) and v:
        return [v]
    return []


def _is_publication_owned(label: str) -> bool:
    if not label:
        return False
    return str(label).startswith(PUBLICATION_PREFIX)


def _is_protected(label: str) -> bool:
    if not label:
        return False
    s = str(label).strip()
    if s in PROTECTED_LABELS:
        return True
    # Protect family-prefixed labels e.g. "LINEUP_SCRATCHED:details".
    head = s.split(":", 1)[0]
    return head in PROTECTED_LABELS


def _dedupe_preserve_order(xs: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in xs:
        if not x:
            continue
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out


# ── Public API ──────────────────────────────────────────────────────
def add_publication_rejection(pick: dict,
                              *,
                              reason: str,
                              set_no_bet: bool = True) -> dict:
    """Stamp a publication-owned rejection reason onto ``pick``.

    Writes to:
      * ``off_board_reasons: list[str]`` (array authority)
      * ``no_bet_reasons: list[str]`` (array authority) — only when
        ``set_no_bet`` is True.
      * Legacy ``off_board_reason: str`` — latest label wins (write-
        through so existing readers keep working).
      * Flags ``off_board=True`` / ``no_bet=True`` (iff set_no_bet).
    """
    label = f"{PUBLICATION_PREFIX}{reason}" if not _is_publication_owned(reason) else reason
    ob = _as_list(pick.get("off_board_reasons")) + _as_list(pick.get("off_board_reason"))
    ob.append(label)
    pick["off_board_reasons"] = _dedupe_preserve_order(ob)
    pick["off_board"] = True
    pick["off_board_reason"] = label  # legacy write-through
    if set_no_bet:
        nb = _as_list(pick.get("no_bet_reasons"))
        nb.append(label)
        pick["no_bet_reasons"] = _dedupe_preserve_order(nb)
        pick["no_bet"] = True
    return pick


def clear_publication_owned_reasons(pick: dict) -> dict:
    """Remove ONLY publication-owned reasons.  Independent blockers
    (settled / scratch / lineup / manual / contradiction / MODEL_
    UNAVAILABLE / etc.) are preserved verbatim.

    After the clear pass:
      * If no reasons remain in ``off_board_reasons`` → ``off_board``
        becomes False and legacy ``off_board_reason`` is cleared.
      * Same for ``no_bet``.
      * All PROTECTED_LABELS entries survive untouched.
    """
    ob_old = _as_list(pick.get("off_board_reasons"))
    nb_old = _as_list(pick.get("no_bet_reasons"))
    legacy = pick.get("off_board_reason")

    ob_new = [r for r in ob_old if not _is_publication_owned(r) or _is_protected(r)]
    nb_new = [r for r in nb_old if not _is_publication_owned(r) or _is_protected(r)]

    # Legacy singular field: if it was a publication-owned label and
    # not protected, clear it when no non-publication reasons remain.
    legacy_pub_owned = isinstance(legacy, str) and _is_publication_owned(legacy) and not _is_protected(legacy)

    pick["off_board_reasons"] = ob_new
    pick["no_bet_reasons"] = nb_new

    if ob_new:
        pick["off_board"] = True
        # Keep legacy string pointed at the first surviving reason for
        # readers that have not migrated to the array yet.
        pick["off_board_reason"] = ob_new[0]
    else:
        pick["off_board"] = False
        if legacy_pub_owned or legacy is None:
            # No remaining reasons AND the legacy label was publication-
            # owned (or already empty) → clear it.
            pick.pop("off_board_reason", None)

    if nb_new:
        pick["no_bet"] = True
    else:
        pick["no_bet"] = False

    return pick


def has_only_publication_rejections(pick: dict) -> bool:
    """Return True iff every reason on the pick is publication-owned
    (and none is protected).  Used by the publication retry path to
    decide whether a legitimate re-publish is safe.
    """
    reasons = _as_list(pick.get("off_board_reasons")) + _as_list(pick.get("no_bet_reasons"))
    if not reasons:
        return True
    for r in reasons:
        if not _is_publication_owned(r):
            return False
        if _is_protected(r):
            return False
    return True


__all__ = [
    "PUBLICATION_PREFIX",
    "PROTECTED_LABELS",
    "add_publication_rejection",
    "clear_publication_owned_reasons",
    "has_only_publication_rejections",
]
