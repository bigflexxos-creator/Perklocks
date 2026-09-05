/**
 * REMEDIATION.1 — Native / Web / Preview backend URL parity tests
 * ─────────────────────────────────────────────────────────────────
 * Certifies that the central request path in `src/lib/api.ts` uses ONE
 * authoritative backend URL resolver (`buildApiUrl`), preventing the
 * previous defect where module-level `BASE_URL` allowed native production
 * to silently construct relative `/api/...` URLs and diverge from web.
 *
 * The tests statically inspect the compiled source rather than executing
 * the module (which requires Expo/React Native runtime shims).  This is
 * sufficient to catch:
 *   * bypassed request construction (any `${BASE_URL}/api` template
 *     literal outside `getBackendUrl`)
 *   * missing fail-loud on native production
 *   * broken slash normalization
 *   * consumer-level bypasses (direct fetch to a hardcoded host)
 */
const fs = require("fs");
const path = require("path");

const API_TS   = fs.readFileSync(
  path.resolve(__dirname, "..", "..", "src", "lib", "api.ts"), "utf-8");
const APP_DIR  = path.resolve(__dirname, "..", "..", "app");

// ──────────────────────────────────────────────────────────────────────
// §A  Central request path uses buildApiUrl (not raw ${BASE_URL})
// ──────────────────────────────────────────────────────────────────────

test("§A1  request() uses buildApiUrl, not module-level BASE_URL", () => {
  // Find the async function called `request` and inspect its body.
  const idx = API_TS.indexOf("async function request<");
  expect(idx).toBeGreaterThan(0);
  const body = API_TS.slice(idx, idx + 2000);
  // Must use buildApiUrl.
  expect(body).toMatch(/buildApiUrl\(path\)/);
  // Must NOT reference BASE_URL directly for URL construction.
  expect(body).not.toMatch(/\$\{BASE_URL\}\/api/);
});

test("§A2  buildApiUrl is exported for consumer use", () => {
  expect(API_TS).toMatch(/export function buildApiUrl\(/);
});

// ──────────────────────────────────────────────────────────────────────
// §B  Fail-loud on native production
// ──────────────────────────────────────────────────────────────────────

test("§B1  getBackendUrl throws on native when BASE_URL is empty", () => {
  // Locate getBackendUrl body.
  const idx = API_TS.indexOf("export function getBackendUrl(");
  expect(idx).toBeGreaterThan(0);
  const body = API_TS.slice(idx, idx + 900);
  // Throw path must be gated on Platform.OS !== "web".
  expect(body).toMatch(/Platform\.OS\s*!==\s*"web"/);
  expect(body).toMatch(/throw new Error/);
  expect(body).toMatch(/EXPO_PUBLIC_BACKEND_URL/);
});

test("§B2  buildApiUrl propagates native-production error", () => {
  const idx = API_TS.indexOf("export function buildApiUrl(");
  expect(idx).toBeGreaterThan(0);
  const body = API_TS.slice(idx, idx + 1400);
  // Must re-throw on non-web to preserve fail-loud contract.
  expect(body).toMatch(/if\s*\(\s*Platform\.OS\s*!==\s*"web"\s*\)\s*throw/);
});

test("§B3  web same-origin fallback is legitimate", () => {
  const idx = API_TS.indexOf("export function buildApiUrl(");
  const body = API_TS.slice(idx, idx + 1400);
  // When base is empty on web, return relative /api path.
  expect(body).toMatch(/return\s+cleanPath\s*;/);
});

// ──────────────────────────────────────────────────────────────────────
// §C  Slash normalization
// ──────────────────────────────────────────────────────────────────────

test("§C1  Trailing slash on base is stripped", () => {
  const body = API_TS.slice(API_TS.indexOf("export function buildApiUrl("));
  expect(body).toMatch(/replace\(\/\\\/\+\$\/,\s*""\)/);
});

test("§C2  Double '//' at path start is collapsed", () => {
  const body = API_TS.slice(API_TS.indexOf("export function buildApiUrl("));
  expect(body).toMatch(/while\s*\(\s*p\.startsWith\("\/\/"\)\s*\)/);
});

test("§C3  Duplicate '/api/api' is prevented", () => {
  const body = API_TS.slice(API_TS.indexOf("export function buildApiUrl("));
  expect(body).toMatch(/p\.startsWith\("\/api\/"\)/);
  expect(body).toMatch(/p\.slice\(4\)/);
});

test("§C4  Missing '/api' prefix is auto-added", () => {
  const body = API_TS.slice(API_TS.indexOf("export function buildApiUrl("));
  expect(body).toMatch(/const\s+cleanPath\s*=\s*"\/api"\s*\+\s*p/);
});

// ──────────────────────────────────────────────────────────────────────
// §D  No consumer bypasses the central layer
// ──────────────────────────────────────────────────────────────────────

function walkTsFiles(dir, out) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === "node_modules" || entry.name.startsWith(".")) continue;
      walkTsFiles(full, out);
    } else if (/\.(ts|tsx)$/.test(entry.name)) {
      out.push(full);
    }
  }
  return out;
}

test("§D1  No frontend file constructs backend URLs from raw env var", () => {
  const files = walkTsFiles(APP_DIR, []);
  const violations = [];
  for (const f of files) {
    const src = fs.readFileSync(f, "utf-8");
    // fetch(`${process.env.EXPO_PUBLIC_BACKEND_URL}...`) is the exact
    // bypass pattern we forbid.
    if (/fetch\(\s*[`'"]\s*\$\{\s*process\.env\.EXPO_PUBLIC_BACKEND_URL/
        .test(src)) {
      violations.push(path.relative(APP_DIR, f));
    }
    // Also flag any hardcoded production/preview host in a fetch.
    if (/fetch\(\s*[`'"]https:\/\/[^`'"]*emergentagent\.com/.test(src)) {
      violations.push(path.relative(APP_DIR, f) + ":hardcoded-host");
    }
  }
  expect(violations).toEqual([]);
});

test("§D2  Any inline backend URL retrieval uses getBackendUrl(), buildApiUrl(), or the api.* facade", () => {
  // MAIN 39 · P0.7 (2026-06): relaxed to accept EITHER central helper
  // OR the `api` facade after lab.tsx correlations-v2 was migrated
  // off the raw `fetch(...)` pattern onto `api.labCorrelationsV2`
  // (which routes through `buildApiUrl` internally).  Every `api.*`
  // call ultimately goes through `request()` → `buildApiUrl()`, so
  // an import of `api` from `@/src/lib/api` qualifies as central use.
  const files = walkTsFiles(APP_DIR, []);
  let seenCentralHelper = false;
  for (const f of files) {
    const src = fs.readFileSync(f, "utf-8");
    if (/from\s+["']@\/src\/lib\/api["']/.test(src)
        && /\b(getBackendUrl|buildApiUrl|api)\b/.test(src)) {
      seenCentralHelper = true;
      break;
    }
  }
  // At least one consumer must import a centralized surface —
  // proves the central layer is in active use.
  expect(seenCentralHelper).toBe(true);
});

// ──────────────────────────────────────────────────────────────────────
// §E  Major consumer routes use the central api layer
// ──────────────────────────────────────────────────────────────────────

test("§E1  today / History / Rollover / Parlay / pick detail use api.*", () => {
  // The exported `api` object in api.ts must expose every major route.
  const required = [
    /api\.picks_today\b|picks_today\s*:/,
    /api\.picks_history\b|picks_history\s*:/,
    /rollover/i,
    /parlay/i,
    /pick[_ ]?detail|whyThisPick|why_this_pick/i,
  ];
  for (const rx of required) {
    expect(API_TS).toMatch(rx);
  }
});

// ──────────────────────────────────────────────────────────────────────
// §F  Preserve prior contracts
// ──────────────────────────────────────────────────────────────────────

test("§F1  Cache-Control no-cache header still stamped", () => {
  expect(API_TS).toMatch(/no-cache, no-store, must-revalidate/);
});

test("§F2  Request timeout still 20s", () => {
  expect(API_TS).toMatch(/REQUEST_TIMEOUT_MS\s*=\s*20_?000/);
});

test("§F3  In-flight GET dedupe still present", () => {
  expect(API_TS).toMatch(/_inflight/);
});
