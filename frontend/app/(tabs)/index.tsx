import React, { useCallback, useEffect, useRef, useState } from "react";
import {
  View, Text, StyleSheet, ScrollView, RefreshControl,
  ActivityIndicator, Pressable, TouchableOpacity, Animated, Easing,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { router } from "expo-router";
import { Ionicons } from "@expo/vector-icons";
import { COLORS, SPORTS } from "@/src/theme";
import { api, Pick, LineType, SortKey, SortDirection, PickFilters } from "@/src/lib/api";
import { LockPickCard } from "@/src/components/LockPickCard";
import { ChipRow } from "@/src/components/ChipRow";
import { FilterButton, FilterSheet } from "@/src/components/FilterSheet";
import { GameFilterButton, GameFilterSheet } from "@/src/components/GameFilterSheet";
import { SportFilterBar } from "@/src/components/SportFilterBar";
import { NFLIntelligenceSection } from "@/src/components/NFLIntelligenceSection";
import { StaleVersionBanner } from "@/src/components/StaleVersionBanner";
import { StaleBuildBanner } from "@/src/components/StaleBuildBanner";
import { EventGroupSkeleton } from "@/src/components/Skeleton";
import { storage } from "@/src/utils/storage";
import { useFocusRefetch } from "@/src/lib/useFocusRefetch";
import { useFilters } from "@/src/stores/useFilters";

const PREFS_KEY = "locks_feed_prefs_v2";
type FeedPrefs = { sport?: string; sortKey?: SortKey; lineType?: LineType };

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
  const [picks, setPicks] = useState<Pick[]>([]);
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
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [stats, setStats] = useState<{ total_picks: number; elite_count: number; avg_edge_percent: number } | null>(null);
  const [lastLoadedAt, setLastLoadedAt] = useState<Date | null>(null);
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
  const pulseOpacity = pulseAnim.interpolate({ inputRange: [0, 1], outputRange: [0.55, 1] });

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
  const visiblePicks = filterStore.events.length > 0
    ? picks.filter((p) => filterStore.events.includes(p.event || ""))
    : filters.event
      ? picks.filter((p) => (p.event || "") === filters.event)
      : picks;

  const clearAllNarrowingFilters = () => {
    // Wipe BOTH local pick-filters AND the persisted multi-select
    // arrays in the global store. Sport + sort + lineType are
    // preserved (those are view prefs, not filters). User can hit
    // RESET ALL on the top bar to wipe the store completely.
    setFilters({});
    resetAllFilters();
  };

  // Request-token guard: each call to load() captures a monotonically
  // increasing token. When the response arrives, we only commit state
  // if the token still matches `latestLoadTokenRef.current` (i.e. no
  // newer load was kicked off in the meantime). Without this guard,
  // switching sports rapidly OR an in-flight previous-sport request
  // landing after a focus refetch will populate the WRONG sport tab —
  // exactly the bug user reported: "soccer under Tennis... fixes
  // itself but can we stop this".
  const latestLoadTokenRef = useRef(0);
  const lastLoadedForSportRef = useRef<string>("");
  // Last filter signature painted on screen. We use this to flush the
  // picks array IMMEDIATELY when the user changes ANY narrowing
  // filter (market pill / league pill / game pill) — without this,
  // the previous filter's picks linger on screen while the new fetch
  // is in flight, making H+R+RBI picks appear under the Strikeouts
  // pill etc. (user report 2026-06-25: "make organized hit run rbi be
  // under strikeouts sometimes" / "takes me back to the main tab").
  const lastFilterSignatureRef = useRef<string>("");

  const load = useCallback(async (s: string, lt: LineType, sk: SortKey, f: PickFilters, dir: SortDirection) => {
    const myToken = latestLoadTokenRef.current + 1;
    latestLoadTokenRef.current = myToken;
    // Snapshot the requested sport so a late response can prove it
    // matches the CURRENTLY selected sport before painting picks.
    const requestedSport = s;
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
        }),
        api.stats().catch(() => null),
      ]);
      // Discard if a newer load was fired after we sent this one.
      if (myToken !== latestLoadTokenRef.current) return;
      // Clear any prior load-error banner — we got a clean response.
      setLoadError(null);
      // Defensive client-side filter — protect users from production
      // backends that haven't yet deployed the KBO removal. We do NOT
      // filter by event_time here because player props for in-progress
      // games (e.g. batter Over 0.5 Hits) are still legitimate locks
      // that the user wants to see on the slate even after first pitch.
      let fresh = (picksRes.picks || []).filter((p: any) => p.sport !== "KBO");
      // Sport-mismatch guard (uses requestedSport declared at top of try block)
      if (requestedSport && requestedSport.toLowerCase() !== "all") {
        fresh = fresh.filter((p: any) => p.sport === requestedSport);
      }
      // NOTE: filters.event is applied at RENDER time (see `visiblePicks`
      // below) — not here — so the GameFilterSheet always sees every
      // game on the slate. Filtering here would shrink the sheet's
      // dropdown to the currently-selected event only.
      // Sim Edge floor (replaces the old binary toggle 2026-06-24, user
      // feedback: "Sim edge blocking a lot of picks — I just wanted it
      // to able to be filtered"). User now picks their own floor via
      // the FilterSheet chip strip. Backward-compat: legacy
      // `simEdgeOnly: true` from older client state still works via the
      // FilterSheet's initialiser which maps it to 75.
      //
      // 2026-06-27: SYNTHETIC PICKS ARE EXEMPT.
      // Synthetic CSL goalscorers (Cryzan, Felipe Silva, Fábio Abreu,
      // Leonardo, Bakambu, Negrão, Wesley Moraes, etc.) use a closed-
      // form Poisson model — there's no 10k-run Monte Carlo so
      // `sim_win_probability` is whatever the Poisson math returned
      // (32-65%). Stale `simEdgeFloor=75` state from a previous mobile
      // session would silently strip every single one of them and the
      // user lands on "No locks on the board" even though the API
      // returned 9 picks. Bypass the floor for any pick tagged
      // `synthetic` or `force_injected` — the user clearly opted in
      // by selecting CSL + Anytime Goal Scorer, and the lock_score is
      // already the source of truth for confidence (97 for tier-1
      // Golden Boot winners).
      const simFloor =
        typeof f.simEdgeFloor === "number" && f.simEdgeFloor > 0
          ? f.simEdgeFloor
          : f.simEdgeOnly
            ? 75
            : 0;
      if (simFloor > 0) {
        fresh = fresh.filter((p: any) =>
          // Synthetic picks bypass the Sim Edge floor entirely.
          p.synthetic === true ||
          p.force_injected === true ||
          (p.synthetic_source && String(p.synthetic_source).length > 0) ||
          // Real picks: keep if they meet the sim floor.
          (typeof p.sim_win_probability === "number" &&
            p.sim_win_probability >= simFloor),
        );
      }
      // CRITICAL (2026-06-28, patched 2026-02 iter-84): if the new
      // response came back EMPTY but we already had cached picks for
      // the same sport filter, treat it as a transient "slate
      // refreshing" signal and KEEP the cached picks visible. The
      // backend refresh briefly returns picks=[] for <100ms during the
      // atomic-swap window; without this guard the user sees their
      // slate vanish to "No locks on the board" every refresh tick.
      // Per user spec: "do NOT clear existing picks".
      //
      // IMPORTANT: read cached count from `picksRef.current` (mirror
      // of the picks state) — reading `picks` directly here captures
      // the initial-render snapshot from the useCallback closure and
      // the guard NEVER fires. This was the confirmed root cause of
      // the iter-84 "loaded picks then crashed" bug report.
      const lastSport = lastLoadedForSportRef.current;
      const sameFilter = lastSport === requestedSport;
      if (fresh.length === 0 && picksRef.current.length > 0 && sameFilter) {
        setLoadError("Slate refreshing… showing your cached picks. Tap to retry.");
        // Skip setPicks([]) — keep cached.
        return;
      }
      setPicks(fresh);
      lastLoadedForSportRef.current = requestedSport;
      // Alt-line availability diagnostic (2026-07-13): backend tells us
      // when this ALT query hit a book-coverage gap so we can render
      // the reason in the empty state instead of a generic "no locks".
      const altDiag: any = (picksRes as any).alt_availability;
      if (lt === "alt" && fresh.length === 0 && altDiag && altDiag.supported === false) {
        setAltUnavailable({
          message: String(altDiag.message || ""),
          suggestion: altDiag.suggestion ? String(altDiag.suggestion) : undefined,
        });
      } else {
        setAltUnavailable(null);
      }
      // Stats: if backend hasn't deployed KBO removal yet, recompute the
      // top-row totals locally so the hero card matches the visible list.
      if (statsRes) {
        const kboCount = (picksRes.picks || []).filter((p: any) => p.sport === "KBO").length;
        if (kboCount > 0 && typeof statsRes.total_picks === "number") {
          setStats({
            ...statsRes,
            total_picks: Math.max(0, statsRes.total_picks - kboCount),
          });
        } else {
          setStats(statsRes);
        }
      }
      setLastLoadedAt(new Date());
    } catch (e: any) {
      // CRITICAL (2026-06-28): preserve previously loaded picks on a
      // network failure (e.g. Cloudflare 520 during a uvicorn --reload
      // window). We deliberately DO NOT call setPicks([]). Instead we
      // surface a lightweight retry banner over the cached slate so
      // the user keeps seeing the last good picks and can tap to
      // recover.
      if (myToken === latestLoadTokenRef.current) {
        const msg = (e && e.message) ? String(e.message) : "Network error";
        // Strip noisy CF 520 HTML if the body bled through.
        const cleanMsg = /520|cloudflare|origin web server/i.test(msg)
          ? "Connection hiccup — tap to retry."
          : msg.length > 140 ? msg.slice(0, 140) + "…" : msg;
        setLoadError(cleanMsg);
        console.warn("load locks failed (cached picks kept):", e);
      }
    } finally {
      // Only clear loading flags if this is still the latest request.
      if (myToken === latestLoadTokenRef.current) {
        setLoading(false);
        setRefreshing(false);
      }
    }
  }, [
    // load() captures filterStore.{sports,leagues,markets,gameIds,searchText}
    // via its closure on each render. We MUST list them here so React
    // recreates the callback whenever the store changes — otherwise the
    // first-render's empty arrays are baked in forever and clicking
    // chips never affects the fetched picks.
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
      setPicks([]);
    }
    lastFilterSignatureRef.current = sig;
    setLoading(true);
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
    () => { load(sport, lineType, sortKey, filters, sortDir); loadCooldown(); },
    [sport, lineType, sortKey, filters, sortDir, load, loadCooldown],
    5_000,
  );

  const onRefresh = () => {
    setRefreshing(true);
    load(sport, lineType, sortKey, filters, sortDir);
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
      const res = await api.refresh();
      // Backend tells us the next-allowed time. Drive the countdown from
      // that instead of guessing client-side.
      if (res.next_refresh_at) {
        const t = Date.parse(res.next_refresh_at);
        if (!isNaN(t)) setNextRefreshAt(t);
      }
      await load(sport, lineType, sortKey, filters, sortDir);
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
      <View style={styles.header}>
        <View style={{ flex: 1 }}>
          <Text style={styles.brand}>PERKLOCKS</Text>
          <Text style={styles.tagline}>LOCK IN. CASH OUT.</Text>
          <Text style={styles.date}>
            Today&apos;s Locks · {new Date().toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" })}
          </Text>
          <View style={styles.updatedRow}>
            <Animated.View style={[styles.updatedDot, { opacity: pulseOpacity }]} />
            <Text style={styles.updatedLabel}>Updated {timeAgo(lastLoadedAt)}</Text>
          </View>
          {remaining > 0 && (
            <Text style={styles.cooldownLabel} testID="refresh-cooldown-label">
              New picks in {formatCountdown(remaining)}
            </Text>
          )}
        </View>
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
      </View>

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
      <StaleVersionBanner onRefresh={() => load(sport, lineType, sortKey, filters, sortDir)} />

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
          totalGames={Array.from(new Set(picks.map(p => p.event).filter(Boolean))).length}
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

      <ScrollView
        contentContainerStyle={styles.content}
        refreshControl={<RefreshControl tintColor={COLORS.textPrimary} refreshing={refreshing} onRefresh={onRefresh} />}
        showsVerticalScrollIndicator={false}
        testID="locks-scroll"
      >
        {/* RETRY BANNER (2026-06-28): renders on top of cached picks
            when the last /api/picks/today fetch failed (e.g. Cloudflare
            520 during a worker reload). Tapping triggers an immediate
            re-fetch; cached picks remain visible underneath so the
            user is never dumped into an empty board on a transient
            network blip. */}
        {!!loadError && (
          <TouchableOpacity
            activeOpacity={0.85}
            onPress={() => {
              setLoadError(null);
              setRefreshing(true);
              load(sport, lineType, sortKey, filters, sortDir);
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
                Connection hiccup
              </Text>
              <Text style={{ color: "rgba(255,255,255,0.78)", fontSize: 12, marginTop: 2 }}>
                Showing your last good slate. Tap to retry.
              </Text>
            </View>
            <Text style={{ color: "#ffb4b4", fontWeight: "800", fontSize: 13 }}>
              RETRY ↻
            </Text>
          </TouchableOpacity>
        )}
        {loading ? (
          <View testID="board-skeleton">
            {/* Milestone 1.2 — Skeleton loader replaces the plain spinner
                so the user sees the SHAPE of what's about to appear
                (event groups + pick cards) instead of a blank frame.
                Matches the real event-grouped list layout exactly. */}
            <EventGroupSkeleton picks={2} />
            <EventGroupSkeleton picks={3} />
            <EventGroupSkeleton picks={2} />
            <View style={styles.center}>
              <ActivityIndicator color={COLORS.voltBlue} />
            </View>
          </View>
        ) : visiblePicks.length === 0 ? (
          <View style={styles.emptyCard} testID="empty-board">
            <Ionicons
              name={altUnavailable ? "information-circle-outline" : "lock-open-outline"}
              size={42}
              color={COLORS.textMuted}
            />
            <Text style={styles.emptyTitle}>
              {altUnavailable ? "Alt lines unavailable" : "No locks on the board"}
            </Text>

            {/* Alt-line book-coverage-gap diagnostic (2026-07-13).
                When the ALT tab is empty because the current sport's
                tournaments are outside The Odds API's alt-market
                coverage (currently: every tennis tournament we
                surface — Umag, Bastad, Gstaad, Iasi WTA, Athens WTA,
                Kitzbühel WTA — is 250-tier and not covered), show
                the backend-provided explanation + suggestion instead
                of the generic "no locks" empty state. */}
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
            ) :
            /* ── Self-diagnostic empty state ──
                Recurring P0 ("App still not showing picks") usually has
                one of three root causes: (1) user has a narrowing
                filter on (SIM EDGE / market / lock floor) and forgot,
                (2) user's persisted sport tab is on MLB/NBA/NFL which
                has 0 picks today while Soccer/Tennis still do, or (3)
                the slate genuinely has nothing live. We surface the
                most likely cause + a 1-tap fix instead of a dead-end
                "pull to refresh" message. */
            filtersAreNarrowing ? (
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
        ) : (
          /* Picks render in the grouped block below */ null
        )}
        {/* Grouped render — replaces the flat list when picks exist.
            First pick of the FIRST group ("TODAY" in typical case) is
            visually promoted as the "featured" hero card per mockup §4:
            layered black/glass surface, subtle sport-colored outer glow,
            stadium ambient gradient, and stronger premium border. */}
        {visiblePicks.length > 0 && groupPicksByDay(visiblePicks).map((group, gIdx) => {
          const uniqueEvents = new Set(group.items.map((p) => p.event || "")).size;
          return (
            <View key={group.key} style={styles.dayGroup}>
              <View style={styles.dayHeader}>
                <Text style={styles.dayLabel}>{group.label}</Text>
                <Text style={styles.dayCount}>
                  {uniqueEvents} {uniqueEvents === 1 ? "GAME" : "GAMES"} · {group.items.length} {group.items.length === 1 ? "PICK" : "PICKS"}
                </Text>
              </View>
              {group.items.map((p, pIdx) => (
                <LockPickCard
                  key={p.id}
                  pick={p}
                  featured={gIdx === 0 && pIdx === 0}
                />
              ))}
            </View>
          );
        })}
      </ScrollView>
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
  toast: {
    position: "absolute", top: 110, alignSelf: "center", zIndex: 10,
    flexDirection: "row", alignItems: "center", gap: 8,
    paddingHorizontal: 14, paddingVertical: 10, borderRadius: 24,
    backgroundColor: "rgba(0,0,0,0.92)",
    borderWidth: 1, borderColor: "rgba(0,255,170,0.35)",
  },
  toastText: { color: COLORS.textPrimary, fontSize: 13, fontWeight: "700" },
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
  content: { paddingHorizontal: 20, paddingTop: 10, paddingBottom: 24 },
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
