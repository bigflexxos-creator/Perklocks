#!/usr/bin/env node
/**
 * PERKLOCKS · MAIN 40 — Expo SDK 57 compatibility certification.
 *
 * Static-source proof that the migration from SDK 54 → 57 is complete
 * without regressing any of MAIN 39's reliability / performance work.
 *
 * Runs under plain Node (no jest), mirroring
 * `api_url_parity.runner.js` and `main39_slice2.static.runner.js`.
 */
"use strict";
const fs   = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const PKG  = JSON.parse(fs.readFileSync(path.join(ROOT, "package.json"), "utf-8"));
const APP_JSON = JSON.parse(fs.readFileSync(path.join(ROOT, "app.json"), "utf-8"));
const LAYOUT   = fs.readFileSync(path.join(ROOT, "app", "_layout.tsx"), "utf-8");
const SHARE    = fs.readFileSync(path.join(ROOT, "src", "lib", "shareBetSlip.ts"), "utf-8");
const API_TS   = fs.readFileSync(path.join(ROOT, "src", "lib", "api.ts"), "utf-8");
const FOCUS_TS = fs.readFileSync(path.join(ROOT, "src", "lib", "useFocusRefetch.ts"), "utf-8");
const CARD_TS  = fs.readFileSync(path.join(ROOT, "src", "components", "LockPickCard.tsx"), "utf-8");
const THEME_TS = fs.readFileSync(path.join(ROOT, "src", "theme.ts"), "utf-8");

let passed = 0, failed = 0;
const failures = [];
function T(name, fn) {
  try { fn(); passed++; console.log("  PASS  " + name); }
  catch (e) { failed++; failures.push({name, msg: e.message});
              console.log("  FAIL  " + name + "\n        " + e.message); }
}
function must(cond, msg) { if (!cond) throw new Error(msg); }

// ── SDK 57 dependency floor ─────────────────────────────────────────
console.log("\n== MAIN 40 · SDK 57 dependency floor ==");
const deps    = PKG.dependencies    || {};
const devDeps = PKG.devDependencies || {};

T("expo pinned to SDK 57 (~57.x.x)", () => {
  const v = deps.expo || "";
  must(/^\D*57\./.test(v), `expo version is ${v}`);
});

T("expo-router pinned to ~57.x", () => {
  must(/^\D*57\./.test(deps["expo-router"] || ""),
       `expo-router version is ${deps["expo-router"]}`);
});

// Core native modules that must all bump to the SDK 57 track.
const SDK57_NATIVES = [
  "expo-splash-screen", "expo-status-bar", "expo-font", "expo-image",
  "expo-haptics", "expo-linking", "expo-constants", "expo-blur",
  "expo-web-browser", "expo-secure-store", "expo-symbols",
  "expo-system-ui", "expo-clipboard", "expo-file-system",
  "expo-linear-gradient", "expo-media-library", "expo-sharing",
];
for (const p of SDK57_NATIVES) {
  T(`${p} pinned to ~57.x`, () => {
    const v = deps[p] || "";
    must(/^\D*57\./.test(v), `${p} version is ${v}`);
  });
}

T("react is 19.2.x (SDK 57 baseline)", () => {
  must(/^19\.2\./.test(deps.react || ""), `react is ${deps.react}`);
});
T("react-dom is 19.2.x", () => {
  must(/^19\.2\./.test(deps["react-dom"] || ""), `react-dom is ${deps["react-dom"]}`);
});
T("react-native is 0.86.x (SDK 57 baseline)", () => {
  must(/^0\.86\./.test(deps["react-native"] || ""),
       `react-native is ${deps["react-native"]}`);
});
T("react-native-reanimated is 4.5.x", () => {
  must(/^\D*4\.5\./.test(deps["react-native-reanimated"] || ""),
       `reanimated is ${deps["react-native-reanimated"]}`);
});
T("react-native-screens is 4.26.x", () => {
  must(/^\D*4\.26\./.test(deps["react-native-screens"] || ""),
       `screens is ${deps["react-native-screens"]}`);
});
T("react-native-safe-area-context is 5.7.x", () => {
  must(/^\D*5\.7\./.test(deps["react-native-safe-area-context"] || ""),
       `safe-area is ${deps["react-native-safe-area-context"]}`);
});
T("react-native-gesture-handler is 2.32.x", () => {
  must(/^\D*2\.32\./.test(deps["react-native-gesture-handler"] || ""),
       `gesture-handler is ${deps["react-native-gesture-handler"]}`);
});

// ── SDK 56 breaking change: react-navigation removed ────────────────
console.log("\n== SDK 56 breaking change — react-navigation must NOT coexist ==");
T("no @react-navigation/* in dependencies", () => {
  for (const k of Object.keys(deps)) {
    must(!k.startsWith("@react-navigation/"),
         `illegal standalone react-navigation dep: ${k}`);
  }
});
T("no @react-navigation/* in devDependencies", () => {
  for (const k of Object.keys(devDeps)) {
    must(!k.startsWith("@react-navigation/"),
         `illegal standalone react-navigation devDep: ${k}`);
  }
});
T("app/_layout.tsx imports ThemeProvider from expo-router", () => {
  must(/from\s+["']expo-router["']/.test(LAYOUT), "expo-router import missing");
  must(/import\s+\{[^}]*ThemeProvider[^}]*\}\s+from\s+["']expo-router["']/.test(LAYOUT),
       "ThemeProvider must come from expo-router, not @react-navigation/native");
});
T("app/_layout.tsx no longer imports from @react-navigation/native", () => {
  must(!/from\s+["']@react-navigation\/native["']/.test(LAYOUT),
       "@react-navigation/native import still present in _layout.tsx");
});

// ── app.json schema — SDK 57 removed newArchEnabled / edgeToEdgeEnabled ─
console.log("\n== app.json schema (Expo Doctor 21/21) ==");
T("newArchEnabled removed (SDK 57 default)", () => {
  must(!("newArchEnabled" in APP_JSON.expo),
       "newArchEnabled must not appear in app.json (SDK 57 default)");
});
T("android.edgeToEdgeEnabled removed (SDK 57 default)", () => {
  must(!(APP_JSON.expo.android && "edgeToEdgeEnabled" in APP_JSON.expo.android),
       "android.edgeToEdgeEnabled must not appear in app.json");
});
T("android.adaptiveIcon.backgroundColor is 6-char hex", () => {
  const c = APP_JSON.expo.android.adaptiveIcon.backgroundColor;
  must(/^#[0-9a-fA-F]{6}$/.test(c),
       `backgroundColor must be 6-char hex, got ${c}`);
});
T("plugins array declares expo-image / expo-secure-store / expo-sharing / expo-status-bar / expo-web-browser", () => {
  const flat = (APP_JSON.expo.plugins || []).map(p => Array.isArray(p) ? p[0] : p);
  for (const p of ["expo-image", "expo-secure-store", "expo-sharing",
                    "expo-status-bar", "expo-web-browser"]) {
    must(flat.includes(p), `plugin ${p} missing from app.json`);
  }
});

// ── expo-media-library native module rename (SDK 57) ────────────────
console.log("\n== expo-media-library native module rename ==");
T("shareBetSlip.ts lazy-loads expo-media-library on native only", () => {
  // Top-level eager `import * as MediaLibrary from "expo-media-library"`
  // must be gone (crashes on web with ExpoMediaLibraryNext not found).
  must(!/^import\s+\*\s+as\s+MediaLibrary\s+from\s+["']expo-media-library["']/m.test(SHARE),
       "top-level eager import of expo-media-library still present");
  // Lazy loader must exist.
  must(/getMediaLibrary\s*\(\)/.test(SHARE),
       "lazy getMediaLibrary() helper missing");
  must(/require\(\s*["']expo-media-library["']\s*\)/.test(SHARE),
       "runtime require of expo-media-library missing");
  must(/Platform\.OS\s*===\s*["']web["']/.test(SHARE),
       "web guard missing on lazy loader");
});

// ── MAIN 39 slice regressions must be intact ────────────────────────
console.log("\n== MAIN 39 · Slice 1-3 regressions still intact ==");
T("api.ts non-retryable 4xx guard preserved (Slice 2 · P0.4)", () => {
  must(/if \(err && err\.nonRetryable === true\)/.test(API_TS),
       "non-retryable 4xx short-circuit removed by upgrade");
  must(/const shouldRetry = res\.status >= 500 \|\| res\.status === 408 \|\| res\.status === 429/.test(API_TS),
       "retry predicate changed");
});
T("api.labCorrelationsV2 wrapper still exists (Slice 2 · P0.7)", () => {
  must(/labCorrelationsV2:/.test(API_TS), "labCorrelationsV2 wrapper missing");
  must(/\/lab\/correlations-v2/.test(API_TS), "correlations-v2 endpoint missing");
});
T("useFocusRefetch success/failure semantics preserved (Slice 2 · P0.6)", () => {
  must(/if \(result === false\)\s*\{\s*lastFetchRef\.current = 0/.test(FOCUS_TS),
       "false-return reset removed");
  must(/inFlightRef/.test(FOCUS_TS), "inFlight guard removed");
});
T("LockPickCard arePropsEqual does not identity-check pick_rationale (Slice 3)", () => {
  must(!/if\s*\(\s*\(a as any\)\.pick_rationale\s*!==\s*\(b as any\)\.pick_rationale\s*\)\s*return\s+false\s*;/
        .test(CARD_TS),
       "pick_rationale identity gate came back");
});
T("CONFIDENCE_GRADIENT is a readonly tuple (SDK 57 LinearGradient typing)", () => {
  // `as const` produces the readonly tuple that satisfies the new
  // `readonly [ColorValue, ColorValue, ...ColorValue[]]` prop.
  must(/CONFIDENCE_GRADIENT\s*=\s*\[[\s\S]*?\]\s*as\s+const/.test(THEME_TS),
       "CONFIDENCE_GRADIENT should be tagged `as const`");
});

// ── Slice 3 backend parallelization guard (indirect: through
// exp/import surface).  Full pytest coverage lives on the backend
// side; here we just prove the frontend didn't reintroduce any raw
// fetch bypass that would defeat Slice 2's centralization.
console.log("\n== Frontend has no raw fetch to /api/lab/correlations-v2 ==");
T("Lab tab still routes through api.labCorrelationsV2", () => {
  const lab = fs.readFileSync(path.join(ROOT, "app", "(tabs)", "lab.tsx"), "utf-8");
  must(/api\.labCorrelationsV2\(/.test(lab), "Lab lost the centralized call");
  must(!/fetch\(\s*[`'"][^`'"]*\/api\/lab\/correlations-v2/.test(lab),
       "raw fetch to correlations-v2 came back");
});

// ── final summary ───────────────────────────────────────────────────
console.log("\n──────────────────────────────────────────────");
console.log(`  ${passed} passed / ${failed} failed`);
if (failed > 0) {
  console.log("\nFailures:");
  failures.forEach(f => console.log(`  • ${f.name}\n    ${f.msg}`));
  process.exit(1);
}
process.exit(0);
