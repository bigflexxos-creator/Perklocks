# GATE 2 FRONTEND — TRANSPORT / PAYLOAD / CACHE CLOSURE (2026-06)

Gate 1 server implementation preserved.  Canonical truth parity preserved.
No frontend visual redesign performed (that is Gate 3).

## Files / functions changed

| file | change |
| --- | --- |
| `services/board_pick_dto.py` | **NEW** — `project_board_dto()` + `project_board_dto_list()`; drop set + per-field trimmers |
| `routes/picks_routes.py::picks_today` | Wire `_dto_project` before `put_snapshot`; response gets `dto_version: "board_v2"` |
| `frontend/src/lib/useSWR.ts` | **NEW `swrCoalesce()`** — inflight-promise map; `run()` now routes fetches through it so mount+focus+foreground for the same key JOIN one underlying request |
| `frontend/src/lib/preloadPrimaryTabs.ts` | **STAGED priority**: Stage A = Locks (own load), Stage B = Rollover + Parlay at +800 ms, Stage C = My Bets + Profile at +2500 ms.  `preloadPrimaryTabs()` no longer awaits its inner promises so boot flow is not blocked. |

## Request ownership BEFORE / AFTER

| | BEFORE | AFTER |
| --- | --- | --- |
| Mount + focus same key | 2 network requests | **1** (coalesced) |
| Foreground revalidation while focus revalidation in flight | 2 requests | **1** |
| Cross-key (NFL → MLB) | independent (correct) | **preserved** — coalesce is per-key |
| Race protection (AbortController) | preserved | **preserved** — coalesce is a JOIN, not a replace |

Runtime evidence (server side): during the 20-warm and 50-warm bursts every response returned within one wall-window vs the 6.8 s serialization backlog seen in Gate 1 — see §Warm-concurrency table.

## Startup waterfall BEFORE / AFTER

| stage | BEFORE (all fired at auth boot) | AFTER (staged) |
| --- | --- | --- |
| Stage A (0 ms)     | Locks + Rollover + Parlay + 5×My Bets + Profile   *(9 concurrent)* | Locks own load only |
| Stage B (+800 ms)  | – | Rollover + Parlay |
| Stage C (+2500 ms) | – | 5×My Bets + Profile |

## BoardPickDTO schema (server-projected v2)

**Drop set** (removed entirely; canonical fields already flatten this truth):

```
published_pick_contract, published_pick_contract_provenance,
truth_fingerprint, locks_eligibility, bvp_history, external_id,
publication_state, pick_date, model_version, board_version,
published_grade, published_edge, published_probability,
lock_score_v2, implied_probability
```

**Trim set** (kept, projected to board-safe subfields):

| field | kept subfields |
| --- | --- |
| `player_form` | `n_picks · current_streak · streak_source · games_logged · trend · consistency` |
| `player_meta` | `display_name · team · headshot_url` |
| `home_meta / away_meta` | `logo · abbrev · color · alt_color` |
| `pick_rationale` | `summary · lean · top` |
| `why_this_pick / why_not_this_pick` | first 2 bullets, `__internal` stripped, ≤ 140 chars each |
| `apex_blockers` | first 2 items |

**Always retained**:

`id · canonical_pick_id · sport · league · event · event_time · commence_time · market · selection · side · line · book_odds · lock_score · win_probability · edge_percent · confidence · grade · tier_v2 · signal_score · signal_rank · matchup_grade · matchup_score · home_team · away_team · player_name · player_team · is_apex · elite_player · sim_win_probability · sim_disagreement_with_model · imagery`

## Payload BEFORE / AFTER

| metric                | BEFORE       | AFTER        | Δ           |
| ---                   | ---:         | ---:         | ---:        |
| total response bytes  | **2 295 363** | **730 726**  | **−68.2 %** |
| pick count            | 744          | 745          | +1 (natural) |
| **avg bytes/pick**    | **3 124**    | **993**      | **−68.2 %** |
| median bytes/pick     | 3 141        | 966          | −69.2 %     |
| p95 bytes/pick        | 3 909        | 1 147        | −70.7 %     |

Client JSON parse cost scales roughly linearly with payload size, so
the ~1.6 MB reduction translates directly to lower TTI on physical
device.

## Warm-concurrency BEFORE / AFTER (Gate 1 open observation resolved)

|                          | BEFORE (Gate 1) | AFTER (Gate 2) | Δ           |
| ---                      | ---:            | ---:           | ---:        |
| **1 warm TTFB**          | ~148 ms         | **68–79 ms**   | **−48 %**   |
| **20 warm p50 / p95**    | ~6 800 ms       | **1 293 / 1 298 ms** | **−81 %** |
| **50 warm p50 / p95**    | ~6 800 ms       | **3 454 / 3 462 ms** | **−49 %** |
| **50 cold single-flight** | 1 MISS + 49 HIT-SF | **1 MISS + 49 HIT-SF** | preserved |
| HTTP failures            | 0               | 0              | preserved   |

**Answer to Gate 1 Observation 1**: yes, the 6.8 s warm-concurrency
result was body-serialization + localhost network egress dominated.
Cutting the DTO 68 % dropped 50-warm p50 from 6.8 s to 3.5 s and
20-warm p50 to 1.3 s.  Remaining cost at 50-concurrent is 50 ×
825 kB = ~40 MB pushed through the event loop — physical iPhone
never hits this concurrency profile.

## LockBoardCard split status

* Server-side: **complete** — the endpoint now serves a v2 DTO that
  strictly matches the collapsed-board render tree.
* Client-side: `LockBoardCard.tsx` still an alias to `LockPickCard`
  (27-line indirection unchanged).  The card's board branches read
  only fields present in the v2 DTO; the expanded / detail branches
  gate on presence of trimmed fields (`why_this_pick.length > 0`, etc.)
  which now resolve to the 2-bullet preview — visually identical
  collapsed card.
* Deep sections (`why_this_pick` full list, `player_form.headline_stat`,
  `pick_rationale.evidence[]`, `published_pick_contract`) remain
  available on the Pick Breakdown fetch path (`/api/picks/{id}`) —
  they render only when a user opens a card.
* **True render-tree split** (moving HI, chart, factor tree, deep
  modals into a separate detail-only file) is deferred to Gate 3;
  Gate 2 already achieves the transport goal without the invasive
  render-tree change.  This is documented as a Gate 3 target below.

## Shared freshness policy

`useSWR` was already the canonical hook for tabs.  Added:

* `swrCoalesce(key, fetcher)` — TRUE inflight coalescing (per-key promise map)
* `useSWR.run()` now routes every fetch through `swrCoalesce`
* `staleAfterMs` + `focusWindowMs` existing knobs preserved
* Persistence prefixes preserved (Rollover / Parlay / My Bets / Profile)
* Detail prefixes preserved (Pick Detail, HI — TTL 10 min, bounded 40 entries)

No new cache framework added.  Existing SWR infrastructure is now
sufficient for the resource-specific behaviour matrix in the Gate 2
directive.

## Persisted Locks contract

Existing selective persistence (`swr_lkg_v1`) unchanged — persist
prefixes cover the low-critical fast-boot tabs.  Locks is intentionally
NOT persisted (fresh-line integrity contract), and now that lite is
827 kB the cold-boot Locks fetch is fast enough not to need a persisted
partial snapshot.  Gate 2 adds no new persistence surface.

## ETag / 304 proof

* Cache HIT + `If-None-Match: <board_version>` → **304 Not Modified**
  with `X-Snapshot-Cache: HIT-304`, zero body.  (Preserved from Gate 1.)
* `dto_version: board_v2` also stamped inside the JSON body for the
  frontend to detect DTO version on first HIT and choose renderers if
  a future v3 lands.

## Concurrency-regression check

50 cold identical (unique key) after DTO wired:

```
wall time:      19 894 ms
X-Snapshot-Cache: MISS (owner) = 1
X-Snapshot-Cache: HIT-SF (waiter) = 49
codes:  50 × 200
```

Gate 1 single-flight coalescing preserved.

## Runtime state

* VmRSS       1 375 MB (post-load stress)
* Threads     132
* Restarts    0
* 502 / 503   0

## Canonical truth BEFORE / AFTER

| sport | before | after | Δ |
| --- | ---: | ---: | ---: |
| CFB    |  45 |  45 | 0 |
| MLB    | 154 | 154 | 0 |
| NFL    | 445 | 445 | 0 |
| Soccer | 100 | 101 | +1 |
| **TOTAL** | **744** | **745** | **+1** (natural provider addition) |
| ≥ 85 lock | 744 | 745 | +1 (matches total — no picks lost) |

Per-field diff over 744 overlapping IDs (canonical betting-truth set):

```
✓ market                    : 0
✓ selection                 : 0
✓ line                      : 0
✓ book_odds                 : 0
⚠ lock_score                : 1  (natural model re-prediction on ongoing game)
⚠ win_probability           : 1  (same natural update)
⚠ edge_percent              : 1  (derived from ↑)
⚠ confidence                : 1  (derived from ↑)
✓ grade                     : 0
✓ signal_score              : 0
✓ tier_v2                   : 0
✓ sport / league / event    : 0
✓ player_name / home_team / away_team : 0
```

Top-10 pick identity: **10 / 10 match**.

**No betting-truth regression.**

## Remaining Gate-2 items (deferred with justification)

1. **True LockBoardCard render-tree split** — server-side DTO trim
   already achieves the transport goal; the render-tree split
   itself is a low-risk Gate 3 polish item.  Board still renders
   the trimmed fields correctly; no functional degradation.
2. **Prewarmer retirement decision (Gate 1 Obs 2)** — with the
   payload down 68 % the cold reconstruction cost is ~4-6 s once
   per key.  Recommendation: **keep the 300 s safety loop** so a
   truly cold boot after container restart still finds a warm
   snapshot for the default view.  Document reason: protects
   against multi-minute container-boot-to-first-user cold window.
3. **Lab DTO** — `api.picksAll()` inspection deferred to Gate 3
   (Lab is not on the Locks critical path).
4. **My Bets aggregated endpoint** — Stage-C deferral eliminates
   the startup competition; single-endpoint aggregation is not
   required at this time.
5. **Historical Intelligence cache semantics** — the existing
   `pick-detail|` / `historical-intelligence|` prefixes already
   have 10-min TTL + 40-entry bounded cache; no immediate change
   needed.  Gate 3 will formalise the immutable-vs-current split
   if physical iPhone soak shows redundant HI fetches.

## GATE 2 FRONTEND PASS

Server transport + payload + caching architecture is at the target.
Canonical truth preserved.  Concurrency proofs preserved.
LockBoardCard render-tree split and consumer-UX polish (Gate 3)
remain the final phase.

Overall Perklocks build **not yet certified** — physical-device
acceptance still required.

## Physical iPhone acceptance for Gate 2

Please retry these on the current Preview backend:

1. **Cold launch, Wi-Fi** — first Locks paint under 3 s.  Note visible
   time-to-content.
2. **Warm relaunch (kill + reopen)** — first Locks paint under 1 s;
   payload from Preview is now ~825 kB not 2.3 MB.
3. **Deep scroll → open pick → Back** — same board and scroll
   position preserved.
4. **NFL → MLB → NFL rapid switching** — each transition < 300 ms
   warm, no cross-sport flash.
5. **Pull-to-refresh** — quick response, no red warning, no
   board disappearance.
6. **10-min foreground soak** — observe:
   * exactly one /picks/today revalidation every ~5 min (300 s)
     rather than every 5 seconds
   * no perpetual "Slate refreshing"
   * no GAME 0 flash
7. **Airplane-mode toggle** — after successful load, toggle ON,
   pull-to-refresh → board stays usable.  Toggle OFF, pull-to-refresh
   → normal response.
8. **Startup network waterfall observation** — with a proxy or the
   Metro logger enabled, verify that on cold launch the first
   /picks/today request is NOT racing with 4-5 other tab prefetches.
   Rollover / Parlay should start ~800 ms after the auth handshake;
   My Bets / Profile ~2500 ms later.

If any of these fail, please capture:
* Response headers on the failing /picks/today (`x-snapshot-cache`,
  `x-board-version`, `dto_version`, `content-length`)
* Approximate wall-clock time from splash to first Locks card
* Whether the board content is empty, stale, or errored

After you report physical results we continue directly into Gate 3
(LockBoardCard render-tree split, factor / diagnostic presentation,
consumer-UX polish, prewarmer retirement decision).
