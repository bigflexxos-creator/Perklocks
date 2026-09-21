/**
 * FRONTEND BUILD REVISION — hard-coded constant that advances with
 * every meaningful source change so the phone can PROVE which bundle
 * it is running.  Renders in the `/__diag` route below and in the
 * pick-detail header of the Northwestern @ Indiana canary.
 *
 * If the physical Expo Go session shows a revision string OLDER than
 * the value below, the device is on a stale JS bundle — no client
 * code change can help until the bundle is reloaded.
 */
export const FRONTEND_BUILD_REVISION = "2026-06-21-canonical-epoch-v2-signfix";

/**  Human-readable diff summary shipped with this bundle.  Present on
 *  the diag route so the phone can prove semantic parity even if the
 *  revision string alone is not distinctive enough. */
export const FRONTEND_BUILD_MANIFEST = {
  revision: "2026-06-21-canonical-epoch-v2-signfix",
  builtAt: new Date().toISOString(),
  features: [
    "canonicalEpoch.ts — ordered revision authority",
    "canonicalConsumers.ts — mounted-consumer registry",
    "useSWR.ts — epoch stamp + revision-ordered sweeper",
    "pick/[id].tsx — BOOK IMPLIED guarded (no undefined%)",
    "HistoricalIntelligence — YY M/D dates, _shortTeam mascot-safe short names",
    "Locks — PicksCache stamped, single-flight release",
  ] as string[],
};
