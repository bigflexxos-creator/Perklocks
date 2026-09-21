# PerksLocks — Product Requirements (Live Delta 2026-06-21)

## Canonical Epoch v2 Contract — CLOSED (unpublished)

Ordered-revision authority. Origin-bound. Single-flight refresh. Shared consumer registry that revalidates mounted React state on advance. Late N responses after N+1 are `stale` and IGNORED — authority never regresses.

Six root defects closed:
1. Opaque hash used as ordered version → replaced with integer `revision`.
2. Locks tab caches outside SWR → stamped with `CanonicalEpoch`.
3. Cache deletion didn't refresh mounted state → shared consumer registry.
4. Refresh single-flight → `targetRevision` guard + explicit ownership release.
5. `/api/version` no-cache + `canonical_epoch` in body.
6. Origin now part of the epoch key.

**Tests targeted at shared contract only: 31 (6 backend + 25 frontend). PASS.**

Full sport / model suites intentionally NOT rerun.

Handed back for one physical Expo Go check. NOT published.

Full report: `/app/memory/canonical_epoch_v2_closure_2026_06_21.md`.

---

## Prior — CFB Sign Fix + Soccer HI (still valid)
`/app/memory/p0_universal_root_closure_final_2026_06_21.md`

---

## 2026-06-21 · Backend DATA_VERSION sync (surgical)
- Support confirmed Expo stale-bundle root cause is external. Investigation CLOSED.
- Preview QR (`exp://canonical-parity.preview.emergentagent.com`) is the sole physical test target.
- Deployed snapshot (`bet-edge-ai-1.emergent.host`) remains intentionally frozen — DO NOT republish.
- `backend/server.py` `DATA_VERSION` bumped from `2026.06.11-soccer-shots-sot-assists-wired-v49` → `2026.06.21-canonical-epoch-v2-signfix` to align `/api/version` with the current Canonical Parity frontend.
- Verified on Preview:
  - `GET /api/version` → `data_version: 2026.06.21-canonical-epoch-v2-signfix`
  - Profile screen shows Build/Source/Backend all pointing at Canonical Parity.
- NOT touched: Canonical Epoch logic, board generation, DB, API origins, Metro config.

