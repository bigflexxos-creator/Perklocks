/**
 * Primary-tab preloader — μ-closure P3 (2026-06).
 *
 * Fires background fetches for the highest-value primary tabs on
 * authenticated app boot and seeds the SWR cache. Subsequent first
 * visits to those tabs paint instantly instead of showing the
 * cold skeleton.
 *
 * Contract:
 *  • Fire-and-forget — failures are silent (screens will do their
 *    own load on visit).
 *  • Runs at most ONCE per app session (idempotent guard).
 *  • Only runs after a user is authenticated (auth-scoped data).
 *  • Bounded concurrency: all fetches run in parallel via Promise.all
 *    on Promise.allSettled to prevent one slow tab from blocking the
 *    others.
 */
import { api } from "@/src/lib/api";
import { swrCacheWrite } from "@/src/lib/useSWR";
import { MY_BETS_KEY, PROFILE_STATS_KEY, parlayKey } from "@/src/lib/serverStateKeys";

let _preloaded = false;

export function resetPrimaryTabPreload(): void {
  _preloaded = false;
}

export async function preloadPrimaryTabs(): Promise<void> {
  if (_preloaded) return;
  _preloaded = true;

  // Rollover default view (both lines, all sports, no filters).
  const rolloverKey = `rollover|both|All|{}`;
  const rolloverPromise = (async () => {
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
    } catch { /* silent — screen will retry on visit */ }
  })();

  // P1 · Parlay summary — SAME key the Parlay screen consumes (the user's
  // persisted prefs + rank 1, no locks, nonce 0).
  const parlayPromise = (async () => {
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

  // P1 · My Bets snapshot — same shape/key as my-bets.tsx.
  const myBetsPromise = (async () => {
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

  // P1 · Profile summary — shared with the Locks header stats.
  const profilePromise = (async () => {
    try {
      const res = await api.stats();
      if (res) swrCacheWrite(PROFILE_STATS_KEY, res);
    } catch { /* silent */ }
  })();

  // One failed prefetch never blocks the others.
  await Promise.allSettled([rolloverPromise, parlayPromise, myBetsPromise, profilePromise]);
}
