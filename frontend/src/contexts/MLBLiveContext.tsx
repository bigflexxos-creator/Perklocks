import React, { createContext, useCallback, useEffect, useMemo, useRef, useSyncExternalStore } from "react";
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

// ── EXTERNAL STORE (native performance closure) ────────────────────────
// The 60-second poll used to call setGames() → new context value → every
// mounted Lock card re-rendered (context bypasses React.memo).  Live
// state now lives in a module-level store; each MLB card subscribes ONLY
// to its own event through a keyed selector (useSyncExternalStore) and
// re-renders only when ITS LiveGame actually changed.  Non-MLB cards pass
// a null event → constant null snapshot → never re-render from polling.
const _store: { games: LiveMap; listeners: Set<() => void> } = { games: {}, listeners: new Set() };

function _publish(next: LiveMap): void {
  _store.games = next || {};
  _store.listeners.forEach((l) => { try { l(); } catch { /* listener error must not break polling */ } });
}
function _subscribe(l: () => void): () => void {
  _store.listeners.add(l);
  return () => { _store.listeners.delete(l); };
}
function _sameGame(a: LiveGame | null, b: LiveGame | null): boolean {
  if (a === b) return true;
  if (!a || !b) return false;
  return a.home_score === b.home_score && a.away_score === b.away_score && a.status === b.status
    && a.abstract_status === b.abstract_status && a.is_live === b.is_live && a.is_final === b.is_final
    && a.home === b.home && a.away === b.away && (a.commence_time || null) === (b.commence_time || null);
}

function _lookupIn(games: LiveMap, event: string | undefined | null, eventTime?: string | null): LiveGame | null {
  if (!event) return null;
  const sameScheduledGame = (g: LiveGame | undefined | null): boolean => {
    if (!g) return false;
    if (!eventTime || typeof eventTime !== "string" || eventTime.length < 10) return false;
    const pickTs = Date.parse(eventTime);
    const liveTs = Date.parse(g.commence_time || "");
    if (!Number.isFinite(pickTs) || !Number.isFinite(liveTs)) return false;
    return Math.abs(liveTs - pickTs) <= 6 * 3600 * 1000;
  };
  if (eventTime && typeof eventTime === "string" && eventTime.length >= 10) {
    const dated = games[`${event}|${eventTime.slice(0, 10)}`];
    if (dated && sameScheduledGame(dated)) return dated;
  }
  const g = games[event] || null;
  if (g && sameScheduledGame(g)) return g;
  return null;
}

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
      _publish(res.games || {});
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
      _publish({});
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
    (event: string | undefined | null, eventTime?: string | null): LiveGame | null =>
      _lookupIn(_store.games, event, eventTime),
    [],
  );

  const value = useMemo<Ctx>(() => ({ lookup, refresh }), [lookup, refresh]);
  return (
    <MLBLiveContext.Provider value={value}>{children}</MLBLiveContext.Provider>
  );
}

/** Hook returning live state for a single pick — keyed subscription.
 *  Re-renders ONLY when this event's LiveGame changes; a null `event`
 *  (non-MLB card) yields a constant null and never re-renders from polling.
 *  Pass `eventTime` (the pick's ISO commence string) to ensure a multi-game
 *  series doesn't leak yesterday's FINAL onto tomorrow's matching card. */
export function useMLBLive(
  event: string | undefined | null,
  eventTime?: string | null,
): LiveGame | null {
  const lastRef = useRef<LiveGame | null>(null);
  const getSnapshot = useCallback((): LiveGame | null => {
    if (!event) { lastRef.current = null; return null; }
    const next = _lookupIn(_store.games, event, eventTime);
    if (_sameGame(lastRef.current, next)) return lastRef.current;   // stable identity → no re-render
    lastRef.current = next;
    return next;
  }, [event, eventTime]);
  return useSyncExternalStore(event ? _subscribe : _noopSubscribe, getSnapshot, getSnapshot);
}

const _noopSubscribe = () => () => {};
