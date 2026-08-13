#!/usr/bin/env node
/**
 * REMEDIATION.1 — buildApiUrl behavioral unit test (no framework).
 *
 * Extracts and executes ``buildApiUrl`` from api.ts against three
 * scenarios: web same-origin, preview/configured native, and
 * native-production-without-config.  Uses a lightweight shim for
 * ``Platform.OS`` + ``BASE_URL`` so we can exercise the real
 * normalization logic.
 */
"use strict";
const fs = require("fs");
const path = require("path");

function extractHelpers(src) {
  // Grab getBackendUrl + buildApiUrl bodies.
  const gStart = src.indexOf("export function getBackendUrl(");
  const bStart = src.indexOf("export function buildApiUrl(");
  const bEnd   = src.indexOf("\n}\n", bStart) + 2;
  return src.slice(gStart, bEnd);
}

const API_TS = fs.readFileSync(
  path.resolve(__dirname, "..", "src", "lib", "api.ts"), "utf-8");

const helpers = extractHelpers(API_TS)
  // Strip the TypeScript type annotations for eval.
  .replace(/:\s*string\s*(?=[),])/g, "")
  .replace(/\)\s*:\s*string\s*\{/g, ") {")
  // Remove the `export` keywords for plain eval.
  .replace(/^export\s+function/gm, "function");

function makeEnv({ platformOS, base }) {
  const Platform = { OS: platformOS };
  let BASE_URL = base;
  // Rebuild getBackendUrl + buildApiUrl in a fresh closure.
  const src = helpers;
  // eslint-disable-next-line no-new-func
  return new Function(
    "Platform", "BASE_URL",
    `${src}\nreturn { getBackendUrl, buildApiUrl };`
  )(Platform, BASE_URL);
}

let passed = 0, failed = 0;
function T(name, fn) {
  try { fn(); passed++; console.log("  PASS  " + name); }
  catch (e) { failed++; console.log("  FAIL  " + name + "\n        " + e.message); }
}
function eq(a, b) { if (a !== b) throw new Error(`expected ${JSON.stringify(b)}, got ${JSON.stringify(a)}`); }
function throws(fn, rx) {
  try { fn(); throw new Error("did not throw"); }
  catch (e) {
    if (!rx.test(e.message)) throw new Error("threw wrong msg: " + e.message);
  }
}

console.log("\n== Web same-origin (BASE_URL = '') ==");
const web = makeEnv({ platformOS: "web", base: "" });
T("web /today builds /api/today", () => eq(web.buildApiUrl("/today"), "/api/today"));
T("web /api/today collapses no double", () => eq(web.buildApiUrl("/api/today"), "/api/today"));
T("web /picks/history builds /api/picks/history", () =>
    eq(web.buildApiUrl("/picks/history"), "/api/picks/history"));
T("web '' builds /api/", () => eq(web.buildApiUrl(""), "/api/"));

console.log("\n== Configured (preview/dev/native w/ EXPO_PUBLIC_BACKEND_URL) ==");
const prev = makeEnv({ platformOS: "ios", base: "https://preview.example.com" });
T("preview /today builds https://.../api/today", () =>
    eq(prev.buildApiUrl("/today"), "https://preview.example.com/api/today"));
T("preview /api/today NOT doubled", () =>
    eq(prev.buildApiUrl("/api/today"), "https://preview.example.com/api/today"));
T("preview trailing slash stripped", () => {
  const p = makeEnv({ platformOS: "ios", base: "https://preview.example.com/" });
  eq(p.buildApiUrl("/today"), "https://preview.example.com/api/today");
});
T("preview double slash in path collapsed", () =>
    eq(prev.buildApiUrl("//today"), "https://preview.example.com/api/today"));
T("preview missing leading slash added", () =>
    eq(prev.buildApiUrl("today"), "https://preview.example.com/api/today"));

console.log("\n== Native production (BASE_URL='', OS='ios') ==");
const nat = makeEnv({ platformOS: "ios", base: "" });
T("native missing BASE_URL throws loudly", () =>
    throws(() => nat.buildApiUrl("/today"),
           /Backend URL is not configured|EXPO_PUBLIC_BACKEND_URL/));
T("native android same fail-loud", () => {
  const n2 = makeEnv({ platformOS: "android", base: "" });
  throws(() => n2.buildApiUrl("/today"),
         /Backend URL is not configured|EXPO_PUBLIC_BACKEND_URL/);
});

console.log("\n──────────────────────────────────────────────");
console.log(`  ${passed} passed / ${failed} failed`);
if (failed > 0) process.exit(1);
process.exit(0);
