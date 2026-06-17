import React, { useCallback, useEffect, useState } from "react";
import {
  View, Text, StyleSheet, ScrollView, RefreshControl,
  ActivityIndicator, Pressable,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { Ionicons } from "@expo/vector-icons";
import { COLORS, SPORTS } from "@/src/theme";
import { api, Pick, LineType, SortKey, PickFilters } from "@/src/lib/api";
import { LockPickCard } from "@/src/components/LockPickCard";
import { ChipRow } from "@/src/components/ChipRow";
import { LineTypeToggle } from "@/src/components/LineTypeToggle";
import { SortSelector } from "@/src/components/SortSelector";
import { FilterButton, FilterSheet } from "@/src/components/FilterSheet";
import { SportFilterBar } from "@/src/components/SportFilterBar";

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
  const [, forceTick] = useState(0);

  // Tick every 30s so the "X min ago" label stays accurate.
  useEffect(() => {
    const t = setInterval(() => forceTick((n) => n + 1), 30000);
    return () => clearInterval(t);
  }, []);

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
      setPicks(picksRes.picks);
      if (statsRes) setStats(statsRes);
      setLastLoadedAt(new Date());
    } catch (e) {
      console.warn("load locks", e);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => { setLoading(true); load(sport, lineType, sortKey, filters); }, [sport, lineType, sortKey, filters, load]);

  const onRefresh = () => { setRefreshing(true); load(sport, lineType, sortKey, filters); };

  const showToast = (msg: string) => {
    setToast(msg);
    setTimeout(() => setToast(null), 2200);
  };

  const onForceRefresh = async () => {
    setLoading(true);
    try {
      const res = await api.refresh();
      await load(sport, lineType, sortKey, filters);
      showToast(`Refreshed · ${res.count} picks`);
    } catch (e) {
      console.warn(e);
      setLoading(false);
      showToast("Refresh failed");
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
        </View>
        <Pressable
          testID="refresh-button"
          onPress={onForceRefresh}
          style={styles.refreshBtn}
          hitSlop={10}
        >
          <Ionicons name="refresh" size={20} color={COLORS.textPrimary} />
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
          <View style={styles.center}>
            <Ionicons name="lock-open-outline" size={48} color={COLORS.textMuted} />
            <Text style={styles.emptyTitle}>No games available</Text>
            <Text style={styles.emptyMsg}>
              {sport === "All"
                ? "No fixtures on the board right now. Pull to refresh."
                : `No ${sport} fixtures returned by the sports API for today. Try another sport or refresh.`}
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
  toast: {
    position: "absolute", top: 110, alignSelf: "center", zIndex: 10,
    flexDirection: "row", alignItems: "center", gap: 8,
    paddingHorizontal: 14, paddingVertical: 10, borderRadius: 24,
    backgroundColor: "rgba(0,0,0,0.92)",
    borderWidth: 1, borderColor: "rgba(0,255,170,0.35)",
  },
  toastText: { color: COLORS.textPrimary, fontSize: 13, fontWeight: "700" },
  refreshBtn: {
    width: 40, height: 40, borderRadius: 20,
    backgroundColor: COLORS.surface, alignItems: "center", justifyContent: "center",
    borderWidth: 1, borderColor: COLORS.borderDefault,
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
  emptyMsg: { color: COLORS.textMuted, fontSize: 13, marginTop: 6, textAlign: "center", paddingHorizontal: 40 },
});
