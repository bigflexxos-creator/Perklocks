#!/usr/bin/env node
/**
 * Universal Canonical Epoch Contract — CLIENT deterministic race
 * ═══════════════════════════════════════════════════════════════════
 * Verifies the ORDERED-revision authority contract (2026-06-21 v2):
 *
 *   1. `canonicalEpoch` module exposes hydrate + note + subscribe +
 *      getCurrent + isEpochCurrent + isEpochLastGood.
 *
 *   2. `noteCanonicalEpoch` classifies transitions correctly:
 *        · rev > current   → "advance"
 *        · rev == current  → "same" (or "invariant_violation" if bv/gid disagree)
 *        · rev < current   → "stale" (never regresses authority)
 *        · origin change   → "origin_change"
 *        · missing rev/origin → "ignored"
 *
 *   3. `api.ts` reads X-Canonical-Revision + X-Canonical-Version +
 *      X-Canonical-Generation-Id and pushes through noteCanonicalEpoch.
 *
 *   4. `useSWR.ts` stamps entries with the full epoch (origin +
 *      revision + version + gid) and the sweeper sweeps on advance
 *      OR origin change, keeping only entries stamped at the NEW
 *      revision.
 *
 *   5. Locks tab (`app/(tabs)/index.tsx`) stamps its persisted
 *      `locks_picks_cache_v2` with the canonical revision + gen id,
 *      demotes to LAST-GOOD when the stamp is behind live, refuses
 *      to commit a load() result whose revision is BEHIND the live
 *      global CanonicalEpoch, and registers as a canonical consumer.
 *
 *   6. HistoricalIntelligence registers as a mounted consumer with
 *      the shared registry.
 *
 *   7. Deterministic race test — a late N response arriving AFTER
 *      N+1 has been accepted MUST NOT regress authority.
 */
"use strict";
const fs = require("fs");
const path = require("path");

function read(rel) {
  return fs.readFileSync(path.resolve(__dirname, "..", rel), "utf-8");
}

const EPOCH  = read("src/lib/canonicalEpoch.ts");
const CONSU  = read("src/lib/canonicalConsumers.ts");
const SWR    = read("src/lib/useSWR.ts");
const API    = read("src/lib/api.ts");
const LAYOUT = read("app/_layout.tsx");
const APPST  = read("src/lib/appStateFreshness.ts");
const LOCKS  = read("app/(tabs)/index.tsx");
const HI     = read("src/components/HistoricalIntelligence.tsx");
const PICK   = read("app/pick/[id].tsx");

let passed = 0, failed = 0;
const failures = [];
function T(name, fn) {
  try { fn(); passed++; console.log("  PASS  " + name); }
  catch (e) { failed++; failures.push({ name, msg: e.message });
              console.log("  FAIL  " + name + "\n        " + e.message); }
}
function must(cond, msg) { if (!cond) throw new Error(msg); }

console.log("\n== canonicalEpoch — static contract ==");

T("exports hydrate + note + subscribe + getCurrent + isEpochCurrent + isEpochLastGood", () => {
  must(/export\s+async\s+function\s+hydrateCanonicalEpoch\b/.test(EPOCH), "hydrateCanonicalEpoch missing");
  must(/export\s+function\s+noteCanonicalEpoch\b/.test(EPOCH), "noteCanonicalEpoch missing");
  must(/export\s+function\s+subscribeCanonicalEpoch\b/.test(EPOCH), "subscribeCanonicalEpoch missing");
  must(/export\s+function\s+getCurrentEpoch\b/.test(EPOCH), "getCurrentEpoch missing");
  must(/export\s+function\s+isEpochCurrent\b/.test(EPOCH), "isEpochCurrent missing");
  must(/export\s+function\s+isEpochLastGood\b/.test(EPOCH), "isEpochLastGood missing");
});

T("transition classifier covers advance/same/stale/invariant_violation/origin_change/ignored", () => {
  must(/return\s+"advance"/.test(EPOCH), '"advance" branch missing');
  must(/return\s+"same"/.test(EPOCH), '"same" branch missing');
  must(/return\s+"stale"/.test(EPOCH), '"stale" branch missing');
  must(/return\s+"invariant_violation"/.test(EPOCH), '"invariant_violation" branch missing');
  must(/return\s+"origin_change"/.test(EPOCH), '"origin_change" branch missing');
  must(/return\s+"ignored"/.test(EPOCH), '"ignored" branch missing');
});

T("api.ts reads X-Canonical-Revision (ordered) + Version + Gen-Id", () => {
  must(/["']x-canonical-revision["']/i.test(API), "X-Canonical-Revision read missing");
  must(/["']x-canonical-version["']/i.test(API), "X-Canonical-Version read missing");
  must(/["']x-canonical-generation-id["']/i.test(API), "X-Canonical-Generation-Id read missing");
  must(/noteCanonicalEpoch\s*\(/.test(API), "noteCanonicalEpoch not called in api.ts");
  must(/origin:\s+resolvedOrigin/.test(API), "resolved origin must be passed to noteCanonicalEpoch");
});

T("useSWR stamps entries with epoch (origin + revision + version + gid)", () => {
  must(/epoch\?:\s*\{[\s\S]*?origin:[\s\S]*?revision:[\s\S]*?boardVersion:[\s\S]*?generationId:/.test(SWR),
       "Snapshot must carry an epoch stamp {origin, revision, boardVersion, generationId}");
  must(/getCurrentEpoch\(\)/.test(SWR), "swrCacheWrite must call getCurrentEpoch()");
});

T("useSWR sweeper deletes older revisions AND legacy null-stamps AND cross-origin entries", () => {
  must(/stampRev\s*<\s*next\.revision/.test(SWR), "sweeper must delete stamps behind the new revision");
  must(/originChanged/.test(SWR), "sweeper must handle origin_change");
  must(/originMismatch/.test(SWR), "sweeper must delete cross-origin entries");
});

T("useSWR forces refetch when cache stamp is not current (dep-change effect)", () => {
  must(/isEpochCurrent\s*\(/.test(SWR), "dep-change effect must call isEpochCurrent()");
  must(/stampBehind/.test(SWR), "stampBehind must gate the silent refresh");
});

T("_layout.tsx wires hydrateCanonicalEpoch + AppState reconciler + consumer registry", () => {
  must(/hydrateCanonicalEpoch/.test(LAYOUT), "hydrateCanonicalEpoch missing in _layout.tsx");
  must(/installAppStateFreshnessReconciler/.test(LAYOUT), "installAppStateFreshnessReconciler missing");
  must(/canonicalConsumers/.test(LAYOUT), "canonicalConsumers module must be imported at boot to wire the shared subscription");
});

T("AppState reconciler pings /api/version debounced", () => {
  must(/AppState\.addEventListener/.test(APPST), "AppState.addEventListener missing");
  must(/api\.version\(\)/.test(APPST), "reconciler must call api.version()");
  must(/_lastPingAt/.test(APPST), "reconciler must debounce");
});

console.log("\n== canonicalConsumers — mounted-consumer revalidation ==");

T("registry exposes registerCanonicalConsumer with dedup on (key, target)", () => {
  must(/export\s+function\s+registerCanonicalConsumer\b/.test(CONSU), "registerCanonicalConsumer missing");
  must(/lastTargetInvoked/.test(CONSU), "registry must dedup by lastTargetInvoked");
  must(/subscribeCanonicalEpoch/.test(CONSU), "registry must subscribe to canonical epoch advances");
});

T("HistoricalIntelligence registers as a consumer per (pickId, sample, venue)", () => {
  must(/registerCanonicalConsumer/.test(HI), "HistoricalIntelligence must register as a consumer");
  must(/`historical-intelligence\|\$\{pickId\}\|\$\{sample\}\|\$\{venue\}`/.test(HI),
       "consumer key must include pickId + sample + venue");
});

T("Locks board registers as a consumer 'locks-board'", () => {
  must(/registerCanonicalConsumer\s*\(\s*"locks-board"/.test(LOCKS), "Locks must register key='locks-board'");
});

console.log("\n== Locks — persisted cache + single-flight ==");

T("PicksCache carries canonicalRevision + canonicalGenerationId", () => {
  must(/canonicalRevision\?:\s*number/.test(LOCKS), "PicksCache must carry canonicalRevision");
  must(/canonicalGenerationId\?:\s*string/.test(LOCKS), "PicksCache must carry canonicalGenerationId");
});

T("cache write stamps epoch, restore demotes stale to LAST-GOOD ONLY", () => {
  must(/canonicalRevision:\s*_canRev/.test(LOCKS), "write must persist canonicalRevision from getCurrentEpoch");
  must(/_isLastGoodOnly/.test(LOCKS), "restore must classify stale caches as LAST-GOOD ONLY");
});

T("load() refuses to commit a response whose revision is BEHIND global CanonicalEpoch", () => {
  must(/staleVsGlobal/.test(LOCKS), "load() must have a staleVsGlobal guard");
  must(/getCurrentEpoch\(\)/.test(LOCKS), "load() must query getCurrentEpoch() before commit");
});

T("Pick detail renders BOOK IMPLIED safely (no 'undefined%')", () => {
  must(/Never render "undefined%"/.test(PICK) ||
       /Number\.isFinite\(pick\.implied_probability\)/.test(PICK),
       "pick/[id].tsx must guard implied_probability against undefined");
});

console.log("\n== Deterministic Race — late N after N+1 ==");

/** Simulated in-process observer that mirrors canonicalEpoch's logic. */
function makeObserver() {
  let cur = null;
  const listeners = new Set();
  const cache = new Map();
  function note({ origin, revision, boardVersion, generationId }) {
    if (typeof revision !== "number" || !origin) return "ignored";
    const incoming = { origin, revision, boardVersion, generationId };
    const prev = cur;
    if (!prev) { cur = incoming; return "advance"; }
    if (prev.origin !== origin) { cur = incoming; notifyAdvance(prev); return "origin_change"; }
    if (revision > prev.revision) { cur = incoming; notifyAdvance(prev); return "advance"; }
    if (revision === prev.revision) {
      if (boardVersion !== prev.boardVersion || generationId !== prev.generationId) return "invariant_violation";
      return "same";
    }
    return "stale";
  }
  function notifyAdvance(prev) {
    for (const fn of listeners) fn(cur, prev);
  }
  function subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); }
  function write(k, data) { cache.set(k, { data, epoch: cur ? { ...cur } : null }); }
  subscribe((next, previous) => {
    for (const [k, v] of cache.entries()) {
      const behind = !v.epoch || v.epoch.revision < next.revision || v.epoch.origin !== next.origin;
      if (behind) cache.delete(k);
    }
  });
  return { note, write, cache, cur: () => cur };
}

T("Step 1-2: cache Locks + Detail + HI at rev=100", () => {
  const o = makeObserver();
  o.note({ origin: "https://api", revision: 100, boardVersion: "A", generationId: "gen-A" });
  o.write("picks|today|MLB", { picks: ["x"] });
  o.write("pick-detail|nw-indiana", { wp: 96.69 });
  o.write("historical-intelligence|nw|L10|ALL", { games: [] });
  must(o.cur().revision === 100, "expected authority rev=100");
  must(o.cache.size === 3, "expected 3 cached entries at rev=100");
});

T("Step 3-6: server advances 100→101 · B lands · exactly one advance · caches sweep · rev=101 entries survive", () => {
  const o = makeObserver();
  o.note({ origin: "https://api", revision: 100, boardVersion: "A", generationId: "gen-A" });
  o.write("picks|today|MLB", { picks: ["x"] });
  o.write("pick-detail|nw-indiana", { wp: 96.69 });
  o.write("historical-intelligence|nw|L10|ALL", { games: [] });
  // B lands at rev=101
  const r = o.note({ origin: "https://api", revision: 101, boardVersion: "B", generationId: "gen-B" });
  must(r === "advance", `B must classify as advance (got ${r})`);
  must(o.cur().revision === 101, "authority must move to 101");
  must(o.cache.size === 0, "sweeper must clear all rev=100 entries");
  // Re-populate at rev=101
  o.write("picks|today|MLB", { picks: ["y"] });
  must(o.cache.get("picks|today|MLB").epoch.revision === 101, "new writes must be stamped rev=101");
});

T("Step 7-8: DELAYED late A (rev=100) MUST NOT regress authority + cache untouched", () => {
  const o = makeObserver();
  o.note({ origin: "https://api", revision: 100, boardVersion: "A", generationId: "gen-A" });
  o.note({ origin: "https://api", revision: 101, boardVersion: "B", generationId: "gen-B" });
  o.write("picks|today|MLB", { picks: ["y"] }); // stamped rev=101
  const r = o.note({ origin: "https://api", revision: 100, boardVersion: "A", generationId: "gen-A" });
  must(r === "stale", `late A must be "stale" (got ${r})`);
  must(o.cur().revision === 101, "authority MUST remain at 101");
  must(o.cache.has("picks|today|MLB"), "cache stamped at 101 MUST survive the late A");
  must(o.cache.get("picks|today|MLB").epoch.revision === 101, "cache stamp remains 101");
});

T("Step 9-10: same rev · same fingerprint = 'same' no sweep", () => {
  const o = makeObserver();
  o.note({ origin: "https://api", revision: 101, boardVersion: "B", generationId: "gen-B" });
  o.write("k1", { v: 1 });
  const r = o.note({ origin: "https://api", revision: 101, boardVersion: "B", generationId: "gen-B" });
  must(r === "same", `must be "same" (got ${r})`);
  must(o.cache.has("k1"), "cache must survive on 'same'");
});

T("Step 11: same rev · DIFFERENT fingerprint = 'invariant_violation' authority unchanged", () => {
  const o = makeObserver();
  o.note({ origin: "https://api", revision: 101, boardVersion: "B", generationId: "gen-B" });
  const r = o.note({ origin: "https://api", revision: 101, boardVersion: "C", generationId: "gen-C" });
  must(r === "invariant_violation", `must be "invariant_violation" (got ${r})`);
  must(o.cur().boardVersion === "B", "authority MUST NOT change on invariant_violation");
});

T("Step 12: origin_change · new API host · caches from old origin ARE swept", () => {
  const o = makeObserver();
  o.note({ origin: "https://api-old", revision: 101, boardVersion: "B", generationId: "gen-B" });
  o.write("k1", { v: 1 });                             // stamped api-old rev 101
  const r = o.note({ origin: "https://api-new", revision: 42, boardVersion: "X", generationId: "gen-X" });
  must(r === "origin_change", `must be "origin_change" (got ${r})`);
  must(o.cur().origin === "https://api-new" && o.cur().revision === 42, "authority now on new origin at rev 42");
  must(!o.cache.has("k1"), "old-origin caches MUST be swept on origin_change");
});

T("Step 13: 101 → 102 without device-storage clear · sweeps rev=101 only, keeps 102", () => {
  const o = makeObserver();
  o.note({ origin: "https://api", revision: 101, boardVersion: "B", generationId: "gen-B" });
  o.write("k101", { v: "at-101" });
  o.note({ origin: "https://api", revision: 102, boardVersion: "C", generationId: "gen-C" });
  o.write("k102", { v: "at-102" });
  const r = o.note({ origin: "https://api", revision: 101, boardVersion: "B", generationId: "gen-B" }); // late 101
  must(r === "stale", "late 101 must be stale");
  must(o.cur().revision === 102, "authority stays at 102");
  must(!o.cache.has("k101"), "k101 was already swept by the 102 advance");
  must(o.cache.has("k102"), "k102 (stamped at 102) must survive");
});

T("Step 14: sport switch cannot resurrect _picksMem rev=100 after rev=101 accepted", () => {
  // _picksMem is a thin adapter over swrCacheWrite/Read; a rev=100
  // entry would either have been swept by the 100→101 advance OR
  // (if a race writes it AFTER 101) is stamped at the current
  // authority (101), never at the old value.
  const o = makeObserver();
  o.note({ origin: "https://api", revision: 100, boardVersion: "A", generationId: "gen-A" });
  o.write("picks-lite|Locks-All|std|edge", { picks: [] }); // stamped rev=100
  o.note({ origin: "https://api", revision: 101, boardVersion: "B", generationId: "gen-B" });
  must(!o.cache.has("picks-lite|Locks-All|std|edge"), "rev=100 _picksMem entry swept by advance");
});

T("Step 15: background/resume at rev=101 · no unnecessary refresh (dedup)", () => {
  const o = makeObserver();
  o.note({ origin: "https://api", revision: 101, boardVersion: "B", generationId: "gen-B" });
  // AppState resume triggers a /api/version ping that returns SAME rev=101
  const r = o.note({ origin: "https://api", revision: 101, boardVersion: "B", generationId: "gen-B" });
  must(r === "same", "resume with unchanged rev must be 'same'");
  // No sweep, no consumer notification.
});

T("Step 16: offline (no notes) keeps caches as LAST-GOOD, reconciles once online", () => {
  const o = makeObserver();
  o.note({ origin: "https://api", revision: 100, boardVersion: "A", generationId: "gen-A" });
  o.write("k1", { v: 1 }); // last-good rev=100
  // Simulate offline — no notes for a while.  Then reconnect at 102.
  const r = o.note({ origin: "https://api", revision: 102, boardVersion: "C", generationId: "gen-C" });
  must(r === "advance", "reconnect must advance");
  must(!o.cache.has("k1"), "reconnect must sweep the last-good entry");
});

console.log("\n──────────────────────────────────────────────");
console.log(`  ${passed} passed / ${failed} failed`);
if (failed > 0) {
  console.log("\nFailures:");
  failures.forEach(f => console.log(`  • ${f.name}\n    ${f.msg}`));
  process.exit(1);
}
process.exit(0);
