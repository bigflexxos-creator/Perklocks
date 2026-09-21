# PerksLocks — Product Requirements (Live Delta 2026-06-21)

## P0 Physical Acceptance Root Closure — COMPLETE (unpublished)

**Scope**: Universal closure of the two P0 physical-acceptance failures reported after the CFB Totals + Intelligence audit.

### Failures Closed

**A. Intelligence / H2H Cross-Sport Canonical Parity (was OPEN)**
- One universal endpoint `GET /api/picks/{id}/historical-intelligence` proven to deliver GAME LOGS + VS OPP + SPLITS + DISTRIBUTION across MLB, NFL, CFB, Soccer, Tennis with real data, real entity, real opponent, real provenance.
- Soccer team identity aliases extended (Nott'm Forest, Ipswich, Newcastle, Bournemouth, Brighton + 15 EPL short forms).
- Soccer HI adapter now uses `canonical_team_key` — provider display names in `soccer_matches` (Man United / Nott'm Forest) resolve against canonical pick names.
- BTTS / total / handicap / double-chance entity leak fixed — `team="Yes"` publisher noise no longer becomes the query entity.
- Mobile readability: dates carry year (`25 10/25`); no more "Illinois Fig" mid-word slice; hero row renders across 2 lines.
- Same `buildApiUrl` code path on Preview (web) and Expo Go (native); `served_by` fingerprint on every Intelligence response for cross-surface parity.

**B. CFB Totals Sign Inversion Runtime Closure (was PENDING RUNTIME)**
- Code fix in `cfb_game_model.py:249-250` preserved.
- Engine version bumped `v2.2026-06-12` → `v3.2026-06-signfix` across `sports_engine.py` (3 sites).
- 210 CFB picks retired, 183 rescored in-place via corrected SP+ math + refreshed `published_*` snapshot fields.
- Northwestern @ Indiana Total O47.5: **96.69 % → 47.17 %** WP; LS **98 → 56**. Grade → PASS (math dictates).
- List ↔ Detail parity proven for all target fixtures. No stale generation surviving in cache / snapshot / published_reader hydrate.

### Tests

66 focused pass — 4 NEW P0 regressions (`tests/test_p0_root_closure_signfix_and_soccer_hi.py`) + 27 HI + 16 phase suites + 12 chalk / apex / phase-6 + 3 cfb_total_sign_flip locks. Zero regressions.

### Guardrails Honoured

NHL / UFC untouched. MLB history / NFL models / Soccer & Tennis scoring / ITF 95+ policy / Magic-APEX contract / ≥85 Locks floor — all untouched. No LS forcing. Zero provider refresh. Zero production publish.

### Handoff

Handed back for physical iPhone / Expo Go acceptance. Full report in `/app/memory/p0_universal_root_closure_final_2026_06_21.md`.
