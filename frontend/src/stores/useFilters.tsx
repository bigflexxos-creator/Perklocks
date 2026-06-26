/**
 * useFilters — Unified, multi-select filter store with AsyncStorage persistence.
 *
 * Design goals (locked from user feedback):
 *   • Multi-select arrays for every category — sports, leagues, markets, gameIds.
 *   • Persistent across app restarts (AsyncStorage, hydration on mount).
 *   • Cross-tab — a single global store all screens read & write to.
 *   • Additive — toggling a chip adds/removes it from the array, never replaces.
 *   • Tabs control VIEW only, not filter state.
 *
 * Architecture:
 *   • One React Context provider at the app root (wrapped in app/_layout.tsx).
 *   • `useFilters()` hook gives any screen the same filter snapshot + actions.
 *   • Selector helpers (`useFilterCount`, `useIsActive`) avoid full re-renders.
 *
 * Backward-compat:
 *   • The store exposes `toQueryParams()` which CSV-serialises arrays for the
 *     backend. Backend endpoints accept the legacy singular params AND the
 *     new array params, so we can roll this out without flag-day churn.
 *
 * Why not Zustand: no extra dep, the codebase already uses Context + custom
 * hooks elsewhere, and the store surface is small (one reducer).
 */
import React, {
  createContext, useCallback, useContext, useEffect, useMemo,
  useReducer, useRef,
} from "react";
import { storage } from "@/src/utils/storage";

// ─────────────────────────── Types ───────────────────────────

export type SortKey = "lock" | "edge" | "time" | "implied";
export type SortDirection = "asc" | "desc";
export type LineType = "moneyline" | "spread" | "total" | "props" | "both";

/**
 * Unified filter state. Every "categorical" filter is an ARRAY — toggling a
 * chip flips its inclusion. Numeric / radio-style filters stay scalar.
 */
export type FilterState = {
  // Multi-select categories — arrays only (NEVER strings).
  sports: string[];        // ["MLB", "Soccer"] — empty = ALL sports
  leagues: string[];       // ["MLB", "EPL"]   — empty = ALL leagues
  markets: string[];       // ["Hits", "ATD"]  — empty = ALL markets
  gameIds: string[];       // ["evt_abc"]      — empty = ALL games
  events: string[];        // Display-string fallback for events lacking IDs

  // Scalar narrowing filters (kept singular by design).
  searchText: string;
  minLock: number;         // 0–100; 0 = no floor
  minImplied: number;      // 0–100; American-odds derived implied prob floor
  maxImplied: number;      // 0–100; 100 = no ceiling
  simEdgeFloor: number;    // 0–100; 0 = no floor

  // View / sort settings — these are NOT filters but live in the same
  // store so tabs share the same canonical sort + line-type prefs.
  lineType: LineType;
  sortKey: SortKey;
  sortDir: SortDirection;
};

export const DEFAULT_FILTERS: FilterState = {
  sports: [],
  leagues: [],
  markets: [],
  gameIds: [],
  events: [],
  searchText: "",
  minLock: 0,
  minImplied: 0,
  maxImplied: 100,
  simEdgeFloor: 0,
  lineType: "both",
  sortKey: "lock",
  sortDir: "desc",
};

// ─────────────────────────── Reducer ───────────────────────────

type Action =
  | { type: "HYDRATE"; state: Partial<FilterState> }
  | { type: "TOGGLE_ARRAY"; key: ArrayKey; value: string }
  | { type: "SET_ARRAY"; key: ArrayKey; values: string[] }
  | { type: "SET_SCALAR"; key: ScalarKey; value: any }
  | { type: "CLEAR_NARROWING" }
  | { type: "RESET_ALL" };

type ArrayKey = "sports" | "leagues" | "markets" | "gameIds" | "events";
type ScalarKey =
  | "searchText" | "minLock" | "minImplied" | "maxImplied"
  | "simEdgeFloor" | "lineType" | "sortKey" | "sortDir";

function reducer(state: FilterState, action: Action): FilterState {
  switch (action.type) {
    case "HYDRATE":
      return { ...state, ...action.state };
    case "TOGGLE_ARRAY": {
      const current = state[action.key] || [];
      const exists = current.includes(action.value);
      const next = exists
        ? current.filter((v) => v !== action.value)
        : [...current, action.value];
      return { ...state, [action.key]: next };
    }
    case "SET_ARRAY":
      return { ...state, [action.key]: [...action.values] };
    case "SET_SCALAR":
      return { ...state, [action.key]: action.value };
    case "CLEAR_NARROWING":
      // Wipe the narrowing predicates BUT preserve sport scope + view prefs.
      // The user said "tabs only control view, not reset filters" — this
      // is the EXPLICIT "clear filters" action.
      return {
        ...state,
        leagues: [],
        markets: [],
        gameIds: [],
        events: [],
        searchText: "",
        minLock: 0,
        minImplied: 0,
        maxImplied: 100,
        simEdgeFloor: 0,
      };
    case "RESET_ALL":
      return { ...DEFAULT_FILTERS };
    default:
      return state;
  }
}

// ─────────────────────────── Storage glue ───────────────────────────

// Schema version baked into the key — bump when shape changes to invalidate
// old serialised state on disk. v3 (2026-06-26): user reported Soccer feed
// going empty with no obvious active filter — symptom was stale persisted
// arrays from earlier sessions silently restricting the slate. Bumping
// the key drops the old snapshot on every device and gives users a
// clean filter state on next launch.
const STORAGE_KEY = "perkslocks_filters_v3";

async function loadPersisted(): Promise<Partial<FilterState> | null> {
  try {
    const raw = await storage.getItem<string>(STORAGE_KEY, "");
    if (!raw) return null;
    const parsed = JSON.parse(raw as any);
    if (parsed && typeof parsed === "object") return parsed;
  } catch (e) {
    console.warn("[useFilters] hydrate failed:", e);
  }
  return null;
}

async function savePersisted(state: FilterState): Promise<void> {
  try {
    await storage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch (e) {
    console.warn("[useFilters] persist failed:", e);
  }
}

// ─────────────────────────── Context + Hook ───────────────────────────

type FilterActions = {
  toggleSport: (s: string) => void;
  toggleLeague: (l: string) => void;
  toggleMarket: (m: string) => void;
  toggleGame: (id: string) => void;
  toggleEvent: (label: string) => void;
  setSports: (vals: string[]) => void;
  setLeagues: (vals: string[]) => void;
  setMarkets: (vals: string[]) => void;
  setGames: (vals: string[]) => void;
  setEvents: (vals: string[]) => void;
  setScalar: <K extends ScalarKey>(key: K, value: FilterState[K]) => void;
  clearNarrowing: () => void;
  resetAll: () => void;
  /** Count of narrowing predicates currently active (chip bubble). */
  activeCount: number;
  /** Convert state → query params for the backend (CSV serialised). */
  toQueryParams: () => Record<string, string>;
};

type ContextValue = {
  state: FilterState;
  hydrated: boolean;
} & FilterActions;

const FiltersContext = createContext<ContextValue | null>(null);

export function FiltersProvider({ children }: { children: React.ReactNode }) {
  const [state, dispatch] = useReducer(reducer, DEFAULT_FILTERS);
  // Track hydration so consumers can avoid running effects with default state
  // until the persisted snapshot has loaded.
  const hydratedRef = useRef(false);
  const [hydrated, setHydrated] = React.useState(false);

  // 1) Hydrate on mount.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const saved = await loadPersisted();
      if (cancelled) return;
      if (saved) {
        dispatch({ type: "HYDRATE", state: saved });
      }
      hydratedRef.current = true;
      setHydrated(true);
    })();
    return () => { cancelled = true; };
  }, []);

  // 2) Persist on every change AFTER initial hydrate.
  useEffect(() => {
    if (!hydratedRef.current) return;
    savePersisted(state);
  }, [state]);

  // ── Actions (memoised so consumers don't re-render needlessly) ──
  const toggleSport = useCallback((s: string) =>
    dispatch({ type: "TOGGLE_ARRAY", key: "sports", value: s }), []);
  const toggleLeague = useCallback((l: string) =>
    dispatch({ type: "TOGGLE_ARRAY", key: "leagues", value: l }), []);
  const toggleMarket = useCallback((m: string) =>
    dispatch({ type: "TOGGLE_ARRAY", key: "markets", value: m }), []);
  const toggleGame = useCallback((id: string) =>
    dispatch({ type: "TOGGLE_ARRAY", key: "gameIds", value: id }), []);
  const toggleEvent = useCallback((label: string) =>
    dispatch({ type: "TOGGLE_ARRAY", key: "events", value: label }), []);

  const setSports = useCallback((vals: string[]) =>
    dispatch({ type: "SET_ARRAY", key: "sports", values: vals }), []);
  const setLeagues = useCallback((vals: string[]) =>
    dispatch({ type: "SET_ARRAY", key: "leagues", values: vals }), []);
  const setMarkets = useCallback((vals: string[]) =>
    dispatch({ type: "SET_ARRAY", key: "markets", values: vals }), []);
  const setGames = useCallback((vals: string[]) =>
    dispatch({ type: "SET_ARRAY", key: "gameIds", values: vals }), []);
  const setEvents = useCallback((vals: string[]) =>
    dispatch({ type: "SET_ARRAY", key: "events", values: vals }), []);

  const setScalar = useCallback<FilterActions["setScalar"]>((key, value) =>
    dispatch({ type: "SET_SCALAR", key, value }), []);
  const clearNarrowing = useCallback(() =>
    dispatch({ type: "CLEAR_NARROWING" }), []);
  const resetAll = useCallback(() =>
    dispatch({ type: "RESET_ALL" }), []);

  // ── Derived helpers ──
  const activeCount = useMemo(() =>
    state.leagues.length
    + state.markets.length
    + state.gameIds.length
    + state.events.length
    + (state.searchText ? 1 : 0)
    + (state.minLock > 0 ? 1 : 0)
    + (state.minImplied > 0 ? 1 : 0)
    + (state.maxImplied < 100 ? 1 : 0)
    + (state.simEdgeFloor > 0 ? 1 : 0),
  [state]);

  const toQueryParams = useCallback((): Record<string, string> => {
    const out: Record<string, string> = {};
    if (state.sports.length) out["sports"] = state.sports.join(",");
    if (state.leagues.length) out["leagues"] = state.leagues.join(",");
    if (state.markets.length) out["markets"] = state.markets.join(",");
    if (state.gameIds.length) out["game_ids"] = state.gameIds.join(",");
    if (state.events.length) out["events"] = state.events.join("|");  // | bc events may contain commas
    if (state.searchText) out["q"] = state.searchText;
    if (state.minLock > 0) out["min_lock"] = String(state.minLock);
    if (state.minImplied > 0) out["min_implied"] = String(state.minImplied);
    if (state.maxImplied < 100) out["max_implied"] = String(state.maxImplied);
    if (state.simEdgeFloor > 0) out["sim_edge_floor"] = String(state.simEdgeFloor);
    if (state.lineType && state.lineType !== "both") out["line_type"] = state.lineType;
    if (state.sortKey) out["sort"] = state.sortKey;
    if (state.sortDir) out["dir"] = state.sortDir;
    return out;
  }, [state]);

  const value = useMemo<ContextValue>(() => ({
    state,
    hydrated,
    toggleSport, toggleLeague, toggleMarket, toggleGame, toggleEvent,
    setSports, setLeagues, setMarkets, setGames, setEvents,
    setScalar, clearNarrowing, resetAll,
    activeCount, toQueryParams,
  }), [
    state, hydrated,
    toggleSport, toggleLeague, toggleMarket, toggleGame, toggleEvent,
    setSports, setLeagues, setMarkets, setGames, setEvents,
    setScalar, clearNarrowing, resetAll,
    activeCount, toQueryParams,
  ]);

  return (
    <FiltersContext.Provider value={value}>
      {children}
    </FiltersContext.Provider>
  );
}

export function useFilters(): ContextValue {
  const ctx = useContext(FiltersContext);
  if (!ctx) {
    throw new Error(
      "useFilters() must be called inside <FiltersProvider> (app/_layout.tsx)",
    );
  }
  return ctx;
}

// ─────────────────────────── Convenience selectors ───────────────────────────

/** True iff `value` is currently in the named array. */
export function useIsSelected(key: ArrayKey, value: string): boolean {
  const { state } = useFilters();
  return state[key].includes(value);
}

/** True iff ANY narrowing predicate is active. */
export function useHasActiveFilters(): boolean {
  const { activeCount } = useFilters();
  return activeCount > 0;
}
