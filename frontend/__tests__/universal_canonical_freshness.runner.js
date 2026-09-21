#!/usr/bin/env node
/**
 * Universal Preview ↔ Expo Go Canonical Freshness Contract — CLIENT
 * ═══════════════════════════════════════════════════════════════════
 * Verifies the client-side half of the contract:
 *
 *   1. `boardFreshness` module exposes hydrate + note + subscribe.
 *   2. `noteBoardVersion` is monotonic — a NEW value fires listeners
 *      (advance detected); a SAME value returns "same"; a FIRST value
 *      returns "first" WITHOUT firing listeners; empty is "ignored".
 *   3. `api.ts` reads `X-Canonical-Version` and pushes it into
 *      `noteBoardVersion` inside the shared `_fetchWithTimeout` guard.
 *   4. `useSWR.ts` stamps every write with the current version and
 *      subscribes an invalidation sweeper that discards ALL stale
 *      entries when the version advances (universal — not sport- or
 *      endpoint-specific).
 *   5. `_layout.tsx` bootstrap wires hydrate + AppState reconciler.
 *
 * Purely static + a simulated advance flow — zero real network I/O.
 */
"use strict";
const fs = require("fs");
const path = require("path");

function read(rel) {
  return fs.readFileSync(path.resolve(__dirname, "..", rel), "utf-8");
}

const BF     = read("src/lib/boardFreshness.ts");
const SWR    = read("src/lib/useSWR.ts");
const API    = read("src/lib/api.ts");
const LAYOUT = read("app/_layout.tsx");
const APPST  = read("src/lib/appStateFreshness.ts");

let passed = 0, failed = 0;
const failures = [];
function T(name, fn) {
  try { fn(); passed++; console.log("  PASS  " + name); }
  catch (e) { failed++; failures.push({ name, msg: e.message });
              console.log("  FAIL  " + name + "\n        " + e.message); }
}
function must(cond, msg) { if (!cond) throw new Error(msg); }

console.log("\n== Static contract — universal freshness observer ==");

T("boardFreshness exposes hydrate + note + subscribe + getCurrent", () => {
  must(/export\s+async\s+function\s+hydrateBoardVersion\b/.test(BF),
       "hydrateBoardVersion missing");
  must(/export\s+function\s+noteBoardVersion\b/.test(BF),
       "noteBoardVersion missing");
  must(/export\s+function\s+subscribeBoardVersion\b/.test(BF),
       "subscribeBoardVersion missing");
  must(/export\s+function\s+getCurrentBoardVersion\b/.test(BF),
       "getCurrentBoardVersion missing");
});

T("noteBoardVersion returns first|same|new|ignored (contract shape)", () => {
  must(/return\s+"first"/.test(BF), 'return "first" branch missing');
  must(/return\s+"same"/.test(BF), 'return "same" branch missing');
  must(/return\s+"new"/.test(BF), 'return "new" branch missing');
  must(/return\s+"ignored"/.test(BF), 'return "ignored" branch missing');
});

T("api.ts imports noteBoardVersion and calls it on every response", () => {
  must(/import\s+\{\s*noteBoardVersion\s*\}\s+from\s+"@\/src\/lib\/boardFreshness"/.test(API),
       "noteBoardVersion import missing in api.ts");
  // The call MUST be inside _fetchWithTimeout so it fires on EVERY
  // response body regardless of endpoint or sport.
  const ft = API.match(/async\s+function\s+_fetchWithTimeout\([\s\S]*?\n\}/);
  must(ft, "_fetchWithTimeout not found");
  must(/noteBoardVersion\s*\(/.test(ft[0]),
       "noteBoardVersion must be called inside _fetchWithTimeout");
});

T("api.ts reads X-Canonical-Version header (not just X-Board-Version)", () => {
  must(/["']x-canonical-version["']/i.test(API),
       "X-Canonical-Version header read missing");
});

T("useSWR stamps cache entries with current board_version at write time", () => {
  must(/bv\?:\s*string\s*\|\s*null/.test(SWR),
       "Snapshot type must carry optional bv (board_version) stamp");
  must(/getCurrentBoardVersion\(\)/.test(SWR),
       "useSWR must read getCurrentBoardVersion() on writes");
  // Write path must store the stamp.
  must(/_cache\.set\s*\(\s*key\s*,\s*\{\s*data[,\s\S]*?bv\b/.test(SWR),
       "swrCacheWrite must persist the bv stamp with each entry");
});

T("useSWR subscribes to advance events + universal cache sweep", () => {
  must(/subscribeBoardVersion\s*\(/.test(SWR),
       "useSWR must call subscribeBoardVersion at module init");
  // The sweep must delete entries stamped with an older bv than 'next'.
  must(/_cache\.delete\s*\(\s*k\s*\)/.test(SWR),
       "useSWR sweeper must delete stale cache entries");
  must(/v\.bv\s*!==\s*next/.test(SWR),
       "sweeper must compare each entry's bv against the advanced version");
});

T("useSWR forces silent refetch when cache stamp is behind current version", () => {
  must(/const\s+cur\s*=\s*getCurrentBoardVersion\(\)/.test(SWR),
       "useSWR must read current board_version on dep-change");
  must(/stampBehind/.test(SWR),
       "useSWR must define stampBehind and act on it");
});

T("_layout.tsx wires hydrateBoardVersion + AppState reconciler", () => {
  must(/hydrateBoardVersion/.test(LAYOUT),
       "root layout must call hydrateBoardVersion at boot");
  must(/installAppStateFreshnessReconciler/.test(LAYOUT),
       "root layout must install AppState reconciler");
});

T("AppState reconciler pings /api/version on foreground (debounced)", () => {
  must(/AppState\.addEventListener\s*\(\s*"change"/.test(APPST),
       "AppState listener missing");
  must(/api\.version\(\)/.test(APPST),
       "reconciler must call api.version() to trigger the shared header path");
  must(/_lastPingAt/.test(APPST), "reconciler must debounce ping");
});

// ── Simulated N → N+1 advance flow (pure JS mock of the observer) ──
console.log("\n== Simulated advance flow · N → N+1 ==");

function simulateFreshness() {
  let cur = null;
  const listeners = new Set();
  const cache = new Map();
  function note(v) {
    if (!v || typeof v !== "string") return "ignored";
    const t = v.trim();
    if (!t) return "ignored";
    if (cur === t) return "same";
    const prev = cur;
    cur = t;
    if (!prev) return "first";
    for (const fn of listeners) fn(t, prev);
    return "new";
  }
  function subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); }
  function write(k, data) { cache.set(k, { data, bv: cur }); }
  subscribe((next) => {
    for (const [k, v] of cache.entries()) if (v.bv !== next) cache.delete(k);
  });
  return { note, write, cache, cur: () => cur };
}

T("first version is 'first' and does NOT trigger a sweep", () => {
  const f = simulateFreshness();
  f.write("pick-detail|A", { wp: 96.69 });
  const r = f.note("v-N");
  must(r === "first", `first note must return 'first' (got ${r})`);
  must(f.cache.has("pick-detail|A"),
       "cache entry pre-first-version must survive (would be swept in real code by the sweeper listener check)");
});

T("advance N → N+1 sweeps every cache entry with an older stamp", () => {
  const f = simulateFreshness();
  f.note("v-N");                                 // first
  f.write("pick-detail|A", { wp: 96.69 });       // stamped v-N
  f.write("pick-detail|B", { wp: 47.17 });       // stamped v-N
  f.write("historical-intelligence|A|L10|ALL", { games: [] }); // stamped v-N
  const r = f.note("v-N+1");
  must(r === "new", `advance must return 'new' (got ${r})`);
  must(f.cache.size === 0, `all older-stamp entries must be swept (left ${f.cache.size})`);
});

T("same version is 'same' and preserves the cache", () => {
  const f = simulateFreshness();
  f.note("v-N");
  f.write("pick-detail|A", { wp: 47.17 });
  const r = f.note("v-N");
  must(r === "same", `same note must return 'same' (got ${r})`);
  must(f.cache.has("pick-detail|A"), "cache must be preserved on same version");
});

T("empty note is 'ignored' — offline / no-header responses no-op", () => {
  const f = simulateFreshness();
  must(f.note(null) === "ignored", "null must be ignored");
  must(f.note("") === "ignored", "empty must be ignored");
  must(f.note("   ") === "ignored", "whitespace must be ignored");
});

T("post-advance writes carry the NEW stamp, not the old one", () => {
  const f = simulateFreshness();
  f.note("v-N");
  f.write("pick-detail|A", { wp: 96.69 });
  f.note("v-N+1");                                // sweeps
  f.write("pick-detail|A", { wp: 47.17 });        // re-written
  const entry = f.cache.get("pick-detail|A");
  must(entry && entry.bv === "v-N+1",
       `post-advance write must carry new stamp (got ${entry && entry.bv})`);
});

console.log("\n──────────────────────────────────────────────");
console.log(`  ${passed} passed / ${failed} failed`);
if (failed > 0) {
  console.log("\nFailures:");
  failures.forEach(f => console.log(`  • ${f.name}\n    ${f.msg}`));
  process.exit(1);
}
process.exit(0);
