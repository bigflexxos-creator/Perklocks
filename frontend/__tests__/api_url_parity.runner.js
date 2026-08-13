#!/usr/bin/env node
/**
 * REMEDIATION.1 parity test runner — plain Node (no jest dependency).
 * Executes the same source-inspection checks as
 * ``__tests__/api_url_parity.test.js`` and exits non-zero on failure.
 *
 * Run:  node __tests__/api_url_parity.runner.js
 */
"use strict";
const fs = require("fs");
const path = require("path");

const API_TS  = fs.readFileSync(
  path.resolve(__dirname, "..", "src", "lib", "api.ts"), "utf-8");
const APP_DIR = path.resolve(__dirname, "..", "app");

let passed = 0, failed = 0;
const failures = [];

function test(name, fn) {
  try {
    fn();
    passed++;
    console.log(`  PASS  ${name}`);
  } catch (e) {
    failed++;
    failures.push({ name, msg: e.message });
    console.log(`  FAIL  ${name}\n        ${e.message}`);
  }
}
function expect(actual) {
  return {
    toMatch(rx) {
      if (!rx.test(actual)) throw new Error(
        `Expected match for ${rx}\n        got: ${String(actual).slice(0, 200)}`);
    },
    notToMatch(rx) {
      if (rx.test(actual)) throw new Error(`Expected no match for ${rx}`);
    },
    toBeGreaterThan(n) {
      if (!(actual > n)) throw new Error(`Expected ${actual} > ${n}`);
    },
    toBe(v) {
      if (actual !== v) throw new Error(
        `Expected ${JSON.stringify(v)}, got ${JSON.stringify(actual)}`);
    },
    toEqual(v) {
      if (JSON.stringify(actual) !== JSON.stringify(v))
        throw new Error(
          `Expected ${JSON.stringify(v)}, got ${JSON.stringify(actual)}`);
    },
  };
}
// Support both toMatch(rx) and notToMatch(rx).
function notMatch(actual, rx) {
  if (rx.test(actual)) throw new Error(`Expected no match for ${rx}`);
}

console.log("\n== §A  Central request path uses buildApiUrl ==");
test("§A1  request() uses buildApiUrl, not module-level BASE_URL", () => {
  const idx = API_TS.indexOf("async function request<");
  expect(idx).toBeGreaterThan(0);
  const body = API_TS.slice(idx, idx + 2000);
  expect(body).toMatch(/buildApiUrl\(path\)/);
  notMatch(body, /\$\{BASE_URL\}\/api/);
});
test("§A2  buildApiUrl is exported", () => {
  expect(API_TS).toMatch(/export function buildApiUrl\(/);
});

console.log("\n== §B  Fail-loud on native production ==");
test("§B1  getBackendUrl throws on native when BASE_URL empty", () => {
  const idx = API_TS.indexOf("export function getBackendUrl(");
  expect(idx).toBeGreaterThan(0);
  const body = API_TS.slice(idx, idx + 900);
  expect(body).toMatch(/Platform\.OS\s*!==\s*"web"/);
  expect(body).toMatch(/throw new Error/);
  expect(body).toMatch(/EXPO_PUBLIC_BACKEND_URL/);
});
test("§B2  buildApiUrl propagates native-production error", () => {
  const idx = API_TS.indexOf("export function buildApiUrl(");
  expect(idx).toBeGreaterThan(0);
  const body = API_TS.slice(idx, idx + 1600);
  expect(body).toMatch(/if\s*\(\s*Platform\.OS\s*!==\s*"web"\s*\)\s*throw/);
});
test("§B3  Web same-origin fallback is legitimate", () => {
  const idx = API_TS.indexOf("export function buildApiUrl(");
  const body = API_TS.slice(idx, idx + 1600);
  expect(body).toMatch(/return\s+cleanPath\s*;/);
});

console.log("\n== §C  Slash normalization ==");
test("§C1  Trailing slash on base stripped", () => {
  const body = API_TS.slice(API_TS.indexOf("export function buildApiUrl("));
  expect(body).toMatch(/\.replace\(\/\\\/\+\$\/,\s*""\)/);
});
test("§C2  Double '//' at path start collapsed", () => {
  const body = API_TS.slice(API_TS.indexOf("export function buildApiUrl("));
  expect(body).toMatch(/while\s*\(\s*p\.startsWith\("\/\/"\)\s*\)/);
});
test("§C3  Duplicate '/api/api' prevented", () => {
  const body = API_TS.slice(API_TS.indexOf("export function buildApiUrl("));
  expect(body).toMatch(/p\.startsWith\("\/api\/"\)/);
  expect(body).toMatch(/p\.slice\(4\)/);
});
test("§C4  '/api' prefix auto-added", () => {
  const body = API_TS.slice(API_TS.indexOf("export function buildApiUrl("));
  expect(body).toMatch(/const\s+cleanPath\s*=\s*"\/api"\s*\+\s*p/);
});

console.log("\n== §D  No consumer bypasses ==");
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
test("§D1  No fetch() with raw EXPO_PUBLIC_BACKEND_URL", () => {
  const files = walkTsFiles(APP_DIR, []);
  const violations = [];
  for (const f of files) {
    const src = fs.readFileSync(f, "utf-8");
    if (/fetch\(\s*[`'"]\s*\$\{\s*process\.env\.EXPO_PUBLIC_BACKEND_URL/
        .test(src)) {
      violations.push(path.relative(APP_DIR, f));
    }
    if (/fetch\(\s*[`'"]https:\/\/[^`'"]*emergentagent\.com/.test(src)) {
      violations.push(path.relative(APP_DIR, f) + ":hardcoded-host");
    }
  }
  expect(violations).toEqual([]);
});
test("§D2  At least one consumer imports getBackendUrl", () => {
  const files = walkTsFiles(APP_DIR, []);
  let seen = false;
  for (const f of files) {
    const src = fs.readFileSync(f, "utf-8");
    if (/from\s+["']@\/src\/lib\/api["']/.test(src)
        && /getBackendUrl/.test(src)) {
      seen = true; break;
    }
  }
  expect(seen).toBe(true);
});

console.log("\n== §E  Major consumer routes wired ==");
test("§E  today/History/Rollover/Parlay/pick-detail all present", () => {
  const required = [
    { name: "picks_today",  rx: /picks[/_ ]today|picksToday|picks_today/i },
    { name: "picks_history", rx: /picks[/_ ]history|picks_history|history/i },
    { name: "rollover",     rx: /rollover/i },
    { name: "parlay",       rx: /parlay/i },
    { name: "pick_detail",  rx: /pick[_-]?detail|whyThisPick|why[_-]this[_-]pick/i },
  ];
  for (const { name, rx } of required) {
    if (!rx.test(API_TS)) throw new Error(`missing route: ${name}`);
  }
});

console.log("\n== §F  Prior contracts preserved ==");
test("§F1  Cache-Control no-cache stamped", () => {
  expect(API_TS).toMatch(/no-cache, no-store, must-revalidate/);
});
test("§F2  Request timeout still 20s", () => {
  expect(API_TS).toMatch(/REQUEST_TIMEOUT_MS\s*=\s*20_?000/);
});
test("§F3  In-flight GET dedupe present", () => {
  expect(API_TS).toMatch(/_inflight/);
});

console.log("\n──────────────────────────────────────────────");
console.log(`  ${passed} passed / ${failed} failed`);
if (failed > 0) {
  console.log("\nFailures:");
  failures.forEach(f => console.log(`  • ${f.name}\n    ${f.msg}`));
  process.exit(1);
}
process.exit(0);
