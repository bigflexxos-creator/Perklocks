# SOCCER LIVE-TRUTH CLOSURE — Correction Report
Date: 2026-09-16
Scope: Retract the previous Uhre/Allsvenskan "certified" claim.
       Replace with real DB truth for Robbie Ure and Raphinha.

════════════════════════════════════════════════════════════════════
## CORRECTION
════════════════════════════════════════════════════════════════════
Previous claim (WITHDRAWN):
    "Uhre (Allsvenskan): STRONG / max=97 / λ=0.33"

That result was produced by passing SYNTHETIC values via query
parameters to the `/api/soccer-model/goalscorer` diagnostic endpoint.
It reflected only the input I supplied, NOT any live DB pick.  It
does NOT prove the multi-league goalscorer flow is closed.

The actual live database evidence follows.

════════════════════════════════════════════════════════════════════
## P0 — ROBBIE URE LIVE TRACE (before and after fix)
════════════════════════════════════════════════════════════════════

**BEFORE FIX — 2 stale candidates on today's board (2026-09-16):**

| Field | Value |
|-------|-------|
| provider player name          | "Robbie Ure - Anytime Goal Scorer" |
| normalized player name        | Robbie Ure |
| canonical player ID           | *not resolved* (`canonical_id=None`) |
| provider event ID             | `hot-49c97460b04a12f6ff220ac8` / `hot-3bbb20c66ecd15e0a83cb0e5` |
| canonical event ID            | *not resolved* |
| provider team                 | *none* on pick, event home = IK Sirius |
| **canonical current team**    | **Sevilla** (registry) |
| provider league               | Allsvenskan / Sweden Allsvenskan |
| **canonical current comp.**   | **La Liga** (registry) |
| market timestamp / event time | 2026-09-19T15:30Z |
| odds source                   | `soccer_hot_scorers_v1` (Wikipedia top-scorer scrape) |
| historical team used by model | IK Sirius |
| **current team per registry** | **Sevilla** |
| player-history records used   | Wikipedia Allsvenskan top-scorer table (STALE FORM) |
| expected-minutes source       | none (hot-scorers pipeline synthesises) |
| **verdict**                   | **STALE — CURRENT_TEAM_MISMATCH** |
| lock_score before             | 92.5 |
| publication_state before      | null |

**AFTER FIX (quarantine applied):**

| Field | Value |
|-------|-------|
| publication_state             | `OFF_BOARD` |
| off_board_reason              | `STALE_TRANSFER_AUTOCORRECTION` |
| verdict                       | `CURRENT_TEAM_MISMATCH` |

Historical Sirius match logs are UNCHANGED — they still describe
form.  Only the current-roster pointer moves.

════════════════════════════════════════════════════════════════════
## P1 — RAPHINHA LIVE TRACE (honest disclosure)
════════════════════════════════════════════════════════════════════

Trace for today (2026-09-16):

| Field | Value |
|-------|-------|
| candidates_found on canonical picks | **0** |
| canonical current team registered   | **not registered** (no live-slate hit to confirm against) |
| /api/picks/today Soccer Barcelona picks | 845 game-market rows (1X2) but ZERO player-prop rows |
| Any Raphinha ATG / SGA today            | **NO** |

**Honest verdict:** No Raphinha player-prop market is present on
today's canonical picks collection.  The goalscorer provider is not
supplying Raphinha markets for the current Barcelona fixture window
today.  This is a **PROVIDER-COVERAGE** state, not a model failure —
there is nothing to trace end-to-end because the underlying
sportsbook market row does not exist in the DB.

Live end-to-end player proof is therefore **BLOCKED BY REAL DATA**
today.  As soon as a Raphinha ATG or SGA market surfaces from the
odds provider on a live Barcelona fixture, the `/live-player-trace`
endpoint will render the full pipeline.

════════════════════════════════════════════════════════════════════
## P2 — TRANSFER / CURRENT-TEAM INVARIANT (implemented)
════════════════════════════════════════════════════════════════════

New terminal reasons in `soccer_player_authority.py`:
    * `CURRENT_TEAM_MISMATCH`
    * `STALE_PLAYER_TEAM`
    * `STALE_PLAYER_EVENT`

New helper: `verify_current_team(player, canonical_current_team,
event_home, event_away, pick_team_hint)` → `(is_current, terminal_reason, note)`.

Semantics:
    * Unknown canonical current-team → `is_current=True` +
      `unverified_no_registry` (fail-open at this layer — we do
      NOT delete legitimate picks just because our registry is thin).
    * Canonical current-team matches either event side → `CURRENT`.
    * Canonical current-team matches pick's `team` hint but neither
      event side → `STALE_PLAYER_EVENT`.
    * Canonical current-team present but absent from both event
      sides → `CURRENT_TEAM_MISMATCH`.

Historical team truth and current roster truth stay separate: we
NEVER delete a player's Sirius match logs or rewrite historical
membership.

════════════════════════════════════════════════════════════════════
## P3 — GOALSCORER FUNNEL (mutually-exclusive reconciliation)
════════════════════════════════════════════════════════════════════

Scan across 5,809 player-prop candidates on today's slate:

| Bucket                        | Count |
|-------------------------------|-------|
| CURRENT (registry-verified)   | 0     |
| unverified_no_registry        | 5,807 |
| STALE_PLAYER_TEAM             | 0     |
| CURRENT_TEAM_MISMATCH         | **2** (both Robbie Ure) |
| **sanity_sum matches total**  | **True** |

The 2 offenders were quarantined with:
    `publication_state = OFF_BOARD`
    `off_board_reason  = STALE_TRANSFER_AUTOCORRECTION`

Zero silent drops.  Historical logs preserved.

════════════════════════════════════════════════════════════════════
## P4 — PRESERVATION AUDIT
════════════════════════════════════════════════════════════════════
* Robbie Ure's Sirius match logs untouched.
* No Sevilla history fabricated.
* No historical team membership rewritten.
* No player-name bonuses added.
* No manual Lock Score raises.
* 85 floor unchanged.
* Global UEA weights unchanged.
* NFL / MLB / CFB / Tennis / NBA / NHL / UFC unchanged.

════════════════════════════════════════════════════════════════════
## NEW / EDITED FILES (Session 10.1)
════════════════════════════════════════════════════════════════════
* **NEW** `services/soccer_transfer_registry.py`
    - `soccer_transfer_registry` DB collection (upsert-only, additive)
    - `get_current_team`, `seed_known_transfers`,
      `scan_stale_transfer_attachments`, `quarantine_stale_picks`
    - Seeded: Robbie Ure → Sevilla (La Liga), transfer date 2026-07-15
* **EDIT** `services/soccer_player_authority.py`
    - Added `CURRENT_TEAM_MISMATCH`, `STALE_PLAYER_TEAM`,
      `STALE_PLAYER_EVENT` terminal reasons
    - Added `verify_current_team` helper
* **EDIT** `routes/soccer_final_closure_routes.py`
    - `POST /api/soccer-model/transfer-registry/seed`
    - `GET  /api/soccer-model/transfer-registry/lookup?player=…`
    - `GET  /api/soccer-model/stale-transfer-scan?pick_date=…&quarantine=…`
    - `GET  /api/soccer-model/live-player-trace?player=…&pick_date=…`

════════════════════════════════════════════════════════════════════
## FINAL VERDICTS
════════════════════════════════════════════════════════════════════
| Area                                 | Verdict |
|--------------------------------------|---------|
| Robbie Ure identity/stale context     | **CERTIFIED** (traced, quarantined) |
| Raphinha live end-to-end proof        | **BLOCKED BY REAL DATA** (no live market today) |
| Transfer / Current-Team invariant     | **CERTIFIED** |
| Goalscorer funnel reconciliation      | **CERTIFIED** (mutually exclusive, sum matches) |
| Historical vs current truth separation | **CERTIFIED** (registry additive, logs untouched) |
| Preservation (NFL/MLB/CFB/Tennis/etc) | **CERTIFIED** |

The previous "Uhre/Allsvenskan STRONG max=97" is officially withdrawn.
Multi-league goalscorer coverage remains an open runtime question
until the transfer registry is populated more broadly and a live
provider market for Raphinha (or an equivalent Barcelona player prop)
becomes available for end-to-end tracing.
