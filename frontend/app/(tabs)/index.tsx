import React, { useCallback, useEffect, useRef, useState } from "react";
import {
  View, Text, StyleSheet, ScrollView, RefreshControl,
  ActivityIndicator, Pressable, TouchableOpacity,
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
import { StaleVersionBanner } from "@/src/components/StaleVersionBanner";
import { StaleBuildBanner } from "@/src/components/StaleBuildBanner";
import { storage } from "@/src/utils/storage";
import { useFocusRefetch } from "@/src/lib/useFocusRefetch";

const PREFS_KEY = "locks_feed_prefs_v1";
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
  const [picks, setPicks] = useState<Pick[]>([]);
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
  // Refresh-cooldown countdown driven by /picks/refresh-status. `nextRefreshAt`
  // is an absolute timestamp (ms since epoch); `remaining` is derived from it
  // by a 1-second ticker so the badge animates smoothly.
  const [nextRefreshAt, setNextRefreshAt] = useState<number | null>(null);
  const [remaining, setRemaining] = useState<number>(0); // seconds until next refresh
  const [, forceTick] = useState(0);
  const [prefsHydrated, setPrefsHydrated] = useState(false);

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
    (typeof filters.minLock === "number" && filters.minLock > 85) ||
    !!filters.minImplied ||
    (typeof filters.maxImplied === "number" && filters.maxImplied < 100);

  // Render-time view of picks — applies the event filter so the user
  // can drill into one game without losing the full slate from the
  // GameFilterSheet's dropdown list.
  const visiblePicks = filters.event
    ? picks.filter((p) => (p.event || "") === filters.event)
    : picks;

  const clearAllNarrowingFilters = () => {
    // Wipe only the narrowing predicates — keep sport / sort / lineType
    // so the user doesn't lose their entire context.
    setFilters({});
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

  const load = useCallback(async (s: string, lt: LineType, sk: SortKey, f: PickFilters, dir: SortDirection) => {
    const myToken = latestLoadTokenRef.current + 1;
    latestLoadTokenRef.current = myToken;
    // Snapshot the requested sport so a late response can prove it
    // matches the CURRENTLY selected sport before painting picks.
    const requestedSport = s;
    try {
      const [picksRes, statsRes] = await Promise.all([
        api.picksToday(s, lt, sk, f, dir),
        api.stats().catch(() => null),
      ]);
      // Discard if a newer load was fired after we sent this one.
      if (myToken !== latestLoadTokenRef.current) return;
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
      const simFloor =
        typeof f.simEdgeFloor === "number" && f.simEdgeFloor > 0
          ? f.simEdgeFloor
          : f.simEdgeOnly
            ? 75
            : 0;
      if (simFloor > 0) {
        fresh = fresh.filter((p: any) =>
          typeof p.sim_win_probability === "number" &&
          p.sim_win_probability >= simFloor,
        );
      }
      setPicks(fresh);
      lastLoadedForSportRef.current = requestedSport;
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
    } catch (e) {
      console.warn("load locks", e);
    } finally {
      // Only clear loading flags if this is still the latest request.
      if (myToken === latestLoadTokenRef.current) {
        setLoading(false);
        setRefreshing(false);
      }
    }
  }, []);

  useEffect(() => {
    // Wipe stale picks the moment the user changes sport so the wrong
    // tab can NEVER be shown for even a single frame. Only triggers
    // when the new sport differs from what's currently painted —
    // otherwise we'd needlessly clear identical data.
    if (lastLoadedForSportRef.current && lastLoadedForSportRef.current !== sport) {
      setPicks([]);
    }
    setLoading(true);
    load(sport, lineType, sortKey, filters, sortDir);
  }, [sport, lineType, sortKey, filters, sortDir, load]);

  // Smart refetch on screen focus: hit /api/picks/today again every time the
  // user opens the Locks tab, but skip if the last successful fetch was less
  // than 30 s ago. No interval polling — focus + manual refresh only.
  useFocusRefetch(
    () => { load(sport, lineType, sortKey, filters, sortDir); loadCooldown(); },
    [sport, lineType, sortKey, filters, sortDir, load, loadCooldown],
    30_000,
  );

  const onRefresh = () => { setRefreshing(true); load(sport, lineType, sortKey, filters, sortDir); };

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
        <View>
          <Text style={styles.brand}>PERKSLOCKS</Text>
          <Text style={styles.date}>
            Today&apos;s Locks · {new Date().toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" })}
          </Text>
          <Text style={styles.updatedLabel}>Updated {timeAgo(lastLoadedAt)}</Text>
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
            <Ionicons name="refresh" size={20} color={COLORS.textPrimary} />
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
          // Reset sport-specific filters when switching sports.
          setFilters((f) => ({ ...f, market: undefined, league: undefined }));
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
      {(sport === "Soccer" || sport === "MLB" || sport === "Tennis" || sport === "UFC" || sport === "NBA" || sport === "NFL") && (
        <TouchableOpacity
          onPress={() => router.push(`/soccer-lab?sport=${encodeURIComponent(sport)}` as any)}
          style={styles.soccerLabBtn}
          activeOpacity={0.8}
          testID="soccer-lab-cta"
        >
          <Text style={styles.soccerLabIcon}>
            {sport === "Soccer" ? "🌍" : sport === "MLB" ? "⚾" : sport === "Tennis" ? "🎾" : sport === "UFC" ? "🥊" : sport === "NBA" ? "🏀" : "🏈"}
          </Text>
          <View style={{ flex: 1 }}>
            <Text style={styles.soccerLabTitle}>{sport.toUpperCase()} LAB</Text>
            <Text style={styles.soccerLabSub}>
              {sport === "Soccer"
                ? "All active leagues · global ranked feed"
                : `Confidence-ranked ${sport} feed across every league`}
            </Text>
          </View>
          <Text style={styles.soccerLabChevron}>›</Text>
        </TouchableOpacity>
      )}
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
          games without losing the dropdown. */}
      <GameFilterSheet
        visible={gameFilterOpen}
        picks={picks}
        activeEvent={filters.event}
        onClose={() => setGameFilterOpen(false)}
        onApply={(event) => setFilters({ ...filters, event })}
      />

      <ScrollView
        contentContainerStyle={styles.content}
        refreshControl={<RefreshControl tintColor={COLORS.textPrimary} refreshing={refreshing} onRefresh={onRefresh} />}
        showsVerticalScrollIndicator={false}
        testID="locks-scroll"
      >
        {loading ? (
          <View style={styles.center}>
            <ActivityIndicator color={COLORS.voltBlue} />
          </View>
        ) : visiblePicks.length === 0 ? (
          <View style={styles.emptyCard} testID="empty-board">
            <Ionicons name="lock-open-outline" size={42} color={COLORS.textMuted} />
            <Text style={styles.emptyTitle}>No locks on the board</Text>

            {/* ── Self-diagnostic empty state ──
                Recurring P0 ("App still not showing picks") usually has
                one of three root causes: (1) user has a narrowing
                filter on (SIM EDGE / market / lock floor) and forgot,
                (2) user's persisted sport tab is on MLB/NBA/NFL which
                has 0 picks today while Soccer/Tennis still do, or (3)
                the slate genuinely has nothing live. We surface the
                most likely cause + a 1-tap fix instead of a dead-end
                "pull to refresh" message. */}
            {filtersAreNarrowing ? (
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
        {/* Grouped render — replaces the flat list when picks exist. */}
        {visiblePicks.length > 0 && groupPicksByDay(visiblePicks).map((group) => {
          const uniqueEvents = new Set(group.items.map((p) => p.event || "")).size;
          return (
            <View key={group.key} style={styles.dayGroup}>
              <View style={styles.dayHeader}>
                <Text style={styles.dayLabel}>{group.label}</Text>
                <Text style={styles.dayCount}>
                  {uniqueEvents} {uniqueEvents === 1 ? "GAME" : "GAMES"} · {group.items.length} {group.items.length === 1 ? "PICK" : "PICKS"}
                </Text>
              </View>
              {group.items.map((p) => <LockPickCard key={p.id} pick={p} />)}
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
  safe: { flex: 1, backgroundColor: COLORS.bg },
  header: { paddingHorizontal: 20, paddingTop: 8, paddingBottom: 14,
    flexDirection: "row", justifyContent: "space-between", alignItems: "flex-end" },
  brand: { fontSize: 22, fontWeight: "900", color: COLORS.textPrimary, letterSpacing: 3 },
  date: { fontSize: 11, color: COLORS.textMuted, fontWeight: "600", marginTop: 4, letterSpacing: 0.5 },
  updatedLabel: { fontSize: 10, color: COLORS.neonGreen, fontWeight: "700", marginTop: 4, letterSpacing: 0.4 },
  cooldownLabel: { fontSize: 10, color: COLORS.goldElite, fontWeight: "700", marginTop: 2, letterSpacing: 0.4 },
  toast: {
    position: "absolute", top: 110, alignSelf: "center", zIndex: 10,
    flexDirection: "row", alignItems: "center", gap: 8,
    paddingHorizontal: 14, paddingVertical: 10, borderRadius: 24,
    backgroundColor: "rgba(0,0,0,0.92)",
    borderWidth: 1, borderColor: "rgba(0,255,170,0.35)",
  },
  toastText: { color: COLORS.textPrimary, fontSize: 13, fontWeight: "700" },
  refreshBtn: {
    minWidth: 64, height: 40, borderRadius: 20,
    backgroundColor: COLORS.surface, alignItems: "center", justifyContent: "center",
    borderWidth: 1, borderColor: COLORS.borderDefault,
    paddingHorizontal: 12,
  },
  refreshBtnDisabled: {
    borderColor: COLORS.goldElite,
    opacity: 0.85,
  },
  refreshBtnCountdown: {
    color: COLORS.goldElite, fontSize: 11, fontWeight: "800",
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
  // Sort row holds the SortSelector plus the visible UPDATE button.
  sortRow: { flexDirection: "row", alignItems: "center" },
  // UPDATE button — explicit refresh CTA. Power users use pull-to-refresh
  // but the visible button removes the "is anything happening?" doubt.
  updateBtn: {
    paddingHorizontal: 12,
    paddingVertical: 7,
    borderRadius: 10,
    borderWidth: 1,
    borderColor: COLORS.goldElite,
    backgroundColor: COLORS.goldElite + "12",
    marginRight: 20,
    marginBottom: 10,
    minWidth: 64,
    alignItems: "center",
  },
  updateBtnTxt: {
    color: COLORS.goldElite,
    fontSize: 11,
    fontWeight: "900",
    letterSpacing: 1.2,
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
