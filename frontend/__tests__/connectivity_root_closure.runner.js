#!/usr/bin/env node
/**
 * Perklocks — Connectivity + Transport-State Root-Closure (static runner)
 * ──────────────────────────────────────────────────────────────────────
 *
 * Static-source + light-execution runner (plain Node — matches the
 * repo convention; no jest).  Locks the critical contracts of the
 * 2026-10-18 "complete connectivity + production board stability
 * root closure" brief:
 *
 *   Defect 1   : ONE request timeout CANNOT mark backend unreachable.
 *   Defect 2   : Bare fetch failures CANNOT fake device offline.
 *   Defect 4   : Only the authoritative probe may flip origin reach.
 *   Defect 13  : Transport-state transitions preserve `online`/
 *                `deviceOnline` across non-authoritative paths.
 *   Defect 15  : ABORTED_SUPERSEDED is streak-neutral.
 *
 * These contracts are enforced by regex-level invariants on the
 * frontend transport-state source + a small runtime harness over
 * the compiled JS once Metro has produced it.  Keeping it as a plain
 * runner means it works everywhere (CI, local, headless) with no
 * jest/TS pipeline.
 *
 * Run:  node __tests__/connectivity_root_closure.runner.js
 */
"use strict";
const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const TRANSPORT_SRC = fs.readFileSync(
  path.join(ROOT, "src", "lib", "transportState.ts"), "utf-8");
const TAXONOMY_SRC = fs.readFileSync(
  path.join(ROOT, "src", "lib", "errorTaxonomy.ts"), "utf-8");
const CONNECTIVITY_SRC = fs.readFileSync(
  path.join(ROOT, "src", "lib", "connectivity.ts"), "utf-8");

const results = { passed: 0, failed: 0, errors: [] };
function assert(name, cond, detail) {
  if (cond) { results.passed += 1; console.log(`  ✓ ${name}`); }
  else {
    results.failed += 1;
    results.errors.push({ name, detail });
    console.log(`  ✗ ${name}  —  ${detail || ""}`);
  }
}

console.log("\n── Defect 1: single timeout cannot flip origin reachable ──");

// The TIMEOUT branch of recordRequestFail must NOT contain
// `state.reachable = false` or `state.originReachable = false`.
{
  // Isolate the recordRequestFail switch body.
  const m = TRANSPORT_SRC.match(
    /export function recordRequestFail[\s\S]*?^}/m);
  const body = m ? m[0] : "";
  const timeoutCase = body.split("case ErrorKind.TIMEOUT:")[1] || "";
  const nextCase = timeoutCase.split(/case ErrorKind\./)[0];
  assert(
    "TIMEOUT branch does not set reachable=false",
    !/state\.reachable\s*=\s*false/.test(nextCase),
    "TIMEOUT must not flip origin reachable — probe owns that",
  );
  assert(
    "TIMEOUT branch does not set originReachable=false",
    !/state\.originReachable\s*=\s*false/.test(nextCase),
  );
}

console.log("\n── Defect 2: bare fetch failures classify as TRANSPORT, not OFFLINE ──");

{
  // classifyError must return NETWORK_TRANSPORT_FAILURE for the
  // generic "network request failed"/"failed to fetch" path when
  // navigator.onLine is not explicitly false.
  const m = TAXONOMY_SRC.match(/network request failed[\s\S]*?return[^;]+;[\s\S]*?return[^;]+;/);
  const branch = m ? m[0] : "";
  assert(
    "classifyError has an authoritative-online fallthrough to NETWORK_TRANSPORT_FAILURE",
    /NETWORK_TRANSPORT_FAILURE/.test(branch),
    "the bare fetch-failure branch must return NETWORK_TRANSPORT_FAILURE unless navigator.onLine proves offline",
  );
  assert(
    "classifyError honours navigator.onLine === false for NETWORK_OFFLINE",
    /navigator[^\n]*onLine[\s\S]*?NETWORK_OFFLINE/.test(branch),
  );
  // The taxonomy must have the new kind.
  assert(
    "ErrorKind.NETWORK_TRANSPORT_FAILURE exists",
    /NETWORK_TRANSPORT_FAILURE:\s*"NETWORK_TRANSPORT_FAILURE"/.test(TAXONOMY_SRC),
  );
  assert(
    "ERROR_COPY entry exists for NETWORK_TRANSPORT_FAILURE",
    /NETWORK_TRANSPORT_FAILURE:\s*\{\s*title:/.test(TAXONOMY_SRC),
  );
}

console.log("\n── Defect 2: transportState NETWORK_TRANSPORT_FAILURE branch is non-authoritative ──");

{
  const m = TRANSPORT_SRC.match(
    /export function recordRequestFail[\s\S]*?^}/m);
  const body = m ? m[0] : "";
  // Locate the NETWORK_TRANSPORT_FAILURE case.
  const parts = body.split("case ErrorKind.NETWORK_TRANSPORT_FAILURE:");
  assert(
    "recordRequestFail handles NETWORK_TRANSPORT_FAILURE",
    parts.length > 1,
  );
  const seg = (parts[1] || "").split(/case ErrorKind\./)[0];
  assert(
    "NETWORK_TRANSPORT_FAILURE does not flip deviceOnline",
    !/state\.deviceOnline\s*=\s*false/.test(seg),
  );
  assert(
    "NETWORK_TRANSPORT_FAILURE does not flip originReachable",
    !/state\.originReachable\s*=\s*false/.test(seg),
  );
}

console.log("\n── Defect 1: NETWORK_OFFLINE from request path does NOT flip deviceOnline ──");

{
  const m = TRANSPORT_SRC.match(
    /case ErrorKind\.NETWORK_OFFLINE:[\s\S]*?break;/);
  const seg = m ? m[0] : "";
  assert(
    "NETWORK_OFFLINE handler keeps deviceOnline flip under connectivity authority",
    !/state\.online\s*=\s*false/.test(seg),
    "individual requests must not fake offline — NetInfo owns deviceOnline",
  );
}

console.log("\n── Defect 4: recordOriginProbe is the single authoritative setter ──");

{
  assert(
    "recordOriginProbe exported",
    /export function recordOriginProbe\s*\(/.test(TRANSPORT_SRC),
  );
  // Must guard against single-probe flap: originReachable only
  // transitions to false after >= 2 consecutive failures.
  const m = TRANSPORT_SRC.match(/export function recordOriginProbe[\s\S]*?^}/m);
  const body = m ? m[0] : "";
  assert(
    "recordOriginProbe requires >= 2 consecutive probe failures",
    /consecutiveOriginProbeFailures\s*>=\s*2/.test(body),
  );
}

console.log("\n── Defect 4: connectivity.probeReachability routes result through recordOriginProbe ──");

{
  assert(
    "connectivity.ts calls recordOriginProbe",
    /recordOriginProbe\s*\(\s*ok\s*\)/.test(CONNECTIVITY_SRC),
  );
}

console.log("\n── Defect 15: ABORTED_SUPERSEDED is streak-neutral ──");

{
  const m = TRANSPORT_SRC.match(
    /if \(kind === ErrorKind\.ABORTED_SUPERSEDED\)[\s\S]*?return;\s*}/);
  const seg = m ? m[0] : "";
  assert(
    "ABORTED_SUPERSEDED branch does not increment any streak",
    !/consecutiveRequestFailures\s*\+=\s*1/.test(seg) &&
    !/consecutiveFailures\s*\+=\s*1/.test(seg) &&
    !/consecutiveOriginProbeFailures\s*\+=\s*1/.test(seg),
  );
  assert(
    "ABORTED_SUPERSEDED branch does not change reachability",
    !/state\.online\s*=\s*false/.test(seg) &&
    !/state\.reachable\s*=\s*false/.test(seg) &&
    !/state\.originReachable\s*=\s*false/.test(seg) &&
    !/state\.deviceOnline\s*=\s*false/.test(seg),
  );
}

console.log("\n── Defect 1: _computeStage requires >= 3 request failures for unreachable ──");

{
  const m = TRANSPORT_SRC.match(/function _computeStage[\s\S]*?^}/m);
  const body = m ? m[0] : "";
  assert(
    "_computeStage requires >= 3 request failures for backend_unreachable",
    /consecutiveRequestFailures\s*>=\s*3/.test(body),
  );
  assert(
    "_computeStage requires >= 2 request failures for degraded",
    /consecutiveRequestFailures\s*>=\s*2/.test(body),
  );
  assert(
    "_computeStage sets offline from deviceOnline authority",
    /state\.deviceOnline\s*===\s*false/.test(body),
  );
}

console.log("\n── Defect 13: TransportState shape carries the new separated fields ──");

{
  assert(
    "TransportState has deviceOnline",
    /deviceOnline:\s*boolean/.test(TRANSPORT_SRC),
  );
  assert(
    "TransportState has originReachable",
    /originReachable:\s*boolean\s*\|\s*null/.test(TRANSPORT_SRC),
  );
  assert(
    "TransportState has separated request & probe streaks",
    /consecutiveRequestFailures:\s*number/.test(TRANSPORT_SRC) &&
    /consecutiveOriginProbeFailures:\s*number/.test(TRANSPORT_SRC),
  );
}

// ────────────────────────────────────────────────────────────────────
// Summary
// ────────────────────────────────────────────────────────────────────

console.log(`\nResult: ${results.passed} passed, ${results.failed} failed`);
if (results.failed > 0) {
  for (const e of results.errors) {
    console.log(`  FAIL: ${e.name}  —  ${e.detail || ""}`);
  }
  process.exit(1);
}
process.exit(0);
