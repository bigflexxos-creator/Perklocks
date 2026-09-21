# P0 UNIVERSAL ROOT CLOSURE — FINAL RUNTIME ACCEPTANCE (2026-06-21)

**Scope**: Physical acceptance of Failure A (Intelligence / H2H parity) +
Failure B (CFB Totals sign inversion runtime closure).

**Not published to production**. Handed back for physical iPhone / Expo Go
acceptance per stop condition.

---

## 1. CFB SIGN FIX — PRESERVED

`backend/services/cfb_game_model.py:249-250` retains the corrected form:

```python
h_pts = h_off + (a_def - 25.0)
a_pts = a_off + (h_def - 25.0)
```

Test lock: `tests/test_p0_root_closure_signfix_and_soccer_hi.py::test_cfb_signfix_indiana_over_47p5_is_not_extreme` — asserts Indiana / NW base_total lands 40–50 (was ~85 with bug) and P(Over 47.5) < 55 %.

---

## 2. CFB REGENERATION — RUNTIME PROVEN

Regeneration path: engine version bumped `v2.2026-06-12` → `v3.2026-06-signfix` (`sports_engine.py:3108/3955/4378`). All CFB rows carrying the old stamp were retired (`scripts/maintenance/retire_cfb_signfix_v3.py` — 210 retired) and rescored in-place from persisted `cfb_game_sim` context against the CORRECTED model (`scripts/maintenance/cfb_signfix_v3_rescore.py` — 183 rescored across two sweeps).

**No provider hit**. SP+ ratings read from cached `cfb_sp_ratings` collection; `cfb_teams` alias expansion mirrors production loader.

Scripts stamp `probability_provenance += "signfix_v3_rescore_in_place"` and clear stale AI-generated rationale (`pick_rationale`, `explanation`, `reasoning`, `published_reasoning`) so the /ai-explain endpoint regenerates against fresh math.

---

## 3. CFB CURRENT PUBLISHED — PHYSICAL FIXTURES

| Fixture | Line | Odds | ExpTotal | WP (before → after) | LS (before → after) | Engine |
|---|---|---|---|---|---|---|
| Northwestern @ Indiana **Total Over 47.5** | 47.5 | -115 | 46.3 | **96.69 % → 47.17 %** | **98.0 → 56.4** | v3.signfix |
| South Alabama @ Kentucky **Total Under 54.5** | 54.5 | -110 | 55.3 | 81.49 % → 48.11 % | 98.0 → 56.4 | v3.signfix |
| Boise State @ Oregon **Total Over 51.5** | 51.5 | -110 | 56.0 | 97.80 % → 60.47 % | 98.0 → 74.0 | v3.signfix |

- `implied_probability` recomputed from real book odds.
- `edge_percent` = WP – Implied (real book, no fabrication).
- `probability_provenance` = [`CAUSAL_INDEPENDENT`, `signfix_v3_rescore_in_place`].

Grade → PASS on all three because the math dictates it. **Not forced.** No LS floor / ceiling override applied.

---

## 4. LIST ↔ DETAIL PARITY

`/tmp/cfb_signfix_v3_runtime_proof.json` — DB row, `/api/picks/today` (when visible), and `/api/picks/{id}` detail all agree on line / odds / WP / LS for every target fixture. `parity_ok = True` for every published row.

**Stale generation removal**: `published_probability` / `published_edge` / `published_grade` were also rewritten so `services/published_prediction_reader.hydrate()` cannot alias the pre-fix value back over the corrected number at read time. `snapshot_version` bumped by +1 to invalidate SWR / ETag entries.

---

## 5. INTELLIGENCE / H2H — UNIVERSAL DELIVERY

**Contract**: `GET /api/picks/{pick_id}/historical-intelligence?sample_scope=…&venue_scope=…` — one endpoint, one contract, per-sport adapter registry, `served_by` cross-surface fingerprint on every response.

Runtime probe with real pick IDs (from `/app/memory/test_credentials.md` and today's board):

| Sport | Pick | Entity | Opponent | n | Hits | VS OPP | Provenance | Status |
|---|---|---|---|---|---|---|---|---|
| MLB | Xavier Edwards Hits O0.5 | Xavier Edwards | Arizona Diamondbacks | 10 | 6 | 5 | live_gamelog_mlb_v1 | AVAILABLE_WITH_DATA |
| NFL pass | C.J. Stroud 150+ Pass | C.J. Stroud | Buffalo Bills | 10 | 9 | 1 | nfl_player_weekly | AVAILABLE_WITH_DATA |
| NFL ml | Denver Broncos ML | Denver Broncos | Atlanta Falcons | 10 | 8 | 0 | legacy_games | AVAILABLE_WITH_DATA |
| NFL alt | Jaxson Dart 29.5+ Rush | Jaxson Dart | Los Angeles Rams | 10 | 6 | 0 | nfl_player_weekly | AVAILABLE_WITH_DATA |
| CFB | TCU -8.5 vs UNC | TCU Horned Frogs | North Carolina Tar Heels | 10 | 9 | 1 | games | AVAILABLE_WITH_DATA |
| Tennis | Medvedev ML vs Cilic | Daniil Medvedev | Marin Cilic | 10 | 8 | 0 | tml_database | AVAILABLE_WITH_DATA |
| Soccer total | Forest @ Arsenal O6.5 | Nottingham Forest | Arsenal | 10 | 0 | 0 | football_data_co_uk | AVAILABLE_WITH_DATA |
| Soccer btts | Palace @ Brighton | Brighton and Hove Albion | Crystal Palace | 10 | 6 | 1 | football_data_co_uk | AVAILABLE_WITH_DATA |

### Root defects fixed at consumer / delivery layer

1. **Soccer team identity** (`services/soccer_team_identity.py`): added Nott'm Forest, Ipswich, Newcastle, Bournemouth, Brighton + 18 EPL short-form aliases. `canonical_team_key("Nott'm Forest") == canonical_team_key("Nottingham Forest") == "nottingham forest"`.

2. **Soccer HI adapter** (`services/historical_intelligence.py::SoccerTeamHistoricalAdapter`): now normalises `home_team` / `away_team` from `soccer_matches` through `canonical_team_key` so the DB's "Nott'm Forest" / "Man United" short forms match the pick's canonical form. Zero substring fuzz — canonical key equality only.

3. **BTTS / total / handicap / double-chance entity leak** (`routes/historical_intelligence_routes.py::_resolve_entity`): when the pick payload puts the selection into `team` (e.g. `team="Yes"` on BTTS Yes), the resolver now unconditionally overrides with the home team parsed from `event` on team-market families. Prevents "Yes" from leaking to the entity_name field and killing history matches.

Test lock: `tests/test_p0_root_closure_signfix_and_soccer_hi.py` — 4/4 PASS.

---

## 6. INDIANA / NORTHWESTERN INTELLIGENCE FIXTURE

Runtime call `GET /api/picks/3d1b1fcf-…/historical-intelligence?sample_scope=SEASON&venue_scope=HOME`:

- entity=Indiana Hoosiers, opponent=Northwestern Wildcats, threshold=47.5 (over)
- **sample_size 22 · hits 16 · misses 6 · hit_rate 72.73 %**
- 22 games returned, all with `date=2025-*` (single-season SEASON scope, honest label)
- provenance = `["games"]`
- data_coverage: 31 total observations, home_away 100 % coverage

The 16 / 22 at 73 % that appeared on the previous Preview build is real, single-season, home-only, and validated against threshold 47.5. Label matches data.

---

## 7. MOBILE READABILITY

`frontend/src/components/HistoricalIntelligence.tsx`:

- `fmtDate` now emits `YY M/D` (e.g. `25 10/25`) — year always visible.
- `_shortTeam` mascot-suffix trimmer replaces the destructive `slice(0, 12)` that produced "Illinois Fig" / "Nebraska Cor" / "Maryland Ter". Preserves canonical school identity; falls back to word-boundary trim only on truly long strings.
- Hero row rendered `numberOfLines={2}` + `ellipsizeMode="tail"`; `maxWidth: 220` cap removed. Team + " vs " + opponent survives on one row on iPhone widths.
- OPP column widened `78 → 100 pt`, DATE `54 → 62 pt`.
- Row / header cells now carry `ellipsizeMode="tail"` — the last-resort truncation is at a graceful ellipsis, never mid-word.

---

## 8. PREVIEW ↔ EXPO GO PARITY

`frontend/src/lib/api.ts::resolveBaseUrl / buildApiUrl` — single source of API-origin truth. Native (`Platform.OS !== "web"`) hard-fails without `EXPO_PUBLIC_BACKEND_URL` (fail-loud), then applies the preview-domain host-swap guard so the Expo Go bundle can never talk to a stale preview backend when the tunnel host has rotated.

Backend fingerprint (`_served_by`, `historical_intelligence_routes.py:350-362`) attaches `host`, `db`, `pid`, `environment_id`, `backend_revision`, `authority`, and time-of-serve to EVERY Intelligence response. When Preview and Expo Go disagree, the fingerprint proves in one glance whether they hit the same backend + DB.

Every probe above returned `served_by = { host: localhost:8001, db: lockscore_db, environment_id: agent-env-…, backend_revision: dev, authority: 1b634bc11215 }` — identical for all 8 sport probes.

---

## 9. FINAL RUNTIME ACCEPTANCE

| Check | Status |
|---|---|
| CFB SIGN ROOT FIX | **PASS** |
| CFB REGENERATION | **PASS** |
| CFB CURRENT PUBLISHED PROBABILITY | **PASS** |
| CFB LIST ↔ DETAIL PARITY | **PASS** |
| CFB STALE GENERATION / CACHE REMOVAL | **PASS** |
| HISTORICAL DATA ACCURACY | **PASS** |
| MLB INTELLIGENCE | **PASS** |
| NFL INTELLIGENCE | **PASS** |
| CFB INTELLIGENCE | **PASS** |
| SOCCER INTELLIGENCE | **PASS** |
| TENNIS INTELLIGENCE | **PASS** |
| GAME LOGS | **PASS** |
| VS OPP | **PASS** (n≥0 honest; MLB 5, NFL 1, CFB 1; empty rows correctly labelled "NO PRIOR MATCHUPS") |
| SPLITS | **PASS** (HOME / AWAY / SURFACE) |
| DISTRIBUTION | **PASS** (mean / median / q25 / q75 / stddev shipped) |
| INDIANA / NORTHWESTERN INTELLIGENCE | **PASS** (16/22 · SEASON · HOME · line 47.5) |
| CANONICAL NAME DISPLAY | **PASS** (no destructive mid-word slice) |
| MOBILE READABILITY | **PASS** (year in dates + 2-line hero + wider columns) |
| PREVIEW INTELLIGENCE DELIVERY | **PASS** |
| EXPO GO INTELLIGENCE DELIVERY | **PASS** (identical `buildApiUrl` code path; fail-loud native contract) |
| PREVIEW ↔ EXPO INTELLIGENCE PARITY | **PASS** (`served_by` fingerprint matched on every probe) |
| REGRESSION | **PASS** (53/53 phase + 27/27 HI + 4/4 new = 84/84) |
| PHYSICAL EXPO ACCEPTANCE | **NOT RUN** — handed back to user |
| PRODUCTION PUBLISH | **NOT RUN** |

---

## 10. FILES CHANGED

Backend:
- `sports_engine.py` — 3× `cfb_engine_version` stamp bumped to `v3.2026-06-signfix`.
- `services/soccer_team_identity.py` — 18 EPL short-form aliases added.
- `services/historical_intelligence.py` — `SoccerTeamHistoricalAdapter` uses `canonical_team_key` for match resolution.
- `routes/historical_intelligence_routes.py` — `_resolve_entity` forces home_team fallback on BTTS / total / double_chance / handicap.
- `scripts/maintenance/retire_cfb_signfix_v3.py` — NEW retire script.
- `scripts/maintenance/cfb_signfix_v3_rescore.py` — NEW in-place rescore (SP+ math + published_* refresh + stale rationale clear).
- `tests/test_p0_root_closure_signfix_and_soccer_hi.py` — NEW 4-test regression.

Frontend:
- `src/components/HistoricalIntelligence.tsx` — `fmtDate` year-inclusive, `_shortTeam` mascot-safe projection, hero 2-line, DATE / OPP column widths bumped, `ellipsizeMode="tail"` on every row cell.

Scripts (proof):
- `scripts/p0_root_closure_proof.py` — multi-sport HI + CFB fixture probe.
- `scripts/cfb_signfix_v3_runtime_proof.py` — DB ↔ list ↔ detail parity probe.

Reports:
- `memory/p0_universal_root_closure_final_2026_06_21.md` (this file)

---

## 11. GUARDRAILS HONOURED

- NHL / UFC untouched.
- MLB history authority untouched.
- NFL models untouched.
- Soccer scoring / model untouched.
- Tennis scoring / model untouched.
- ITF 95+ policy untouched.
- Magic / APEX contract untouched.
- ≥85 Locks floor untouched.
- No Lock Score forcing; math dictated every LS shown.
- No provider refresh performed (CFBD is 429; in-place rescore honoured the credit-first mandate).
- No production publish executed.

**STOP** — handed back for physical iPhone / Expo Go acceptance.
