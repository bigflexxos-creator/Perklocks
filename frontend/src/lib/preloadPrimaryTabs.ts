/**
 * Primary-tab preloader — μ-closure P3 (2026-06).
 *
 * GATE 2 P0 (2026-06): STAGED priority to eliminate startup competition
 * with the first-useful-Locks-paint.
 *
 *   Stage A (0 ms)      Locks tab own load runs on the index screen.
 *   Stage B (+800 ms)   Rollover + Parlay warm.
 *   Stage C (+2500 ms)  My Bets + Profile warm.
 *
 * Rationale: on physical iPhone the four concurrent auth-boot fetches
 * saturated the JS thread and stretched TTFB on the FIRST /picks/today
 * call by 300–900 ms.  Staging shifts them out of the critical path
 * while keeping the warm-tab benefit for subsequent visits.
 *
 * Contract remains:
 *  • Fire-and-forget — failures are silent.
 *  • At most ONCE per app session (idempotent guard).
 *  • Only after user authenticated.
 */
import { api } from "@/src/lib/api";
import { swrCacheWrite } from "@/src/lib/useSWR";
import { parlayKey } from "@/src/lib/serverStateKeys";

let _preloaded = false;

const STAGE_B_DELAY_MS = 800;

const _sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

export function resetPrimaryTabPreload(): void {
  _preloaded = false;
}

export async function preloadPrimaryTabs(): Promise<void> {
  if (_preloaded) return;
  _preloaded = true;

  // ── Stage B — after Locks has grabbed its request slot ────────────
  const rolloverKey = `rollover|both|All|{}`;
  const stageB = async () => {
    await _sleep(STAGE_B_DELAY_MS);
    // Rollover
    (async () => {
      try {
        const res = await api.rollover("both", {}, "All");
        const arr = (res.picks && res.picks.length > 0)
          ? res.picks
          : (res.pick ? [res.pick] : []);
        const picks = arr.filter((p: any) => p.sport !== "KBO");
        swrCacheWrite(rolloverKey, {
          picks,
          pool: res.total_evaluated ?? 0,
          survivability: (res as any).survivability ?? null,
        });
      } catch { /* silent */ }
    })();
    // Parlay
    (async () => {
      try {
        const AsyncStorage = (await import("@react-native-async-storage/async-storage")).default;
        const { DEFAULT_PARLAY_PREFS, STORAGE_KEY } = await import("@/src/lib/useParlayPreferences");
        const raw = await AsyncStorage.getItem(STORAGE_KEY);
        const prefs = { ...DEFAULT_PARLAY_PREFS, ...(raw ? JSON.parse(raw) : {}) };
        const key = parlayKey(prefs.legs, prefs.mode, prefs.sport, prefs.lineType, prefs.includedSports,
                              prefs.excludedSports, prefs.filters, 1, [], prefs.sportMode, prefs.windowHours, 0, undefined);
        const res = await api.parlay(prefs.legs, prefs.mode, prefs.sport, prefs.lineType, prefs.includedSports,
                                     prefs.filters, 1, [], prefs.sportMode, prefs.windowHours, prefs.excludedSports, 0, undefined);
        const parlays = (res as any).parlays || ((res as any).parlay ? [(res as any).parlay] : []);
        swrCacheWrite(key, { parlays, reason: (res as any).reason || "" });
      } catch { /* silent */ }
    })();
  };

  // ── 2026-06-28 · P0-B STAGE-C REMOVED (demand-only loading) ─────
  // Previously fired a 5-way My Bets fan-out + Profile warm on every
  // app launch even when the user never visited those tabs — pure
  // waste.  Both surfaces now load lazily on first navigation
  // (standard SWR path).  Stage B (Rollover + Parlay) is retained
  // because those are high-engagement companion tabs to Locks.

  // Fire Stage B only; Stage C is intentionally no-op.
  void stageB();
}
