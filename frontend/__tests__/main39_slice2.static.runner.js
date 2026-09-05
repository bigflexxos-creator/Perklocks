#!/usr/bin/env node
/**
 * MAIN 39 · Slice 2 — static-source assertions runner (no jest).
 * Mirrors the checks in `main39_slice2.test.js` so this suite runs
 * standalone under plain Node the same way `api_url_parity.runner.js`
 * does.  Exits non-zero on any failure.
 */
"use strict";
const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const API_TS   = fs.readFileSync(path.join(ROOT, "src", "lib", "api.ts"), "utf-8");
const FOCUS_TS = fs.readFileSync(path.join(ROOT, "src", "lib", "useFocusRefetch.ts"), "utf-8");
const LAB_TSX  = fs.readFileSync(path.join(ROOT, "app", "(tabs)", "lab.tsx"), "utf-8");

let passed = 0, failed = 0;
const failures = [];
function test(name, fn) {
  try { fn(); passed++; console.log("  PASS  " + name); }
  catch (e) { failed++; failures.push({name, msg: e.message}); console.log("  FAIL  " + name + "\n        " + e.message); }
}
function expect(a) {
  return {
    toMatch(rx) { if (!rx.test(a)) throw new Error(`expected match ${rx}`); },
    notToMatch(rx) { if (rx.test(a)) throw new Error(`expected NO match ${rx}`); },
    toBeGreaterThan(n) { if (!(a > n)) throw new Error(`expected > ${n}, got ${a}`); },
    toBe(v) { if (a !== v) throw new Error(`expected ${v}, got ${a}`); },
  };
}
function notMatch(a, rx) {
  if (rx.test(a)) throw new Error(`expected NO match ${rx}`);
}

console.log("\n== P0.4  Non-retryable 4xx contract ==");
test("P0.4 · retry predicate is EXACTLY 5xx + 408 + 429", () => {
  expect(API_TS).toMatch(/const shouldRetry = res\.status >= 500 \|\| res\.status === 408 \|\| res\.status === 429/);
});
test("P0.4 · 4xx throws tag Error with status + nonRetryable", () => {
  const idx = API_TS.indexOf("async function request<");
  expect(idx).toBeGreaterThan(0);
  const body = API_TS.slice(idx, idx + 8000);
  expect(body).toMatch(/httpErr\.status\s*=\s*res\.status/);
  expect(body).toMatch(/httpErr\.nonRetryable\s*=\s*!shouldRetry/);
  expect(body).toMatch(/throw httpErr/);
});
test("P0.4 · outer catch re-throws nonRetryable BEFORE retry backoff", () => {
  const idx = API_TS.indexOf("async function request<");
  const body = API_TS.slice(idx, idx + 8000);
  expect(body).toMatch(/if \(err && err\.nonRetryable === true\)\s*\{[\s\S]*?throw err;[\s\S]*?\}/);
});
test("P0.4 · AbortController timeout preserved", () => {
  expect(API_TS).toMatch(/new AbortController\(\)/);
  expect(API_TS).toMatch(/ctrl\.abort\(\)/);
});
test("P0.4 · in-flight GET dedupe preserved", () => {
  expect(API_TS).toMatch(/_inflight\.set\(dedupeKey/);
  expect(API_TS).toMatch(/if \(dedupeKey && _inflight\.has\(dedupeKey\)\)/);
});
test("P0.4 · 401 auto-recover event still fires (auth handling preserved)", () => {
  expect(API_TS).toMatch(/perkslocks:auth-expired/);
  expect(API_TS).toMatch(/!path\.startsWith\("\/auth\/"\)/);
});

console.log("\n== P0.6  useFocusRefetch success/failure contract ==");
test("P0.6 · fetcher signature accepts boolean returns", () => {
  expect(FOCUS_TS).toMatch(/fetcher:\s*\(\)\s*=>[^;]*boolean/);
});
test("P0.6 · lastFetchRef stamped ONLY on success (not speculatively)", () => {
  expect(FOCUS_TS).toMatch(/\.then\(\(result\)\s*=>\s*\{[\s\S]*?lastFetchRef\.current = Date\.now\(\)/);
});
test("P0.6 · result === false resets the stamp", () => {
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
test("P0.6 · invalidate() escape hatch preserved", () => {
  expect(FOCUS_TS).toMatch(/const invalidate = useCallback\(/);
  expect(FOCUS_TS).toMatch(/return \{ invalidate \}/);
});

console.log("\n== P0.6  Screens propagate boolean success signal ==");
const SCREENS = [
  ["app", "(tabs)", "index.tsx"],
  ["app", "(tabs)", "rollover.tsx"],
  ["app", "(tabs)", "parlay.tsx"],
  ["app", "(tabs)", "my-bets.tsx"],
  ["app", "history.tsx"],
];
for (const parts of SCREENS) {
  const rel = parts.join("/");
  test(`P0.6 · ${rel} load() returns explicit boolean`, () => {
    const src = fs.readFileSync(path.join(ROOT, ...parts), "utf-8");
    expect(src).toMatch(/return ok;|return true;|return false;/);
  });
}

console.log("\n== P0.7  Lab correlations-v2 centralized ==");
test("P0.7 · api.labCorrelationsV2 exists + hits /lab/correlations-v2", () => {
  expect(API_TS).toMatch(/labCorrelationsV2:\s*\(opts\?/);
  expect(API_TS).toMatch(/\/lab\/correlations-v2/);
});
test("P0.7 · api.labCorrelationsV2 supports sport + limit_per_section", () => {
  const idx = API_TS.indexOf("labCorrelationsV2:");
  expect(idx).toBeGreaterThan(0);
  const body = API_TS.slice(idx, idx + 800);
  expect(body).toMatch(/params\.set\("sport"/);
  expect(body).toMatch(/params\.set\("limit_per_section"/);
});
test("P0.7 · lab.tsx no longer uses raw fetch() for correlations-v2", () => {
  notMatch(LAB_TSX, /fetch\(\s*[`'"][^`'"]*\/api\/lab\/correlations-v2/);
});
test("P0.7 · lab.tsx now calls api.labCorrelationsV2", () => {
  expect(LAB_TSX).toMatch(/api\.labCorrelationsV2\(/);
});
test("P0.7 · unused getBackendUrl import pruned from lab.tsx", () => {
  notMatch(LAB_TSX, /import\s+\{[^}]*getBackendUrl[^}]*\}\s+from\s+["']@\/src\/lib\/api["']/);
});

console.log("\n──────────────────────────────────────────────");
console.log(`  ${passed} passed / ${failed} failed`);
if (failed > 0) {
  console.log("\nFailures:");
  failures.forEach(f => console.log(`  • ${f.name}\n    ${f.msg}`));
  process.exit(1);
}
process.exit(0);
