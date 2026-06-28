"""PerkLocks `services` package.

This module is the **unified cross-source ingestion layer** introduced
2026-06-27 in response to:

  * Retired / transferred players leaking onto goalscorer + player-prop
    boards (CSL: Cedric Bakambu / Marcao; NBA: rookies from inactive
    rosters; NFL: training-camp cuts).
  * The previous ad-hoc fan-out — every league had its own ingestor with
    no shared "is this player currently usable?" gate — required a
    pipeline edit every time a new source was added.

Architecture
------------
                   ┌──────────────────────────────┐
                   │     active_registry          │  ← single source of
                   │  (per-sport, per-player flag)│    truth for "active"
                   └──────────────────────────────┘
                            ▲          ▲
                            │          │
        ┌───────────────────┘          └───────────────────┐
        │                                                  │
   nba_ingest.py                                      nfl_ingest.py
   ├ nba.com/stats (primary, JSON)                    ├ nfl.com/stats (primary, HTML)
   ├ Basketball-Reference.com (enrichment)            ├ Pro-Football-Reference.com (enrichment)
   └ ESPN public (fallback)                           └ ESPN public (fallback)

Future modules:
   soccer_ingest.py  (FBref + FotMob + Understat — Phase 2)

Every ingestor writes into the *same* registry collections so the picks
pipeline only needs to call `active_registry.is_active(sport, name)` to
gate any player-level prop or scorer pick — works identically across
all sports.
"""
