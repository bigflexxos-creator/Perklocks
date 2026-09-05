/**
 * useParlayPreferences — persist & restore the user's parlay screen prefs
 * across app launches via AsyncStorage.
 *
 * Persisted fields:
 *   - mode          standard | high_risk
 *   - legs          target leg count
 *   - sport         "mix" | "MLB" | "NBA" | ...
 *   - lineType      both | main | alt
 *   - excludedSports  string[]
 *   - filters       PickFilters
 *   - preferredBook   user's go-to sportsbook (DraftKings / FanDuel / etc.)
 *
 * NOT persisted: rank, lockedIds (these are ephemeral per-session).
 */
import AsyncStorage from "@react-native-async-storage/async-storage";
import { useEffect, useRef, useState } from "react";
import type { LineType, PickFilters } from "@/src/lib/api";
import type { SportsbookId } from "@/src/lib/sportsbookLinks";

// ── MAIN 40 (SDK 57 upgrade, 2026-06-05) — versioned migration ──
// SDK 54 clients wrote to `@perkslocks/parlay-prefs-v1`.  When the
// user upgraded to SDK 57 (via Expo Go's SDK 57 client) that stored
// snapshot hydrated into the new client and could carry incompatible
// transient fields (e.g. an obsolete `mode`, a stale `sport` bound
// to yesterday's slate, or an obsolete `windowHours` value that
// SDK 54 supported).  The user reported the Parlay tab broken on
// native while Web/Preview still worked — Web has no persisted
// storage, hence no contamination.
//
// Fix: bump the storage key to `-v2` so SDK 57 clients read `null`
// on first launch.  If a legacy `-v1` snapshot exists we salvage
// the DURABLE user preferences (`preferredBook`, `filters.minLock`)
// and drop transient scope fields (`mode`, `advancedSub`, `sport`,
// `sportMode`, `windowHours`, `includedSports`, `excludedSports`,
// `legs`, `lineType`).  The legacy `-v1` key stays wiped only by
// the cachebust; we do NOT delete it here so a downgrade path can
// still read its own state.
const STORAGE_KEY = "@perkslocks/parlay-prefs-v2";
const LEGACY_STORAGE_KEY_V1 = "@perkslocks/parlay-prefs-v1";

export type SportMode = "auto" | "custom" | "single";

export type ParlayMode = "standard" | "high_risk" | "today_window" | "advanced";
export type AdvancedSub = "safer" | "ev";

export type ParlayPrefs = {
  mode: ParlayMode;
  advancedSub?: AdvancedSub;
  legs: number;
  sport: string;
  lineType: LineType;
  excludedSports: string[];
  includedSports: string[];
  sportMode: SportMode;
  windowHours: number;
  filters: PickFilters;
  preferredBook: SportsbookId | null;
};

export const DEFAULT_PARLAY_PREFS: ParlayPrefs = {
  mode: "standard",
  legs: 3,
  sport: "mix",
  lineType: "both",
  excludedSports: [],
  includedSports: [],
  sportMode: "auto",
  windowHours: 24,
  filters: {},
  preferredBook: null,
};

export function useParlayPreferences() {
  const [prefs, setPrefs] = useState<ParlayPrefs>(DEFAULT_PARLAY_PREFS);
  const [hydrated, setHydrated] = useState(false);
  const hydrationGuard = useRef(false);

  // Hydrate from AsyncStorage once on mount
  useEffect(() => {
    (async () => {
      try {
        const raw = await AsyncStorage.getItem(STORAGE_KEY);
        if (raw) {
          const parsed = JSON.parse(raw);
          setPrefs({ ...DEFAULT_PARLAY_PREFS, ...parsed });
        } else {
          // ── MAIN 40 · SDK 57 one-time migration from -v1 → -v2 ──
          // Salvage durable prefs, reset transient scope so a stale
          // sport / mode / window from the SDK 54 build cannot break
          // the SDK 57 Parlay tab.
          try {
            const legacyRaw = await AsyncStorage.getItem(LEGACY_STORAGE_KEY_V1);
            if (legacyRaw) {
              const legacy = JSON.parse(legacyRaw) || {};
              const salvaged: Partial<ParlayPrefs> = {};
              if (legacy.preferredBook != null) {
                salvaged.preferredBook = legacy.preferredBook;
              }
              // Only carry the minLock durable numeric floor — nothing
              // else from the SDK 54 filters shape is guaranteed to
              // stay wire-compatible with the current PickFilters type.
              if (
                legacy.filters &&
                typeof legacy.filters === "object" &&
                typeof (legacy.filters as any).minLock === "number"
              ) {
                salvaged.filters = { minLock: (legacy.filters as any).minLock } as PickFilters;
              }
              // Reset every transient scope field to DEFAULT_PARLAY_PREFS.
              const migrated: ParlayPrefs = { ...DEFAULT_PARLAY_PREFS, ...salvaged };
              setPrefs(migrated);
              // Seed -v2 so subsequent launches read v2 directly and
              // skip this migration branch.
              await AsyncStorage.setItem(STORAGE_KEY, JSON.stringify(migrated));
              console.log("[useParlayPreferences] migrated v1 → v2 (SDK 57)");
            }
          } catch (mErr) {
            console.warn("parlay prefs v1→v2 migration failed", mErr);
          }
        }
      } catch (e) {
        console.warn("parlay prefs hydrate failed", e);
      } finally {
        hydrationGuard.current = true;
        setHydrated(true);
      }
    })();
  }, []);

  // Persist on change (after hydration completes — avoid stomping on stored
  // value with the empty default during the first render).
  useEffect(() => {
    if (!hydrationGuard.current) return;
    AsyncStorage.setItem(STORAGE_KEY, JSON.stringify(prefs)).catch((e) => {
      console.warn("parlay prefs persist failed", e);
    });
  }, [prefs]);

  // Convenience partial setter
  const updatePrefs = (patch: Partial<ParlayPrefs>) =>
    setPrefs((prev) => ({ ...prev, ...patch }));

  return { prefs, updatePrefs, hydrated };
}
