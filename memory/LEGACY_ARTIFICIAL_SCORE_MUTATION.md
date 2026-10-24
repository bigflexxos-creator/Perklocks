# LEGACY_ARTIFICIAL_SCORE_MUTATION — AUDIT (2026-06, One-Pass PerkLocks Migration)

Status: **CONFIRMED**  
Scope: In-app pick-refresh pipeline (`services/pick_refresh_orchestrator.py`)  
Audit trigger: User-mandated audit of post-model Lock Score mutators under the
One-Pass External-ML Migration brief. The user specifically asked us to
verify whether a player name / reputation can directly boost Lock Score.

---

## 1. Confirmed finding — `apply_elite_boost`

**File:** `backend/elite_players.py:641`  
**Called from:** `backend/services/pick_refresh_orchestrator.py:1034`

```python
# ── Elite Player Boost: world-class players (Mbappé, Haaland, Messi,
# Kane, Judge, Sinner, Jokic, Wilson, etc.) get a +10 lock_score bump
# so they auto-qualify for Lock tier — books price them tightly but
# they're still the safest hit candidates by reputation.
```

- The boost is sourced from a **curated static star-player name list** inside
  `elite_players.py` — no per-pick statistical evidence is attached.
- The in-code comment advertises "+10 lock_score bump"; the actual implementation
  multiplies by `ELITE_BOOST_PCT` (configurable) and clamps to `ELITE_LOCK_FLOOR`
  / 99. Either way, the mutation is **reputation-based, not evidence-based**.
- Impact on publication: picks that would otherwise fall below the 85 threshold
  can be lifted above it purely because the player name matches the curated list.
  This directly contradicts the user's rule that score adjustments must have
  auditable evidence provenance.

**Classification:** `LEGACY_ARTIFICIAL_SCORE_MUTATION`

---

## 2. Other mutators audited (EVIDENCE-BASED — not flagged)

| Mutator | File:line | Evidence provenance |
|---|---|---|
| `apply_player_form` | `backend/player_form.py:230-232` | last-10 games per player; capped at ±5 |
| `apply_bandit_lift` | `backend/bandit.py:296-298` | Thompson sampling over per-arm Beta posteriors; capped at `LIFT_MAX` |
| `apply_learning` (`learning_engine.py:343-353`) | re-computes `lock_score` via `sports_engine.compute_lock_score` using a learned/corrected win probability drawn from the historical ROI confidence bucket |
| `apply_simulations` | `brain.sim_runner` | per-pick Monte Carlo outcome distribution with `sim_runs`, `simulator_name`, CI bands; provenance written to `db.simulator_outputs` |

Each of the above attaches an auditable evidence field to the pick (`form_delta`,
`bandit_arm` + `sampled_mean`, `learning` block, `sim_win_probability` + `sim_runs`
+ `simulator_name`). They remain in the pipeline.

---

## 3. External-ML migration disposition

For this pass we honor the strict directive **"do NOT change scoring math or
lower the 85 threshold"**. The external runner replays the EXACT same mutator
sequence (including `apply_elite_boost`) so Preview semantics are identical to
the pre-migration in-app pipeline. The audit above is the output the user asked
for, and the external_runner script tags every pick with
`pick.audit.applied_mutators = [...]` so the Preview can measure which picks
would move if `apply_elite_boost` were disabled.

**Recommended follow-up (NOT in this pass):**
- Either attach per-pick statistical evidence to the elite boost (e.g. the
  player's actual historical hit rate on this market), or remove the boost
  entirely and let the model-produced lock score stand.
- Both options require their own one-pass change with explicit regression
  measurement.

---

## 4. Secondary finding — stale `picks_refresh:*` lease

**File:** `backend/services/pick_refresh_orchestrator.py`

- The distributed `picks_refresh:{date}:{sport}` lease stored in
  `db.scheduler_leases` was released ONLY on the success path (around the tail
  of the function). Exceptions and `asyncio` cancellation left the lease held
  until the 25-min TTL expired.
- `STALE_REFRESH_LOCK_AFTER_FAILURE` was reported in the brief but does NOT
  exist in the current codebase; what exists is `services/lease_recovery.py`
  with `STALE_EXPIRES_GRACE_SEC=90` / `HEARTBEAT_WINDOW_SEC=120` plus a
  one-shot stale-lease recovery pathway on the next acquisition attempt.
- **Fix (this pass):** `_refresh_picks` now releases the lease in a `finally`
  block scoped to `(lease_key, owner)` so success, exception, AND
  `asyncio.CancelledError` all free the lease immediately. Scoped by owner
  so a lost-race delete can never touch another instance's lease.
