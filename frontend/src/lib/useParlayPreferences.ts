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

const STORAGE_KEY = "@perkslocks/parlay-prefs-v1";

export type SportMode = "auto" | "custom" | "single";

export type ParlayPrefs = {
  mode: "standard" | "high_risk";
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
