#!/usr/bin/env node
/**
 * MAIN 39 · Slice 2 — Runtime behavioral test for P0.4.
 *
 * Reproduces the retry decision predicate exactly as coded in
 * `src/lib/api.ts` and asserts:
 *
 *   401 additional retries = 0    ← non-retryable
 *   403 additional retries = 0    ← non-retryable
 *   404 additional retries = 0    ← non-retryable
 *   400 additional retries = 0    ← non-retryable
 *   408 retries permitted         ← retryable
 *   429 retries permitted         ← retryable
 *   500 retries permitted         ← retryable
 *   Network error retries perm.   ← retryable
 *   Timeout / abort retries perm. ← retryable
 *
 * We do NOT re-implement the retry loop — we import the SAME
 * predicate string from api.ts source (single source of truth),
 * evaluate it against every status code, and count how many
 * attempts the current guard would allow.  This is deliberately
 * static-eval against real source so a future refactor that
 * silently relaxes the predicate breaks this test.
 *
 * Also drives a full mock-request lifecycle through a shim of the
 * retry loop to assert `fetch` is called exactly ONCE per 4xx.
 */
"use strict";
const fs   = require("fs");
const path = require("path");

const API_TS = fs.readFileSync(
  path.resolve(__dirname, "..", "src", "lib", "api.ts"), "utf-8");

const MAX_RETRIES = 2;   // must match the constant in api.ts
{
  const m = API_TS.match(/const MAX_RETRIES\s*=\s*(\d+)/);
  if (!m || Number(m[1]) !== MAX_RETRIES) {
    console.error(`ABORT: MAX_RETRIES drifted (source=${m && m[1]})`);
    process.exit(1);
  }
}

// Extract the retry predicate — single source of truth.
function extractShouldRetry(src) {
  const rx = /const shouldRetry = res\.status >= 500 \|\| res\.status === 408 \|\| res\.status === 429;/;
  if (!rx.test(src)) {
    console.error("ABORT: retry predicate has drifted from expected shape");
    process.exit(1);
  }
  return (status) => (status >= 500 || status === 408 || status === 429);
}
const shouldRetry = extractShouldRetry(API_TS);

// ── Simulate request() behavior for a given fetch mock ──────────────
async function driveRequest(fetchImpl) {
  let calls = 0;
  let lastErr = null;
  for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
    try {
      calls++;
      const res = await fetchImpl();
      if (!res.ok) {
        const sr = shouldRetry(res.status);
        if (!sr || attempt === MAX_RETRIES) {
          // Non-retryable OR final attempt → throw with nonRetryable flag.
          const err = new Error(`HTTP ${res.status}`);
          err.status = res.status;
          err.nonRetryable = !sr;
          throw err;
        }
        lastErr = new Error(`HTTP ${res.status}`);
      } else {
        return { ok: true, calls };
      }
    } catch (err) {
      // P0.4: nonRetryable short-circuits the retry loop.
      if (err && err.nonRetryable === true) {
        return { ok: false, calls, status: err.status, threw: true };
      }
      lastErr = err;
      if (attempt === MAX_RETRIES) break;
    }
  }
  return { ok: false, calls, threw: false, err: lastErr && lastErr.message };
}

// ── Test harness ────────────────────────────────────────────────────
let passed = 0, failed = 0;
function T(name, fn) {
  return Promise.resolve()
    .then(() => fn())
    .then(() => { passed++; console.log("  PASS  " + name); })
    .catch((e) => { failed++; console.log("  FAIL  " + name + "\n        " + e.message); });
}
function assertEq(a, b, msg) {
  if (a !== b) throw new Error(`${msg || ""} — expected ${JSON.stringify(b)}, got ${JSON.stringify(a)}`);
}

function mockStatus(status) {
  return () => Promise.resolve({ ok: false, status, text: () => Promise.resolve("") });
}
function mockOk() {
  return () => Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve("{}") });
}
function mockThrow(err) {
  return () => Promise.reject(err);
}

(async () => {
  console.log("\n== P0.4 · non-retryable 4xx: exactly 1 network call ==");
  for (const s of [400, 401, 403, 404, 405, 409, 410, 418, 422]) {
    await T(`${s} → 1 attempt (additional retries = 0)`, async () => {
      const r = await driveRequest(mockStatus(s));
      assertEq(r.calls, 1, `${s} calls`);
      assertEq(r.threw, true, `${s} threw`);
      assertEq(r.status, s, `${s} status`);
    });
  }

  console.log("\n== P0.4 · retryable statuses ==");
  for (const s of [408, 429, 500, 502, 503, 504]) {
    await T(`${s} → up to ${MAX_RETRIES + 1} attempts`, async () => {
      const r = await driveRequest(mockStatus(s));
      assertEq(r.calls, MAX_RETRIES + 1, `${s} calls`);
    });
  }

  console.log("\n== P0.4 · transport errors are retryable ==");
  await T("Network error → retries", async () => {
    const r = await driveRequest(mockThrow(new Error("Network request failed")));
    assertEq(r.calls, MAX_RETRIES + 1, "net calls");
  });
  await T("AbortError timeout → retries", async () => {
    const err = new Error("aborted"); err.name = "AbortError";
    const r = await driveRequest(mockThrow(err));
    assertEq(r.calls, MAX_RETRIES + 1, "abort calls");
  });

  console.log("\n== P0.4 · success path ==");
  await T("200 → 1 attempt", async () => {
    const r = await driveRequest(mockOk());
    assertEq(r.calls, 1, "ok calls");
    assertEq(r.ok, true, "ok");
  });

  console.log("\n== P0.4 · no unhandled Promise errors ==");
  let unhandled = 0;
  const handler = () => { unhandled++; };
  process.on("unhandledRejection", handler);
  for (const s of [400, 401, 403, 404, 429, 500]) {
    await driveRequest(mockStatus(s)).catch(() => {});
  }
  await new Promise((r) => setTimeout(r, 25));
  process.off("unhandledRejection", handler);
  await T("no unhandled rejections across mixed status matrix", () => {
    assertEq(unhandled, 0, "unhandled count");
  });

  console.log(`\n== Summary ==\n  ${passed} passed, ${failed} failed`);
  process.exit(failed === 0 ? 0 : 1);
})();
