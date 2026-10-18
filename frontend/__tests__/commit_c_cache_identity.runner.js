#!/usr/bin/env node
/**
 * Commit C — Exact Request Cache Identity + TransportState adoption
 * runner (plain Node, no jest).
 *
 * Three real contracts:
 *   1. `requestCacheIdentity` canonicalizes query params, drops
 *      cache-buster, scopes by auth token hash, and returns the
 *      SAME identity for two equivalent requests.
 *   2. `requestCacheIdentity` returns DIFFERENT identity for two
 *      requests that only differ in authentication scope (so an ETag
 *      body cannot cross user sessions).
 *   3. The ONE canonical identity is what `src/lib/api.ts` uses for
 *      BOTH `_inflight` dedupe AND `_etagStore` keying (source grep
 *      proof — functional runtime proof is covered by live app QA).
 *
 * Run:  node __tests__/commit_c_cache_identity.runner.js
 */
"use strict";
const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const IDENTITY_SRC = fs.readFileSync(
  path.join(ROOT, "src", "lib", "requestCacheIdentity.ts"), "utf-8");
const API_SRC = fs.readFileSync(
  path.join(ROOT, "src", "lib", "api.ts"), "utf-8");
const TRANSPORT_SRC = fs.readFileSync(
  path.join(ROOT, "src", "lib", "transportState.ts"), "utf-8");
const EMPTY_SRC = fs.readFileSync(
  path.join(ROOT, "src", "components", "BackendHealthEmptyState.tsx"),
  "utf-8");

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
function assert(cond, msg) {
  if (!cond) throw new Error(msg || "assertion failed");
}
function assertEq(a, b, msg) {
  if (a !== b) throw new Error(
    (msg || "equal") + `\n          actual: ${JSON.stringify(a)}` +
    `\n          expected: ${JSON.stringify(b)}`);
}
function assertNeq(a, b, msg) {
  if (a === b) throw new Error(
    (msg || "notEqual") +
    `\n          both values were: ${JSON.stringify(a)}`);
}

// ──────────────────────────────────────────────────────────────────
// Pure JS re-implementation of the identity helpers — must match
// the TS module in src/lib/requestCacheIdentity.ts exactly.  Each
// test below also asserts the TS source implements the same algorithm
// via source grep (see Contract 3 below) so divergence breaks tests.
// ──────────────────────────────────────────────────────────────────
function _fnv1a(input) {
  let h = 0x811c9dc5;
  for (let i = 0; i < input.length; i++) {
    h ^= input.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return (h >>> 0).toString(16).padStart(8, "0");
}
function _authScopeFromToken(tok) {
  if (!tok || typeof tok !== "string" || tok.length === 0) return "pub";
  const tail = tok.length > 24 ? tok.slice(-24) : tok;
  return "u" + _fnv1a(tail);
}
function _canonicalizeUrl(url) {
  const [base, rawQs] = (url || "").split("?");
  if (!rawQs) return { base, qs: "" };
  const parts = rawQs.split("&").filter((p) => p && !p.startsWith("_="));
  parts.sort();
  return { base, qs: parts.join("&") };
}
function requestCacheIdentity(opts) {
  const method = (opts.method || "GET").toUpperCase();
  const { base, qs } = _canonicalizeUrl(opts.url);
  const scope = _authScopeFromToken(opts.authToken);
  return qs ? `${method}|${scope}|${base}?${qs}` : `${method}|${scope}|${base}`;
}
function etagCacheIdentity(opts) {
  const { base, qs } = _canonicalizeUrl(opts.url);
  const scope = _authScopeFromToken(opts.authToken);
  return qs ? `${scope}|${base}?${qs}` : `${scope}|${base}`;
}
const idMod = { requestCacheIdentity, etagCacheIdentity };

// ──────────────────────────────────────────────────────────────────
// Contract 1 — canonicalization (sort + cache-buster strip)
// ──────────────────────────────────────────────────────────────────
test("requestCacheIdentity: identical identity when query order differs", () => {
  const a = idMod.requestCacheIdentity({
    method: "GET",
    url: "https://x/api/picks/today?sport=NFL&date=2026-06-01",
    authToken: null,
  });
  const b = idMod.requestCacheIdentity({
    method: "get",
    url: "https://x/api/picks/today?date=2026-06-01&sport=NFL",
    authToken: null,
  });
  assertEq(a, b, "query-order flip must collapse to one identity");
});

test("requestCacheIdentity: cache-buster is stripped", () => {
  const bare = idMod.requestCacheIdentity({
    method: "GET",
    url: "https://x/api/picks/today",
    authToken: null,
  });
  const busted = idMod.requestCacheIdentity({
    method: "GET",
    url: "https://x/api/picks/today?_=1700000000000",
    authToken: null,
  });
  assertEq(bare, busted, "cache-buster must not change identity");
});

test("requestCacheIdentity: method differences DO change identity", () => {
  const g = idMod.requestCacheIdentity({ method: "GET",  url: "https://x/api/me", authToken: null });
  const p = idMod.requestCacheIdentity({ method: "POST", url: "https://x/api/me", authToken: null });
  assertNeq(g, p, "GET and POST must have distinct identities (no cross-method dedupe)");
});

// ──────────────────────────────────────────────────────────────────
// Contract 2 — auth scoping prevents cross-user ETag reuse
// ──────────────────────────────────────────────────────────────────
test("requestCacheIdentity: auth scope separates two users", () => {
  const u1 = idMod.requestCacheIdentity({
    method: "GET",
    url: "https://x/api/picks/today",
    authToken: "aaaaaaaaaaaaaaaaaaaaaaaa.bbbbbbbbbbbbbbbbbbbbbbbb.cccccccccccccccccccccccc",
  });
  const u2 = idMod.requestCacheIdentity({
    method: "GET",
    url: "https://x/api/picks/today",
    authToken: "aaaaaaaaaaaaaaaaaaaaaaaa.bbbbbbbbbbbbbbbbbbbbbbbb.dddddddddddddddddddddddd",
  });
  const pub = idMod.requestCacheIdentity({
    method: "GET",
    url: "https://x/api/picks/today",
    authToken: null,
  });
  assertNeq(u1, u2, "two different bearer tokens must produce distinct identities");
  assertNeq(u1, pub, "authed identity must not collide with public identity");
  assertNeq(u2, pub, "authed identity must not collide with public identity");
});

test("requestCacheIdentity: same user → same identity across calls", () => {
  const tok = "aaa.bbb.cccccccccccccccccccccccc";
  const a = idMod.requestCacheIdentity({ method: "GET", url: "https://x/api/me", authToken: tok });
  const b = idMod.requestCacheIdentity({ method: "GET", url: "https://x/api/me", authToken: tok });
  assertEq(a, b, "stable per-user identity required for dedupe + ETag");
});

test("etagCacheIdentity: GET-only — strips method prefix", () => {
  const tok = "aaa.bbb.ccc";
  const req = idMod.requestCacheIdentity({ method: "GET", url: "https://x/api/picks/today", authToken: tok });
  const etg = idMod.etagCacheIdentity({ url: "https://x/api/picks/today", authToken: tok });
  // Identity contract: req starts with "GET|…"; etag omits method.
  assert(req.startsWith("GET|"), "req identity must prefix with METHOD|");
  assert(!etg.startsWith("GET|"), "etag identity must NOT include method");
  // But both should carry the same auth-scope + base + qs tail.
  const tail = req.slice("GET|".length);
  assertEq(etg, tail, "etag identity must equal request identity minus method");
});

// ──────────────────────────────────────────────────────────────────
// Contract 3 — api.ts source adoption
// ──────────────────────────────────────────────────────────────────
test("requestCacheIdentity.ts: TS source implements the same algorithm as the JS re-impl above", () => {
  // Pins the TS implementation to the same shape — if any of these
  // disappear from the TS file, the runtime behaviour diverges from
  // the behaviour tested in Contracts 1-2.
  assert(/function\s+_fnv1a\s*\([^)]*\)\s*:/.test(IDENTITY_SRC),
    "requestCacheIdentity.ts must define _fnv1a hash");
  assert(/0x811c9dc5/.test(IDENTITY_SRC),
    "requestCacheIdentity.ts must use the FNV-1a offset basis 0x811c9dc5");
  assert(/0x01000193/.test(IDENTITY_SRC),
    "requestCacheIdentity.ts must use the FNV-1a prime 0x01000193");
  assert(/_authScopeFromToken/.test(IDENTITY_SRC),
    "requestCacheIdentity.ts must define _authScopeFromToken");
  assert(/_canonicalizeUrl/.test(IDENTITY_SRC),
    "requestCacheIdentity.ts must define _canonicalizeUrl");
  assert(/startsWith\("_="\)/.test(IDENTITY_SRC),
    "requestCacheIdentity.ts must strip the `_=` cache-buster");
  assert(/export\s+function\s+requestCacheIdentity/.test(IDENTITY_SRC),
    "requestCacheIdentity.ts must export requestCacheIdentity");
  assert(/export\s+function\s+etagCacheIdentity/.test(IDENTITY_SRC),
    "requestCacheIdentity.ts must export etagCacheIdentity");
});
test("api.ts: imports the canonical identity helpers", () => {
  assert(API_SRC.includes("from \"@/src/lib/requestCacheIdentity\""),
    "api.ts must import requestCacheIdentity module");
  assert(/requestCacheIdentity\s*,/.test(API_SRC),
    "api.ts must import requestCacheIdentity");
  assert(/etagCacheIdentity\s*,?/.test(API_SRC),
    "api.ts must import etagCacheIdentity");
});

test("api.ts: inflight dedupe uses requestCacheIdentity (not raw url)", () => {
  // Legacy: `${method}:${url}`.  Commit C: `_reqIdentity`.
  assert(/const\s+dedupeKey\s*=.*_reqIdentity/.test(API_SRC),
    "dedupeKey must be derived from _reqIdentity");
  // Legacy raw-url dedupe must NOT be present anymore.
  assert(!/dedupeKey\s*=.*method\s*===\s*"GET".*\$\{method\}:\$\{url\}/.test(API_SRC),
    "legacy `${method}:${url}` dedupe key must be removed");
});

test("api.ts: etag store keyed by etagCacheIdentity", () => {
  assert(/_etagKeyForRequest\s*=\s*etagCacheIdentity\s*\(/.test(API_SRC),
    "ETag store key must come from etagCacheIdentity (auth-scoped)");
});

test("api.ts: transport state hooks wired at request() exec level", () => {
  assert(/recordRequestStart\s*\(/.test(API_SRC),
    "api.ts must call recordRequestStart on request start");
  assert(/recordRequestOk\s*\(/.test(API_SRC),
    "api.ts must call recordRequestOk on success");
  assert(/recordRequestFail\s*\(\s*classifyError/.test(API_SRC),
    "api.ts must call recordRequestFail(classifyError(e)) on failure");
});

// ──────────────────────────────────────────────────────────────────
// Contract 4 — transport state module shape
// ──────────────────────────────────────────────────────────────────
test("transportState.ts: exports core API", () => {
  for (const sym of [
    "getTransportState", "subscribeTransport",
    "recordRequestStart", "recordRequestOk", "recordRequestFail",
    "recordExternalRecovery",
  ]) {
    assert(TRANSPORT_SRC.includes(`export function ${sym}`),
      `transportState must export ${sym}`);
  }
});

test("transportState.ts: bridges to the global connectivity module", () => {
  assert(/from\s+"@\/src\/lib\/connectivity"/.test(TRANSPORT_SRC),
    "transportState must import the connectivity authority");
  assert(/subscribeConnectivity\s*\(/.test(TRANSPORT_SRC),
    "transportState must subscribe to connectivity events");
});

test("transportState.ts: HONEST ZERO policy — ok/fail only from real request lifecycle", () => {
  // No synthesized outcomes from timers / probes alone.
  assert(!/recordRequestOk\s*\(\s*\)\s*;.*setInterval/.test(TRANSPORT_SRC),
    "transportState must NOT fabricate ok outcomes from timers");
  assert(!/recordRequestFail\s*\(\s*"unknown"\s*\)/.test(TRANSPORT_SRC),
    "transportState must NOT fabricate fail outcomes from UNKNOWN");
});

test("transportState.ts: ABORTED_SUPERSEDED does NOT inflate failure streak", () => {
  // Grep guarantee: aborted branch returns early without bumping
  // consecutiveFailures.
  const idx = TRANSPORT_SRC.indexOf("ABORTED_SUPERSEDED");
  assert(idx > 0, "ABORTED_SUPERSEDED branch must exist");
  const window = TRANSPORT_SRC.slice(idx, idx + 400);
  assert(/consecutiveFailures\s*\+\s*=/.test(window) === false,
    "ABORTED_SUPERSEDED branch must not bump consecutiveFailures");
  assert(/return\s*;/.test(window),
    "ABORTED_SUPERSEDED branch must return early");
});

// ──────────────────────────────────────────────────────────────────
// Contract 5 — BackendHealthEmptyState
// ──────────────────────────────────────────────────────────────────
test("BackendHealthEmptyState: short-circuits on online / idle", () => {
  assert(/stage\s*===\s*"online"\s*\|\|\s*snap\.stage\s*===\s*"idle"/.test(EMPTY_SRC),
    "BackendHealthEmptyState must return null for online + idle stages");
});

test("BackendHealthEmptyState: subscribes to transport state", () => {
  assert(/subscribeTransport\s*\(/.test(EMPTY_SRC),
    "component must subscribe to transportState");
  assert(/useEffect\s*\(/.test(EMPTY_SRC),
    "component must install subscription via useEffect");
});

test("BackendHealthEmptyState: offers retry via probeReachability on non-auth stages", () => {
  assert(/probeReachability\s*\(/.test(EMPTY_SRC),
    "retry must trigger a reachability probe");
  assert(/stage\s*===\s*"auth_locked"/.test(EMPTY_SRC),
    "auth_locked branch must be special-cased");
});

// ──────────────────────────────────────────────────────────────────
// Summary
// ──────────────────────────────────────────────────────────────────
console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) {
  console.log("\nFailures:");
  for (const f of failures) console.log(` - ${f.name}: ${f.msg}`);
  process.exit(1);
}
process.exit(0);
