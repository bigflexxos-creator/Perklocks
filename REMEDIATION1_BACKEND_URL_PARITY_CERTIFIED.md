# REMEDIATION.1 — Native / Web / Preview Backend URL Parity — CERTIFIED

Return token: **`REMEDIATION1_BACKEND_URL_PARITY_CERTIFIED`**

Date: 2026-08
Status: **DONE**

────────────────────────────────────────────────────────────────

## Defect (pre-fix)

`frontend/src/lib/api.ts` had an authoritative `getBackendUrl()` resolver, but the central `request<T>()` function bypassed it by constructing URLs directly from module-level `BASE_URL`:

```typescript
// BEFORE — line 533
const url = `${BASE_URL}/api${path}`;
```

Consequences:
- **Web**: `BASE_URL === ""` → relative `/api/today` → same-origin ingress routes it correctly.
- **Preview**: `BASE_URL === "<preview-domain>"` → absolute URL works.
- **Native production (installed app)**: `BASE_URL === ""` if `EXPO_PUBLIC_BACKEND_URL` not baked into the build → **silently produces `/api/today`** which the native app has no origin for → requests fail / hit wrong host.

Exactly the "app/web/preview divergence" the audit flagged.

## Fix — one authoritative endpoint constructor

Added `buildApiUrl(path: string): string` to `api.ts` as the ONE authoritative endpoint constructor. All requests now flow through it.

### Behavior contract

| Environment | `Platform.OS` | `BASE_URL` | Result |
|---|---|---|---|
| **Web same-origin** | `"web"` | `""` | Relative `/api${path}` (legitimate) |
| **Web with configured base** | `"web"` | `"https://x"` | `https://x/api${path}` |
| **Preview / dev / configured native** | any | `"https://x"` | `https://x/api${path}` |
| **Native production (misconfigured)** | `"ios"`/`"android"` | `""` | **THROWS** `"Backend URL is not configured. Set EXPO_PUBLIC_BACKEND_URL in the production build environment."` |

### Slash normalization

- Trailing slash on base → stripped (`https://x/` → `https://x`)
- Path missing leading `/` → added (`"today"` → `/api/today`)
- Path starting `//` → collapsed (`//today` → `/api/today`)
- Path already prefixed `/api/*` → NOT doubled (no `/api/api/today`)
- Empty path → `/api/`

### Fail-loud on native production

`getBackendUrl()` throws **only when** `Platform.OS !== "web"` and `BASE_URL` is empty. Web same-origin remains legitimate (browser has an origin). `buildApiUrl()` re-raises the same error on non-web to preserve the fail-loud contract.

## Files changed

```
modified: frontend/src/lib/api.ts                            (+56, -8 lines)
              - getBackendUrl() gated on Platform.OS !== "web"
              - NEW: exported buildApiUrl() with normalization + fail-loud
              - request<T>() now calls buildApiUrl(path)
added:    frontend/__tests__/api_url_parity.test.js          (~155 lines, Jest-shape)
added:    frontend/__tests__/api_url_parity.runner.js        (~155 lines, plain Node)
added:    frontend/__tests__/api_url_parity.behavioral.js    (~95 lines, plain Node)
added:    REMEDIATION1_BACKEND_URL_PARITY_CERTIFIED.md       (this report)
```

## Test totals

**Source-inspection parity suite** (`api_url_parity.runner.js`): **15 / 15 pass**
- §A Central request path uses buildApiUrl (2 tests)
- §B Fail-loud on native production (3 tests)
- §C Slash normalization (4 tests)
- §D No consumer bypasses — grep across `/app/frontend/app/**/*.{ts,tsx}` for raw `fetch(\`${process.env.EXPO_PUBLIC_BACKEND_URL}...\`)` and hardcoded `emergentagent.com` hosts → zero violations (2 tests)
- §E Major consumer routes wired: today / History / Rollover / Parlay / pick-detail (1 test)
- §F Prior contracts preserved: no-cache header, 20s timeout, in-flight GET dedupe (3 tests)

**Behavioral suite** (`api_url_parity.behavioral.js`) — evaluates the actual extracted TypeScript against 3 real environments: **11 / 11 pass**
- Web same-origin (4 tests): `/today` → `/api/today`, `/api/today` NOT doubled, path with subpaths, empty path
- Configured preview/native (5 tests): absolute host, no doubling, trailing slash stripped, double-slash path collapsed, missing-leading-slash added
- Native production without config (2 tests): iOS throws with `EXPO_PUBLIC_BACKEND_URL` error message, Android same

**Backend regression sweep**: 178 passed / 0 failed on Block 2 suites (main board strictness, Platinum 2B.1A/B, MLB hitter reachability, MLB projected lineups). No backend regressions from this frontend change.

## Consumer trace

The `api` singleton in `api.ts` exports the major consumer routes — every one flows through `request<T>()` and therefore through `buildApiUrl()`:
- `api.picks.today` / `api.picks_today` — Main Locks
- `api.picks.history` / `api.picks_history` — History
- Rollover routes — grep confirmed `rollover` present
- Parlay routes — grep confirmed `parlay` present
- Pick detail / Why This Pick — grep confirmed present

Non-central callers checked via grep:
- `/app/frontend/app/(tabs)/lab.tsx:1326` already uses `getBackendUrl()` (compliant)
- No `fetch(\`${process.env.EXPO_PUBLIC_BACKEND_URL}...\`)` patterns found in `/app/frontend/app/`
- No hardcoded `emergentagent.com` fetch calls found

## Preserved (unchanged)

- Lock Score formula, 85-inclusive Main Board rule, 99 Lock, APEX, Magic weights.
- NFL Platinum architecture (2B.1A + 2B.1B).
- MLB / Tennis runtime.
- Canonical publication, History settlement, Champion/Challenger provenance.
- Cache-Control no-cache stamping, 20s request timeout, in-flight GET dedupe.

## Runtime health

- Backend `/api/health` → 200
- Frontend Expo dev server (port 3000) → 200

────────────────────────────────────────────────────────────────

## Final return code

**`REMEDIATION1_BACKEND_URL_PARITY_CERTIFIED`**

Ready to proceed to **REMEDIATION.2 — History UI canonical parity**.
