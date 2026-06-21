"""Tennis-Extra — Free fallback for tournaments The Odds API doesn't carry.

User complaint: "Why we not getting these tennis games" — referring to
Mallorca ATP, Bad Homburg WTA, Eastbourne, etc. The Odds API genuinely
doesn't cover ATP/WTA 250-level grass-court tune-ups, qualifiers, or
challengers. This module fills that gap using a free scrape of
TennisExplorer.com which covers every ATP/WTA/Challenger/ITF event.

What we ingest:
  • Today's match schedule (tournament, time, players)
  • TennisExplorer's CONSENSUS decimal odds (when present — usually for
    main-tour matches; Challengers may not have odds and we skip those)
  • Tournament tier inference (ATP 250 / WTA 250 / Challenger)

What we OUTPUT (as PerksLocks `pick` documents):
  • One moneyline pick per match where odds exist + edge > 0
  • `source: "tennis_extra"` so settler/UI know these came from scrape
  • `is_extra: True` flag so the UI can badge them as supplementary

What we DO NOT do:
  • No props, no totals, no spreads — only moneyline (one outcome per match).
  • No 250 picks with odds < 1.10 (book is 91%+ confident → no value).
  • No ITF/UTR/exhibition picks — too low-level, settlement unreliable.
"""

from __future__ import annotations

__all__ = ["fetch_extra_tennis_picks"]

from .picks import fetch_extra_tennis_picks  # noqa: E402
