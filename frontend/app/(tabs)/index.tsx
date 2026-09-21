import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  View, Text, StyleSheet, FlatList, RefreshControl,
  ActivityIndicator, Pressable, TouchableOpacity, Animated, Easing,
  Platform,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { router } from "expo-router";
import { Ionicons } from "@expo/vector-icons";
import { COLORS, SPORTS } from "@/src/theme";
import { setFeaturedPickId } from "@/src/lib/featuredStore";
import { picksLiteKey, PROFILE_STATS_KEY } from "@/src/lib/serverStateKeys";
import { swrCacheRead, swrCacheTs, swrCacheWrite } from "@/src/lib/useSWR";
import { api, Pick, LineType, SortKey, SortDirection, PickFilters, getBackendUrl, noteTruthManifest } from "@/src/lib/api";
import { LockBoardCard } from "@/src/components/LockBoardCard";
import { ChipRow } from "@/src/components/ChipRow";
import { FilterButton, FilterSheet } from "@/src/components/FilterSheet";
import { GameFilterButton, GameFilterSheet } from "@/src/components/GameFilterSheet";
import { SportFilterBar } from "@/src/components/SportFilterBar";
import { NFLIntelligenceSection } from "@/src/components/NFLIntelligenceSection";
import { StaleVersionBanner } from "@/src/components/StaleVersionBanner";
import { StaleBuildBanner } from "@/src/components/StaleBuildBanner";
import { EventGroupSkeleton } from "@/src/components/Skeleton";
import { PremiumHeader } from "@/src/components/PremiumHeader";
import { swrCacheClear } from "@/src/lib/useSWR";
import { storage } from "@/src/utils/storage";
import { useFocusRefetch } from "@/src/lib/useFocusRefetch";
import { useFilters } from "@/src/stores/useFilters";
import { classifyError, ErrorKind, ErrorKindT, ERROR_COPY, isSilentError } from "@/src/lib/errorTaxonomy";
import perf, { recordMountedRowCount } from "@/src/lib/perfHUD";
import { DevPerfHUD } from "@/src/components/DevPerfHUD";
import { subscribeConnectivity } from "@/src/lib/connectivity";

const PREFS_KEY = "locks_feed_prefs_v2";
type FeedPrefs = { sport?: string; sortKey?: SortKey; lineType?: LineType };

// ── Picks cache (2026-06 hotfix) ─────────────────────────────────────
// Symptom (Expo Go, Production): "Connection hiccup" / "GAME · 0" on
// cold boot / app-resume, MLB bets appear-then-disappear. Root cause:
// the in-memory `picks` state is [] on every fresh JS-runtime start,
// so the empty-response guard at load() (`picksRef.current.length > 0`)
// cannot fire on the very first refetch that comes back empty during
// the backend atomic-swap window — the [] paints and users see 0.
//
// Fix: persist `picks` to AsyncStorage after every successful load and
// re-hydrate on mount BEFORE the first fetch resolves. TTL 24h so a
// week-old stale slate can't leak in. Sport-scoped so switching tabs
// never restores the wrong sport. Cache is display-only; the fresh
// fetch always wins if it lands with data. Zero backend / scorer /
// pipeline changes.
// P0.7 — cache identity includes the API origin + board_version so a
// slate from another backend / another publication can never be shown
// as current.  Restored data is flagged STALE until a fresh read lands.
const PICKS_CACHE_KEY = "locks_picks_cache_v2";
const PICKS_CACHE_TTL_MS = 24 * 60 * 60 * 1000;
// GATE 3 P0 (2026-06) — truthful last-good persistence.
// A persisted board projection MUST NOT claim complete-canonical-truth.
// ``is_partial``, ``stored_count``, ``source_total_count``,
// ``saved_at`` and ``request_identity`` let any consumer of this cache
// render an honest "cached last-good, N of M picks, saved Xm ago" state.
type PicksCache = {
  sport: string;
  picks: Pick[];
  ts: number;
  origin?: string;
  boardVersion?: string | null;
  // ─── Canonical epoch stamp (2026-06-21 v2) ───────────────────
  // Ordered integer + fingerprint captured at write time.  A
  // restore MUST NOT identify this cache as CURRENT unless the
  // stamped revision matches the live authority for the stamped
  // origin — otherwise the cache is last-good only (visual paint
  // while a fresh fetch runs) and never resurrects across an
  // advance.
  canonicalRevision?: number | null;
  canonicalGenerationId?: string | null;
  stored_count?: number;
  source_total_count?: number;
  is_partial?: boolean;
  saved_at?: string;
  request_identity?: {
    sport: string;
    line_type: string;
    filters_hash: number;
  };
};

// ── 2026-08-27 PERKLOCKS SURGICAL PERF FIX ─────────────────────────
// Module-scope caches — survive tab-navigation unmounts (React
// Navigation `unmountOnBlur`-safe) so returning to the Locks tab
// paints the previous slate SYNCHRONOUSLY on the very first frame
// instead of waiting for AsyncStorage to resolve.
// Native perf closure — ONE keyed server-state authority (useSWR store).
// `_picksMem` / `_statsMem` are thin adapters over canonical SWR keys so
// the board, the Profile stats and any prefetch share ONE cache entry per
// resource; AsyncStorage (PICKS_CACHE_KEY) stays a persisted last-good
// recovery layer that HYDRATES the SWR resource instead of competing.
const picksKey = picksLiteKey;
const STATS_KEY = PROFILE_STATS_KEY;
const _picksMem = {
  get(sport: string): { picks: Pick[]; ts: number } | undefined {
    return swrCacheRead<{ picks: Pick[]; ts: number }>(picksKey(sport));
  },
  set(sport: string, v: { picks: Pick[]; ts: number }): void {
    swrCacheWrite(picksKey(sport), v);
  },
};
const _statsMem = {
  // Same key + same shape as Profile ("profile|stats" ← api.stats()).
  get data(): any { return swrCacheRead<any>(STATS_KEY); },
  get ts(): number { return swrCacheTs(STATS_KEY); },
  get present(): boolean { return swrCacheRead(STATS_KEY) !== undefined; },
  set(v: { data: any; ts: number }): void { if (v.data) swrCacheWrite(STATS_KEY, v.data); },
};
const STATS_STALE_MS = 30_000;      // /stats independently cached 30s
const FETCH_DEDUPE_MS = 1500;       // dedupe overlapping non-manual fetches

// Session 9 · MUST be stable across renders — FlatList throws
// "Changing onViewableItemsChanged on the fly is not supported"
// if the prop identity flips.  Module-scope const pairs guarantee
// referential stability for the lifetime of the JS bundle.
const _viewabilityPairs = [
  {
    viewabilityConfig: { itemVisiblePercentThreshold: 50 },
    onViewableItemsChanged: (info: { viewableItems: any[] }) => {
      try { recordMountedRowCount(info.viewableItems?.length || 0); } catch {}
    },
  },
];

function timeAgo(d: Date | null): string {
  if (!d) return "—";
  const secs = Math.floor((Date.now() - d.getTime()) / 1000);
  if (secs < 5) return "just now";
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return d.toLocaleDateString();
}

function formatCountdown(seconds: number): string {
  if (seconds <= 0) return "";
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${s.toString().padStart(2, "0")}`;
}

export default function LocksScreen() {
  // Global filter store — multi-select arrays + AsyncStorage persistence.
  // Sport pills / market pills / league pills write to this store. The
  // load() effect reads back the arrays and forwards them as CSV to the
  // picksToday() backend call (2026-06-26 unified filter refactor).
  //
  // `hydrated` flips true ONLY after AsyncStorage finishes restoring the
  // persisted snapshot. We gate the very first picks fetch on it so we
  // don't fire twice (once with defaults, again with restored state)
  // — that's what caused the "picks show then disappear" flicker.
  const {
    state: filterStore,
    hydrated: filtersHydrated,
    setEvents,
    setSports:  setStoreSports,
    setLeagues: setStoreLeagues,
    setMarkets: setStoreMarkets,
    setGames:   setStoreGames,
    resetAll: resetAllFilters,
  } = useFilters();
  // Alias — keeps the sport-switch handler readable. Same underlying
  // store setter as `setEvents`.
  const setStoreEvents = setEvents;
  // ── 2026-08-27 SYNCHRONOUS MEMORY SEED ──────────────────────────
  // Seed `picks` synchronously from the module-scope in-memory cache
  // (which survives Locks-tab remounts) so warm revisits paint the
  // previous slate on the FIRST frame. Zero AsyncStorage latency,
  // zero skeleton flash. The AsyncStorage-persist cache still runs
  // as the fallback for cold-boot / JS-runtime restart.
  const _memSeed = _picksMem.get("All");
  const [picks, setPicks] = useState<Pick[]>(_memSeed?.picks ?? []);
  // Ref mirror of `picks` — read INSIDE useCallback closures where reading
  // `picks` directly would capture a stale snapshot. Adding `picks` to the
  // useCallback deps would recreate `load()` on every setPicks call and
  // cause a re-fetch storm every time we hydrate the list. `picksRef` lets
  // us read the current length without invalidating the callback.
  // (2026-02 — iter-84 root cause of "loaded picks then crashed" report:
  // the transient-empty-payload guard at ~line 339 was reading `picks`
  // from the initial-render closure, so the guard was ALWAYS false and
  // any backend refresh tick that briefly returned picks=[] silently
  // wiped the user's cached slate to "No locks on the board".)
  const picksRef = useRef<Pick[]>([]);
  useEffect(() => { picksRef.current = picks; }, [picks]);
  // Network error state — set when /api/picks/today fails (e.g. Cloudflare
  // 520 during a backend uvicorn --reload window). When non-null, we DO
  // NOT clear the existing `picks` array; we just overlay a retry banner
  // so the user keeps seeing the last good slate instead of staring at
  // "No locks on the board" while the backend bounces. User report
  // 2026-06-28: "login and picks are intermittently failing with
  // Cloudflare ... do NOT clear existing picks."
  const [loadError, setLoadError] = useState<string | null>(null);
  // Session 9 taxonomy classification of the LAST load failure.
  // Drives copy + retry visibility.  ABORTED_SUPERSEDED never
  // reaches state — it's short-circuited in load().
  const [errorKind, setErrorKind] = useState<ErrorKindT | null>(null);
  const [sport, setSport] = useState<string>("All");
  const [lineType, setLineType] = useState<LineType>("both");
  // Default sort = "lock" descending so the user immediately sees the
  // strongest locks at the TOP of the feed when they tap the tab. Was
  // previously "time" which buried elite locks below early kickoffs.
  const [sortKey, setSortKey] = useState<SortKey>("lock");
  // Sort direction — defaults to "desc" (highest first) so the BEST locks
  // are always at the top of the feed and the user never has to scroll
  // down to find them. Time sort uses its own chronology logic on the
  // backend (asc=earliest first, desc=latest first).
  const [sortDir, setSortDir] = useState<SortDirection>("desc");
  const [filters, setFilters] = useState<PickFilters>({});
  const [filterOpen, setFilterOpen] = useState(false);
  const [gameFilterOpen, setGameFilterOpen] = useState(false);
  const [loading, setLoading] = useState(_memSeed ? false : true);
  const [refreshing, setRefreshing] = useState(false);
  const [stats, setStats] = useState<{ total_picks: number; elite_count: number; avg_edge_percent: number } | null>(_statsMem.data ?? null);
  const [lastLoadedAt, setLastLoadedAt] = useState<Date | null>(null);
  // P0.7 — canonical board identity of the slate on screen + stale flag.
  const [boardVersion, setBoardVersion] = useState<string | null>(null);
  const [slateStale, setSlateStale] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  // Alt-line availability diagnostic (2026-07-13). Populated by the
  // backend when the ALT tab is empty for a sport that the book doesn't
  // cover (currently: tennis 250s). Renders as a friendly explanation
  // in the empty state instead of the generic "no locks" message.
  const [altUnavailable, setAltUnavailable] = useState<
    { message: string; suggestion?: string } | null
  >(null);
  // Refresh-cooldown countdown driven by /picks/refresh-status. `nextRefreshAt`
  // is an absolute timestamp (ms since epoch); `remaining` is derived from it
  // by a 1-second ticker so the badge animates smoothly.
  const [nextRefreshAt, setNextRefreshAt] = useState<number | null>(null);
  const [remaining, setRemaining] = useState<number>(0); // seconds until next refresh
  const [, forceTick] = useState(0);
  const [prefsHydrated, setPrefsHydrated] = useState(false);
  // NFL Intelligence row refresh — increment to force the three NFL
  // feeds (safe-bets, atd, game-bets) to re-fetch. Driven by
  // pull-to-refresh + the UPDATE button.
  const [nflRefreshTick, setNflRefreshTick] = useState(0);

  // Hydrate persisted feed prefs (sport, sort, lineType) on mount so the
  // user's last view sticks across sessions.
  useEffect(() => {
    (async () => {
      const saved = await storage.getItem<string>(PREFS_KEY, "");
      if (saved) {
        try {
          const p: FeedPrefs = JSON.parse(saved as any);
          if (p.sport) setSport(p.sport);
          if (p.sortKey) setSortKey(p.sortKey);
          if (p.lineType) setLineType(p.lineType);
        } catch {}
      }
      setPrefsHydrated(true);
    })();
  }, []);

  // ── Picks cache restore (2026-06 hotfix) ────────────────────────
  // On mount / sport-change, if `picks` is still empty (cold boot,
  // session bounce, app-resume with dead JS runtime), pull the last
  // good slate for the current sport off AsyncStorage so the user
  // sees SOMETHING (not "GAME · 0") while the fresh fetch is in
  // flight. If the fetch lands with real picks, `setPicks(fresh)`
  // simply replaces the cache. If it lands empty, the existing
  // empty-response guard at load() keeps the restored cache visible.
  useEffect(() => {
    if (!prefsHydrated) return;   // wait for sport to settle
    let cancelled = false;
    (async () => {
      try {
        const raw = await storage.getItem<string>(PICKS_CACHE_KEY, "");
        if (!raw || cancelled) return;
        const c: PicksCache = JSON.parse(raw as any);
        if (!c || !Array.isArray(c.picks)) return;
        // TTL guard — never resurrect a stale slate older than 24h.
        if (Date.now() - (c.ts || 0) > PICKS_CACHE_TTL_MS) return;
        // Sport-scoped — only rehydrate when the cached sport matches
        // what the user is about to view. Prevents "NFL under MLB"
        // flashes on sport switch.
        if (c.sport !== sport) return;
        // P0.7 — never restore a slate that belongs to another API origin.
        let _origin = "";
        try { _origin = getBackendUrl(); } catch {}
        if (c.origin && _origin && c.origin !== _origin) return;
        // Only rehydrate if we don't already have fresh picks in memory.
        if (picksRef.current.length > 0) return;
        // ─── Ordered epoch gate (2026-06-21 v2) ─────────────────────
        // If the persisted cache's stamped revision is BEHIND the
        // live authority, it's LAST-GOOD only: paint it (so the tab
        // isn't empty on cold boot with a slow network) but mark
        // slateStale=true so the header labels it "STALE" and the
        // active refresh replaces it as soon as it lands.
        let _isLastGoodOnly = false;
        try {
          const _epochMod: any = require("@/src/lib/canonicalEpoch");
          const _live = _epochMod.getCurrentEpoch();
          if (_live &&
              typeof c.canonicalRevision === "number" &&
              c.canonicalRevision < _live.revision) {
            _isLastGoodOnly = true;
          }
          // Origin change also demotes to last-good only.
          if (_live && c.origin && c.origin !== _live.origin) {
            _isLastGoodOnly = true;
          }
        } catch { /* module unavailable — proceed */ }
        // Hydrate the canonical SWR resource (persisted last-good → live cache).
        if (!_picksMem.get(c.sport)) _picksMem.set(c.sport, { picks: c.picks, ts: c.ts || Date.now() });
        setPicks(c.picks);
        setBoardVersion(c.boardVersion ?? null);
        setLastLoadedAt(c.ts ? new Date(c.ts) : null);
        setSlateStale(true); // persisted rows are never authoritative
        lastLoadedForSportRef.current = c.sport;
        if (_isLastGoodOnly) {
          // Nudge a fresh fetch immediately — do NOT wait for TTL /
          // pull-to-refresh.  The active canonical epoch tells us
          // this cache is behind, so the shared consumer registry
          // + SWR sweeper will already fire; force a load() here
          // to guarantee the visible slate replaces itself.
          setTimeout(() => { try { (load as any)?.({ silent: true, requestedSport: c.sport }); } catch {} }, 0);
        }
      } catch { /* corrupt cache — ignore */ }
    })();
    return () => { cancelled = true; };
  }, [sport, prefsHydrated]);

  // Persist prefs whenever they change (but only after hydration so we don't
  // overwrite saved values with initial defaults).
  useEffect(() => {
    if (!prefsHydrated) return;
    const payload: FeedPrefs = { sport, sortKey, lineType };
    storage.setItem(PREFS_KEY, JSON.stringify(payload));
  }, [sport, sortKey, lineType, prefsHydrated]);

  // Tick every 30s so the "X min ago" label stays accurate.
  useEffect(() => {
    const t = setInterval(() => forceTick((n) => n + 1), 30000);
    return () => clearInterval(t);
  }, []);

  // ── Locks-Mockup 2026-08-22 micro-interactions ────────────────────
  // • Refresh icon spins while a refresh is in flight (`refreshing`).
  // • The "Updated just now" green status pulses gently while live.
  // Uses Animated.Value refs so the shared driver reuses the frame.
  const spinAnim = useRef(new Animated.Value(0)).current;
  const pulseAnim = useRef(new Animated.Value(0)).current;
  useEffect(() => {
    let loop: Animated.CompositeAnimation | null = null;
    if (refreshing) {
      spinAnim.setValue(0);
      loop = Animated.loop(
        Animated.timing(spinAnim, {
          toValue: 1,
          duration: 900,
          easing: Easing.linear,
          useNativeDriver: true,
        }),
      );
      loop.start();
    } else {
      spinAnim.stopAnimation();
      spinAnim.setValue(0);
    }
    return () => { loop?.stop(); };
  }, [refreshing, spinAnim]);
  useEffect(() => {
    const loop = Animated.loop(
      Animated.sequence([
        Animated.timing(pulseAnim, { toValue: 1, duration: 900, useNativeDriver: true }),
        Animated.timing(pulseAnim, { toValue: 0, duration: 900, useNativeDriver: true }),
      ]),
    );
    loop.start();
    return () => loop.stop();
  }, [pulseAnim]);
  const spin = spinAnim.interpolate({ inputRange: [0, 1], outputRange: ["0deg", "360deg"] });
  // pulseOpacity was consumed by the inline "Updated" dot which moved
  // into PremiumHeader (which owns its own internal pulse loop). Kept
  // pulseAnim in place because future micro-interactions may want it.

  // ── Featured Card Rotation (Locks-Mockup 2026-08-22) ─────────────
  // Rotate the featured hero card between the top-N picks of the
  // first day-group every ~7s. Returning users always see a fresh
  // spotlight instead of the same locked-in pick.
  //
  //   • Only rotates when the first group has ≥2 picks (else the
  //     highlight is static).
  //   • Rotation range = min(5, group.items.length) so we cycle
  //     through the best 5 max.
  //   • Resets to 0 on sport/filter change or when picks payload
  //     mutates (via picks.length dep).
  const [featuredIdx, setFeaturedIdx] = useState(0);
  useEffect(() => {
    setFeaturedIdx(0);
  }, [sport, lineType, filters, picks.length]);
  useEffect(() => {
    if (picks.length < 2) return;
    const t = setInterval(() => {
      setFeaturedIdx((i) => (i + 1) % 5);   // cap 5 — grouper caps below
    }, 7000);
    return () => clearInterval(t);
  }, [picks.length]);

  // 1-second tick to drive the cooldown countdown. Only runs while a
  // cooldown is active to avoid waking the UI thread needlessly.
  useEffect(() => {
    if (nextRefreshAt == null) {
      if (remaining !== 0) setRemaining(0);
      return;
    }
    const update = () => {
      const r = Math.max(0, Math.ceil((nextRefreshAt - Date.now()) / 1000));
      setRemaining(r);
      if (r === 0) setNextRefreshAt(null);
    };
    update();
    const t = setInterval(update, 1000);
    return () => clearInterval(t);
  }, [nextRefreshAt, remaining]);

  // Pull the current cooldown state from the server (zero credit cost).
  const loadCooldown = useCallback(async () => {
    try {
      const res = await api.refreshStatus();
      if (res.next_refresh_at) {
        const t = Date.parse(res.next_refresh_at);
        setNextRefreshAt(isNaN(t) ? null : t);
      } else {
        setNextRefreshAt(null);
      }
    } catch (e) {
      // Silent — countdown is a nicety, not a blocker.
    }
  }, []);

  // Fetch cooldown on first mount.
  useEffect(() => { loadCooldown(); }, [loadCooldown]);

  // Session 9 · Cancel any in-flight fetch on unmount so a stale
  // response can never call setState on a torn-down component.
  useEffect(() => {
    return () => {
      if (inflightControllerRef.current) {
        try { inflightControllerRef.current.abort(); } catch {}
        inflightControllerRef.current = null;
      }
    };
  }, []);

  const activeFilterCount =
    (filters.minLock && filters.minLock > 85 ? 1 : 0) +
    ((filters as any).minSignal && (filters as any).minSignal > 0 ? 1 : 0) +
    (filters.minImplied ? 1 : 0) +
    (filters.maxImplied && filters.maxImplied < 100 ? 1 : 0) +
    ((typeof filters.simEdgeFloor === "number" && filters.simEdgeFloor > 0) || filters.simEdgeOnly ? 1 : 0);

  // Self-diagnostic: does the user have ANY narrowing filters active?
  // This drives the empty-state CTA — if picks are zero but a filter is
  // on, we surface "CLEAR FILTERS" instead of the generic "no locks"
  // message. Recurring P0: users mis-toggle SIM EDGE or a sport-only
  // market pill and the board goes empty with no explanation. Naming
  // it `filtersAreNarrowing` (vs `hasActiveFilters`) so it's clear
  // this is "user actively filtered something OUT", not "user set a
  // preference".
  const filtersAreNarrowing =
    (!!filters.simEdgeOnly || (typeof filters.simEdgeFloor === "number" && filters.simEdgeFloor > 0)) ||
    !!filters.market ||
    !!filters.league ||
    !!filters.event ||
    filterStore.events.length > 0 ||
    filterStore.markets.length > 0 ||
    filterStore.leagues.length > 0 ||
    filterStore.gameIds.length > 0 ||
    filterStore.sports.length > 0 ||   // ← include multi-sport so empty state offers CLEAR FILTERS
    (typeof filters.minLock === "number" && filters.minLock > 85) ||
    (typeof (filters as any).minSignal === "number" && (filters as any).minSignal > 0) ||
    !!filters.minImplied ||
    (typeof filters.maxImplied === "number" && filters.maxImplied < 100);

  // Render-time view of picks — applies the event filter so the user
  // can drill into one game without losing the full slate from the
  // GameFilterSheet's dropdown list.
  //
  // 2026-06-27 multi-select: a non-empty `filterStore.events` array
  // takes precedence over the legacy single `filters.event`. When the
  // user picks multiple games via the GameFilterSheet, every pick on
  // any of those events is kept; empty array = ALL events.
  //
  // 2026-08-27 PERF: memoized — this filter was re-running on every
  // parent render (30s "min ago" tick, resize, etc.) O(n) each time.
  //
  // 2026-08-27 STRICT SPORT INVARIANT (P2 tab-isolation fix):
  //   Adds a HARD sport-scope check so a lingering `picks` state from
  //   the previous sport (e.g. CFB) can NEVER render under a newly
  //   selected sport (e.g. NFL) — even for the single render frame
  //   between the sport-change useState and the sport-clear useEffect.
  //   When selectedSport === "All", every pick's sport is accepted.
  //   When selectedSport !== "All", only `pick.sport === selectedSport`
  //   is admitted.  If the picks array hasn't caught up yet, we render
  //   the honest "GAMES · 0" empty state instead of another sport's
  //   picks.  Zero cache changes, zero request changes, purely a
  //   render-layer invariant.
  const visiblePicks = useMemo(() => {
    let src: Pick[] = picks;
    // Strict destination scope FIRST — never render another sport's picks.
    if (sport && sport.toLowerCase() !== "all") {
      src = src.filter((p) => (p.sport || "") === sport);
    }
    if (filterStore.events.length > 0) {
      const set = new Set(filterStore.events);
      return src.filter((p) => set.has(p.event || ""));
    }
    if (filters.event) {
      return src.filter((p) => (p.event || "") === filters.event);
    }
    return src;
  }, [picks, sport, filterStore.events, filters.event]);
  // 2026-08-27 PERF: memoized day grouping — O(n) grouping ran on every
  // parent render inside the JSX. Now recomputed only when the picks
  // slice actually changes.
  const dayGroups = useMemo(
    () => groupPicksByDay(visiblePicks),
    [visiblePicks],
  );
  // Unique-game count is displayed inside the GameFilterButton pill;
  // memoize the O(n) Set build so it doesn't rerun on every render.
  const uniqueGameCount = useMemo(
    () => new Set(picks.map(p => p.event).filter(Boolean)).size,
    [picks],
  );
  // ── 2026-08-27 PERF: flat sectioned list for FlatList virtualisation ──
  // Replaces the ScrollView + grouped `.map` render that mounted EVERY
  // LockPickCard at once (~150+ heavy cards for a full slate). Flattens
  // the day-groups into one linear stream of `{type: 'header'|'pick'}`
  // items so React Native's built-in windowed renderer only mounts
  // ~10-20 cards at a time. Exact UI preserved: day headers still show
  // "TODAY · 12 GAMES · 34 PICKS", featured hero card rotation still
  // works, pick cards render identically.
  type Row =
    | { type: "header"; key: string; label: string; games: number; count: number }
    | { type: "pick"; key: string; pick: Pick; featured: boolean };
  const listRows: Row[] = useMemo(() => {
    const rows: Row[] = [];
    dayGroups.forEach((group, gIdx) => {
      const uniqueEvents = new Set(group.items.map((p) => p.event || "")).size;
      rows.push({
        type: "header",
        key: `h:${group.key}`,
        label: group.label,
        games: uniqueEvents,
        count: group.items.length,
      });
      group.items.forEach((p, pIdx) => {
        rows.push({
          type: "pick",
          key: p.id,
          pick: p,
          // Hero state is delivered through featuredStore (keyed
          // subscription inside the card) — NOT through row identity.
          featured: false,
        });
      });
    });
    return rows;
  }, [dayGroups]);
  // Native perf closure — the 7-second rotation touches ONLY the featured
  // store; listRows identity is untouched, so FlatList never reconciles the
  // board for a hero flip and only two cards re-render.
  useEffect(() => {
    const first = dayGroups[0];
    const rotationCount = first ? Math.min(5, first.items.length) : 0;
    const target = rotationCount > 0 ? first.items[featuredIdx % rotationCount] : null;
    setFeaturedPickId(target ? target.id : null);
  }, [dayGroups, featuredIdx]);
  const renderRow = useCallback(({ item }: { item: Row }) => {
    if (item.type === "header") {
      return (
        <View style={styles.dayHeader}>
          <Text style={styles.dayLabel}>{item.label}</Text>
          <Text style={styles.dayCount}>
            {item.games} {item.games === 1 ? "GAME" : "GAMES"} · {item.count} {item.count === 1 ? "PICK" : "PICKS"}
          </Text>
        </View>
      );
    }
    return <LockBoardCard pick={item.pick} featured={item.featured} />;
  }, []);
  const rowKeyExtractor = useCallback((item: Row) => item.key, []);

  const clearAllNarrowingFilters = () => {
    // Wipe BOTH local pick-filters AND the persisted multi-select
    // arrays in the global store. Sport + sort + lineType are
    // preserved (those are view prefs, not filters). User can hit
    // RESET ALL on the top bar to wipe the store completely.
    setFilters({});
    resetAllFilters();
  };

  // Session 9 · Client Request Race Controller
  // ────────────────────────────────────────────────────────────────
  // Every call to load() creates a dedicated AbortController.  The
  // previous in-flight controller is aborted BEFORE the new fetch
  // starts, guaranteeing that only the newest user intent can
  // commit state.  Combined with the token counter below (which
  // survives platform quirks where AbortController may not fully
  // cut off already-resolved microtasks), this replaces the old
  // token-only guard.
  //
  // Rules coordinated by this ref:
  //   - initial load             — new controller, no prior to abort
  //   - focus refetch            — abort in-flight refetch (if any)
  //   - foreground/resume        — abort background stale probe
  //   - sport / market / tier    — abort superseded slate
  //   - STARS toggle             — abort the pre-toggle fetch
  //   - refresh                  — abort silent background load
  //   - pagination               — pinned to board_version (useBoardCursor)
  //   - retry                    — abort a still-flying request first
  //   - board-version change     — atomically reset
  const inflightControllerRef = useRef<AbortController | null>(null);
  const latestLoadTokenRef = useRef(0);
  const lastLoadedForSportRef = useRef<string>("");
  // 144-I — highest committed generation revision accepted so far; arrival
  // order never decides truth.
  const acceptedRevisionRef = useRef<number>(0);
  // 2026-08-27 PERF: dedupe overlapping non-manual fetches (tab focus,
  // AppState resume, filter store settle). `manual=true` from onRefresh
  // / onForceRefresh bypasses this guard.
  const lastFetchTsRef = useRef<number>(0);
  const lastFilterSignatureRef = useRef<string>("");

  const load = useCallback(async (s: string, lt: LineType, sk: SortKey, f: PickFilters, dir: SortDirection, opts: { manual?: boolean } = {}) => {
    const now = Date.now();
    if (!opts.manual && (now - lastFetchTsRef.current) < FETCH_DEDUPE_MS) {
      return true; // silently coalesce back-to-back non-manual fetches
    }
    lastFetchTsRef.current = now;
    // Abort any prior in-flight fetch so its late response cannot
    // overwrite fresher user intent.  Every load() call is a NEW
    // AbortController — the previous one gets torn down here.
    if (inflightControllerRef.current) {
      try { inflightControllerRef.current.abort(); } catch {}
      inflightControllerRef.current = null;
    }
    const controller = new AbortController();
    inflightControllerRef.current = controller;
    const myToken = latestLoadTokenRef.current + 1;
    latestLoadTokenRef.current = myToken;
    // Snapshot the requested sport so a late response can prove it
    // matches the CURRENTLY selected sport before painting picks.
    const requestedSport = s;
    // 2026-08-27 PERF: stats has its own 30s stale window — no need to
    // re-fetch it on every picks refresh (tab focus, AppState resume,
    // filter tweak). Cuts request count roughly in half on warm returns.
    const statsFresh = _statsMem.present && (now - _statsMem.ts) < STATS_STALE_MS;
    // MAIN 39 · P0.6 — explicit success/failure signal.
    let ok = false;
    // ── DEV-only perf mark: user-action → paint pipeline ──────────
    const perfMark = perf.startMark("locks.load", { sport: s, manual: !!opts.manual });
    try {
      const [picksRes, statsRes] = await Promise.all([
        api.picksToday(s, lt, sk, f, dir, {
          // Forward multi-select arrays from the global store. Backend
          // accepts these via the new CSV params (`sports=`, `leagues=`,
          // `markets=`, `game_ids=`, `search=`). When all arrays are
          // empty the URL stays exactly as it was — full backward-compat.
          sports:   filterStore.sports,
          leagues:  filterStore.leagues,
          markets:  filterStore.markets,
          gameIds:  filterStore.gameIds,
          search:   filterStore.searchText || undefined,
          // Session 9 Race Controller — wire the AbortSignal so the
          // network layer can tear this call down the moment a newer
          // load() supersedes us.
          signal:   controller.signal,
        }),
        statsFresh ? Promise.resolve(_statsMem.data) : api.stats().catch(() => null),
      ]);
      perfMark.step("response");
      // Discard if a newer load was fired after we sent this one.
      // Belt-and-braces: if the AbortController was flipped between
      // response headers and body-read we already reject via
      // ABORTED_SUPERSEDED, but the token check catches any edge
      // cases where a stale resolve made it through.
      if (myToken !== latestLoadTokenRef.current) return false;
      // Clear any prior load-error banner — we got a clean response.
      setLoadError(null);
      setErrorKind(null);
      // Defensive client-side filter — protect users from production
      // backends that haven't yet deployed the KBO removal. We do NOT
      // filter by event_time here because player props for in-progress
      // games (e.g. batter Over 0.5 Hits) are still legitimate locks
      // that the user wants to see on the slate even after first pitch.
      // P0.5/P0.7 — adopt the canonical board identity from the response.
      const _bv: string | null = (picksRes as any).board_version ?? (picksRes as any).truth_manifest?.board_version ?? null;
      noteTruthManifest((picksRes as any).truth_manifest);
      let fresh = (picksRes.picks || []).filter((p: any) => p.sport !== "KBO");
      // Sport-mismatch guard (uses requestedSport declared at top of try block)
      if (requestedSport && requestedSport.toLowerCase() !== "all") {
        fresh = fresh.filter((p: any) => p.sport === requestedSport);
      }
      const simFloor =
        typeof f.simEdgeFloor === "number" && f.simEdgeFloor > 0
          ? f.simEdgeFloor
          : f.simEdgeOnly
            ? 75
            : 0;
      if (simFloor > 0) {
        fresh = fresh.filter((p: any) =>
          p.synthetic === true ||
          p.force_injected === true ||
          (p.synthetic_source && String(p.synthetic_source).length > 0) ||
          (typeof p.sim_win_probability === "number" &&
            p.sim_win_probability >= simFloor),
        );
      }
      const lastSport = lastLoadedForSportRef.current;
      const sameFilter = lastSport === requestedSport;
      // ── Iteration 144-H/I — committed-generation acceptance ─────────
      // Truth is decided by the committed generation, never by arrival
      // order or by "non-empty".  Responses labelled BUILDING/VALIDATING/
      // FAILED (partial populations) or carrying an OLDER revision than
      // the one already accepted keep the current board visible and are
      // never persisted as last-known-good.
      const _tm: any = (picksRes as any).truth_manifest || null;
      const _gstate: string = String(_tm?.generation_state || "COMMITTED");
      const _grev: number = typeof _tm?.revision === "number" ? _tm.revision : 0;
      const _committed = _gstate === "COMMITTED" || _gstate === "LEGACY_UNTRACKED";
      if (!_committed && picksRef.current.length > 0) {
        setErrorKind(ErrorKind.STALE_FALLBACK);
        setLoadError("Board updating… showing the last committed board.");
        ok = true;
        perfMark.end({ n: fresh.length, uncommitted: _gstate });
        return true;
      }
      if (_committed && _grev > 0 && sameFilter && _grev < acceptedRevisionRef.current) {
        // Older generation arrived after a newer one — discard.
        ok = true;
        perfMark.end({ n: fresh.length, staleGeneration: _grev });
        return true;
      }
      // ─── Global CanonicalEpoch ordered guard (2026-06-21 v2) ─────
      // Even when ``acceptedRevisionRef`` is in-sync, a LATE stale
      // response can arrive whose ``_grev`` matches a prior accept
      // but the GLOBAL authority has advanced further via another
      // endpoint (/api/version ping, HI response, rollover fetch).
      // Refuse to commit any response that is strictly BEHIND the
      // global CanonicalEpoch.revision — this is the ordering
      // safety-net that closes the multi-authority race described
      // in Root Cause #1 without inventing sport-specific hacks.
      try {
        const _epochMod: any = require("@/src/lib/canonicalEpoch");
        const _live = _epochMod.getCurrentEpoch();
        if (_live && _committed && _grev > 0 && _grev < _live.revision) {
          ok = true;
          perfMark.end({ n: fresh.length, staleVsGlobal: _grev, live: _live.revision });
          return true;
        }
      } catch { /* module unavailable — proceed */ }
      if (fresh.length === 0 && picksRef.current.length > 0 && sameFilter) {
        // Session 9 taxonomy — this is STALE_FALLBACK, NOT a network error.
        setErrorKind(ErrorKind.STALE_FALLBACK);
        setLoadError("Slate refreshing… showing your cached picks. Tap to retry.");
        ok = true;
        perfMark.end({ n: 0, staleFallback: true });
        return true;
      }
      perfMark.step("commit");
      if (_committed && _grev > 0) acceptedRevisionRef.current = _grev;
      setPicks(fresh);
      setBoardVersion(_bv);
      setSlateStale(false);
      lastLoadedForSportRef.current = requestedSport;
      _picksMem.set(requestedSport, { picks: fresh, ts: Date.now() });
      if (fresh.length > 0 && _committed) {
        try {
          let _origin = "";
          try { _origin = getBackendUrl(); } catch {}
          // GATE 3 P0 (2026-06) — truthful last-good contract.
          // A persisted board projection MUST NOT claim to be a
          // complete canonical board.  The 200-pick slice is a
          // best-effort fast-boot fallback; the ``is_partial`` +
          // ``stored_count`` / ``source_total_count`` metadata lets
          // any UI that hydrates from this cache render an honest
          // "showing X of Y saved picks · updated Nm ago" surface
          // when the network is unavailable.
          const _stored = Math.min(fresh.length, 200);
          // Stamp the persisted cache with the CanonicalEpoch that was
          // authoritative at write time — restore reads gate on this.
          let _canRev: number | null = null;
          let _canGid: string | null = null;
          try {
            const _epoch = (require("@/src/lib/canonicalEpoch") as any)
              .getCurrentEpoch();
            _canRev = _epoch ? _epoch.revision : null;
            _canGid = _epoch ? _epoch.generationId : null;
          } catch { /* module unavailable at cold boot */ }
          const cache: PicksCache = {
            sport: requestedSport,
            picks: fresh.slice(0, 200),
            ts: Date.now(),
            origin: _origin,
            boardVersion: _bv,
            canonicalRevision: _canRev,
            canonicalGenerationId: _canGid,
            stored_count: _stored,
            source_total_count: fresh.length,
            is_partial: _stored < fresh.length,
            saved_at: new Date().toISOString(),
            request_identity: {
              sport: requestedSport,
              line_type: lt,
              filters_hash: (typeof filters === "object" && filters)
                ? JSON.stringify(filters).length : 0,
            },
          } as PicksCache;
          storage.setItem(PICKS_CACHE_KEY, JSON.stringify(cache));
        } catch { /* storage full / serialize err — silent */ }
      }
      const altDiag: any = (picksRes as any).alt_availability;
      if (lt === "alt" && fresh.length === 0 && altDiag && altDiag.supported === false) {
        setAltUnavailable({
          message: String(altDiag.message || ""),
          suggestion: altDiag.suggestion ? String(altDiag.suggestion) : undefined,
        });
      } else {
        setAltUnavailable(null);
      }
      if (statsRes) {
        const kboCount = (picksRes.picks || []).filter((p: any) => p.sport === "KBO").length;
        let nextStats;
        if (kboCount > 0 && typeof statsRes.total_picks === "number") {
          nextStats = { ...statsRes, total_picks: Math.max(0, statsRes.total_picks - kboCount) };
        } else {
          nextStats = statsRes;
        }
        setStats(nextStats);
        _statsMem.set({ data: nextStats, ts: Date.now() });
      }
      setLastLoadedAt(new Date());
      ok = true;
      perfMark.end({ n: fresh.length });
    } catch (e: any) {
      // Session 9 · Error taxonomy classification at the request boundary.
      const kind = classifyError(e);
      // ABORTED_SUPERSEDED is NOT a user-facing error — a newer
      // request has taken over.  We do NOT clear picks, we do NOT
      // show a banner, we do NOT toast.  Simply return.
      if (kind === ErrorKind.ABORTED_SUPERSEDED) {
        perfMark.end({ superseded: true });
        return false;
      }
      // Only commit the error banner if this call is still the newest.
      if (myToken === latestLoadTokenRef.current) {
        // P0.11 — keep last-good visible, but mark it STALE explicitly.
        if (picksRef.current.length > 0) setSlateStale(true);
        setErrorKind(kind);
        const copy = ERROR_COPY[kind];
        // Never leak "Connection Hiccup" for anything other than a
        // real network / server failure.  Copy comes from the taxonomy.
        setLoadError(copy.title ? `${copy.title} — ${copy.message}` : (e?.message || "Network error"));
        console.warn("[locks.load] failed", { kind, err: e });
      }
      ok = false;
      perfMark.end({ errorKind: kind });
    } finally {
      // ─── Single-flight refresh ownership release (2026-06-21 v2) ─
      // Every terminal path (SUCCESS / ERROR / TIMEOUT / ABORT /
      // SUPERSEDED) MUST release ownership.  If a superseding load()
      // has already claimed ownership, we skip our own state writes
      // — but if no active owner exists we FORCIBLY clear the
      // ``refreshing`` flag to satisfy the invariant "no active
      // owner → refreshing is false".
      const stillOwner = (myToken === latestLoadTokenRef.current);
      if (stillOwner) {
        setLoading(false);
        setRefreshing(false);
        if (inflightControllerRef.current === controller) {
          inflightControllerRef.current = null;
        }
      } else {
        // Superseded — the newer load() owns the visible state now.
        // Do NOT touch UI state.  BUT: if the AbortController we
        // installed is still lingering (rare race), release it.
        if (inflightControllerRef.current === controller) {
          inflightControllerRef.current = null;
        }
      }
    }
    return ok;
  }, [
    filterStore.sports, filterStore.leagues, filterStore.markets,
    filterStore.gameIds, filterStore.searchText,
  ]);

  useEffect(() => {
    // Wipe stale picks the moment the user changes sport OR any
    // narrowing filter (market / league / event / store arrays) so
    // the wrong tab can NEVER be shown for even a single frame.
    // Without this, a previous fetch's picks remain visible while the
    // new fetch is in-flight, producing the "H+R+RBI under Strikeouts"
    // visual leak that users (rightly) interpret as broken filtering.
    //
    // CRITICAL (2026-06-27): include the FULL multi-select store
    // signature too — leagues / markets / gameIds / events. The
    // previous sig (sport + local filters only) missed multi-select
    // changes, so when the persistent store hydrated late on cold
    // start (or the user toggled a chip), the previous slate kept
    // rendering until the new fetch landed → "picks showing up
    // then leaving" complaint.
    // CHANGED 2026-06-28: wipe picks ONLY when the SPORT actually changes.
    // Previously we wiped on every filter-signature change (market,
    // league, event, multi-select arrays). That meant a network blip
    // mid-fetch left the user staring at "No locks on the board" with
    // no way back to their cached slate. Per user spec: "If picks
    // request fails: show cached picks, show retry button, do NOT
    // clear existing picks."  Market/league/event filters apply at
    // render time (see `visiblePicks` below), so leaving the picks
    // array untouched during filter tweaks is safe — and resilient.
    const sig = sport;
    if (lastFilterSignatureRef.current && lastFilterSignatureRef.current !== sig) {
      // Sport actually changed. Seed from module-scope cache for the
      // new sport so we render its cached slate instantly (or blank
      // if no cache exists yet). Zero AsyncStorage latency.
      const _newSeed = _picksMem.get(sig);
      if (_newSeed) {
        setPicks(_newSeed.picks);
      } else {
        setPicks([]);
      }
    }
    lastFilterSignatureRef.current = sig;
    // 2026-08-27 PERF: Only show the skeleton when we truly have NO
    // cached picks. On warm returns and filter tweaks (which reuse
    // the current picks array) we render the existing slate instantly
    // and refresh silently in the background.
    if (picksRef.current.length === 0) {
      setLoading(true);
    }
    if (!prefsHydrated) return;
    // Don't fire the picks fetch until the AsyncStorage-persisted filter
    // store has finished hydrating. Otherwise we hit the API twice on
    // cold start (once with default filters, once with the restored ones)
    // and the UI shows picks → wipes them → shows the restored set.
    //
    // CRITICAL: `filtersHydrated` and `prefsHydrated` are in the dep
    // array. Without them, a fresh-install cold start (no persisted
    // state on disk) would hydrate without dispatching any state change
    // — the array deps stay `[]` → effect never re-fires → picks never
    // load → user sees an infinite spinner. Listing the hydration
    // flags as deps guarantees a re-fire the moment hydration finishes.
    if (!filtersHydrated) return;
    load(sport, lineType, sortKey, filters, sortDir);
  }, [
    sport, lineType, sortKey, filters, sortDir, load,
    prefsHydrated, filtersHydrated,
    // Re-fire when ANY multi-select dimension changes — sports/leagues/
    // markets/games/events/search. JSON.stringify keeps the dep array
    // stable so React doesn't fire on every render, only when the
    // arrays actually mutate.
    JSON.stringify(filterStore.sports),
    JSON.stringify(filterStore.leagues),
    JSON.stringify(filterStore.markets),
    JSON.stringify(filterStore.gameIds),
    JSON.stringify(filterStore.events),
    filterStore.searchText,
  ]);

  // Smart refetch on screen focus: hit /api/picks/today again every time the
  // user opens the Locks tab. 5 s cooldown (down from 30 s on 2026-07-28) so
  // freshly-emitted picks (e.g. new H+R+RBI market family) surface as soon
  // as the user tabs back — without hammering the API on every focus.
  useFocusRefetch(
    () => {
      loadCooldown();
      // MAIN 39 · P0.6 — propagate load()'s boolean so a failed
      // silent refresh doesn't lock out the next focus behind the
      // 5s cooldown.
      return load(sport, lineType, sortKey, filters, sortDir);
    },
    [sport, lineType, sortKey, filters, sortDir, load, loadCooldown],
    5_000,
  );

  // ── Client freshness fix (Expo Go 2026-08-22) ────────────────────
  //   Expo Go users left the app in the background then re-opened it
  //   30+ min later; `useFocusEffect` does NOT fire on foreground
  //   resume when the Locks tab was already focused before pause. So
  //   picks stayed pinned to the pre-pause payload until the user
  //   manually switched tabs or hit UPDATE.
  //
  //   Fix: subscribe to AppState. On every "active" transition, force
  //   a silent refetch of the board + cooldown. Bounded by the same
  //   5s cooldown as focus refetch to avoid a burst on rapid toggles.
  //   P0.10 — foreground handling now comes from the ONE global
  //   connectivity authority (src/lib/connectivity.ts): a single debounced
  //   `foreground` signal per active transition, after a reachability probe.
  useEffect(() => {
    const unsub = subscribeConnectivity((_s, event) => {
      if (event !== "foreground") return;
      load(sport, lineType, sortKey, filters, sortDir);
      loadCooldown();
    });
    return unsub;
  }, [sport, lineType, sortKey, filters, sortDir, load, loadCooldown]);

  // ─── Canonical epoch revalidation (2026-06-21 v2) ────────────────
  // Register Locks as a mounted canonical consumer.  When the
  // CanonicalEpoch advances (N → N+1) via any endpoint (/api/version
  // ping, HI response, rollover fetch), the shared registry invokes
  // our revalidate callback exactly ONCE per target revision.  We
  // silently reload the board — the epoch-ordered guard inside
  // ``load()`` guarantees a stale N response cannot then overwrite
  // the N+1 truth we're now fetching.
  useEffect(() => {
    let unregister: (() => void) | null = null;
    let mounted = true;
    import("@/src/lib/canonicalConsumers").then((m) => {
      if (!mounted) return;
      unregister = m.registerCanonicalConsumer("locks-board", () => {
        void load(sport, lineType, sortKey, filters, sortDir);
      });
    }).catch(() => {});
    return () => {
      mounted = false;
      if (unregister) try { unregister(); } catch {}
    };
  }, [sport, lineType, sortKey, filters, sortDir, load]);

  const onRefresh = () => {
    setRefreshing(true);
    load(sport, lineType, sortKey, filters, sortDir, { manual: true });
    // Also bump the NFL-intelligence tick so the three NFL feature rows
    // re-fetch in lockstep with the picks feed. Pull-to-refresh now
    // refreshes EVERYTHING on screen, not just the locks list.
    setNflRefreshTick((n) => n + 1);
  };

  const showToast = (msg: string) => {
    setToast(msg);
    setTimeout(() => setToast(null), 2200);
  };

  const onForceRefresh = async () => {
    if (remaining > 0) {
      // Cooldown active — surface the countdown instead of burning credits.
      const mins = Math.ceil(remaining / 60);
      showToast(`New picks in ${mins} min`);
      return;
    }
    setLoading(true);
    try {
      // Client-freshness fix 2026-08-22 — manual UPDATE invalidates
      // the in-memory SWR cache for ALL primary tabs so a stale
      // sibling snapshot (e.g. Rollover / Parlay) can't survive.
      // On the next focus of any tab, a fresh fetch is guaranteed.
      swrCacheClear();
      const res = await api.refresh();
      // Backend tells us the next-allowed time. Drive the countdown from
      // that instead of guessing client-side.
      if (res.next_refresh_at) {
        const t = Date.parse(res.next_refresh_at);
        if (!isNaN(t)) setNextRefreshAt(t);
      }
      await load(sport, lineType, sortKey, filters, sortDir, { manual: true });
      if (res.rate_limited) {
        showToast(res.message || "Refresh on cooldown");
      } else {
        showToast(`Refreshed · ${res.count} picks`);
      }
    } catch (e) {
      console.warn(e);
      setLoading(false);
      showToast("Refresh failed");
      // Re-pull status in case server already advanced the cooldown.
      loadCooldown();
    }
  };

  return (
    <SafeAreaView style={styles.safe} edges={["top"]}>
      <PremiumHeader
        title="PERKLOCKS"
        tagline="LOCK IN. CASH OUT."
        subtitle={`Today's Locks · ${new Date().toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" })}`}
        status={{ label: `Updated ${timeAgo(lastLoadedAt)}` }}
        right={
          <Pressable
            testID="refresh-button"
            onPress={onForceRefresh}
            style={[styles.refreshBtn, remaining > 0 && styles.refreshBtnDisabled]}
            hitSlop={10}
          >
            {remaining > 0 ? (
              <Text style={styles.refreshBtnCountdown} testID="refresh-button-countdown">
                {formatCountdown(remaining)}
              </Text>
            ) : (
              <Animated.View style={{ transform: [{ rotate: spin }] }}>
                <Ionicons name="refresh" size={20} color={COLORS.goldElite} />
              </Animated.View>
            )}
          </Pressable>
        }
      />
      {remaining > 0 && (
        <Text
          style={styles.cooldownStripe}
          testID="refresh-cooldown-label"
        >
          New picks in {formatCountdown(remaining)}
        </Text>
      )}

      {toast && (
        <View style={styles.toast} pointerEvents="none">
          <Ionicons name="checkmark-circle" size={16} color={COLORS.neonGreen} />
          <Text style={styles.toastText}>{toast}</Text>
        </View>
      )}

      <ChipRow
        options={SPORTS}
        active={sport}
        onChange={(s) => {
          // Reset sport-specific narrowing filters when switching sports.
          // `event`, `market`, and `league` are all sport-bound — a
          // Phillies game doesn't exist on NBA night, an MLB strikeouts
          // market doesn't exist for NFL, etc. Carrying them across
          // sport switches produces empty-board confusion ("took me
          // back to main tab" complaint, 2026-06-25).
          setFilters((f) => ({ ...f, market: undefined, league: undefined, event: undefined }));
          // CRITICAL (2026-06-27): the persistent multi-select store
          // ALSO holds sport-bound arrays (`leagues`, `markets`,
          // `gameIds`, `events`). On a sport switch they're nearly
          // always stale — an MLB "Yankees @ Red Sox" event has no
          // Soccer counterpart, an NFL "Touchdown Scorer" market
          // doesn't exist for Tennis. Leaving them in the store
          // makes the backend filter Soccer picks down to zero
          // (verified bug: persisted `events=Yankees @ Red Sox`
          // returns 0 Soccer picks). Wipe them here in sync with
          // the local `filters` reset above.
          setStoreLeagues([]);
          setStoreMarkets([]);
          setStoreGames([]);
          setStoreEvents([]);
          // CRITICAL BUG FIX (2026-06-28): also clear the persisted multi-
          // select `sports` array on a single-sport tap. Without this, a
          // stale value (e.g., `sports=["NBA"]` carried over from a prior
          // multi-select session) was OVERRIDING the tapped `sport=MLB`
          // on the backend, returning 0 picks and dumping the user into
          // the "No locks on the board → SHOW ALL X PICKS" empty state
          // (user report: "soccer mlb etc no picks").
          setStoreSports([]);
          setSport(s);
        }}
        testIDPrefix="sport-chip"
      />
      <SportFilterBar sport={sport} filters={filters} onChange={setFilters} />
      <StaleBuildBanner />
      <StaleVersionBanner onRefresh={() => load(sport, lineType, sortKey, filters, sortDir, { manual: true })} />

      {/* ── Cleaned-up controls row ──
          User spec: "we can take lock, elite and edge at top of page off
          the line and sort should [be] in the filters". So we removed
          the StatsRow + LineTypeToggle + SortSelector from this header.
          The FilterSheet now owns LINE + SORT controls. The only
          permanent controls here are the FILTER trigger and the
          UPDATE button so users can always pull fresh data. */}
      <View style={styles.controlsRow}>
        <View style={styles.filterBtnWrap}>
          <FilterButton
            onPress={() => setFilterOpen(true)}
            activeCount={activeFilterCount + (lineType !== "both" ? 1 : 0) + (sortKey !== "lock" ? 1 : 0) + (sortDir !== "desc" ? 1 : 0)}
            testID="locks-filter-button"
          />
        </View>
        {/* Per-game drill-down — opens the GameFilterSheet listing every
            unique event on the slate. One tap narrows the board to a
            single match (e.g. "PSG @ Arsenal"). */}
        <GameFilterButton
          onPress={() => setGameFilterOpen(true)}
          activeEvent={filters.event}
          activeEventsCount={filterStore.events.length}
          totalGames={uniqueGameCount}
        />
        <TouchableOpacity
          onPress={onRefresh}
          activeOpacity={0.7}
          style={styles.updateBtn}
          accessibilityLabel="Update slate"
          testID="locks-update"
        >
          <Text style={styles.updateBtnTxt}>{refreshing ? "…" : "UPDATE"}</Text>
        </TouchableOpacity>
      </View>
      {/* Banners removed 2026-07-19 per user request:
          "delete banners off app nrfi/yrfi and sports lab across all
          sports tabs". Kept the top slate header + game-total picks
          only; no more per-sport lab / NRFI / ATD / NFL Intel CTAs. */}
      {/* HR entry point moved to SportFilterBar — appears as a "🚀 HR"
          chip next to Hits / H+R+RBI / Strikeouts / Outs Recorded. */}
      <FilterSheet
        visible={filterOpen}
        onClose={() => setFilterOpen(false)}
        filters={filters}
        onApply={setFilters}
        lineType={lineType}
        onLineTypeChange={setLineType}
        sortKey={sortKey}
        onSortKeyChange={setSortKey}
        sortDir={sortDir}
        onSortDirChange={setSortDir}
      />

      {/* Game (event) drill-down sheet. Always sees the FULL slate
          (`picks` — pre-event-filter) so the user can swap between
          games without losing the dropdown.
          Multi-select wired to the global filter store's `events`
          array. The legacy `filters.event` single-select is still
          synced so older empty-state CTAs etc. keep working. */}
      <GameFilterSheet
        visible={gameFilterOpen}
        picks={picks}
        activeEvents={filterStore.events}
        activeEvent={filters.event}
        onClose={() => setGameFilterOpen(false)}
        onApplyEvents={(events) => {
          setEvents(events);
          // Keep `filters.event` synced to the FIRST chosen event (or
          // clear it) so the lazy single-event consumers (empty-state
          // CTAs, game-pill label) stay in lockstep. When multiple
          // events are picked, leave `filters.event` undefined so the
          // legacy code falls through to the store-driven path.
          setFilters({ ...filters, event: events.length === 1 ? events[0] : undefined });
        }}
      />

      <FlatList
        style={styles.list}
        contentContainerStyle={styles.content}
        refreshControl={<RefreshControl tintColor={COLORS.textPrimary} refreshing={refreshing} onRefresh={onRefresh} />}
        showsVerticalScrollIndicator={false}
        testID="locks-scroll"
        // 2026-08-27 PERF: virtualisation — only ~10-20 cards mounted
        // at any time instead of the entire slate. Keeps the LockPickCard
        // React.memo unchanged.
        data={visiblePicks.length > 0 ? listRows : []}
        keyExtractor={rowKeyExtractor}
        renderItem={renderRow}
        // ── Root Closure 2026-06 LIVE MOBILE FIX ───────────────────
        // React Native Web + `removeClippedSubviews=true` aggressively
        // unmounts off-screen DOM nodes and, on iOS Safari's mobile
        // rendering path, LEAKS the "clipped" state to the layout
        // measurer — the FlatList's inner scroll container reports
        // a truncated `contentSize.height` (~8 cards worth) even
        // though ``data`` contains the full slate. Result: user
        // scrolls, hits `atBottom=true` on a fake 8-card content,
        // sees a huge black region below, and CFB `16 → reachable 8`.
        // Native Expo does NOT exhibit this bug, so we keep the
        // native optimisation and disable it ONLY on web.
        // Also raise `initialNumToRender` + `windowSize` on web so
        // large slates paint completely without a virtualisation
        // measurement race that strands cards outside contentSize.
        // ── Root Closure §J — ADAPTIVE VIRTUALIZATION AUTO-TUNE ────
        // Slate-size-aware windowing so 20-pick and 200-pick boards
        // both feel snappy on native AND web.  Never reintroduces the
        // fixed RN Web `removeClippedSubviews=true` truncation bug
        // (invariant `WEB_UNREACHABLE_ELIGIBLE=0`); the web branch
        // always keeps `removeClippedSubviews=false`.
        //
        //   slate ≤ 30    → render everything up front (no window)
        //   slate 31-80   → moderate window
        //   slate 81+     → wide window on web to cover all rows,
        //                   tight window on native (native scrolling
        //                   handles clipping efficiently)
        removeClippedSubviews={Platform.OS !== "web"}
        initialNumToRender={
          Platform.OS === "web"
            ? Math.min(listRows.length, listRows.length <= 30 ? listRows.length : listRows.length <= 80 ? 40 : 60)
            : 8
        }
        maxToRenderPerBatch={
          Platform.OS === "web"
            ? (listRows.length <= 30 ? 15 : listRows.length <= 80 ? 25 : 40)
            : 8
        }
        windowSize={
          Platform.OS === "web"
            ? (listRows.length <= 30 ? 21 : listRows.length <= 80 ? 41 : 61)
            : 7
        }
        updateCellsBatchingPeriod={Platform.OS === "web" ? 30 : 50}
        // Session 9 · DEV-only virtualization instrumentation.
        // Records the currently mounted row count so the DevPerfHUD
        // can display it alongside p50/p95 tap-to-paint timings.
        // NB: FlatList requires this prop identity to be STABLE across
        // renders — recreating the callback triggers a
        // "Changing onViewableItemsChanged on the fly is not
        // supported" invariant.  We stash the callback + config in
        // module-scope refs (see viewabilityConfigCallbackPairsRef).
        viewabilityConfigCallbackPairs={_viewabilityPairs}
        ListHeaderComponent={
          <>
            {/* P0.11 STALE-WHILE-REVALIDATE pill — restored/last-good slate
                is visible but explicitly marked until a fresh read lands. */}
            {slateStale && !loadError && picks.length > 0 && (
              <View testID="stale-slate-pill" style={styles.stalePill}>
                <ActivityIndicator size="small" color={COLORS.goldRich} />
                <Text style={styles.stalePillTxt}>
                  {refreshing || loading ? "UPDATING" : "STALE"} · LAST UPDATED {timeAgo(lastLoadedAt).toUpperCase()}
                  {boardVersion ? ` · BOARD ${String(boardVersion).slice(0, 8)}` : ""}
                </Text>
              </View>
            )}
            {/* RETRY BANNER (2026-06-28) */}
            {!!loadError && (
              <TouchableOpacity
                activeOpacity={0.85}
                onPress={() => {
                  setLoadError(null);
                  setRefreshing(true);
                  load(sport, lineType, sortKey, filters, sortDir, { manual: true });
                }}
                style={{
                  backgroundColor: "rgba(255, 88, 88, 0.15)",
                  borderColor: "rgba(255, 88, 88, 0.55)",
                  borderWidth: 1,
                  borderRadius: 12,
                  paddingHorizontal: 14,
                  paddingVertical: 12,
                  marginBottom: 14,
                  flexDirection: "row",
                  alignItems: "center",
                  justifyContent: "space-between",
                }}
              >
                <View style={{ flex: 1, marginRight: 12 }}>
                  <Text style={{ color: "#ffb4b4", fontWeight: "700", fontSize: 13 }}>
                    {(errorKind && ERROR_COPY[errorKind]?.title) || "Connection hiccup"}
                  </Text>
                  <Text style={{ color: "rgba(255,255,255,0.78)", fontSize: 12, marginTop: 2 }}>
                    {(errorKind && ERROR_COPY[errorKind]?.message) || "Showing your last good slate. Tap to retry."}
                  </Text>
                  {slateStale && picks.length > 0 && (
                    <Text testID="stale-slate-meta" style={{ color: "rgba(255,255,255,0.6)", fontSize: 10.5, marginTop: 4, letterSpacing: 0.6, fontWeight: "700" }}>
                      STALE · LAST UPDATED {timeAgo(lastLoadedAt).toUpperCase()}{boardVersion ? ` · BOARD ${String(boardVersion).slice(0, 8)}` : ""}
                    </Text>
                  )}
                </View>
                {(!errorKind || ERROR_COPY[errorKind]?.showRetry) && (
                  <Text style={{ color: "#ffb4b4", fontWeight: "800", fontSize: 13 }}>
                    RETRY ↻
                  </Text>
                )}
              </TouchableOpacity>
            )}
          </>
        }
        ListEmptyComponent={
          loading ? (
            <View testID="board-skeleton">
              {/* Milestone 1.2 skeleton loader — matches event-grouped list. */}
              <EventGroupSkeleton picks={2} />
              <EventGroupSkeleton picks={3} />
              <EventGroupSkeleton picks={2} />
              <View style={styles.center}>
                <ActivityIndicator color={COLORS.voltBlue} />
              </View>
            </View>
          ) : (
            <View style={styles.emptyCard} testID="empty-board">
              <Ionicons
                name={altUnavailable ? "information-circle-outline" : "lock-open-outline"}
                size={42}
                color={COLORS.textMuted}
              />
              <Text style={styles.emptyTitle}>
                {altUnavailable ? "Alt lines unavailable" : "No locks on the board"}
              </Text>
              {altUnavailable ? (
                <>
                  <Text style={styles.emptyMsg} testID="empty-msg-alt-unavailable">
                    {altUnavailable.message}
                  </Text>
                  {!!altUnavailable.suggestion && (
                    <TouchableOpacity
                      onPress={() => setLineType("main")}
                      style={styles.emptyCta}
                      activeOpacity={0.8}
                      testID="empty-switch-main"
                    >
                      <Text style={styles.emptyCtaTxt}>{altUnavailable.suggestion}</Text>
                    </TouchableOpacity>
                  )}
                </>
              ) : filtersAreNarrowing ? (
                <>
                  <Text style={styles.emptyMsg} testID="empty-msg-filters">
                    Filters are hiding picks from the board. Clear them to see
                    today&apos;s full slate
                    {typeof stats?.total_picks === "number" ? ` (${stats.total_picks} picks)` : ""}.
                  </Text>
                  <TouchableOpacity
                    onPress={clearAllNarrowingFilters}
                    style={styles.emptyCta}
                    activeOpacity={0.8}
                    testID="empty-clear-filters"
                  >
                    <Text style={styles.emptyCtaTxt}>CLEAR ALL FILTERS</Text>
                  </TouchableOpacity>
                </>
              ) : sport !== "All" && (stats?.total_picks ?? 0) > 0 ? (
                <>
                  <Text style={styles.emptyMsg} testID="empty-msg-wrong-sport">
                    No pregame {sport} setups cleared the lock-score gate today —
                    but {stats?.total_picks} pick{stats?.total_picks === 1 ? "" : "s"}{" "}
                    {stats?.total_picks === 1 ? "is" : "are"} live in other sports.
                  </Text>
                  <TouchableOpacity
                    onPress={() => setSport("All")}
                    style={styles.emptyCta}
                    activeOpacity={0.8}
                    testID="empty-show-all"
                  >
                    <Text style={styles.emptyCtaTxt}>
                      SHOW ALL {stats?.total_picks} PICKS
                    </Text>
                  </TouchableOpacity>
                </>
              ) : (
                <Text style={styles.emptyMsg} testID="empty-msg-generic">
                  {sport === "All"
                    ? "All today's games are either started or below our lock-score gate."
                    : `No pregame ${sport} setups cleared the lock-score gate.`}
                </Text>
              )}
              <View style={styles.emptyDivider} />
              <Text style={styles.emptyHintLabel}>
                {remaining > 0
                  ? `NEXT REFRESH IN ${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, "0")}`
                  : "PULL DOWN TO REFRESH"}
              </Text>
              <Text style={styles.emptyHintSub}>
                {filtersAreNarrowing
                  ? "Tip: SIM EDGE only surfaces sim ≥75%, which is a small slice of the board."
                  : "Tip: try other sports — soccer + tennis often have late slates."}
              </Text>
            </View>
          )
        }
      />
      {/* Session 9 · DEV-only Performance HUD.  Gated by
          __DEV__ && EXPO_PUBLIC_PERF_HUD === "1".  Production
          bundles render nothing. */}
      <DevPerfHUD />
    </SafeAreaView>
  );
}

/** Group picks by event date (TODAY / TOMORROW / weekday). Honour the
 *  current sort/direction inside each group so high-lock picks still
 *  surface at the top of TODAY before TOMORROW shows up at all. */
function groupPicksByDay(
  picks: Pick[],
): Array<{ key: string; label: string; items: Pick[] }> {
  const now = new Date();
  const startOfToday = new Date(
    now.getFullYear(), now.getMonth(), now.getDate(),
  );
  const startOfTomorrow = new Date(startOfToday.getTime() + 86_400_000);
  const startOfDayAfter = new Date(startOfToday.getTime() + 2 * 86_400_000);
  const labelFor = (d: Date): string =>
    d.toLocaleDateString(undefined, { weekday: "long" }).toUpperCase();

  const groups: Record<string, { key: string; label: string; sort: number; items: Pick[] }> = {};
  for (const p of picks) {
    const t = p.event_time ? new Date(p.event_time as any) : null;
    let key = "later", label = "LATER", sort = 9;
    if (!t || isNaN(t.getTime())) {
      key = "tba"; label = "TBA"; sort = 99;
    } else if (t < startOfTomorrow) {
      key = "today"; label = "TODAY"; sort = 0;
    } else if (t < startOfDayAfter) {
      key = "tomorrow"; label = "TOMORROW"; sort = 1;
    } else {
      // Group future days by weekday name (TUESDAY, WEDNESDAY, …) and
      // sort chronologically. Cap at 7 day-keys so we don't sprawl into
      // 14 sections during long-tournament weeks.
      const dayStart = new Date(t.getFullYear(), t.getMonth(), t.getDate());
      const offset = Math.round((dayStart.getTime() - startOfToday.getTime()) / 86_400_000);
      key = `d${offset}`;
      label = labelFor(t);
      sort = 2 + offset;
    }
    if (!groups[key]) groups[key] = { key, label, sort, items: [] };
    groups[key].items.push(p);
  }
  return Object.values(groups).sort((a, b) => a.sort - b.sort);
}

function StatTile({ label, value, color = COLORS.textPrimary }: { label: string; value: string; color?: string }) {
  return (
    <View style={styles.statTile}>
      <Text style={styles.statLabel}>{label}</Text>
      <Text style={[styles.statValue, { color }]}>{value}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  // `safe` is the screen-root container. backgroundColor is "transparent"
  // so the global ImageBackground in app/_layout.tsx (the PerkLocks stadium
  // composite + scrim) shows through every tab.
  safe: { flex: 1, backgroundColor: "transparent" },
  // Phase 17 defect — Locks board scrolling closure (2026-09-02).
  // FlatList requires `flex:1` on the list itself when it lives inside
  // a Safe/View tree that has flex parents — otherwise on React Native
  // Web the list collapses to intrinsic content height and only ~5
  // cards render before the parent viewport clips the rest.  Bottom
  // padding raised to 140 so the FINAL card clears the bottom tab bar
  // (≈ 80px web / 96px iOS + generous safe margin).  Full board (up to
  // 100+ picks) is now physically scrollable end-to-end.
  list: { flex: 1 },
  content: { paddingHorizontal: 20, paddingTop: 10, paddingBottom: 140,
    // Phase 17 defect closure — kept in sync with the new `list`
    // style above; duplicate `content` removed post-consolidation.
  },
  header: {
    paddingHorizontal: 20, paddingTop: 8, paddingBottom: 14,
    flexDirection: "row", justifyContent: "space-between", alignItems: "flex-end",
  },
  brand: {
    fontSize: 30,
    fontWeight: "900",
    color: COLORS.goldElite,
    letterSpacing: 5,
    // Luminous glow around the wordmark — matches mockup §3/§9.
    // Bright inner + wider outer halo for the "premium gold wordmark" feel.
    textShadowColor: "rgba(255,215,0,0.75)",
    textShadowOffset: { width: 0, height: 0 },
    textShadowRadius: 18,
  },
  tagline: {
    fontSize: 10,
    fontWeight: "800",
    color: COLORS.goldRich,
    letterSpacing: 3.2,
    marginTop: 4,
    marginBottom: 2,
    opacity: 0.95,
  },
  date: {
    fontSize: 11.5,
    color: COLORS.textSecondary,
    fontWeight: "600",
    marginTop: 6,
    letterSpacing: 0.4,
  },
  updatedRow: { flexDirection: "row", alignItems: "center", gap: 6, marginTop: 5 },
  updatedDot: {
    width: 7, height: 7, borderRadius: 4,
    backgroundColor: COLORS.neonGreen,
    shadowColor: COLORS.neonGreen,
    shadowOpacity: 0.85,
    shadowRadius: 5,
    shadowOffset: { width: 0, height: 0 },
  },
  updatedLabel: {
    fontSize: 11, color: COLORS.neonGreen, fontWeight: "800", letterSpacing: 0.5,
  },
  cooldownLabel: {
    fontSize: 10.5, color: COLORS.goldRich, fontWeight: "800",
    marginTop: 3, letterSpacing: 0.4,
  },
  // Locks-Mockup 2026-08-22: subtle cooldown stripe under header when
  // a refresh cooldown is active. Restored after PremiumHeader refactor.
  cooldownStripe: {
    fontSize: 11, color: COLORS.goldRich, fontWeight: "800",
    letterSpacing: 0.5, paddingHorizontal: 20, paddingBottom: 4,
    marginTop: -8,
  },
  toast: {
    position: "absolute", top: 110, alignSelf: "center", zIndex: 10,
    flexDirection: "row", alignItems: "center", gap: 8,
    paddingHorizontal: 14, paddingVertical: 10, borderRadius: 24,
    backgroundColor: "rgba(0,0,0,0.92)",
    borderWidth: 1, borderColor: "rgba(0,255,170,0.35)",
  },
  toastText: { color: COLORS.textPrimary, fontSize: 13, fontWeight: "700" },
  stalePill: {
    flexDirection: "row", alignItems: "center", gap: 8,
    paddingHorizontal: 12, paddingVertical: 8, marginBottom: 12,
    borderRadius: 10, borderWidth: 1,
    borderColor: COLORS.borderDefault, backgroundColor: COLORS.surface,
  },
  stalePillTxt: { color: COLORS.goldRich, fontSize: 10.5, fontWeight: "800", letterSpacing: 0.8 },
  refreshBtn: {
    minWidth: 44, height: 44, borderRadius: 22,
    backgroundColor: "rgba(0,0,0,0.65)",
    alignItems: "center", justifyContent: "center",
    borderWidth: 1.4, borderColor: "rgba(255,215,0,0.65)",
    paddingHorizontal: 10,
    shadowColor: "#FFD700",
    shadowOpacity: 0.55,
    shadowRadius: 10,
    shadowOffset: { width: 0, height: 0 },
  },
  refreshBtnDisabled: {
    borderColor: COLORS.goldElite,
    backgroundColor: "rgba(0,0,0,0.85)",
    opacity: 0.95,
  },
  refreshBtnCountdown: {
    color: COLORS.goldElite, fontSize: 11, fontWeight: "900",
    letterSpacing: 0, fontVariant: ["tabular-nums"],
    textAlign: "center",
  },
  statsRow: { flexDirection: "row", paddingHorizontal: 20, gap: 10, marginBottom: 6 },
  controlsRow: { flexDirection: "row", alignItems: "center" },
  filterBtnWrap: { paddingRight: 20, paddingBottom: 10 },
  statTile: {
    flex: 1, padding: 12, borderRadius: 12, borderWidth: 1,
    borderColor: COLORS.borderDefault, backgroundColor: COLORS.surface,
  },
  statLabel: { fontSize: 9, color: COLORS.textMuted, fontWeight: "800", letterSpacing: 1.3 },
  statValue: { fontSize: 20, fontWeight: "900", marginTop: 2, letterSpacing: -0.5 },
  // `content` styles defined earlier alongside `list` (defect closure).
  center: { paddingVertical: 80, alignItems: "center" },
  emptyTitle: { color: COLORS.textPrimary, fontSize: 16, fontWeight: "800", marginTop: 14 },
  emptyMsg: { color: COLORS.textMuted, fontSize: 13, marginTop: 6, textAlign: "center", paddingHorizontal: 24, lineHeight: 18 },
  emptyCard: {
    marginHorizontal: 20,
    marginTop: 30,
    paddingVertical: 28,
    paddingHorizontal: 18,
    backgroundColor: COLORS.surface,
    borderRadius: 16,
    borderWidth: 1,
    borderColor: COLORS.borderDefault,
    alignItems: "center",
  },
  emptyDivider: {
    height: 1,
    width: "60%",
    backgroundColor: COLORS.borderDefault,
    marginVertical: 16,
  },
  emptyHintLabel: {
    color: COLORS.voltBlue,
    fontSize: 11,
    fontWeight: "900",
    letterSpacing: 1.4,
    fontVariant: ["tabular-nums"],
  },
  emptyHintSub: {
    color: COLORS.textMuted,
    fontSize: 11,
    fontWeight: "600",
    marginTop: 6,
    textAlign: "center",
    paddingHorizontal: 16,
    lineHeight: 16,
  },
  // Empty-state recovery CTA — the most-clicked button in the empty
  // state. Bright voltBlue fill so the user can't miss it; replaces
  // the dead-end "pull to refresh" hint as the primary affordance.
  emptyCta: {
    marginTop: 16,
    paddingHorizontal: 18,
    paddingVertical: 11,
    borderRadius: 10,
    backgroundColor: COLORS.voltBlue,
  },
  emptyCtaTxt: {
    color: "#0b0e16",
    fontSize: 12,
    fontWeight: "900",
    letterSpacing: 1.2,
  },
  soccerLabBtn: {
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
    marginHorizontal: 20,
    marginTop: 10,
    paddingHorizontal: 14,
    paddingVertical: 12,
    borderRadius: 12,
    borderWidth: 1,
    borderColor: COLORS.voltBlue + "55",
    backgroundColor: COLORS.voltBlue + "10",
  },
  // NRFI/YRFI CTA — same shape as soccerLabBtn but amber-themed so it
  // reads as a sibling discovery surface, not a duplicate.
  nrfiBtn: {
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
    marginHorizontal: 20,
    marginTop: 8,
    paddingHorizontal: 14,
    paddingVertical: 12,
    borderRadius: 12,
    borderWidth: 1,
    borderColor: "#f59e0b55",
    backgroundColor: "#f59e0b10",
  },
  nrfiIcon: { fontSize: 22 },
  nrfiTitle: {
    color: COLORS.textPrimary,
    fontSize: 13,
    fontWeight: "900",
    letterSpacing: 0.8,
  },
  nrfiSub: {
    color: COLORS.textMuted,
    fontSize: 11,
    fontWeight: "600",
    marginTop: 1,
  },
  // Sort row holds the SortSelector plus the visible UPDATE button.
  sortRow: { flexDirection: "row", alignItems: "center" },
  // UPDATE button — explicit refresh CTA. Power users use pull-to-refresh
  // but the visible button removes the "is anything happening?" doubt.
  updateBtn: {
    paddingHorizontal: 14,
    paddingVertical: 8,
    borderRadius: 10,
    borderWidth: 1.4,
    borderColor: "rgba(255,215,0,0.80)",
    backgroundColor: "rgba(0,0,0,0.65)",
    marginRight: 20,
    marginBottom: 10,
    minWidth: 72,
    alignItems: "center",
    shadowColor: "#FFD700",
    shadowOpacity: 0.45,
    shadowRadius: 8,
    shadowOffset: { width: 0, height: 0 },
  },
  updateBtnTxt: {
    color: COLORS.goldElite,
    fontSize: 11,
    fontWeight: "900",
    letterSpacing: 1.5,
  },
  // Reset-all-filters pill — destructive accent, only shown when any
  // narrowing predicate is active. Placed between the GameFilter
  // button and the UPDATE button on the controls row.
  resetAllBtn: {
    flexDirection: "row",
    alignItems: "center",
    gap: 4,
    paddingHorizontal: 10,
    paddingVertical: 7,
    borderRadius: 10,
    borderWidth: 1,
    borderColor: COLORS.electricBlaze,
    backgroundColor: COLORS.electricBlaze + "12",
    marginLeft: 8,
    marginRight: 8,
    marginBottom: 10,
    minHeight: 30,
  },
  resetAllBtnTxt: {
    color: COLORS.electricBlaze,
    fontSize: 10.5,
    fontWeight: "900",
    letterSpacing: 1.0,
  },
  // Date-section grouping for the Locks feed.
  dayGroup: { marginBottom: 4 },
  dayHeader: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingHorizontal: 20,
    paddingVertical: 10,
    marginTop: 8,
    borderBottomWidth: 1,
    borderBottomColor: COLORS.borderDefault,
  },
  dayLabel: {
    color: COLORS.textPrimary,
    fontSize: 13,
    fontWeight: "900",
    letterSpacing: 1.4,
  },
  dayCount: {
    color: COLORS.textMuted,
    fontSize: 10,
    fontWeight: "800",
    letterSpacing: 1,
  },
  soccerLabIcon: { fontSize: 22 },
  soccerLabTitle: {
    color: COLORS.voltBlue,
    fontSize: 12,
    fontWeight: "900",
    letterSpacing: 1.2,
  },
  soccerLabSub: {
    color: COLORS.textSecondary,
    fontSize: 11,
    fontWeight: "600",
    marginTop: 2,
  },
  soccerLabChevron: {
    color: COLORS.voltBlue,
    fontSize: 24,
    fontWeight: "300",
  },
});
