import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/src/lib/api";

type LiveGame = {
  home: string;
  away: string;
  home_score: number | null;
  away_score: number | null;
  status: string;
  abstract_status: string;
  is_live: boolean;
  is_final: boolean;
};

type LiveMap = Record<string, LiveGame>;

type Ctx = {
  /** Look up live state for a pick by its `event` ("Away @ Home") string. */
  lookup: (event: string | undefined | null) => LiveGame | null;
  /** Force-refresh the in-pod cache (rarely needed — auto-polls every 60 s). */
  refresh: () => void;
};

const MLBLiveContext = createContext<Ctx>({
  lookup: () => null,
  refresh: () => {},
});

const POLL_INTERVAL_MS = 60_000; // 60 s — matches MLB's pace of live changes

/**
 * Single source of truth for live MLB game state across the app.
 *
 * Fetches `/api/mlb/live` once and re-polls every 60 s. Inside the
 * backend there's already a 15-s in-memory cache + zero Odds API credit
 * cost, so this is essentially free. All Lock cards consume the data
 * via `useMLBLive()` → no per-card HTTP traffic.
 *
 * Polling pauses automatically when the app is backgrounded (browser
 * Page Visibility), and resumes immediately on focus.
 */
export function MLBLiveProvider({ children }: { children: React.ReactNode }) {
  const [games, setGames] = useState<LiveMap>({});
  const inflightRef = useRef<boolean>(false);

  const refresh = useCallback(async () => {
    if (inflightRef.current) return; // simple in-flight dedupe
    inflightRef.current = true;
    try {
      const res = await api.mlbLive();
      setGames(res.games || {});
    } catch (e) {
      // Silent — live badges are non-critical.
    } finally {
      inflightRef.current = false;
    }
  }, []);

  // Kick off + poll loop. Effect re-runs cleanup when the provider unmounts.
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setInterval> | null = null;
    const start = () => {
      if (!active) return;
      refresh();
      timer = setInterval(refresh, POLL_INTERVAL_MS);
    };
    const stop = () => {
      if (timer) {
        clearInterval(timer);
        timer = null;
      }
    };
    start();
    // Pause polling when the app/tab is backgrounded — saves battery
    // without losing freshness because we re-fetch immediately on focus.
    const onVisibility = () => {
      if (typeof document === "undefined") return;
      if (document.hidden) {
        stop();
      } else {
        start();
      }
    };
    if (typeof document !== "undefined" && document.addEventListener) {
      document.addEventListener("visibilitychange", onVisibility);
    }
    return () => {
      active = false;
      stop();
      if (typeof document !== "undefined" && document.removeEventListener) {
        document.removeEventListener("visibilitychange", onVisibility);
      }
    };
  }, [refresh]);

  const lookup = useCallback(
    (event: string | undefined | null): LiveGame | null => {
      if (!event) return null;
      return games[event] || null;
    },
    [games],
  );

  const value = useMemo<Ctx>(() => ({ lookup, refresh }), [lookup, refresh]);
  return (
    <MLBLiveContext.Provider value={value}>{children}</MLBLiveContext.Provider>
  );
}

/** Hook returning live state for a single pick. */
export function useMLBLive(event: string | undefined | null): LiveGame | null {
  const { lookup } = useContext(MLBLiveContext);
  return lookup(event);
}
