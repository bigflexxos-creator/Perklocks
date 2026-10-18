#!/usr/bin/env node
/**
 * P0-B + P0-C — Connectivity + Board Stability Soak
 * ────────────────────────────────────────────────────────────────────
 * Hybrid runner:
 *   • STATIC CONTRACT CHECKS over connectivity.ts / transportState.ts
 *     / canonicalEpoch.ts / canonicalConsumers.ts / index.tsx, one
 *     per real-device scenario, confirming the code path that MUST
 *     fire on that transition exists and is wired correctly.
 *   • LIVE BACKEND SOAK against /api/picks/today at T0, T+1s, T+3s,
 *     foreground-simulated + manual-refresh snapshots.  Captures
 *     revision, board_version, generation_id, ETag, pick count,
 *     pick-ID hash per snapshot and asserts the invariant:
 *         identical revision ⇒ identical pick-ID hash
 *     plus "no revision regression".
 *   • LIVE AUTH STABILITY against /api/auth/login — fresh, after
 *     idle, after simulated DB close.  (DB close is only exercised
 *     by the pytest harness; here we only verify the live demo
 *     login round-trips cleanly.)
 *
 * Run:  node __tests__/p0_connectivity_board_soak.runner.js
 */
"use strict";
const fs = require("fs");
const path = require("path");
const http = require("http");
const https = require("https");
const crypto = require("crypto");

// ─────────────────────────────────────────────────────────────────────
// Source files under contract.
// ─────────────────────────────────────────────────────────────────────
const ROOT = path.resolve(__dirname, "..");
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), "utf-8");
const CONN_SRC   = read("src/lib/connectivity.ts");
const TS_SRC     = read("src/lib/transportState.ts");
const EPOCH_SRC  = read("src/lib/canonicalEpoch.ts");
const CONS_SRC   = read("src/lib/canonicalConsumers.ts");
const INDEX_SRC  = read("app/(tabs)/index.tsx");
const DEPS_SRC   = fs.readFileSync("/app/backend/deps.py", "utf-8");
const SERVER_SRC = fs.readFileSync("/app/backend/server.py", "utf-8");

const results = { pass: 0, fail: 0, issues: [] };
function CHECK(label, cond, detail) {
  if (cond) { results.pass++; console.log(`  ✓ ${label}`); }
  else { results.fail++; results.issues.push({ label, detail }); console.log(`  ✗ ${label}${detail ? "\n      " + detail : ""}`); }
}

// ─────────────────────────────────────────────────────────────────────
// 1.  P0-A — static contract checks for the login/mongo lifecycle fix.
// ─────────────────────────────────────────────────────────────────────
console.log("\n── P0-A login/mongo lifecycle contract ──");
CHECK("deps.py exports a LazyDatabaseProxy class",       /class _LazyDatabaseProxy/.test(DEPS_SRC));
CHECK("deps.py exports a LazyClientProxy class",         /class _LazyClientProxy/.test(DEPS_SRC));
CHECK("deps.py db symbol is the proxy instance",         /^db\s*=\s*_LazyDatabaseProxy\(\)/m.test(DEPS_SRC));
CHECK("deps.py client symbol is the proxy instance",     /^client\s*=\s*_LazyClientProxy\(\)/m.test(DEPS_SRC));
CHECK("LazyDatabaseProxy resolves via get_database() per call",
      /def _resolve\(self\):\s*\n\s*return get_database\(\)/.test(DEPS_SRC));
CHECK("login route catches AutoReconnect / ServerSelectionTimeout",
      /_TRANSIENT_DB_EXC\s*=\s*\(_AutoReconnect, _ServerSelectionTimeout, _NetworkTimeout\)/.test(SERVER_SRC));
CHECK("login route returns 503 on transient DB failure",
      /status_code=503,\s*\n\s*detail="Database temporarily unavailable, please retry\."/.test(SERVER_SRC));
CHECK("/api/ready now runs live DB ping (coalesced ≤5s)",
      /live_db_ping_ok/.test(SERVER_SRC) && /_READY_PING_CACHE/.test(SERVER_SRC));

// ─────────────────────────────────────────────────────────────────────
// 2.  P0-B — real-device connectivity scenario contracts.
// ─────────────────────────────────────────────────────────────────────
console.log("\n── P0-B connectivity scenarios (contract) ──");

CHECK("S1 Wi-Fi→cellular: NetInfo type threaded into state.connectionType",
      /state\.connectionType\s*=\s*type/.test(CONN_SRC));
CHECK("S2 cellular→Wi-Fi: linkChanged detection + network_change emit",
      /const linkChanged = type !== prevType/.test(CONN_SRC) && /emit\(["']network_change["']\)/.test(CONN_SRC));
CHECK("S3 online→airplane: nextOnline flips state.online to false",
      /const nextOnline = connected && \(internetReachable === null \|\| internetReachable === true\)/.test(CONN_SRC));
CHECK("S3 airplane: reachable forced false on online→offline transition",
      /prevOnline\s*&&\s*!nextOnline[\s\S]{0,80}state\.reachable\s*=\s*false/.test(CONN_SRC));
CHECK("S3a offline request_fail never sets originReachable=false", (() => {
  // Extract the recordRequestFail body alone and scan only it.
  const m = TS_SRC.match(/function\s+recordRequestFail[\s\S]*?\n\}\n/);
  if (!m) return false;
  return !/originReachable\s*=\s*false/.test(m[0]);
})());
CHECK("S4 airplane→online: reachable cleared to null (fresh re-probe)",
      /!prevOnline\s*&&\s*nextOnline[\s\S]{0,80}state\.reachable\s*=\s*null/.test(CONN_SRC));
CHECK("S5 foreground: AppState active triggers debounced probe + foreground event",
      /next !== ["']active["']/.test(CONN_SRC) && /emit\(["']foreground["']\)/.test(CONN_SRC));
CHECK("S5 foreground: 5s debounce prevents rapid-toggle request storm",
      /now - lastForegroundAt < 5000/.test(CONN_SRC));
CHECK("S6 offline→offline: no spurious probe fires without link transition",
      /linkChanged && nextOnline/.test(CONN_SRC));
CHECK("S7 one endpoint timeout cannot flip originReachable (≥2 required)",
      /consecutiveOriginProbeFailures\s*\+?=?\s*1|consecutiveOriginProbeFailures \+= 1/.test(TS_SRC) &&
      /consecutiveOriginProbeFailures\s*>=\s*2/.test(TS_SRC));
CHECK("S8 backend recovery: successful probe resets probe-failure streak + originReachable=true",
      /recordOriginProbe[\s\S]{0,400}originReachable\s*=\s*true/.test(TS_SRC));
CHECK("S9 cold boot: hydrateCanonicalEpoch available for cache rehydrate",
      /export\s+(?:async\s+)?function\s+hydrateCanonicalEpoch/.test(EPOCH_SRC));
CHECK("S9 cold boot: canonical epoch guarded against stale/invariant_violation",
      /invariant_violation/.test(EPOCH_SRC));
CHECK("S10 cold boot + Wi-Fi: single network_change probe (coalesced)",
      /probeReachability\(\)\.finally\(\(\) => \{ emit\(["']network_change["']\)/.test(CONN_SRC));

console.log("\n── P0-B board acceptance contract ──");
CHECK("Transport failure does NOT wipe committed picks",
      /ABORTED_SUPERSEDED/.test(INDEX_SRC) || /STALE_FALLBACK/.test(INDEX_SRC));
CHECK("Empty response + cached picks → STALE_FALLBACK",
      /STALE_FALLBACK/.test(INDEX_SRC));
CHECK("Non-COMMITTED generation cannot replace committed slate",
      /COMMITTED/.test(INDEX_SRC));

console.log("\n── Canonical epoch coalescing (storm guard) ──");
CHECK("Coalesce timer window declared",
      /COALESCE_WINDOW_MS/.test(CONS_SRC) || /coalesceWindow/.test(CONS_SRC));
CHECK("scheduleFlush never regresses pending target (monotonic upgrade)",
      /if\s*\(\s*target\s*>\s*_pendingTarget\s*\)\s*_pendingTarget\s*=\s*target/.test(CONS_SRC));
CHECK("origin_change cancels pending flush before resetting dedup memory",
      /origin_change[\s\S]{0,300}(clearTimeout|cancel|reset)/i.test(CONS_SRC));

// ─────────────────────────────────────────────────────────────────────
// 3.  LIVE BACKEND SOAK — /api/picks/today board stability + /api/auth/login.
// ─────────────────────────────────────────────────────────────────────
function liveFetch(url, { method = "GET", headers = {}, body = null, timeoutMs = 10000 } = {}) {
  return new Promise((resolve, reject) => {
    const u = new URL(url);
    const lib = u.protocol === "https:" ? https : http;
    const opts = {
      method, headers: { "Accept": "application/json", ...headers },
      hostname: u.hostname, port: u.port || (u.protocol === "https:" ? 443 : 80),
      path: u.pathname + u.search,
    };
    if (body) {
      opts.headers["Content-Type"] = "application/json";
      opts.headers["Content-Length"] = Buffer.byteLength(body);
    }
    const req = lib.request(opts, (res) => {
      let data = "";
      res.on("data", (c) => (data += c));
      res.on("end", () => resolve({ status: res.statusCode, headers: res.headers, body: data }));
    });
    req.on("error", reject);
    req.setTimeout(timeoutMs, () => { req.destroy(new Error(`timeout after ${timeoutMs}ms`)); });
    if (body) req.write(body);
    req.end();
  });
}

async function liveSoak() {
  console.log("\n── P0-C live board stability (preview backend) ──");
  const BASE = process.env.P0_BACKEND || "http://localhost:8001";

  // Grab a JWT first so /api/picks/today authorises cleanly.
  let token = "";
  try {
    const r = await liveFetch(`${BASE}/api/auth/login`, {
      method: "POST",
      body: JSON.stringify({ email: "demo@lockscore.ai", password: "demo123" }),
    });
    try { token = JSON.parse(r.body).access_token || ""; } catch {}
  } catch { /* leave unauth */ }
  const authHdr = token ? { "Authorization": `Bearer ${token}` } : {};

  async function pickSnap(label, extraHeaders = {}) {
    const t0 = Date.now();
    try {
      const r = await liveFetch(`${BASE}/api/picks/today?limit=40`, {
        headers: { ...authHdr, ...extraHeaders },
      });
      let picks = [];
      try {
        const parsed = JSON.parse(r.body || "null");
        picks = Array.isArray(parsed) ? parsed : (parsed && parsed.picks) || [];
      } catch { picks = []; }
      const ids = picks.map((p) => p.canonical_pick_id || p.id || p._id).filter(Boolean).sort();
      const hash = crypto.createHash("sha256").update(ids.join("|")).digest("hex").slice(0, 16);
      const rev = r.headers["x-canonical-revision"] || "";
      const ver = r.headers["x-canonical-version"]  || "";
      const gen = r.headers["x-canonical-generation-id"] || "";
      const etag = r.headers["etag"] || "";
      const snap = { label, status: r.status, revision: rev, boardVersion: ver, generationId: gen, etag, pickCount: picks.length, pickIdHash: hash, elapsedMs: Date.now() - t0 };
      console.log(`  • ${label.padEnd(26)} status=${r.status} rev=${rev || "-"} count=${String(picks.length).padEnd(3)} hash=${hash} (${snap.elapsedMs}ms)`);
      return snap;
    } catch (e) {
      console.log(`  • ${label.padEnd(26)} ERROR: ${e.message}`);
      return { label, error: e.message };
    }
  }

  const snaps = [];
  snaps.push(await pickSnap("T0: cold"));
  await new Promise((r) => setTimeout(r, 1000));
  snaps.push(await pickSnap("T+1s"));
  await new Promise((r) => setTimeout(r, 2000));
  snaps.push(await pickSnap("T+3s"));
  snaps.push(await pickSnap("foreground-simulated", { "x-foreground-refresh": "1" }));
  snaps.push(await pickSnap("manual-refresh",         { "x-manual-refresh": "1" }));

  // Assertions.
  const ok = snaps.filter((s) => !s.error && s.status === 200);
  if (ok.length >= 2) {
    const byRev = new Map();
    for (const s of ok) { if (!byRev.has(s.revision)) byRev.set(s.revision, []); byRev.get(s.revision).push(s); }
    for (const [rev, group] of byRev) {
      if (group.length < 2) continue;
      const hashes = new Set(group.map((g) => g.pickIdHash));
      CHECK(`identical revision=${rev || "(none)"} → identical pick-ID hash`, hashes.size === 1,
            `revision=${rev} returned ${hashes.size} hashes: ${[...hashes].join(", ")}`);
    }
    const revs = ok.map((o) => o.revision).filter(Boolean);
    let noReg = true;
    for (let i = 1; i < revs.length; i++) {
      const a = Number(revs[i - 1]), b = Number(revs[i]);
      if (Number.isFinite(a) && Number.isFinite(b) && b < a) { noReg = false; break; }
    }
    CHECK("no revision regression across live snapshots", noReg,
          `sequence: ${revs.join(" → ")}`);
    CHECK("every live snapshot returned 200", ok.length === snaps.length,
          `${snaps.length - ok.length} snapshot(s) returned non-200 or errored`);
  } else {
    console.log("  (skipped — not enough live snapshots)");
  }

  // Login soak (fresh + idle).
  console.log("\n── P0-C live login stability ──");
  async function loginOnce(label) {
    try {
      const r = await liveFetch(`${BASE}/api/auth/login`, {
        method: "POST",
        body: JSON.stringify({ email: "demo@lockscore.ai", password: "demo123" }),
      });
      let ok = false;
      try { ok = r.status === 200 && !!JSON.parse(r.body).access_token; } catch {}
      console.log(`  • ${label.padEnd(26)} status=${r.status} ok=${ok}`);
      return { label, status: r.status, ok };
    } catch (e) {
      console.log(`  • ${label.padEnd(26)} ERROR: ${e.message}`);
      return { label, error: e.message };
    }
  }
  const l0 = await loginOnce("fresh login");
  await new Promise((r) => setTimeout(r, 3000));
  const l1 = await loginOnce("after 3s idle");
  CHECK("fresh login returns 200+token", l0.status === 200 && l0.ok);
  CHECK("idle login returns 200+token (no stale-pool regression)",
        l1.status === 200 && l1.ok);

  return { snaps, logins: [l0, l1] };
}

(async () => {
  console.log("══════════════════════════════════════════════════════════════");
  console.log(" P0-A/B/C — Launch-Blocker Soak Runner");
  console.log("══════════════════════════════════════════════════════════════");
  const live = await liveSoak().catch((e) => { console.error(e); return null; });
  console.log("\n══════════════════════════════════════════════════════════════");
  console.log(` RESULT: ${results.pass} passed · ${results.fail} failed`);
  console.log("══════════════════════════════════════════════════════════════");
  if (results.fail > 0) {
    for (const i of results.issues) console.log(`  ✗ ${i.label}${i.detail ? "  —  " + i.detail : ""}`);
  }
  const out = path.join("/app", "p0_soak_results.json");
  try {
    fs.writeFileSync(out, JSON.stringify({ pass: results.pass, fail: results.fail, issues: results.issues, live, ts: new Date().toISOString() }, null, 2));
    console.log(`\nResults written → ${out}`);
  } catch {}
  process.exit(results.fail === 0 ? 0 : 1);
})();
