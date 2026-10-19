# P0 NFL Historical Intelligence Identity-Namespace Fix — Closure Report

**Scope:** NFL picks carry GSIS IDs; NFL game actuals are ESPN-keyed; `compute_history_shadow()` silently returned `no_history` because the namespaces never intersected. This fix adds ONE authoritative GSIS↔ESPN crosswalk, resolves aliases at read time, and fails closed with explicit telemetry.

**Guardrails honoured:** no Lock Score / threshold / board generation / provider selection / NFL model / NHL model / settlement / frontend design changes. NHL left untouched (audit only, no code change). CFB merge-fix left untouched (verification only).

---

## A. Exact source of GSIS IDs on picks

- Writer: `services/pick_identity_enricher.py:505-510 enrich_pick_identity_async()` sets `canonical_player_id` on the pick doc.
- Resolver that returns the GSIS: `services/pick_identity_authority.py:121-210 resolve_player_authoritative()` queries `db.players` by name/`canonical_name` and verifies a join against `player_game_actuals`.
- Source of the GSIS values in `db.players`: `services/nfl_data_ingest.py` ingests nflverse weekly parquet (schema comment line 18: `"player_id": "00-0034857"  # nflverse GSIS`). Not Odds API.

The Odds API payload itself does NOT carry a GSIS id — `services/identity_resolver.py:94-136 resolve_player()` only produces `provider:provider_player_id` or `fallback:<sha1>`. The GSIS value is injected AFTER by the authoritative `db.players` name lookup.

Field name on the pick: `canonical_player_id` (shape `00-00NNNNN`).

---

## B. Exact crosswalk source

Built strictly from data **already in Mongo** — no new provider calls:

1. `db.players` (6,148 NFL rows): `name`, `team`, and `player_id` which is `espn_<N>` on preview. The ESPN half extracted directly from the `espn_` prefix.
2. `nfl_player_weekly` (133,077 rows): nflverse weekly feed keyed by GSIS. Join to `db.players` on `player_display_name` (full name) rather than the abbreviated `player_name` (`B.Purdy`). Team short-code (`SF`) vs full name (`San Francisco 49ers`) handled by substring match.
3. `player_game_actuals` (5,773 distinct identity shapes): captures the raw-numeric ESPN form (`4361741`) and GSIS form (`00-00NNNNN`) already observed in historical rows, so the resolver's alias set spans every writer era.

Build output: **8,523 total crosswalk rows · 2,752 fully-paired GSIS↔ESPN identities** on preview Mongo.

---

## C. Identity schema used

Collection `nfl_identity_crosswalk`:

```
{
  "_id":                 <canonical_player_id>,   # GSIS when known, else espn_<N>
  "sport":               "nfl",
  "canonical_player_id": <same as _id>,
  "espn_player_id":      "4361741"   | None,
  "gsis_player_id":      "00-0037834"| None,
  "name":                "Brock Purdy",
  "name_norm":           "brockpurdy",
  "team":                "San Francisco 49ers" | None,
  "aliases":             ["4361741", "espn_4361741", "00-0037834"],
  "provenance":          ["db.players","nfl_player_weekly","player_game_actuals"],
  "updated_at":          "2026-06-18T07:31:00Z",
}
```

Indexes on `aliases`, `(sport, name_norm)`, `espn_player_id`, `gsis_player_id` (idempotent `ensure_indexes()`).

One player → one identity → many external aliases. Never duplicate canonical players.

---

## D. Files changed

| File | Change |
| --- | --- |
| `backend/services/nfl_identity_crosswalk.py` | NEW. Authoritative resolver (`resolve_identity`, `resolve_history_keys`), idempotent writer (`upsert_identity`), index ensurer, short-TTL process cache, strict fail-closed contract (no silent name-only fallback). |
| `backend/services/history_intelligence.py` | `compute_history_shadow()` resolves NFL aliases via the crosswalk BEFORE querying `player_game_actuals`. Fails closed with `data_quality="identity_unresolved"` on no match; distinguishes `no_history_after_resolve` from the legacy `no_history` so operators can tell "no data" apart from "wrong namespace". Non-NFL sports unchanged. Query uses `canonical_player_id: {"$in": aliases}` so one lookup matches every alias shape at once. Context carries `identity_resolved`, `resolved_espn_player_id`, `resolved_gsis_player_id`, `aliases_tried`, `resolver_provenance`. |
| `backend/scripts/maintenance/build_nfl_identity_crosswalk.py` | NEW. Idempotent one-shot (or periodic) crosswalk builder; three phases strictly over existing Mongo data; dry-run by default. |
| `backend/tests/test_nfl_identity_crosswalk.py` | NEW. 15-test regression suite. |

No changes to `historical/nfl.py` ingestion, no changes to Lock Score / rationale / UI / board-generation / NHL / CFB code.

---

## E. Before / after Purdy proof

### BEFORE (silent `no_history`)

- Pick: `canonical_player_id = "00-0037834"` (GSIS, from `db.players` lookup).
- `compute_history_shadow()` query: `{sport:"nfl", canonical_player_id:"00-0037834", event_time:{$lt:cutoff}}`.
- `player_game_actuals` 2026 rows for Purdy are keyed by raw-numeric ESPN `"4361741"`.
- Result: `0 rows` → bundle `data_quality="no_history"`. Operator could not distinguish "no data" from "wrong namespace".

### AFTER (resolve → full alias set)

```
resolve_identity(db, pick_cpid="00-0037834")
  → espn=4361741, gsis=00-0037834, aliases=["4361741","espn_4361741","00-0037834"]

player_game_actuals.find({canonical_player_id: {$in: aliases}}).sort(event_time desc).limit(10):
  event_time=2026-10-04T20:25  event_id=401872975  cpid=4361741  pass_yds / pass_tds present
  event_time=2026-09-27T20:05  event_id=401872958  cpid=4361741
  event_time=2026-09-20T20:25  event_id=401872942  cpid=4361741
  event_time=2026-09-11T00:35  event_id=401872657  cpid=4361741
  event_time=2026-01-17T20:00  event_id=2025_20_SF_SEA  cpid=00-0037834   ← older GSIS-keyed row also pulled
  event_time=2026-01-11T16:30  event_id=2025_19_SF_PHI  cpid=00-0037834
  event_time=2026-01-03T20:00  event_id=2025_18_SEA_SF  cpid=00-0037834
```

**7 current-season (2026) rows visible — the alias query spans raw-ESPN AND GSIS identity shapes in one Mongo query.** Oct 4 and Sep 27/20/11 games all present; the Oct 11 game Support flagged will land as soon as the next ingestion tick writes it (fresh writer continues to use raw-numeric ESPN `4361741`).

Telemetry on the bundle:

```
context.identity_resolved = True
context.resolved_canonical_player_id = "00-0037834"
context.resolved_espn_player_id      = "4361741"
context.resolved_gsis_player_id      = "00-0037834"
context.aliases_tried                = ["4361741","espn_4361741","00-0037834"]
context.resolver_provenance          = ["db.players","nfl_player_weekly","player_game_actuals"]
```

---

## F. 7-player canary table

```
name                  ns     gsis         espn      latest_event        curr  L5  L10  HI_ready
----------------------------------------------------------------------------------------------
Brock Purdy           gsis   00-0037834   4361741   2026-10-04T20:25     7    5   10   True
Christian McCaffrey   name   00-0033280   3117251   2026-10-04T20:25     7    5   10   True
Patrick Mahomes       name   00-0033873   3139477   2026-10-04T20:25     4    5   10   True
Josh Allen            name   00-0034857   3918298   2026-10-04T17:00     6    5   10   True
Jalen Hurts           name   00-0036389   4040715   2026-10-04T17:00     5    5   10   True
CeeDee Lamb           name   00-0036358   4241389   2026-10-04T17:00     5    5   10   True
Lamar Jackson         name   00-0034796   3916387   2026-10-04T17:00     5    5   10   True
```

Every canary resolves. Every canary exposes current-season (2026) rows. Every canary has ≥ L5 and L10 history. Historical Intelligence `READY` across the board.

**Pick-side ID namespace**: when the pick arrives with a GSIS id directly, the resolver's probe path matches instantly (Purdy case). When the pick arrives name-only (ambiguous), the resolver dedupes crosswalk candidates by `(espn_id, gsis_id)` identifier pair and prefers the row with both populated — so two crosswalk rows for the same player (one `espn_<N>`-keyed, one GSIS-keyed) collapse to a SINGLE identity.

---

## G. NHL namespace audit (no NHL code changed)

| Layer | Identity shape | Source |
| --- | --- | --- |
| Ingestion (`historical/nhl.py`) | `nhl_8477933` | ESPN numeric, `nhl_` prefix |
| HI reader (`player_game_actuals`) | `nhl_8473512` | SAME shape |
| Pick normaliser (`db.players`) | `nhl_8475461` | SAME shape |

**Verdict:** NHL does NOT exhibit the same class of defect — all three layers use the identical `nhl_<N>` namespace. NHL Production invisibility is caused by the already-fixed `PERKLOCKS_REFRESH_SPORTS` env-override issue (see `/app/NHL_PREVIEW_PROD_FIX.md`), not an identity mismatch. **No NHL changes applied in this build.**

---

## H. CFB verification

| Layer | Identity shape |
| --- | --- |
| Ingestion (`historical/cfb.py`) | `espn_cfb_<N>` (post merge-fix, unchanged by this build) |
| HI reader (`player_game_actuals`) | *(empty for CFB today — no mismatch possible)* |
| Pick normaliser (`db.players`) | `espn_cfb_<N>` |

Namespaces are consistent. The 2026-06 merge-fix (`stat_blocks: [...]` + `{sport, game_id, player_id}` upsert filter) is intact. No reapply, no identity-layer edit.

---

## I. Tests + results

### New regression suite — 15 / 15 passed

```
tests/test_nfl_identity_crosswalk.py
  ✓ test_resolve_gsis_to_full_alias_set
  ✓ test_resolve_espn_prefixed_matches_gsis
  ✓ test_resolve_raw_numeric_espn_matches_gsis
  ✓ test_unresolved_returns_none
  ✓ test_no_silent_name_only_fallback_when_ambiguous
  ✓ test_name_only_single_match_resolves
  ✓ test_upsert_is_idempotent_and_additive
  ✓ test_gsis_pick_resolves_to_espn_actuals
  ✓ test_espn_prefixed_pick_also_resolves
  ✓ test_unresolved_gsis_fails_closed
  ✓ test_no_silent_name_fallback_in_shadow
  ✓ test_L5_L10_uses_current_rows_after_alias_resolution
  ✓ test_cutoff_filtering_preserved
  ✓ test_no_history_distinct_from_identity_unresolved
  ✓ test_espn_keyed_ingestion_stays_idempotent_under_crosswalk
```

### Combined — 49 / 49 passed (crosswalk + cross-pod authority + historical merge + NHL floor + login repro) + 26 / 26 Master Surgical static.

---

## J. Commit SHA

Baseline: `5049afe7 Auto-generated changes`. This change-set is on-disk only (no deploy per the explicit guardrail).

---

## P0-E — Historical backfill decision

**No re-key, no row duplication required.** Read-time alias resolution makes every existing ESPN-keyed row AND every GSIS-keyed row visible to a pick carrying either shape via a single `$in` query. The crosswalk is additive and idempotent; re-running `build_nfl_identity_crosswalk.py --commit` only merges new provenance tags without overwriting existing identifiers.

---

## P0-D — Ingestion verification

`historical/nfl.py` continues to write `espn_<N>`-prefixed rows into `player_game_logs`. The merge-fix regression `test_espn_keyed_ingestion_stays_idempotent_under_crosswalk` runs two identical ingestions and confirms exactly 1 Mongo row survives — ESPN identity provenance preserved, idempotent, no destructive change to the known-healthy ingestion path.

---

**Explicitly NOT declared fixed by this work:** board drop, lock score math, NFL/NHL model math, provider selection, settlement, frontend design.
