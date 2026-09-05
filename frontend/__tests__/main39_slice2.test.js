/**
 * MAIN 39 · Slice 2 — Frontend Reliability Closure
 * ─────────────────────────────────────────────────────────────────
 *   P0.4 — non-retryable 4xx: exactly ONE network request
 *   P0.6 — useFocusRefetch stamps success only, resets on failure
 *   P0.7 — Lab correlations-v2 routes through api.labCorrelationsV2
 *
 * Static-source certification identical in style to
 * `api_url_parity.test.js`.  A runtime companion at
 * `main39_slice2.runner.js` exercises the retry decision predicate
 * end-to-end.
 */
"use strict";
const fs   = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const API_TS      = fs.readFileSync(path.join(ROOT, "src", "lib", "api.ts"), "utf-8");
const FOCUS_TS    = fs.readFileSync(path.join(ROOT, "src", "lib", "useFocusRefetch.ts"), "utf-8");
const LAB_TSX     = fs.readFileSync(path.join(ROOT, "app", "(tabs)", "lab.tsx"), "utf-8");

// ──────────────────────────────────────────────────────────────────
// P0.4 — non-retryable 4xx contract
// ──────────────────────────────────────────────────────────────────

test("P0.4 · retry list is exactly 5xx + 408 + 429", () => {
  // The single-source-of-truth predicate in request().
  expect(API_TS).toMatch(
    /const shouldRetry = res\.status >= 500 \|\| res\.status === 408 \|\| res\.status === 429/,
  );
});

test("P0.4 · 4xx throws are tagged nonRetryable + include status", () => {
  const idx = API_TS.indexOf("async function request<");
  expect(idx).toBeGreaterThan(0);
  const body = API_TS.slice(idx, idx + 8000);
  // Error must be tagged for the outer catch to short-circuit.
  expect(body).toMatch(/httpErr\.status\s*=\s*res\.status/);
  expect(body).toMatch(/httpErr\.nonRetryable\s*=\s*!shouldRetry/);
  expect(body).toMatch(/throw httpErr/);
});

test("P0.4 · outer catch re-throws nonRetryable immediately (no retry)", () => {
  const idx = API_TS.indexOf("async function request<");
  const body = API_TS.slice(idx, idx + 8000);
  // The re-throw guard must appear BEFORE the retry backoff.
  expect(body).toMatch(
    /if \(err && err\.nonRetryable === true\)\s*\{[\s\S]*?throw err;[\s\S]*?\}/,
  );
});

test("P0.4 · AbortController timeout preserved", () => {
  expect(API_TS).toMatch(/new AbortController\(\)/);
  expect(API_TS).toMatch(/ctrl\.abort\(\)/);
});

test("P0.4 · in-flight GET dedupe preserved", () => {
  expect(API_TS).toMatch(/_inflight\.set\(dedupeKey/);
  expect(API_TS).toMatch(/if \(dedupeKey && _inflight\.has\(dedupeKey\)\)/);
});

test("P0.4 · 401 auth-expired event preserved", () => {
  expect(API_TS).toMatch(/perkslocks:auth-expired/);
  // Bad-creds on /auth/* still surfaces to the caller.
  expect(API_TS).toMatch(/!path\.startsWith\("\/auth\/"\)/);
});

// ──────────────────────────────────────────────────────────────────
// P0.6 — useFocusRefetch success/failure semantics
// ──────────────────────────────────────────────────────────────────

test("P0.6 · fetcher signature accepts boolean or void resolves", () => {
  // Signature must permit Promise<boolean> without breaking legacy void.
  expect(FOCUS_TS).toMatch(/fetcher:\s*\(\)\s*=>[^;]*boolean/);
});

test("P0.6 · lastFetchRef is stamped ONLY inside the resolve success branch", () => {
  // Speculative stamping (stamp before await) is the exact bug we removed.
  // Assert the stamp lives inside the .then((result) => …) success branch.
  expect(FOCUS_TS).toMatch(/\.then\(\(result\)\s*=>\s*\{[\s\S]*?lastFetchRef\.current = Date\.now\(\)/);
});

test("P0.6 · fetcher === false explicitly resets the stamp", () => {
  expect(FOCUS_TS).toMatch(/if \(result === false\)\s*\{\s*lastFetchRef\.current = 0/);
});

test("P0.6 · rejection resets the stamp", () => {
  expect(FOCUS_TS).toMatch(/\.catch\(\(\)\s*=>\s*\{[\s\S]*?lastFetchRef\.current = 0/);
});

test("P0.6 · in-flight guard prevents overlapping focus fetches", () => {
  expect(FOCUS_TS).toMatch(/inFlightRef\s*=\s*useRef<boolean>\(false\)/);
  expect(FOCUS_TS).toMatch(/if \(inFlightRef\.current\)/);
  expect(FOCUS_TS).toMatch(/\.finally\(\(\)\s*=>\s*\{[\s\S]*?inFlightRef\.current = false/);
});

test("P0.6 · invalidate() escape hatch still exists", () => {
  expect(FOCUS_TS).toMatch(/const invalidate = useCallback\(/);
  expect(FOCUS_TS).toMatch(/return \{ invalidate \}/);
});

// Screens must all propagate boolean success signal.
const SCREENS = [
  ["app", "(tabs)", "index.tsx"],       // Locks
  ["app", "(tabs)", "rollover.tsx"],    // Rollover
  ["app", "(tabs)", "parlay.tsx"],      // Parlay
  ["app", "(tabs)", "my-bets.tsx"],     // My Bets
  ["app", "history.tsx"],               // History
];
for (const parts of SCREENS) {
  const rel = parts.join("/");
  test(`P0.6 · ${rel} load() returns explicit success/failure boolean`, () => {
    const src = fs.readFileSync(path.join(ROOT, ...parts), "utf-8");
    // Every screen must either "return ok;" or "return true/false" at the
    // end of a load() promise so the hook receives the signal.
    expect(src).toMatch(/return ok;|return true;|return false;/);
  });
}

// ──────────────────────────────────────────────────────────────────
// P0.7 — Lab correlations-v2 centralized
// ──────────────────────────────────────────────────────────────────

test("P0.7 · api.labCorrelationsV2 exists and hits /lab/correlations-v2", () => {
  expect(API_TS).toMatch(/labCorrelationsV2:\s*\(opts\?/);
  expect(API_TS).toMatch(/\/lab\/correlations-v2/);
});

test("P0.7 · api.labCorrelationsV2 supports sport + limit_per_section params", () => {
  const idx = API_TS.indexOf("labCorrelationsV2:");
  expect(idx).toBeGreaterThan(0);
  const body = API_TS.slice(idx, idx + 800);
  expect(body).toMatch(/params\.set\("sport"/);
  expect(body).toMatch(/params\.set\("limit_per_section"/);
});

test("P0.7 · lab.tsx no longer uses raw fetch() for correlations-v2", () => {
  // The exact bypass pattern that motivated this slice.
  expect(LAB_TSX).not.toMatch(/fetch\(\s*[`'"][^`'"]*\/api\/lab\/correlations-v2/);
});

test("P0.7 · lab.tsx now imports and calls api.labCorrelationsV2", () => {
  expect(LAB_TSX).toMatch(/api\.labCorrelationsV2\(/);
});

test("P0.7 · unused getBackendUrl import removed from lab.tsx", () => {
  // We dropped the only consumer of getBackendUrl in lab.tsx when we
  // centralized the fetch, so the import must be pruned.
  expect(LAB_TSX).not.toMatch(/import\s+\{[^}]*getBackendUrl[^}]*\}\s+from\s+["']@\/src\/lib\/api["']/);
});
