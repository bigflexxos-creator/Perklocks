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

  // Additional primary tabs (parlay, my-bets, profile) can be seeded
  // here as their screens adopt useSWR. For now Rollover is the
  // canonical example.

  await Promise.allSettled([rolloverPromise]);
}
