import React, { useCallback, useEffect, useState } from "react";
import {
  View, Text, StyleSheet, ScrollView, RefreshControl,
  ActivityIndicator, Pressable,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { Ionicons } from "@expo/vector-icons";
import { COLORS, SPORTS } from "@/src/theme";
import { api, Pick } from "@/src/lib/api";
import { LockPickCard } from "@/src/components/LockPickCard";
import { ChipRow } from "@/src/components/ChipRow";

export default function LocksScreen() {
  const [picks, setPicks] = useState<Pick[]>([]);
  const [sport, setSport] = useState<string>("All");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [stats, setStats] = useState<{ total_picks: number; elite_count: number; avg_edge_percent: number } | null>(null);

  const load = useCallback(async (s: string) => {
    try {
      const [picksRes, statsRes] = await Promise.all([
        api.picksToday(s),
        api.stats().catch(() => null),
      ]);
      setPicks(picksRes.picks);
      if (statsRes) setStats(statsRes);
    } catch (e) {
      console.warn("load locks", e);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => { setLoading(true); load(sport); }, [sport, load]);

  const onRefresh = () => { setRefreshing(true); load(sport); };

  const onForceRefresh = async () => {
    setLoading(true);
    try {
      await api.refresh();
      await load(sport);
    } catch (e) {
      console.warn(e);
      setLoading(false);
    }
  };

  return (
    <SafeAreaView style={styles.safe} edges={["top"]}>
      <View style={styles.header}>
        <View>
          <Text style={styles.brand}>LOCKSCORE</Text>
          <Text style={styles.date}>
            Today&apos;s Locks · {new Date().toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" })}
          </Text>
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

      <ChipRow options={SPORTS} active={sport} onChange={setSport} testIDPrefix="sport-chip" />

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
            <Text style={styles.emptyTitle}>No locks meeting threshold</Text>
            <Text style={styles.emptyMsg}>Tap refresh or check back later for today&apos;s picks.</Text>
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
  refreshBtn: {
    width: 40, height: 40, borderRadius: 20,
    backgroundColor: COLORS.surface, alignItems: "center", justifyContent: "center",
    borderWidth: 1, borderColor: COLORS.borderDefault,
  },
  statsRow: { flexDirection: "row", paddingHorizontal: 20, gap: 10, marginBottom: 6 },
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
