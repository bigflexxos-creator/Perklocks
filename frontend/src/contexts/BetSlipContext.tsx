import React, { createContext, useContext, useEffect, useRef, useState, ReactNode, useCallback } from "react";
import AsyncStorage from "@react-native-async-storage/async-storage";
import { Pick, api } from "@/src/lib/api";

const SLIP_KEY = "perkslocks.betslip.v1";
export const MAX_SLIP_SIZE = 25;

type AddResult = { ok: boolean; reason?: string };

type BetSlipState = {
  picks: Pick[];
  addPick: (pick: Pick) => AddResult;
  removePick: (id: string) => void;
  clear: () => void;
  has: (id: string) => boolean;
  count: number;
  hydrated: boolean;
};

const SlipCtx = createContext<BetSlipState | undefined>(undefined);

// Pick objects can be large (factors, insights, explanations). We use
// AsyncStorage (not SecureStore) because bet slip state is non-sensitive
// and SecureStore caps values at ~2KB on iOS — 25 enriched picks blow past
// that easily.
export function BetSlipProvider({ children }: { children: ReactNode }) {
  const [picks, setPicks] = useState<Pick[]>([]);
  const [hydrated, setHydrated] = useState(false);
  const hydratedRef = useRef(false);

  // Hydrate from storage on mount, THEN immediately fetch fresh server data
  // for every cached pick. This guarantees that stale explanations / insights
  // / odds baked into the local copy when the pick was originally added (e.g.
  // the old fabricated tennis records) are replaced with the current truth.
  // Picks no longer in the backend (yesterday's slate, since removed) are
  // silently dropped from the slip.
  useEffect(() => {
    (async () => {
      let initial: Pick[] = [];
      try {
        const raw = await AsyncStorage.getItem(SLIP_KEY);
        if (raw) {
          const parsed = JSON.parse(raw);
          if (Array.isArray(parsed)) initial = parsed as Pick[];
        }
      } catch (e) {
        console.warn("[BetSlip] hydrate failed", e);
      }
      // Show the cached picks immediately so the UI isn't blank.
      if (initial.length > 0) setPicks(initial);
      hydratedRef.current = true;
      setHydrated(true);

      // Refresh each cached pick against the live backend.
      if (initial.length === 0) return;
      try {
        const fresh = await Promise.all(
          initial.map(async (p) => {
            try {
              const live = await api.pickDetail(p.id);
              return live as Pick;
            } catch {
              return null;  // pick gone from backend — drop it
            }
          }),
        );
        const refreshed = fresh.filter((p): p is Pick => p !== null);
        // Detect ANY user-visible drift between the cached and the
        // live payload — not just id/explanation. Bug history: a
        // previous version of this diff only compared `id` and
        // `explanation`, so when the same pick_id was re-keyed to a
        // different market label by the generator (e.g. "Tyra Grant
        // -1.5 Spread" rotated to "Tyra Grant Over 17.0 Games (Alt)"
        // when the Odds API stopped exposing the spread market for
        // that match), `changed` evaluated false and the slip kept
        // showing the stale label forever. Comparing all display-
        // relevant fields makes the slip always reflect the
        // backend's current truth.
        const changed = refreshed.length !== initial.length ||
          refreshed.some((p, i) => {
            const old = initial[i];
            if (!old || p.id !== old.id) return true;
            return (
              p.market         !== old.market          ||
              p.book_odds      !== old.book_odds       ||
              p.lock_score     !== old.lock_score      ||
              p.win_probability !== old.win_probability ||
              p.bet            !== old.bet             ||
              p.selection      !== old.selection       ||
              p.explanation    !== old.explanation
            );
          });
        if (changed) setPicks(refreshed);
      } catch (e) {
        console.warn("[BetSlip] refresh-on-hydrate failed", e);
      }
    })();
  }, []);

  // Persist after hydration so we don't overwrite saved state with the
  // initial empty array during the first render pass.
  useEffect(() => {
    if (!hydratedRef.current) return;
    AsyncStorage.setItem(SLIP_KEY, JSON.stringify(picks)).catch((e) =>
      console.warn("[BetSlip] persist failed", e),
    );
  }, [picks]);

  const has = useCallback((id: string) => picks.some((p) => p.id === id), [picks]);

  const addPick = useCallback((pick: Pick): AddResult => {
    if (picks.some((p) => p.id === pick.id)) {
      return { ok: false, reason: "Already in your slip" };
    }
    if (picks.length >= MAX_SLIP_SIZE) {
      return { ok: false, reason: `Slip is full (max ${MAX_SLIP_SIZE} legs)` };
    }
    setPicks((prev) => [...prev, pick]);
    return { ok: true };
  }, [picks]);

  const removePick = useCallback((id: string) => {
    setPicks((prev) => prev.filter((p) => p.id !== id));
  }, []);

  const clear = useCallback(() => setPicks([]), []);

  return (
    <SlipCtx.Provider
      value={{
        picks,
        addPick,
        removePick,
        clear,
        has,
        count: picks.length,
        hydrated,
      }}
    >
      {children}
    </SlipCtx.Provider>
  );
}

export function useBetSlip(): BetSlipState {
  const v = useContext(SlipCtx);
  if (!v) throw new Error("useBetSlip must be inside BetSlipProvider");
  return v;
}

// Combined parlay math for a given slip (American → decimal product → back to American).
export function computeParlay(picks: Pick[]): {
  legCount: number;
  decimalOdds: number;
  americanOdds: string;
  payoutOn100: number;
  profitOn100: number;
} {
  if (!picks.length) {
    return { legCount: 0, decimalOdds: 1, americanOdds: "+0", payoutOn100: 0, profitOn100: 0 };
  }
  const toDecimal = (american: number) =>
    american >= 0 ? 1 + american / 100 : 1 + 100 / -american;
  const combined = picks.reduce((acc, p) => acc * toDecimal(p.book_odds), 1);
  const toAmerican = (dec: number) => {
    if (dec >= 2) return `+${Math.round((dec - 1) * 100)}`;
    return `${Math.round(-100 / (dec - 1))}`;
  };
  const payout = combined * 100;
  return {
    legCount: picks.length,
    decimalOdds: combined,
    americanOdds: toAmerican(combined),
    payoutOn100: Number(payout.toFixed(2)),
    profitOn100: Number((payout - 100).toFixed(2)),
  };
}
