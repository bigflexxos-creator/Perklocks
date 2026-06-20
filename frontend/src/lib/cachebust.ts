/**
 * App-data cache buster.
 *
 * Reason this exists:
 *   Expo Go aggressively caches React state + AsyncStorage across launches.
 *   When the backend ships a content/format change (e.g. we scrub fabricated
 *   tennis stats out of pick explanations), the user's phone still happily
 *   renders the OLD pick payloads it cached the last time the app was opened.
 *   No amount of pull-to-refresh / shake-reload clears those stale objects
 *   because they live in AsyncStorage, not in the in-memory pick list.
 *
 * How it works:
 *   On app launch, `runCacheBustIfNeeded()` compares the baked-in
 *   `APP_DATA_VERSION` constant against the version we last wrote to
 *   AsyncStorage. If they differ, every AsyncStorage key the app owns is
 *   wiped, then the new version is written. Subsequent launches see the
 *   matching version and do nothing (zero perf cost).
 *
 * Bump `APP_DATA_VERSION` whenever you ship a server-side fix that needs
 * to flush client-cached pick data (e.g. removed fabricated stats, changed
 * pick schema, fixed odds math, etc.).
 */
import AsyncStorage from "@react-native-async-storage/async-storage";

// Bump on every server-side data shape / content change.
// Format: YYYYMMDD-N  so collisions are obvious in git history.
export const APP_DATA_VERSION = "20260620-1-hits";

const VERSION_KEY = "perkslocks.app_data_version";

// Every AsyncStorage key the app writes. Keep in sync with new caches.
const KNOWN_CACHE_KEYS = [
  "perkslocks.betslip.v1",          // BetSlipContext
  "perkslocks.parlay_prefs.v1",     // useParlayPreferences
];

export async function runCacheBustIfNeeded(): Promise<{ wiped: boolean; reason?: string }> {
  try {
    const stored = await AsyncStorage.getItem(VERSION_KEY);
    if (stored === APP_DATA_VERSION) return { wiped: false };
    // Mismatch (or first run) — purge every known cache key.
    await Promise.all(KNOWN_CACHE_KEYS.map((k) => AsyncStorage.removeItem(k)));
    await AsyncStorage.setItem(VERSION_KEY, APP_DATA_VERSION);
    return {
      wiped: true,
      reason: `data version: stored="${stored ?? "none"}" → current="${APP_DATA_VERSION}"`,
    };
  } catch (e) {
    // Worst case: AsyncStorage unavailable. Don't crash the app.
    console.warn("[cachebust] failed:", e);
    return { wiped: false };
  }
}
