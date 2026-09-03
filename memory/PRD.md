# PerkLocks — Product Requirements (living doc)

**Last update:** 2026-09-03 (session-end checkpoint)

## Product vision
Sports betting research + Lock intelligence app: Expo React Native
frontend, FastAPI + MongoDB backend. Each pick published on the board
must be **immutable canonical truth** flowing losslessly from
publication → board → research → settlement → History across Preview
Web and Expo Go.

## Certified in this session
See `/app/memory/perklocks_main_34_checkpoint.md` for the full log.

### Performance breakthroughs (kept)
- Slice 1.2B — Lightweight Board DTO whitelist: 1.08 MB → 165 KB
  (-84.8 %) on `/api/picks/today?lite=true`
- Slice 1.6 — LockBoardCard render split (lazy Modals + preload MatchupGradeBadge)
- Slice 1.1 — Cold-start unblocked from `/api/version`; 500 ms icon-font watchdog
- Slice 3 — expo-image with memory-disk cache for crests + headshots

### Root closures (this session)
- **P0A/P0B** — Full ↔ Lite board membership parity (per-sport +
  per-market-family) with 5 live-DB contract tests
- **P0I/P0J/P0K/P0M** — Strategy Lab tap & search hardening
  (debounce, generation guard, min-length gate, canonical commit
  fast-path, explicit UX states)
- **P0D** — `/api/picks/history` ships `settlement_freshness` and
  Expo History auto-repolls when a settlement pass is in-flight

### Preserved
- Universal 85+ reachability + rescue
- Immutable prediction snapshots + settlement ledger
- Rollover/Parlay canonical base migration
- Adaptive virtualization
- Phase-24 Slice 1.2 contract tests

## Remaining scope (deferred safely)
- P0C — one canonical published-pick contract module
- P0E/F/G/H — universal grader registry + coverage matrix
- P0L — Lab learns from canonical settlement (not mutable status)
- P1 Slices 4-8, 10 — Why This Pick real-evidence, Soccer goalscorer
  10X, Pick Breakdown 2.0, same-snapshot Web/Native/API parity

## Testing / credentials
- Test creds : `demo@lockscore.ai` / `demo123`
- Frontend :  Expo Router; home board uses `<LockBoardCard>`
- Contract tests : `backend/tests/test_phase24_*` +
  `backend/tests/test_perklocks_main_34_*` (38 new tests this session)
