# Phase 4E.7 — Soccer Settlement Replay

**Since:** 2026-02-07T21:41:39.360170+00:00
**Picks examined:** 0
**Abandoned/postponed matches:** 0
**Flagged inconsistencies:** 0

## Market breakdown


## Policy notes

* Current soccer settler (FotMob/ESPN) explicitly voids penalty misses and does not double-count own goals for scorer markets; audit confirms no silent policy change.
* score_or_assist is settled distinctly by _settle_scorer_market which checks both goal and assist events.