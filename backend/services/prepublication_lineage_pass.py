"""Universal Pre-Publication Lineage Pass (Blocks 1, 2, 11).

Runs at the orchestrator layer AFTER producers emit candidate picks
but BEFORE canonical publication.  Walks every candidate; for picks
that carry an ``observed_quotes`` list (populated by migrated
producers), re-selects the authoritative sportsbook quote via the
shared ``harden_lineage()`` adapter and attaches:

    pick["book_odds"]    = exact observed sportsbook price
    pick["bookmaker"]    = exact observed book slug
    pick["odds_source"]  = "the_odds_api" (or the real trusted label)
    pick["observed_at"]  = ISO timestamp of the observation

If a pick does not carry ``observed_quotes`` (unmigrated producer),
the row is left EXACTLY as the producer emitted it — zero regression.

If every observed quote fails validation (SYNTHETIC_BOOK_ODDS or no
trusted book), the pass sets ``no_real_book_line=True``, blanks the
book_odds, and tags ``odds_source="MODEL_ONLY"`` — the same fail-closed
contract soccer_prop_inject already uses.

This is a UNIVERSAL migration surface — MLB, NHL, NFL, CFB, Tennis
producers all benefit the moment they attach `observed_quotes` to
their emitted dict.  Zero formula changes.  Zero producer-internal
coupling.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

logger = logging.getLogger("lockscore.prepublication_lineage")


_LINEAGE_FIELDS = (
    "book_odds", "bookmaker", "odds_source", "observed_at",
    "no_real_book_line",
    # 2026-10-22 · implied_probability must track book_odds. Prior to
    # this, alt-line picks (e.g. NHL shots_on_goal_alternate) kept the
    # emitter's `implied_probability` derived from the MAIN-LINE
    # `odds_at_pick` (e.g. -560 → 84.8%) while `book_odds` was swapped
    # to the actual observed alt price (+146 → ~40.6%). The
    # `canonical_publication_boundary` detector then flagged every such
    # row as `IMPOSSIBLE_IMPLIED_PROBABILITY` and `off_board=true`,
    # which is why zero NHL alt-line picks have ever cleared the main
    # board. Re-deriving the two implied-prob fields from the HARDENED
    # `book_odds` keeps them in lockstep with the authoritative price.
    "implied_probability", "raw_implied_probability",
)


def _upgrade_pick_lineage(pick: dict) -> dict:
    """Return a per-pick upgrade summary; mutates ``pick`` in place.

    The summary has keys:
      {"migrated": bool, "lineage_reason": None | str,
       "bookmaker_before": any, "bookmaker_after": any}
    """
    quotes = pick.get("observed_quotes")
    if not quotes or not isinstance(quotes, (list, tuple)):
        return {"migrated": False, "reason": "no_observed_quotes"}
    try:
        from services.quote_lineage_guard import harden_lineage
    except Exception as e:
        return {"migrated": False, "reason": f"import_error:{e}"}

    sport        = pick.get("sport") or ""
    market       = pick.get("market_family") or pick.get("market") or ""
    event_id     = pick.get("event_id") or pick.get("provider_event_id") or ""
    before_book  = pick.get("bookmaker")

    hardened = harden_lineage(quotes, market_family=market,
                               event_id=str(event_id), sport=str(sport))

    if hardened.get("no_real_book_line"):
        pick["book_odds"] = None
        pick["bookmaker"] = None
        pick["odds_source"] = "MODEL_ONLY"
        pick["no_real_book_line"] = True
        pick["observed_at"] = None
        return {"migrated": False,
                "reason": hardened.get("lineage_reason") or "no_trusted_book",
                "bookmaker_before": before_book,
                "bookmaker_after": None}

    new_book_odds = int(hardened["book_odds"])
    pick["book_odds"] = new_book_odds
    pick["bookmaker"] = hardened["bookmaker"]
    pick["odds_source"] = hardened["odds_source"]
    pick["observed_at"] = hardened.get("observed_at")
    pick["no_real_book_line"] = False
    # consensus_price is analytics-only; store under a dedicated field.
    pick["consensus_price"] = hardened.get("consensus_price")

    # 2026-10-22 — recompute implied_probability from the hardened
    # alt-line price so it never drifts out of sync with book_odds.
    # Without this, alt-line publication is universally rejected by
    # `canonical_publication_boundary::IMPOSSIBLE_IMPLIED_PROBABILITY`.
    try:
        from services.probability_units import implied_probability_from_odds
        _ip_frac = implied_probability_from_odds(new_book_odds)
        if _ip_frac is not None:
            _ip_pct = round(_ip_frac * 100.0, 1)
            pick["implied_probability"] = _ip_pct
            pick["raw_implied_probability"] = _ip_pct
    except Exception as _ip_err:
        # Never block the lineage upgrade on a probability-helper
        # failure — the publication boundary check will still catch
        # a genuinely bad row, and we log for post-mortem visibility.
        logger.warning(
            "implied_probability recompute skipped for pick %s: %s",
            pick.get("id"), _ip_err,
        )

    return {"migrated": True, "reason": None,
            "bookmaker_before": before_book,
            "bookmaker_after": hardened["bookmaker"]}


def prepublication_lineage_pass(picks: Iterable[dict]) -> dict:
    """Run the universal upgrade across a list of candidate picks.

    Returns a summary dict:
      {"total": N, "migrated": M, "no_real_book": K,
       "unchanged": N-M-K, "by_sport": {sport: ...}}
    """
    total = migrated = no_real = 0
    by_sport: dict[str, dict[str, int]] = {}
    for p in picks:
        if not isinstance(p, dict):
            continue
        total += 1
        res = _upgrade_pick_lineage(p)
        s = (p.get("sport") or "unknown").strip() or "unknown"
        bucket = by_sport.setdefault(s, {"total": 0, "migrated": 0,
                                           "no_real_book": 0})
        bucket["total"] += 1
        if res.get("migrated"):
            migrated += 1
            bucket["migrated"] += 1
        elif res.get("reason") and res["reason"] != "no_observed_quotes":
            no_real += 1
            bucket["no_real_book"] += 1
    summary = {
        "total": total, "migrated": migrated,
        "no_real_book": no_real, "unchanged": total - migrated - no_real,
        "by_sport": by_sport,
    }
    if migrated or no_real:
        logger.info("prepublication_lineage_pass: %s", summary)
    return summary


__all__ = ["prepublication_lineage_pass", "_upgrade_pick_lineage"]
