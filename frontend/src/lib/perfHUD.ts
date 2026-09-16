/**
 * PERKLOCKS · DEV-only Performance Instrumentation
 * ──────────────────────────────────────────────────────────────────
 * Bounded ring-buffer that captures user-action → paint timings on
 * the client.  DEV-only surface — the sample store lives at module
 * scope so it survives navigation and can be inspected from the HUD
 * component or via `getPerfSnapshot()` from a debug menu.
 *
 * Callers instrument the pipeline like:
 *
 *   const t = perf.startMark("locks.load");
 *   ...
 *   const r = await api.picksToday(...);
 *   t.step("response");
 *   ...commit...
 *   t.step("commit");
 *   t.end({ n: picks.length });
 *
 * Steps between start() and end() are captured as sub-samples so we
 * can measure the FULL user-action → request-start → response →
 * parse → state-commit → React-commit → FIRST paint pipeline.
 *
 * Zero cost when `__DEV__` is false: startMark returns a no-op stub
 * so production bundles pay nothing.
 */

type Sample = {
  label:  string;
  ms:     number;
  meta?:  Record<string, unknown>;
  ts:     number;
};

const RING_CAP = 200;
const _samples: Sample[] = [];
const _now = () => (typeof performance !== "undefined" && performance.now
  ? performance.now()
  : Date.now());

function _push(s: Sample) {
  _samples.push(s);
  if (_samples.length > RING_CAP) _samples.splice(0, _samples.length - RING_CAP);
}

export type PerfHandle = {
  step: (name: string, meta?: Record<string, unknown>) => void;
  end:  (meta?: Record<string, unknown>) => number;
};

const _NOOP: PerfHandle = { step: () => {}, end: () => 0 };

/** Start a new mark.  In production ((__DEV__ === false) it's a
 *  zero-cost no-op that satisfies the same interface. */
export function startMark(label: string, meta?: Record<string, unknown>): PerfHandle {
  if (typeof __DEV__ !== "undefined" && !__DEV__) return _NOOP;
  const t0 = _now();
  let lastStep = t0;
  return {
    step: (name: string, sMeta?: Record<string, unknown>) => {
      const t = _now();
      _push({ label: `${label}.${name}`, ms: t - lastStep, meta: sMeta, ts: t });
      lastStep = t;
    },
    end: (eMeta?: Record<string, unknown>) => {
      const t = _now();
      const total = t - t0;
      _push({ label, ms: total, meta: { ...(meta || {}), ...(eMeta || {}) }, ts: t });
      // eslint-disable-next-line no-console
      if (typeof console !== "undefined") {
        console.log(`[perf] ${label} total=${total.toFixed(1)}ms`, eMeta || "");
      }
      return total;
    },
  };
}

/** Percentile over samples in the ring for a given label. */
function _percentile(label: string, p: number): number | null {
  const xs = _samples.filter((s) => s.label === label).map((s) => s.ms);
  if (xs.length === 0) return null;
  xs.sort((a, b) => a - b);
  const idx = Math.min(xs.length - 1, Math.floor(p * (xs.length - 1)));
  return xs[idx];
}

export function p50(label: string): number | null { return _percentile(label, 0.5); }
export function p95(label: string): number | null { return _percentile(label, 0.95); }

/** Aggregate snapshot for the HUD.  DEV-only — safe to call anywhere. */
export function getPerfSnapshot(): {
  labels: string[];
  rows:   Array<{ label: string; n: number; p50: number | null; p95: number | null; last: number | null }>;
} {
  const byLabel = new Map<string, number[]>();
  for (const s of _samples) {
    if (!byLabel.has(s.label)) byLabel.set(s.label, []);
    byLabel.get(s.label)!.push(s.ms);
  }
  const rows = [...byLabel.entries()].map(([label, xs]) => {
    const sorted = [...xs].sort((a, b) => a - b);
    return {
      label,
      n:    xs.length,
      p50:  sorted[Math.floor(0.5 * (sorted.length - 1))] ?? null,
      p95:  sorted[Math.floor(0.95 * (sorted.length - 1))] ?? null,
      last: xs[xs.length - 1] ?? null,
    };
  }).sort((a, b) => a.label.localeCompare(b.label));
  return { labels: [...byLabel.keys()], rows };
}

/** Clear ring — useful for scoped runs. */
export function resetPerfSnapshot(): void { _samples.length = 0; }

// ── FPS meter (rAF-driven, DEV-only) ─────────────────────────────
// Runs when startFpsMeter() is called.  Records a rolling 1-second
// FPS sample into the ring so the HUD can pull `perf.fps` p50/p95.
let _fpsRunning = false;
let _fpsRaf: any = null;
export function startFpsMeter(): void {
  if (_fpsRunning) return;
  if (typeof __DEV__ !== "undefined" && !__DEV__) return;
  if (typeof requestAnimationFrame === "undefined") return;
  _fpsRunning = true;
  let last = _now();
  let frames = 0;
  const tick = () => {
    if (!_fpsRunning) return;
    frames += 1;
    const t = _now();
    if (t - last >= 1000) {
      const fps = (frames * 1000) / (t - last);
      _push({ label: "perf.fps", ms: fps, ts: t });
      last = t; frames = 0;
    }
    _fpsRaf = requestAnimationFrame(tick);
  };
  _fpsRaf = requestAnimationFrame(tick);
}
export function stopFpsMeter(): void {
  _fpsRunning = false;
  if (_fpsRaf && typeof cancelAnimationFrame !== "undefined") {
    try { cancelAnimationFrame(_fpsRaf); } catch {}
  }
  _fpsRaf = null;
}

/** Called by the Locks FlatList onViewableItemsChanged so we can
 *  measure the mounted-card count and record the last render commit.  */
export function recordMountedRowCount(n: number): void {
  if (typeof __DEV__ !== "undefined" && !__DEV__) return;
  _push({ label: "list.mounted_rows", ms: n, ts: _now() });
}
export function recordRerender(component: string): void {
  if (typeof __DEV__ !== "undefined" && !__DEV__) return;
  _push({ label: `render.${component}`, ms: 1, ts: _now() });
}

export default { startMark, p50, p95, getPerfSnapshot, resetPerfSnapshot, startFpsMeter, stopFpsMeter, recordMountedRowCount, recordRerender };
