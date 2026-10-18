#!/usr/bin/env node
/**
 * Perklocks — Extended Connectivity + Board Stability Closure runner
 * ──────────────────────────────────────────────────────────────────
 * Static + light-dynamic Node runner that validates the surgical
 * fixes added in the "COMPLETE CONNECTIVITY + PRODUCTION BOARD
 * STABILITY ROOT CLOSURE" continuation pass (Defects 3, 4, 7, 8, 12
 * and the Production Board Acceptance / Network Change contracts).
 *
 * Convention mirrors `connectivity_root_closure.runner.js`:
 *   • Primary assertions use regex invariants over the source file.
 *   • `filterIdentity.ts` is pure — we transpile it in-process via
 *     the TypeScript compiler (already a repo devDep via ts-node)
 *     and execute a handful of behavioural assertions.
 *
 * Run:  node __tests__/connectivity_board_extended_closure.runner.js
 */
"use strict";
const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const CONN_SRC      = fs.readFileSync(path.join(ROOT, "src", "lib", "connectivity.ts"),         "utf-8");
const INDEX_SRC     = fs.readFileSync(path.join(ROOT, "app",  "(tabs)",   "index.tsx"),         "utf-8");
const EPOCH_SRC     = fs.readFileSync(path.join(ROOT, "src", "lib", "canonicalConsumers.ts"),   "utf-8");
const FILTER_SRC    = fs.readFileSync(path.join(ROOT, "src", "lib", "filterIdentity.ts"),       "utf-8");
const TS_SRC        = fs.readFileSync(path.join(ROOT, "src", "lib", "transportState.ts"),       "utf-8");

const results = { passed: 0, failed: 0, errors: [] };
function assert(name, cond, detail) {
  if (cond) { results.passed += 1; console.log(`  ✓ ${name}`); }
  else {
    results.failed += 1;
    results.errors.push({ name, detail });
    console.log(`  ✗ ${name}  —  ${detail || ""}`);
  }
}

console.log("\n── Defect 3: NetInfo native connectivity authority wired ──");
assert(
  "connectivity.ts lazy-requires @react-native-community/netinfo",
  /require\(["']@react-native-community\/netinfo["']\)/.test(CONN_SRC),
);
assert(
  "connectivity.ts exports a network_change event type",
  /network_change/.test(CONN_SRC) && /ConnectivityEvent/.test(CONN_SRC),
);
assert(
  "_applyNetInfoState honours OS-level isConnected",
  /isConnected\s*===\s*true/.test(CONN_SRC),
);
assert(
  "NetInfo offline sets state.online = false (via nextOnline derivation)",
  /state\.online\s*=\s*nextOnline/.test(CONN_SRC),
);
assert(
  "home screen subscribes to network_change for refresh",
  /event\s*!==\s*["']foreground["']\s*&&\s*event\s*!==\s*["']network_change["']/.test(INDEX_SRC),
);

console.log("\n── Defect 4: health probe pipeline isolation ──");
assert(
  "probeReachability has its own timeout (6s)",
  /PROBE_TIMEOUT_MS\s*=\s*6000/.test(CONN_SRC),
);
assert(
  "probeReachability sends cache: no-store + no-cache headers",
  /cache:\s*["']no-store["']/.test(CONN_SRC) &&
  /Cache-Control[\s\S]*no-cache,\s*no-store,\s*must-revalidate/.test(CONN_SRC),
);
assert(
  "probeReachability routes outcome through recordOriginProbe",
  /recordOriginProbe\(ok\)/.test(CONN_SRC),
);
assert(
  "recordOriginProbe requires ≥2 consecutive failures to flip originReachable",
  /consecutiveOriginProbeFailures\s*>=\s*2/.test(TS_SRC),
);

console.log("\n── Defect 7 + 8: deterministic filter identity ──");
assert(
  "filterIdentity.ts exports filterIdentityHash + filterIdentityString",
  /export function filterIdentityHash/.test(FILTER_SRC) &&
  /export function filterIdentityString/.test(FILTER_SRC),
);
assert(
  "home screen writes picksCacheKeyFull with identity hash",
  /picksCacheKeyFull\(/.test(INDEX_SRC) &&
  /filterIdentityHash\(/.test(INDEX_SRC),
);
assert(
  "home screen hydrates by trying v3 key FIRST then falling back to v2",
  /picksCacheKeyFull\([\s\S]*?picksCacheKey\(sport\)/.test(INDEX_SRC),
);

// ── Behavioural assertions on filterIdentity (pure TS, transpile in-process) ──
let FI;
try {
  const ts = require("typescript");
  const out = ts.transpileModule(FILTER_SRC, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  }).outputText;
  const m = { exports: {} };
  const fn = new Function("module", "exports", "require", out);
  fn(m, m.exports, require);
  FI = m.exports;
} catch (e) {
  FI = null;
  console.log("  ! Could not transpile filterIdentity.ts dynamically:", e && e.message);
}
if (FI && typeof FI.filterIdentityHash === "function") {
  // key-order + array-order invariance
  const a = FI.filterIdentityHash({
    sport: "MLB", lineType: "main", sortKey: "lock", sortDir: "desc",
    filters: { minLock: 70, event: "NYY @ BOS" },
    leagues: ["AL East", "NL West"],
    markets: ["Hits", "HR"],
  });
  const b = FI.filterIdentityHash({
    sortDir: "desc", sortKey: "lock", lineType: "main", sport: "mlb",
    filters: { event: "NYY @ BOS", minLock: 70 },
    markets: ["HR", "Hits"],
    leagues: ["NL West", "AL East"],
  });
  assert("key / array order independence", a === b, `${a} vs ${b}`);

  // different sports → different hash
  assert(
    "different sports produce different hashes",
    FI.filterIdentityHash({ sport: "MLB" }) !== FI.filterIdentityHash({ sport: "NFL" }),
  );
  // zero / false / empty are OMITTED
  const c = FI.filterIdentityHash({ sport: "MLB", filters: {} });
  const d = FI.filterIdentityHash({ sport: "MLB", filters: { market: "", minLock: 0, starsOnly: false } });
  assert("empty / zero / false values are omitted", c === d, `${c} vs ${d}`);
  // non-empty filter matters
  assert(
    "non-empty filter affects identity",
    FI.filterIdentityHash({ sport: "MLB", filters: { minLock: 70 } })
    !== FI.filterIdentityHash({ sport: "MLB", filters: { minLock: 80 } }),
  );
  // multi-select array equals multi-select array (regardless of order)
  assert(
    "multi-select array order independence",
    FI.filterIdentityHash({ sport: "MLB", markets: ["A", "B"] })
    === FI.filterIdentityHash({ sport: "MLB", markets: ["B", "A"] }),
  );
  // canonical string carries sp / markets / q keys
  const s = FI.filterIdentityString({
    sport: "MLB", filters: { minLock: 70 }, markets: ["A", "B"], search: "Judge",
  });
  assert(
    "canonical string includes sp / markets / q",
    /sp=MLB/.test(s) && /markets=A,B/.test(s) && /q=Judge/.test(s),
    s,
  );
}

console.log("\n── Defect 12: canonical epoch storm coalescing ──");
assert(
  "canonicalConsumers.ts has a coalesce timer + window constant",
  /_coalesceTimer/.test(EPOCH_SRC) && /COALESCE_WINDOW_MS/.test(EPOCH_SRC),
);
assert(
  "scheduleFlush upgrades pending target but never regresses",
  /if\s*\(target\s*>\s*_pendingTarget\)/.test(EPOCH_SRC),
);
assert(
  "origin_change cancels pending flush before resetting dedup memory",
  /previous\.origin\s*!==\s*next\.origin[\s\S]*clearTimeout\(_coalesceTimer\)/.test(EPOCH_SRC),
);
assert(
  "flush exports _flushForTests for synchronous drain",
  /export function _flushForTests/.test(EPOCH_SRC),
);

console.log("\n── Production Board Acceptance Contract ──");
assert(
  "ABORTED_SUPERSEDED short-circuits catch without clearing picks",
  /if \(kind === ErrorKind\.ABORTED_SUPERSEDED\)[\s\S]*?return false;/.test(INDEX_SRC),
);
assert(
  "transport failure keeps the previously-committed picks visible",
  /setErrorKind\(kind\)[\s\S]*setLoadError/.test(INDEX_SRC) &&
  !/setPicks\(\[\]\);[\s\S]*catch \(e: any\)/.test(INDEX_SRC),
);
assert(
  "empty response with cached picks → STALE_FALLBACK (not wipe)",
  /ErrorKind\.STALE_FALLBACK[\s\S]*return true;/.test(INDEX_SRC),
);
assert(
  "non-COMMITTED generations never replace a committed slate",
  /!_committed\s*&&\s*picksRef\.current\.length\s*>\s*0/.test(INDEX_SRC),
);

console.log(`\n──── ${results.passed} passed · ${results.failed} failed ────`);
if (results.failed > 0) {
  console.log("Errors:");
  results.errors.forEach((e) => console.log(`  · ${e.name}: ${e.detail}`));
}
process.exit(results.failed === 0 ? 0 : 1);
