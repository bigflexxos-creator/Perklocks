# P0-1 NHL Funnel Proof — Preview

**Verdict**: NHL player-prop publication is HEALTHY in Preview.
**Code change required**: NONE. Fix is systemic env-parity, delivered via P0-3 bootstrap.

## Live API proof (demo user auth)
`GET /api/picks/today?sport=NHL` → **113 picks returned**
- Lock score range: **85.0 – 91.7** (avg 87.7)
- All rows: `publication_state=PUBLISHED`, `publication_source=canonical_pipeline`
- Newest `created_at`: 2026-10-20T10:50:26Z

## Sample published pick
```
id=c0853ca1-c6fa-5081-969f-12ed404330a8
pick="Vegas Golden Knights Moneyline"
odds_at_pick=-112  lock_score=90.0  publication_state=PUBLISHED
```

## Candidate funnel (last orchestrator cycle 2026-10-20T10:51:02Z)
```
eval=350 acc=107 rej=243 pub=107 elig=107
new=0 existing=160 mismatches=153 errors=0 board=board-20261020T105101Z
```

## Market coverage (113 live NHL picks)
- Team Moneyline: multiple (Golden Knights, Oilers, Blues, Devils, Capitals, Islanders, Kraken …)
- Shots on Goal (SOG) Over 0.5 / 1.5 / 2.5 / 3.5 (incl. **ALT LOCK** lines)
- Goals Over 0.5 / 1.5 / 2.5
- Totals Over/Under 5.5 / 6.5

## Rejection breakdown (last cycle)
- `IMPOSSIBLE_IMPLIED_PROBABILITY`: 230 (seeded synthetic rows — correctly rejected)
- `PLAYER_TEAM_UNRESOLVED`: 141 (identity cold-start — non-blocking)
- `SYNTHETIC_BOOK_ODDS`: 76 (no real sportsbook offering)

## Why Production appears "stuck at Oct 2"
`services/database.py:342-344` → `_PrefixedDatabase` rewrites every
`db.picks.find/insert` to `canonical_picks` when
`USE_CANONICAL_DB=true` AND `CANONICAL_FALLBACK_MODE=true`.

In Preview `.env` both are TRUE, so new NHL picks (Oct 15 → today) land
in `canonical_picks` AND the reader queries `canonical_picks`. 113 served.

If Production has env drift (either flag false on any pod), writer and
reader diverge → reader sees only the Oct-2 legacy snapshot.

## Remediation
Delivered by `scripts/maintenance/production_bootstrap.py` in P0-3:
- Verify env parity across all pods
- Fail-loud smoke check on namespace agreement
- Non-destructive, idempotent, dry-run by default
