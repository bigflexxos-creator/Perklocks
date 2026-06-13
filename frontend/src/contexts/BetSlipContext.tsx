import React, { createContext, useContext, useEffect, useRef, useState, ReactNode, useCallback } from "react";
import AsyncStorage from "@react-native-async-storage/async-storage";
import { Pick } from "@/src/lib/api";

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

  // Hydrate from storage on mount.
  useEffect(() => {
    (async () => {
      try {
        const raw = await AsyncStorage.getItem(SLIP_KEY);
        if (raw) {
          const parsed = JSON.parse(raw);
          if (Array.isArray(parsed)) setPicks(parsed as Pick[]);
        }
      } catch (e) {
        console.warn("[BetSlip] hydrate failed", e);
      } finally {
        hydratedRef.current = true;
        setHydrated(true);
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
