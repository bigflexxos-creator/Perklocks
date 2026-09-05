#!/usr/bin/env node
/**
 * MAIN 40 — SDK 57 NATIVE RESET · state-migration safety runner.
 *
 * Certifies the surgical Step 2 fixes are in place:
 *
 *   1. APP_DATA_VERSION bumped to a SDK 57 tag → forces the L3
 *      client cachebust to wipe every KNOWN_CACHE_KEY on every
 *      native device's first launch of the new bundle.
 *
 *   2. KNOWN_CACHE_KEYS now includes the ACTUAL parlay-prefs
 *      storage key ("@perkslocks/parlay-prefs-v1", slash + hyphen)
 *      — the previous mislabelled entry ("perkslocks.parlay_prefs.v1")
 *      never matched useParlayPreferences.ts so stale SDK 54 state
 *      SURVIVED every prior cachebust.  Root cause of the "Parlay
 *      does not work correctly" report on native.
 *
 *   3. useFilters STORAGE_KEY bumped v6 → v7.  Every SDK 57 client
 *      reads null on first launch → the ALL tab starts with the
 *      empty categorical arrays (sports/leagues/markets/gameIds/
 *      events/searchText) so stale SDK 54 narrowing state cannot
 *      silently contaminate the ALL slate.
 *
 *   4. useParlayPreferences bumped STORAGE_KEY v1 → v2 with a
 *      one-time versioned migration that salvages only durable
 *      preferences (preferredBook, filters.minLock) and resets
 *      every transient scope field (mode, sport, sportMode,
 *      windowHours, includedSports, excludedSports, legs, lineType,
 *      advancedSub) to DEFAULT_PARLAY_PREFS.
 *
 * MAIN 39 Slice 2/3 invariants must ALL still hold.
 */
"use strict";
const fs   = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const CACHEBUST = fs.readFileSync(path.join(ROOT, "src", "lib", "cachebust.ts"), "utf-8");
const FILTERS   = fs.readFileSync(path.join(ROOT, "src", "stores", "useFilters.tsx"), "utf-8");
const PARLAYP   = fs.readFileSync(path.join(ROOT, "src", "lib", "useParlayPreferences.ts"), "utf-8");
const API_TS    = fs.readFileSync(path.join(ROOT, "src", "lib", "api.ts"), "utf-8");
const FOCUS_TS  = fs.readFileSync(path.join(ROOT, "src", "lib", "useFocusRefetch.ts"), "utf-8");
const CARD_TSX  = fs.readFileSync(path.join(ROOT, "src", "components", "LockPickCard.tsx"), "utf-8");
const ENV       = fs.readFileSync(path.join(ROOT, ".env"), "utf-8");

let passed = 0, failed = 0;
const failures = [];
function T(name, fn) {
  try { fn(); passed++; console.log("  PASS  " + name); }
  catch (e) { failed++; failures.push({ name, msg: e.message });
              console.log("  FAIL  " + name + "\n        " + e.message); }
}
function must(cond, msg) { if (!cond) throw new Error(msg); }

// ── Step 1 — backend origin parity between native & web/preview ─────
console.log("\n== Step 1 · Backend origin parity ==");
T("EXPO_PUBLIC_BACKEND_URL set and non-empty", () => {
  must(/^EXPO_PUBLIC_BACKEND_URL=\S+/m.test(ENV),
       "EXPO_PUBLIC_BACKEND_URL must be defined in frontend/.env");
});
T("EXPO_PUBLIC_BACKEND_URL and EXPO_PACKAGER_HOSTNAME point to the same origin", () => {
  const pub  = (ENV.match(/EXPO_PUBLIC_BACKEND_URL=(\S+)/) || [,""])[1];
  const pack = (ENV.match(/EXPO_PACKAGER_HOSTNAME=(\S+)/)   || [,""])[1];
  // Native uses EXPO_PUBLIC_BACKEND_URL; web/preview inherits from the
  // packager hostname.  If they disagree, native traffic goes to a
  // different backend than web — the classic "works on web, not on
  // Expo Go" cause.  We assert parity here.
  must(pub === pack,
    `origin mismatch: EXPO_PUBLIC_BACKEND_URL='${pub}' vs EXPO_PACKAGER_HOSTNAME='${pack}'`);
});

// ── Step 2 — cachebust wipes the RIGHT keys ─────────────────────────
console.log("\n== Step 2a · Cachebust key manifest ==");
T("APP_DATA_VERSION tagged SDK 57 (forces one-time L3 wipe)", () => {
  must(/APP_DATA_VERSION\s*=\s*["'](?:20\d{6}-)?sdk57-/.test(CACHEBUST),
       "APP_DATA_VERSION should carry an 'sdk57-' tag to trigger the L3 wipe");
});
T("KNOWN_CACHE_KEYS contains the ACTUAL parlay-prefs-v1 storage key", () => {
  // The bug: previous entry was 'perkslocks.parlay_prefs.v1' (dot
  // notation) which never matched useParlayPreferences.ts's real
  // key '@perkslocks/parlay-prefs-v1' (slash + hyphen).  Both must
  // now be listed — the correct one is what actually wipes SDK 54
  // state; the legacy mislabelled entry stays for defensive purity.
  must(/["']@perkslocks\/parlay-prefs-v1["']/.test(CACHEBUST),
       "cache-bust must list the REAL @perkslocks/parlay-prefs-v1 slot");
  must(/["']@perkslocks\/parlay-prefs-v2["']/.test(CACHEBUST),
       "cache-bust must list the new @perkslocks/parlay-prefs-v2 slot");
});
T("KNOWN_CACHE_KEYS contains perkslocks_filters_v7 (SDK 57 filter key)", () => {
  must(/["']perkslocks_filters_v7["']/.test(CACHEBUST),
       "cache-bust must list the new perkslocks_filters_v7 slot");
});
T("KNOWN_CACHE_KEYS retains v3-v6 filter keys (legacy defensive)", () => {
  for (const k of ["perkslocks_filters_v3", "perkslocks_filters_v4",
                    "perkslocks_filters_v5", "perkslocks_filters_v6"]) {
    must(new RegExp(`["']${k}["']`).test(CACHEBUST),
         `legacy key ${k} must still be listed for full historical wipe`);
  }
});

// ── Step 2b — useFilters STORAGE_KEY bump ───────────────────────────
console.log("\n== Step 2b · Locks filter store bumped to v7 ==");
T("STORAGE_KEY is perkslocks_filters_v7", () => {
  must(/const\s+STORAGE_KEY\s*=\s*["']perkslocks_filters_v7["']/.test(FILTERS),
       "useFilters.STORAGE_KEY must be 'perkslocks_filters_v7'");
});
T("HYDRATE reducer still drops categorical narrowing on cold launch", () => {
  // Preserves the existing ALL-safety net: on every cold hydrate we
  // strip sports/leagues/markets/gameIds/events/searchText so a stale
  // sport-bound snapshot cannot silently narrow the slate to zero.
  // We look for the destructuring block (variables may be aliased),
  // not the raw field names in order.
  const rx = /case\s+["']HYDRATE["'][\s\S]{0,2000}?const\s*\{[\s\S]*?sports:[\s\S]*?leagues:[\s\S]*?markets:[\s\S]*?gameIds:[\s\S]*?events:[\s\S]*?searchText:[\s\S]*?\.\.\.persistScalars\s*\}/;
  must(rx.test(FILTERS),
       "HYDRATE must still drop sports/leagues/markets/gameIds/events/searchText");
});

// ── Step 2c — useParlayPreferences bump + one-time migration ────────
console.log("\n== Step 2c · Parlay prefs bumped v1 → v2 with migration ==");
T("STORAGE_KEY is @perkslocks/parlay-prefs-v2", () => {
  must(/const\s+STORAGE_KEY\s*=\s*["']@perkslocks\/parlay-prefs-v2["']/.test(PARLAYP),
       "useParlayPreferences.STORAGE_KEY must be @perkslocks/parlay-prefs-v2");
});
T("LEGACY_STORAGE_KEY_V1 constant declared (points to old key)", () => {
  must(/const\s+LEGACY_STORAGE_KEY_V1\s*=\s*["']@perkslocks\/parlay-prefs-v1["']/.test(PARLAYP),
       "must declare LEGACY_STORAGE_KEY_V1 = '@perkslocks/parlay-prefs-v1'");
});
T("Hydration attempts LEGACY read only when v2 is empty", () => {
  // Guarded read: `if (raw) { ... } else { legacy migration }`.
  const rx = /AsyncStorage\.getItem\(STORAGE_KEY\)[\s\S]*?if\s*\(raw\)[\s\S]*?else\s*\{[\s\S]*?LEGACY_STORAGE_KEY_V1/;
  must(rx.test(PARLAYP), "legacy read must be inside the else branch");
});
T("Migration salvages preferredBook (durable pref)", () => {
  must(/salvaged\.preferredBook\s*=\s*legacy\.preferredBook/.test(PARLAYP),
       "must salvage preferredBook from v1 → v2");
});
T("Migration salvages filters.minLock (durable pref)", () => {
  must(/salvaged\.filters\s*=\s*\{\s*minLock:\s*\(legacy\.filters as any\)\.minLock\s*\}/.test(PARLAYP),
       "must salvage filters.minLock from v1 → v2");
});
T("Migration seeds v2 to skip the branch on next launch", () => {
  must(/AsyncStorage\.setItem\(STORAGE_KEY,\s*JSON\.stringify\(migrated\)\)/.test(PARLAYP),
       "must persist the migrated payload to STORAGE_KEY (v2)");
});
T("Migration does NOT continuously re-migrate on every launch", () => {
  // Migration must sit inside the `else { … }` (raw is null) branch,
  // not on every hydrate.  If we ran the legacy read every time we'd
  // stomp v2 with v1 on every mount.  Simple structural check:
  // there is exactly one AsyncStorage.getItem(LEGACY_STORAGE_KEY_V1)
  // in the file.
  const hits = (PARLAYP.match(/AsyncStorage\.getItem\(LEGACY_STORAGE_KEY_V1\)/g) || []).length;
  must(hits === 1, `expected exactly 1 legacy read, got ${hits}`);
});

// ── Regression: MAIN 39 Slice 2/3 invariants must still hold ────────
console.log("\n== Regression · MAIN 39 invariants ==");
T("Slice 2 · P0.4 non-retryable 4xx short-circuit preserved", () => {
  must(/if \(err && err\.nonRetryable === true\)/.test(API_TS),
       "non-retryable 4xx guard removed");
});
T("Slice 2 · P0.6 focus-refetch success/failure preserved", () => {
  must(/if \(result === false\)\s*\{\s*lastFetchRef\.current = 0/.test(FOCUS_TS),
       "false-return reset removed from useFocusRefetch");
  must(/inFlightRef/.test(FOCUS_TS), "inFlight guard removed");
});
T("Slice 2 · P0.7 Lab centralization preserved", () => {
  must(/labCorrelationsV2:/.test(API_TS),
       "api.labCorrelationsV2 wrapper missing");
});
T("Slice 3 · arePropsEqual pick_rationale identity gate still removed", () => {
  must(!/if\s*\(\s*\(a as any\)\.pick_rationale\s*!==\s*\(b as any\)\.pick_rationale\s*\)\s*return\s+false\s*;/
        .test(CARD_TSX),
       "pick_rationale identity gate reintroduced");
});

// ── Final ───────────────────────────────────────────────────────────
console.log("\n──────────────────────────────────────────────");
console.log(`  ${passed} passed / ${failed} failed`);
if (failed > 0) {
  console.log("\nFailures:");
  failures.forEach(f => console.log(`  • ${f.name}\n    ${f.msg}`));
  process.exit(1);
}
process.exit(0);
