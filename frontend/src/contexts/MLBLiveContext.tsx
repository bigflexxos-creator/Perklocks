import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/src/lib/api";
import { useAuth } from "@/src/contexts/AuthContext";

type LiveGame = {
  home: string;
  away: string;
  home_score: number | null;
  away_score: number | null;
  status: string;
  abstract_status: string;
  is_live: boolean;
  is_final: boolean;
  /** ISO UTC commence time of THIS scheduled game — used to verify the live
   *  badge attaches to the right game in a multi-game series. */
  commence_time?: string | null;
};

type LiveMap = Record<string, LiveGame>;

type Ctx = {
  /** Look up live state for a pick by its `event` ("Away @ Home") string +
   *  optional `eventTime` (ISO UTC) so a card never shows yesterday's
   *  FINAL score on tomorrow's matchup. */
  lookup: (event: string | undefined | null, eventTime?: string | null) => LiveGame | null;
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
  // Only fetch /api/mlb/live once the user is authenticated. Otherwise the
  // mount-time poll fires before AsyncStorage has hydrated the token and the
  // backend returns 401, flooding the logs and ESPN-style retry loops.
  const { user } = useAuth();
  const isAuthed = !!user;

  const refresh = useCallback(async () => {
    if (!isAuthed) return;          // hard gate — no auth, no call
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
  }, [isAuthed]);

  // Kick off + poll loop. Effect re-runs cleanup when the provider unmounts.
  useEffect(() => {
    if (!isAuthed) {
      // Make sure stale data from a previous session isn't shown to the
      // next anonymous user (e.g. after sign-out).
      setGames({});
      return;
    }
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
  }, [refresh, isAuthed]);

  const lookup = useCallback(
    (event: string | undefined | null, eventTime?: string | null): LiveGame | null => {
      if (!event) return null;
      // 1) Try the dated key first — guarantees we get the right game
      //    when teams play a multi-game series (Thu + Fri + Sat all
      //    "Reds @ Yankees" — yesterday's FINAL must NOT leak onto
      //    tomorrow's card).
      if (eventTime && typeof eventTime === "string" && eventTime.length >= 10) {
        const dated = games[`${event}|${eventTime.slice(0, 10)}`];
        if (dated) return dated;
      }
      // 2) Fall back to the bare event key (back-compat for callers
      //    that don't pass event_time).
      const g = games[event] || null;
      // Even with bare key, defensively check that the dates align —
      // if the live game's commence date doesn't match the pick's date,
      // suppress the badge instead of showing a misleading FINAL.
      if (g && eventTime && typeof eventTime === "string" && eventTime.length >= 10) {
        const liveDate = (g.commence_time || "").slice(0, 10);
        const pickDate = eventTime.slice(0, 10);
        if (liveDate && liveDate !== pickDate) return null;
      }
      return g;
    },
    [games],
  );

  const value = useMemo<Ctx>(() => ({ lookup, refresh }), [lookup, refresh]);
  return (
    <MLBLiveContext.Provider value={value}>{children}</MLBLiveContext.Provider>
  );
}

/** Hook returning live state for a single pick.
 *  Pass `eventTime` (the pick's ISO commence string) to ensure a multi-game
 *  series doesn't leak yesterday's FINAL onto tomorrow's matching card. */
export function useMLBLive(
  event: string | undefined | null,
  eventTime?: string | null,
): LiveGame | null {
  const { lookup } = useContext(MLBLiveContext);
  return lookup(event, eventTime);
}
