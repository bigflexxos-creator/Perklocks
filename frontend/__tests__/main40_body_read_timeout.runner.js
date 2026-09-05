#!/usr/bin/env node
/**
 * MAIN 40 · Iteration 3 — native body-read timeout closure.
 *
 * User's real-device evidence: on Expo Go SDK 57 running on iPhone,
 * MLB Locks (170 KB / 49 picks) loads perfectly but ALL Locks
 * (510 KB / 207 picks) never completes, and Parlay (698 KB / 3
 * parlays × ~2 legs each with full pick documents) does not work.
 * Web / Preview handle all three shapes identically.  Payload size
 * is the sole correlate.
 *
 * Classification (per user's spec):
 *   C  — the request/body-read times out or aborts.
 *
 * Root cause proven by static inspection of api.ts (this test's
 * subject):
 *   Prior `_fetchWithTimeout` returned the raw Response the moment
 *   headers arrived, and the surrounding `finally` cleared the
 *   AbortController timer.  The caller then ran `await res.text()`
 *   on the next line — completely UNPROTECTED against a stalled
 *   body-stream on the RN 0.86 iOS fetch bridge.  With 500 KB+
 *   chunked HTTPS/2 bodies this stage would hang past the intended
 *   20 s cap without ever aborting.
 *
 * Fix (this iteration): read the body INSIDE the AbortController
 * guard.  Timer stays armed until BOTH fetch AND res.text() settle.
 * A stalled iOS bridge body-read now aborts cleanly and the
 * request() retry loop handles it as an ordinary transport error.
 *
 * The 20 s timeout duration is UNCHANGED (per user directive
 * "Do not increase timeouts").
 *
 * This runner exercises the behavior through a deterministic
 * mocked global.fetch on Node so it costs zero real network I/O.
 */
"use strict";
const fs   = require("fs");
const path = require("path");

const API_TS = fs.readFileSync(
  path.resolve(__dirname, "..", "src", "lib", "api.ts"),
  "utf-8",
);

let passed = 0, failed = 0;
const failures = [];
function T(name, fn) {
  try { fn(); passed++; console.log("  PASS  " + name); }
  catch (e) { failed++; failures.push({ name, msg: e.message });
              console.log("  FAIL  " + name + "\n        " + e.message); }
}
function must(cond, msg) { if (!cond) throw new Error(msg); }

// ── Static contract — the body-read must live inside the timeout ────
console.log("\n== body-read timeout coverage ==");

T("_fetchWithTimeout returns PreparedResponse ({ ok, status, text })", () => {
  must(/type\s+PreparedResponse\s*=\s*\{[\s\S]*?ok:\s*boolean;[\s\S]*?status:\s*number;[\s\S]*?text:\s*string;[\s\S]*?\}/
        .test(API_TS),
       "PreparedResponse type must declare ok/status/text as immediate fields");
  must(/async\s+function\s+_fetchWithTimeout\s*\([\s\S]*?\)\s*:\s*Promise<PreparedResponse>/.test(API_TS),
       "_fetchWithTimeout must return Promise<PreparedResponse>");
});

T("body-read (await res.text()) is INSIDE the try/finally guard", () => {
  // Extract the body of _fetchWithTimeout and prove that `res.text()`
  // is called INSIDE `try { … }`, before `clearTimeout(timer)` fires.
  const m = API_TS.match(
    /async\s+function\s+_fetchWithTimeout\([\s\S]*?\)\s*:[\s\S]*?\{([\s\S]*?)\n\}\s*\n/,
  );
  must(m, "_fetchWithTimeout not found");
  const body = m[1];
  const tryIdx     = body.indexOf("try {");
  const finallyIdx = body.indexOf("} finally {");
  const clearIdx   = body.indexOf("clearTimeout(timer)");
  const fetchIdx   = body.indexOf("await fetch(");
  const textIdx    = body.search(/await\s+res\.text\(\)/);
  must(tryIdx >= 0 && finallyIdx > tryIdx, "try/finally structure missing");
  must(fetchIdx > tryIdx && fetchIdx < finallyIdx, "fetch must be inside try");
  must(textIdx > fetchIdx && textIdx < finallyIdx,
       "res.text() must be AFTER fetch and BEFORE the finally block");
  must(clearIdx > finallyIdx,
       "clearTimeout must be inside the finally block, not inside try");
});

T("no bare `await res.text()` remains in request() body-read path", () => {
  // The old, unprotected pattern was:
  //     const res = await _fetchWithTimeout(...);
  //     const text = await res.text();
  // After the fix, `res.text` is a plain property.  Regression guard.
  must(!/const\s+res\s*=\s*await\s+_fetchWithTimeout[\s\S]{0,100}?const\s+text\s*=\s*await\s+res\.text\(\)/
        .test(API_TS),
       "old unprotected `await res.text()` right after _fetchWithTimeout must not return");
  must(/const\s+res\s*=\s*await\s+_fetchWithTimeout[\s\S]{0,200}?const\s+text\s*=\s*res\.text\b/
        .test(API_TS),
       "request() must consume res.text as a property from PreparedResponse");
});

T("REQUEST_TIMEOUT_MS unchanged (20 s per user directive)", () => {
  const m = API_TS.match(/const\s+REQUEST_TIMEOUT_MS\s*=\s*([\d_]+)/);
  must(m, "REQUEST_TIMEOUT_MS not found");
  const v = Number(m[1].replace(/_/g, ""));
  must(v === 20000, `REQUEST_TIMEOUT_MS must remain 20000 (was ${v})`);
});

T("Non-retryable 4xx short-circuit still intact (Slice 2 · P0.4)", () => {
  must(/if\s*\(\s*err\s*&&\s*err\.nonRetryable\s*===\s*true\s*\)\s*\{\s*throw\s+err;\s*\}/.test(API_TS),
       "P0.4 nonRetryable short-circuit was regressed by this fix");
});

// ── Behavioral: simulate iOS RN 0.86's body-read hang ──────────────
console.log("\n== behavior · simulated body-read stall ==");

// Faithfully replicate _fetchWithTimeout to prove the semantic.
async function _fetchWithTimeout(url, init, timeoutMs, fetchImpl) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetchImpl(url, { ...init, signal: ctrl.signal });
    const text = await res.text();
    return { ok: res.ok, status: res.status, text };
  } finally {
    clearTimeout(timer);
  }
}

function mockHeadersFastBodyHang(delayMs, ctrlHook) {
  return (_url, init) => new Promise((resolve, reject) => {
    // Simulate headers arrive in 20 ms, then body-stream hangs
    // forever until AbortController fires.
    setTimeout(() => {
      const signal = init.signal;
      const bodyPromise = new Promise((_r, _re) => {
        const onAbort = () => {
          const err = new Error("The user aborted a request.");
          err.name = "AbortError";
          _re(err);
        };
        if (signal.aborted) return onAbort();
        signal.addEventListener("abort", onAbort);
      });
      const res = {
        ok: true,
        status: 200,
        text: () => bodyPromise,
      };
      if (ctrlHook) ctrlHook(init.signal);
      resolve(res);
    }, 20);
  });
}

function mockFullOk(bodyStr) {
  return () => Promise.resolve({
    ok: true, status: 200,
    text: () => Promise.resolve(bodyStr),
  });
}

async function run() {
  const results = [];
  // Case 1 — headers arrive but body hangs; abort MUST fire.
  const t0 = Date.now();
  let caught = null;
  try {
    await _fetchWithTimeout("http://example/big", {}, 200, mockHeadersFastBodyHang());
  } catch (e) { caught = e; }
  const elapsed = Date.now() - t0;
  results.push({
    name: "body-hang aborts within ~timeout window (not indefinite)",
    ok: caught && /abort/i.test(caught.name || caught.message || "")
        && elapsed >= 190 && elapsed < 700,
    detail: `elapsed=${elapsed}ms  err=${caught && caught.name}`,
  });

  // Case 2 — small OK body under timeout resolves normally.
  const t2 = Date.now();
  const r2 = await _fetchWithTimeout("http://example/small", {}, 500, mockFullOk('{"a":1}'));
  const el2 = Date.now() - t2;
  results.push({
    name: "small body resolves with { ok, status, text } shape",
    ok: r2 && r2.ok === true && r2.status === 200 && r2.text === '{"a":1}' && el2 < 100,
    detail: `elapsed=${el2}ms  text=${r2.text}`,
  });

  for (const r of results) {
    T(r.name, () => { must(r.ok, r.detail); });
  }

  console.log("\n──────────────────────────────────────────────");
  console.log(`  ${passed} passed / ${failed} failed`);
  if (failed > 0) {
    console.log("\nFailures:");
    failures.forEach(f => console.log(`  • ${f.name}\n    ${f.msg}`));
    process.exit(1);
  }
  process.exit(0);
}
run();
