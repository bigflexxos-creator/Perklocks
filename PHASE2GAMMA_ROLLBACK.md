# Phase 2γ — Rollback Instructions

## Current checkpoint (Phase 2β approved)

- **Commit hash to push before beginning cutover:** `b07701b1` (Auto-generated changes on the last agent run)
- **Recommended tag:** `phase-2b-approved`
- **Push command (run manually):**
  ```bash
  cd /app
  git tag -a phase-2b-approved -m "Phase 2β approved checkpoint"
  git push origin main --tags
  ```

Confirm before starting Phase 2γ:

- [ ] `backend/.env` is gitignored (verified: `.gitignore:91:*.env → backend/.env`)
- [ ] `backend/.env.example` contains **variable names only**, no values
- [ ] Actual production values live in the Emergent environment config
- [ ] `ODDS_GATEWAY_ENABLED` and `ODDS_GLOBAL_REFRESH_MODE` env vars are configured on the deployment target

## Rollback matrix

| Symptom | Mitigation | Full rollback |
|---|---|---|
| Credit burn spikes above baseline | Set `ODDS_GATEWAY_ENABLED=false`. Legacy `cached_httpx_get` path continues, budget + coordinator remain active. | See "Hard rollback" below. |
| Global board goes stale between scheduled snapshots | Set `ODDS_GLOBAL_REFRESH_MODE=legacy_hourly`. Coordinator + budget + gateway still enforced. | See "Hard rollback" below. |
| A specific admin refresh is stuck | `POST /api/admin/ops/jobs/leases/recover` then re-trigger. | — |
| Duplicate reservations leaking capacity | `POST /api/admin/ops/budget/reservations/sweep` | — |
| Publication drift observed | Not related to 2γ — see Phase 1 immutable snapshot docs. | — |

## Hard rollback

If the flag toggles are not sufficient, revert the branch:

```bash
cd /app
git fetch --tags
git checkout phase-2b-approved -B phase-2c-hotfix-rollback
git push origin phase-2c-hotfix-rollback --force-with-lease
```

Then in the Emergent deployment console, redeploy `phase-2c-hotfix-rollback`.
The Phase 2β state contains:

- JobCoordinator + ProviderBudget (foundation)
- Shadow-mode observation for alt-lines / MLS / soccer prop snapshots
- Hardened admin force-refresh route
- DB-only `POST /api/picks/refresh` for normal users

## Post-rollback checklist

1. Verify `/api/admin/ops/budget/status` returns valid state.
2. Verify `/api/admin/ops/jobs` shows the expected scheduled_jobs rows.
3. Compare `/api/admin/ops/budget/reconcile` with the previous 24h baseline in `PHASE2_BASELINE_REPORT.md`.
4. Confirm `services/odds_api_gateway.py` module still imports cleanly (feature flag off means transport falls back, imports remain valid).
5. File a bug report before attempting a re-cutover.

## Contact

- Support: support@emergent.sh
