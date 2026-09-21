# PerksLocks — Product Requirements (Live Delta 2026-06-21)

## Universal Preview ↔ Expo Go Canonical Freshness Contract — CLOSED (unpublished)

**Scope**: One shared client-side freshness contract that applies to every canonical surface (Locks, Pick Breakdown, Historical Intelligence, Rollover, Parlay, My Bets, Lab, Analytics) across every sport (MLB, NFL, CFB, Soccer, Tennis, NBA). Fixes the observed Preview↔Expo Go stale-data drift WITHOUT sport-specific hacks, WITHOUT manual AsyncStorage clears, and WITHOUT touching sport / model math.

### Mechanism
Server-side: `BoardVersionHeaderMiddleware` stamps `X-Canonical-Version` on every canonical GET response, sourced from `board_generation.active()`. Cross-process resync every 8 s. CORS `expose_headers` includes it so browsers can read it.

Client-side (shared web + Expo Go code path): `api.ts::_fetchWithTimeout` pushes every observed header into `boardFreshness.noteBoardVersion()`. That observer persists the last-known value to AsyncStorage (`board_version_v1`) and fires subscribers only when a NEWER version appears. The `useSWR` cache stamps every entry with the current version at write time; on advance it sweeps every entry whose stamp lags and force-refetches on next read.

### Result
- Same fingerprint appears on every canonical surface at any moment.
- Preview and Expo Go converge on identical truth after any rescore/republish.
- Offline last-good preserved — sweep only runs when the network returns.
- Zero sport-specific patches; zero endpoint-specific hacks.

### Tests
- Backend contract 6/6 pass (`test_universal_canonical_freshness_contract.py`)
- Frontend contract 14/14 pass (`__tests__/universal_canonical_freshness.runner.js`)
- Prior P0 regression suite 25/25 remains green.

### Handoff
Handed back for one physical Expo Go check. NOT published.
Full report: `/app/memory/universal_freshness_contract_closure_2026_06_21.md`.

---

## Prior — CFB Sign Fix + Soccer HI (still valid)
See `/app/memory/p0_universal_root_closure_final_2026_06_21.md`.
