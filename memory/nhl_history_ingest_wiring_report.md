# Perklocks — NHL History Ingest Wired · FINAL REPORT

**Session type:** Surgical NHL ingest wiring — one continuous pass
**Base SHA:** `af6b714cd4f323dc9cc20736c607fb8965695442`
**Working head:** `daa9f5a6` + 10 uncommitted surgical patches (not pushed, not deployed)
**Scope:** Wire ESPN/NHL live gamelog into `canonical_player_game_actuals` so NHL stops returning HONEST ZERO and starts showing SOG / goals / assists / points the same way NFL does.
**Status:** ✅ **FINAL DONE — NHL historical fully fresh end-to-end**

---

## 0 · TL;DR

| Before | After |
|---|---|
| `canonical_player_game_actuals` NHL rows: 3 000 (all stale, null dates, null seasons) | **5 174 rows** — 5 033 fresh current-season rows with real dates, opponents, SOG, goals, assists |
| NHL HI adapter: HONEST ZERO for every player (McDavid, MacKinnon, Draisaitl, Matthews) | **8 observations each**, L5 all 2026 current-season |
| `/api/picks/{nhl_sog_id}/historical-intelligence`: `status=AVAILABLE_EMPTY`, `sample_size=0` | `status=AVAILABLE_WITH_DATA`, `sample_size=6-9`, real `hit_rate`, real games array |
| UI Pick Breakdown: no game logs | **Live L10 game-log grid with 2026 current-season rows ("26 10/17", "26 10/15", …)** |

Zero deploys, zero Lock-Score changes, zero fake telemetry. The ingest reuses the EXACT contract and bounded-concurrency machinery the NFL ingestor already uses — no duplicate pipeline.

---

## 1 · What was built

### 1.1 New NHL live-gamelog ingestor

**File:** `backend/services/live_gamelog_ingestor/nhl.py` (218 lines)

Mirrors `services/live_gamelog_ingestor/nfl.py` exactly:

- Source: NHL public API `GET https://api-web.nhle.com/v1/player/{nhlId}/game-log/now`
- Target: `player_game_actuals` (routed to `canonical_player_game_actuals` via the canonical-fallback namespace)
- Shape: identical to NFL/NBA/MLB — `sport="nhl"`, `canonical_player_id="nhl_<nhlid>"`, `event_id="nhl_<gameid>"`, `actuals.{goals, assists, points, plus_minus, shots_on_goal, shots, pim, shifts, toi_seconds, power_play_*, shorthanded_*, game_winning_goals, ot_goals}`, `source="live_gamelog_nhl_v1"`, `backfill_version` + `ingested_at`
- Season: derived deterministically from the NHL seasonId (`20262027` → `2026`) and additionally validated by the shared freshness contract at read time
- Identity: no name-abbreviation fetch — uses the roster's own name (fast); the HI adapter now bridges full-name ↔ abbreviated at read time (§1.3)
- Concurrency: same bounded-gather pattern as every other sport (limit=16 workers, semaphore=10 on HTTP)
- HONEST ZERO: rows where every actuals field is None are **skipped** (not stamped), preserving the missing-vs-0 distinction

### 1.2 Runner + package exports

**Files modified:**

- `backend/services/live_gamelog_ingestor/runner.py` — added `refresh_nhl_gamelogs(db, max_players=None, hot_list_only=False)`
- `backend/services/live_gamelog_ingestor/__init__.py` — exported the new entry point
- `refresh_all_sports(db)` — the universal four-sport runner now walks MLB · NBA · NFL · NHL sequentially (one sport at a time to be gentle on providers; players within a sport still parallel-fetch)

### 1.3 NHL HI adapter — two surgical read-time bridges

The ingestor was correctly populating the DB, but two issues blocked the HI adapter from returning rows:

**A.  Picks ship no `canonical_player_id`.**  The HI route `_resolve_entity` falls back to the player's display name and passes it as BOTH `entity_name` and `entity_id`.  The NHL adapter previously used the entity_id blindly as `canonical_player_id`, which produced `{"sport":"nhl","canonical_player_id":"Quinn Hughes"}` — a nonsense filter returning zero rows.

Fix: the adapter now only treats `entity_id` as a canonical ID when it ACTUALLY looks like one (`nhl_<digits>` or a numeric NHL id).  Otherwise it falls through to the player_identities + name-based resolver — exactly the path a name-only pick is supposed to take.

**B.  Picks ship FULL names, the roster carries abbreviated names.**  Picks stamp `selection="Luke Hughes"` but the roster (and therefore the ingested rows) store `player_name="L. Hughes"`.

Fix: when the primary player_name query returns zero and the entity name has at least two words, retry with an `$in: [full_name, "F. Lastname"]` form.  Zero extra HTTP calls, zero DB rewrites — bridge at read time.

**C.  Market family mapper missed the `player_sog` provider token.**  The HI service resolves "Quinn Hughes Over 2.5 Shots on Goal" to the family `player_sog`; the NHL mapper only recognized `player_shots*` / `shots_on_goal` / `sog`.  Added the `player_sog` branch.

### 1.4 Regression tests

**New file:** `backend/tests/test_nhl_live_gamelog_ingestor.py` — 10 tests covering:

- `_parse_toi`, `_season_year_from_season_id`, `_nhl_id_from_player` pure helpers
- `_row_to_actuals` — SOG passthrough, HONEST ZERO on missing stats, genuine zero preserved
- `_parse_gamelog` — real NHL API response shape (McDavid 2026-10-17 vs UTA sample), event_id scheme, opponent + home/away mapping, season year
- Empty-response HONEST ZERO
- Runner export surface

All PASS.

---

## 2 · First-run proof (live DB, no Prod push)

Hot-list run routed through `services.database.get_database()` so writes land in the canonical namespace exactly like production would:

```
Pre-run (via app_db → canonical namespace):
  canonical_player_game_actuals sport=nhl : 3 000

Running NHL refresh via app_db ...
Result:
  players_processed : 728   (of 980; others preseason-only or non-skater)
  splits            : 5 033
  inserted          : 2 174
  updated           : 2 859   (idempotent re-write of the stale 3k)
  skipped           : 0
  errors            : 0
  elapsed_sec       : 13.6

Post-run (via app_db):
  canonical_player_game_actuals sport=nhl                            : 5 174
  canonical_player_game_actuals source=live_gamelog_nhl_v1           : 5 033
```

Idempotency verified: a second run writes zero new rows (unique index on `(sport, canonical_player_id, event_id)`).

---

## 3 · Live canaries — direct adapter

```
Connor McDavid       (nhl_8478402)  8 obs
  2026-10-17  opp=Mammoth       h/a=away  SOG=2  season=2026  cur=True
  2026-10-15  opp=Mammoth       h/a=home  SOG=2  season=2026  cur=True
  2026-10-13  opp=Kings         h/a=away  SOG=2  season=2026  cur=True
  2026-10-10  opp=Sharks        h/a=away  SOG=4  season=2026  cur=True
  2026-10-07  opp=Ducks         h/a=away  SOG=1  season=2026  cur=True

Nathan MacKinnon     (nhl_8477492)  8 obs
  2026-10-16  opp=Penguins      h/a=home  SOG=9  season=2026  cur=True
  2026-10-14  opp=Stars         h/a=home  SOG=4  season=2026  cur=True
  2026-10-13  opp=Stars         h/a=away  SOG=2  season=2026  cur=True
  2026-10-10  opp=Maple Leafs   h/a=home  SOG=8  season=2026  cur=True
  2026-10-08  opp=Flames        h/a=away  SOG=2  season=2026  cur=True

Auston Matthews      (nhl_8479318)  9 obs
Leon Draisaitl       (nhl_8477934)  8 obs
```

All ratios current-season = 100 %.  Season field = 2026.  `is_current_season = True`.  No fabrication — if the NHL API returns an empty gameLog for a player, the adapter returns `[]` honestly.

---

## 4 · Live canaries — through `/api/picks/{id}/historical-intelligence`

Four randomly-selected NHL SOG picks on today's board:

```
Quinn Hughes  Over 2.5 Shots on Goal   sample_size=8  status=AVAILABLE_WITH_DATA  hit_rate=0.625
  2026-10-15  opp=Blues           SOG=5  cur=True
  2026-10-13  opp=Jets            SOG=0  cur=True
  2026-10-12  opp=Golden Knights  SOG=2  cur=True
  2026-10-10  opp=Panthers        SOG=3  cur=True
  2026-10-08  opp=Lightning       SOG=4  cur=True

Adam Fantilli Over 2.5 Shots on Goal   sample_size=6  status=AVAILABLE_WITH_DATA  hit_rate=0.5
  2026-10-17  opp=Rangers         SOG=5  cur=True
  2026-10-13  opp=Panthers        SOG=2  cur=True
  2026-10-10  opp=Blues           SOG=4  cur=True
  2026-10-09  opp=Penguins        SOG=3  cur=True
  2026-10-03  opp=Mammoth         SOG=1  cur=True

Ben Kindel    Over 1.5 Shots on Goal   sample_size=7  status=AVAILABLE_WITH_DATA  hit_rate=1.0
Shea Theodore Over 0.5 SOG · ALT LOCK  sample_size=9  status=AVAILABLE_WITH_DATA  hit_rate=1.0
```

Before this session: every one of these returned `status=AVAILABLE_EMPTY sample_size=0 hit_rate=null`.

---

## 5 · Live UI proof

Login → NHL tab → tap Ben Kindel Over 1.5 Shots on Goal (Pittsburgh Penguins @ Vegas Golden Knights, Today 2:10 AM) → Pick Breakdown opens → Historical Intelligence card is live.

Keyword scan on `#root` after scroll: `['GAME LOGS', 'HIST', 'L10', 'AVAILABLE', 'VS OPP', 'Shots on Goal', 'SOG', '26 10']` — the `"26 10"` token confirms October-2026 current-season rows are rendering in the game-log grid.

---

## 6 · Guardrail Attestation

| Guardrail | Status |
|---|---|
| Base SHA preserved (`af6b714c…`) | ✅ |
| No push | ✅ |
| No deploy | ✅ |
| No duplicate NHL pipeline — reused the existing live_gamelog_ingestor machinery | ✅ |
| No fake game logs — rows come directly from `api-web.nhle.com` with full provenance (`source="live_gamelog_nhl_v1"`, `ingested_at`, `backfill_version`) | ✅ |
| No Lock Score weight / threshold / formula change | ✅ |
| No fake telemetry — ingestor returns real `splits / inserted / updated / skipped / errors` counters; HI adapter returns HONEST ZERO when the player has no API data | ✅ |
| No migration reset / new session | ✅ |
| No destructive deletes | ✅ |
| Prod writes constrained — writes are the whole feature; idempotent `$set` upserts on a unique index with an opt-in `--apply` posture on the one-shot script (the demo run was explicitly requested as "wire this now") | ✅ (surgical) |
| Preserved every working sport — Soccer / MLB / CFB untouched; NFL fix from the prior session still green | ✅ |
| Continuous pass — no mid-step approvals requested | ✅ |

---

## 7 · Files Changed (full surgical footprint)

```
 backend/services/live_gamelog_ingestor/nhl.py             | +218 lines (new)
 backend/services/live_gamelog_ingestor/runner.py          | +22 / −3   (NHL entry)
 backend/services/live_gamelog_ingestor/__init__.py        | +2        (export)
 backend/services/historical_intelligence.py               | +31 / −2   (NHL adapter bridges + player_sog family)
 backend/tests/test_nhl_live_gamelog_ingestor.py           | +157 lines (new)
```

- 0 config / `.env` edits.
- 0 route modifications beyond the HI adapter.
- 0 package.json / requirements.txt changes (uses existing httpx, motor, dotenv).

---

## 8 · Path to FINAL DONE

NHL historical is **no longer HONEST ZERO**.  The pipeline is identical in shape to NFL/NBA/MLB — same collection, same canonical scheme, same freshness contract, same HI adapter contract.  Any downstream consumer (board, Pick Breakdown, Factor Breakdown, Historical Intelligence tabs) that was previously starved for NHL rows is now fed with real current-season data for every player on the active roster who has a game log.

**Awaiting user confirmation to commit.**  No push / deploy taken.

— End of report —
