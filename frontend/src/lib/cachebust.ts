/**
 * App-data cache buster — 3-layer defense against "ghost data on phone".
 *
 * THE PROBLEM
 * -----------
 * Expo Go aggressively caches the JS bundle, the React in-memory state, AND
 * AsyncStorage across launches. When the backend ships a content / format
 * change (e.g. scrubbed fabricated stats, fixed odds math, removed a sport,
 * new lock-score scale), the user's phone still happily renders the OLD
 * payloads it pinned the last time the app opened. No amount of pull-to-
 * refresh / shake-reload clears those stale objects because they live in
 * AsyncStorage, not in the in-memory pick list.
 *
 * THE FIX (3 LAYERS)
 * ------------------
 *  Layer 1  Manual nuke button — Profile screen calls `forceClearAllCaches()`.
 *           Wipes every AsyncStorage key the app owns and reloads. Used when
 *           the user explicitly wants to start clean.
 *
 *  Layer 2  Backend-driven cache bust — on app launch (and on Locks tab focus)
 *           the app fetches `/api/version` and compares the returned
 *           `data_version` against the version it last persisted. If they
 *           differ, every owned AsyncStorage key is wiped and the new version
 *           recorded. ANY server-side data change → ALL phones auto-wipe on
 *           next launch.
 *
 *  Layer 3  Client-baked cache bust — the legacy `APP_DATA_VERSION` constant
 *           still triggers a wipe on launch when bumped, for the rare case
 *           we ship a frontend-only data shape change.
 *
 * BUMPING VERSIONS
 * ----------------
 *  • Server-side data change → bump `DATA_VERSION` in /app/backend/server.py.
 *  • Frontend-only data change → bump `APP_DATA_VERSION` below.
 *  Either one will cause every phone to wipe on the next launch.
 */
import AsyncStorage from "@react-native-async-storage/async-storage";

// ─── Client-baked cache version (Layer 3) ───────────────────────────────────
// Bump on every CLIENT-side data shape / content change.
// Format: YYYYMMDD-N  so collisions are obvious in git history.
export const APP_DATA_VERSION = "20260716-dedupe-both-sides-v68";

// ─── Backend-version snapshot (Layer 2 - stored after each /api/version call)
const CLIENT_VERSION_KEY = "perkslocks.client_data_version";
const BACKEND_VERSION_KEY = "perkslocks.backend_data_version";

// ─── Every AsyncStorage key the app writes. Keep in sync with new caches. ──
const KNOWN_CACHE_KEYS = [
  "perkslocks.betslip.v1",          // BetSlipContext
  "perkslocks.parlay_prefs.v1",     // useParlayPreferences
  "locks_feed_prefs_v1",            // Home tab persisted sport / sortKey / lineType (legacy)
  "locks_feed_prefs_v2",            // Home tab persisted sport / sortKey / lineType (current)
  // ── Persisted filter store ──
  // useFilters.tsx schema-versioned key. Listing ALL historical versions
  // here so a cache bust wipes orphaned restrictive filter state from
  // any previous version of the app — critical for "Goalscorers showing
  // on web not app" where mobile had pre-fix v4 state that hid the new
  // CSL elites.
  "perkslocks_filters_v3",
  "perkslocks_filters_v4",
  "perkslocks_filters_v5",
  "perkslocks_filters_v6",
  // Add any new AsyncStorage keys here so a cache bust actually wipes them.
];

// Auth + the version keys themselves are intentionally NOT wiped — clearing
// auth would log the user out on every server bump, and clearing the version
// keys would defeat the purpose.

async function wipeKnownCaches(reason: string): Promise<void> {
  await Promise.all(KNOWN_CACHE_KEYS.map((k) => AsyncStorage.removeItem(k)));
  // eslint-disable-next-line no-console
  console.log("[cachebust] wiped", KNOWN_CACHE_KEYS.length, "keys —", reason);
}

/**
 * Layer 3 — runs on app launch. Compares baked-in `APP_DATA_VERSION` with the
 * version we last persisted; wipes the known caches if they differ.
 */
export async function runCacheBustIfNeeded(): Promise<{ wiped: boolean; reason?: string }> {
  try {
    const stored = await AsyncStorage.getItem(CLIENT_VERSION_KEY);
    if (stored === APP_DATA_VERSION) return { wiped: false };
    await wipeKnownCaches(`client version ${stored ?? "none"} → ${APP_DATA_VERSION}`);
    await AsyncStorage.setItem(CLIENT_VERSION_KEY, APP_DATA_VERSION);
    return {
      wiped: true,
      reason: `client data version: stored="${stored ?? "none"}" → current="${APP_DATA_VERSION}"`,
    };
  } catch (e) {
    // Worst case: AsyncStorage unavailable. Don't crash the app.
    console.warn("[cachebust] client check failed:", e);
    return { wiped: false };
  }
}

/**
 * Layer 2 — fetches `/api/version` and wipes caches if the backend reports a
 * newer data_version than the one we last saw. Safe to call on app launch
 * and on tab focus; network failures are silent.
 */
export async function runBackendCacheBustIfNeeded(
  fetchBackendVersion: () => Promise<string | null>,
): Promise<{ wiped: boolean; backendVersion: string | null; reason?: string }> {
  try {
    const backendVersion = await fetchBackendVersion();
    if (!backendVersion) return { wiped: false, backendVersion: null };
    const stored = await AsyncStorage.getItem(BACKEND_VERSION_KEY);
    if (stored === backendVersion) return { wiped: false, backendVersion };
    await wipeKnownCaches(`backend version ${stored ?? "none"} → ${backendVersion}`);
    await AsyncStorage.setItem(BACKEND_VERSION_KEY, backendVersion);
    return {
      wiped: true,
      backendVersion,
      reason: `backend data version: stored="${stored ?? "none"}" → current="${backendVersion}"`,
    };
  } catch (e) {
    console.warn("[cachebust] backend check failed:", e);
    return { wiped: false, backendVersion: null };
  }
}

/**
 * Layer 1 — manual nuke. Used by the "Refresh App Data" button in Profile.
 * Wipes every owned AsyncStorage key (including version snapshots so the next
 * launch re-records them) but leaves auth alone so the user isn't logged out.
 */
export async function forceClearAllCaches(): Promise<void> {
  try {
    await wipeKnownCaches("manual force-clear");
    // Also wipe the version snapshots so the next launch fully re-syncs.
    await AsyncStorage.removeItem(CLIENT_VERSION_KEY);
    await AsyncStorage.removeItem(BACKEND_VERSION_KEY);
  } catch (e) {
    console.warn("[cachebust] force clear failed:", e);
  }
}

/**
 * Quiet helper for the StaleVersionBanner — returns the currently-stored
 * backend version (without mutating anything). The banner uses this to
 * decide whether to show.
 */
export async function getStoredBackendVersion(): Promise<string | null> {
  try {
    return await AsyncStorage.getItem(BACKEND_VERSION_KEY);
  } catch {
    return null;
  }
}
