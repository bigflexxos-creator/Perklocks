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
import { MY_BETS_KEY, PROFILE_STATS_KEY, parlayKey } from "@/src/lib/serverStateKeys";

let _preloaded = false;

const STAGE_B_DELAY_MS = 800;
const STAGE_C_DELAY_MS = 2500;

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

  // ── Stage C — later idle window ───────────────────────────────────
  const stageC = async () => {
    await _sleep(STAGE_C_DELAY_MS);
    // My Bets (5-way fan-out, but only if user visits the tab)
    (async () => {
      try {
        const [s, sp, mk, p, h] = await Promise.all([
          api.myAnalyticsSummary().catch(() => null),
          api.myAnalyticsBySport().catch(() => null),
          api.myAnalyticsByMarket().catch(() => null),
          api.listMyBets({ status: "pending", limit: 200 }).catch(() => null),
          api.myAnalyticsHistory(100).catch(() => null),
        ]);
        if (s || sp || mk || p || h) {
          swrCacheWrite(MY_BETS_KEY, { summary: s, bySport: sp, byMarket: mk, pending: p, history: h });
        }
      } catch { /* silent */ }
    })();
    // Profile
    (async () => {
      try {
        const res = await api.stats();
        if (res) swrCacheWrite(PROFILE_STATS_KEY, res);
      } catch { /* silent */ }
    })();
  };

  // Fire Stages B and C but do NOT await — the caller's boot flow
  // continues immediately (Locks paints unimpeded).
  void stageB();
  void stageC();
}
