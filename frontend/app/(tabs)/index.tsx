import React, { useCallback, useEffect, useState } from "react";
import {
  View, Text, StyleSheet, ScrollView, RefreshControl,
  ActivityIndicator, Pressable, TouchableOpacity,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { router } from "expo-router";
import { Ionicons } from "@expo/vector-icons";
import { COLORS, SPORTS } from "@/src/theme";
import { api, Pick, LineType, SortKey, PickFilters } from "@/src/lib/api";
import { LockPickCard } from "@/src/components/LockPickCard";
import { ChipRow } from "@/src/components/ChipRow";
import { LineTypeToggle } from "@/src/components/LineTypeToggle";
import { SortSelector } from "@/src/components/SortSelector";
import { FilterButton, FilterSheet } from "@/src/components/FilterSheet";
import { SportFilterBar } from "@/src/components/SportFilterBar";
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
  const [sortKey, setSortKey] = useState<SortKey>("time");
  const [filters, setFilters] = useState<PickFilters>({});
  const [filterOpen, setFilterOpen] = useState(false);
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
    (filters.maxImplied && filters.maxImplied < 100 ? 1 : 0);

  const load = useCallback(async (s: string, lt: LineType, sk: SortKey, f: PickFilters) => {
    try {
      const [picksRes, statsRes] = await Promise.all([
        api.picksToday(s, lt, sk, f),
        api.stats().catch(() => null),
      ]);
      // Defensive client-side filter — protect users from production
      // backends that haven't yet deployed the KBO removal. We do NOT
      // filter by event_time here because player props for in-progress
      // games (e.g. batter Over 0.5 Hits) are still legitimate locks
      // that the user wants to see on the slate even after first pitch.
      const fresh = (picksRes.picks || []).filter((p: any) => p.sport !== "KBO");
      setPicks(fresh);
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
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => { setLoading(true); load(sport, lineType, sortKey, filters); }, [sport, lineType, sortKey, filters, load]);

  // Smart refetch on screen focus: hit /api/picks/today again every time the
  // user opens the Locks tab, but skip if the last successful fetch was less
  // than 30 s ago. No interval polling — focus + manual refresh only.
  useFocusRefetch(
    () => { load(sport, lineType, sortKey, filters); loadCooldown(); },
    [sport, lineType, sortKey, filters, load, loadCooldown],
    30_000,
  );

  const onRefresh = () => { setRefreshing(true); load(sport, lineType, sortKey, filters); };

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
      await load(sport, lineType, sortKey, filters);
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

      {stats && (
        <View style={styles.statsRow}>
          <StatTile label="LOCKS" value={`${stats.total_picks}`} />
          <StatTile label="ELITE" value={`${stats.elite_count}`} color={COLORS.goldElite} />
          <StatTile
            label="AVG EDGE"
            value={`${stats.avg_edge_percent > 0 ? "+" : ""}${stats.avg_edge_percent}%`}
            color={COLORS.neonGreen}
          />
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
      <View style={styles.controlsRow}>
        <View style={{ flex: 1 }}>
          <LineTypeToggle value={lineType} onChange={setLineType} testIDPrefix="locks-line" />
        </View>
        <View style={styles.filterBtnWrap}>
          <FilterButton
            onPress={() => setFilterOpen(true)}
            activeCount={activeFilterCount}
            testID="locks-filter-button"
          />
        </View>
      </View>
      <SortSelector value={sortKey} onChange={setSortKey} testIDPrefix="locks-sort" />
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
        ) : picks.length === 0 ? (
          <View style={styles.emptyCard}>
            <Ionicons name="lock-open-outline" size={42} color={COLORS.textMuted} />
            <Text style={styles.emptyTitle}>No locks on the board</Text>
            <Text style={styles.emptyMsg}>
              {sport === "All"
                ? "All today's games are either started or below our lock-score gate."
                : `No pregame ${sport} setups cleared the lock-score gate.`}
            </Text>
            <View style={styles.emptyDivider} />
            <Text style={styles.emptyHintLabel}>
              {remaining > 0
                ? `NEXT REFRESH IN ${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, "0")}`
                : "PULL DOWN TO REFRESH"}
            </Text>
            <Text style={styles.emptyHintSub}>
              Tip: try other sports — soccer + tennis often have late slates.
            </Text>
          </View>
        ) : (
          picks.map((p) => <LockPickCard key={p.id} pick={p} />)
        )}
      </ScrollView>
    </SafeAreaView>
  );
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
    width: 56, height: 40, borderRadius: 20,
    backgroundColor: COLORS.surface, alignItems: "center", justifyContent: "center",
    borderWidth: 1, borderColor: COLORS.borderDefault,
    paddingHorizontal: 8,
  },
  refreshBtnDisabled: {
    borderColor: COLORS.goldElite,
    opacity: 0.85,
  },
  refreshBtnCountdown: {
    color: COLORS.goldElite, fontSize: 12, fontWeight: "800",
    letterSpacing: 0.3, fontVariant: ["tabular-nums"],
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
