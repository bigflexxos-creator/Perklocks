import React, { useCallback, useEffect, useState } from "react";
import {
  View, Text, StyleSheet, ScrollView, ActivityIndicator,
  RefreshControl, Pressable,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { Ionicons } from "@expo/vector-icons";
import { Stack, useRouter } from "expo-router";
import { COLORS } from "@/src/theme";
import { api } from "@/src/lib/api";
import { formatGameTime } from "@/src/lib/formatGameTime";
import { useFocusRefetch } from "@/src/lib/useFocusRefetch";

type HistoryPick = {
  id: string;
  sport: string;
  event: string;
  event_time?: string | null;
  market: string;
  lock_score: number;
  win_probability: number;
  book_odds: number;
  status?: string;
  settled_at?: string;
  final_score?: Record<string, number>;
  loss_analysis?: string;
};

const FILTERS = ["All", "Lost", "Won", "Rollover"] as const;

export default function HistoryScreen() {
  const router = useRouter();
  const [picks, setPicks] = useState<HistoryPick[]>([]);
  const [stats, setStats] = useState<{ total: number; won: number; lost: number; push: number; hit_rate: number; rollover_hit_rate: number; rollover_decided: number } | null>(null);
  const [filter, setFilter] = useState<typeof FILTERS[number]>("All");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [openId, setOpenId] = useState<string | null>(null);
  const [analyzing, setAnalyzing] = useState<string | null>(null);
  const [analyses, setAnalyses] = useState<Record<string, string>>({});

  const load = useCallback(async () => {
    try {
      const res = await api.history(30, filter === "Rollover");
      const all = (res?.picks as HistoryPick[]) ?? [];
      // Defensive: hide KBO history per product decision (no KBO anywhere).
      setPicks(all.filter((p) => p.sport !== "KBO"));
      setStats(res?.stats ?? null);
    } catch (e) {
      setPicks([]);
      setStats(null);
      console.warn("history load", e);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [filter]);

  useEffect(() => { setLoading(true); load(); }, [load]);

  // Smart refetch on focus — re-pull history when the user returns, but
  // suppress duplicate calls inside 30 s.
  useFocusRefetch(load, [load], 30_000);

  const onRefresh = async () => {
    setRefreshing(true);
    try { await api.triggerSettle(); } catch {}
    await load();
  };

  const filtered = picks.filter((p) => {
    if (filter === "Lost") return p.status === "lost";
    if (filter === "Won") return p.status === "won";
    return true;
  });

  const openAnalysis = async (pick: HistoryPick) => {
    if (openId === pick.id) { setOpenId(null); return; }
    setOpenId(pick.id);
    if (analyses[pick.id]) return;
    if (pick.status !== "lost") {
      setAnalyses((s) => ({ ...s, [pick.id]: "No analysis — pick wasn't a loss." }));
      return;
    }
    if (pick.loss_analysis) {
      setAnalyses((s) => ({ ...s, [pick.id]: pick.loss_analysis ?? "" }));
      return;
    }
    setAnalyzing(pick.id);
    try {
      const res = await api.lossAnalysis(pick.id);
      setAnalyses((s) => ({ ...s, [pick.id]: res.analysis }));
    } catch {
      setAnalyses((s) => ({ ...s, [pick.id]: "Could not load analysis." }));
    } finally {
      setAnalyzing(null);
    }
  };

  return (
    <SafeAreaView style={styles.safe} edges={["top"]}>
      <Stack.Screen options={{ headerShown: false }} />
      <View style={styles.header}>
        <Pressable onPress={() => router.back()} hitSlop={10} style={styles.backBtn}>
          <Ionicons name="arrow-back" size={24} color={COLORS.textPrimary} />
        </Pressable>
        <Text style={styles.title}>PICK HISTORY</Text>
        <View style={{ width: 24 }} />
      </View>

      {stats && (
        <View style={styles.statsCard}>
          <View style={styles.statCol}>
            <Text style={styles.statLabel}>HIT RATE</Text>
            <Text style={[styles.statValue, { color: COLORS.neonGreen }]}>{stats.hit_rate}%</Text>
            <Text style={styles.statSub}>{stats.won}W · {stats.lost}L</Text>
          </View>
          <View style={styles.statColDivider} />
          <View style={styles.statCol}>
            <Text style={styles.statLabel}>ROLLOVER</Text>
            <Text style={[styles.statValue, { color: COLORS.goldElite }]}>{stats.rollover_hit_rate}%</Text>
            <Text style={styles.statSub}>{stats.rollover_decided} decided</Text>
          </View>
          <View style={styles.statColDivider} />
          <View style={styles.statCol}>
            <Text style={styles.statLabel}>TOTAL</Text>
            <Text style={styles.statValue}>{stats.total}</Text>
            <Text style={styles.statSub}>last 30 days</Text>
          </View>
        </View>
      )}

      <View style={styles.filterRow}>
        {FILTERS.map((f) => (
          <Pressable
            key={f}
            onPress={() => setFilter(f)}
            style={[styles.chip, filter === f && styles.chipActive]}
          >
            <Text style={[styles.chipText, filter === f && styles.chipTextActive]}>{f}</Text>
          </Pressable>
        ))}
      </View>

      <ScrollView
        contentContainerStyle={styles.list}
        refreshControl={<RefreshControl tintColor={COLORS.textPrimary} refreshing={refreshing} onRefresh={onRefresh} />}
        showsVerticalScrollIndicator={false}
      >
        {loading ? (
          <View style={styles.center}><ActivityIndicator color={COLORS.voltBlue} /></View>
        ) : filtered.length === 0 ? (
          <View style={styles.center}>
            <Ionicons name="time-outline" size={48} color={COLORS.textMuted} />
            <Text style={styles.emptyTitle}>No settled picks yet</Text>
            <Text style={styles.emptyMsg}>
              Once today&apos;s games end, results will appear here automatically.
              Pull to refresh to trigger a settlement check.
            </Text>
          </View>
        ) : (
          filtered.map((p) => {
            // μ-closure LIVE (2026-06) — History Fix 3A: explicit
            // canonical state mapping. Only a genuine `pending` value
            // may show PENDING; `void` and `unresolved` must show
            // themselves so users don't see settled picks as PENDING.
            const s = (p.status || "").toLowerCase();
            const isLoss = s === "lost";
            const isWin = s === "won";
            const isPush = s === "push";
            const isVoid = s === "void";
            const isUnresolved = s === "unresolved";
            const isPending = s === "pending" || s === "";
            const statusColor =
              isWin        ? COLORS.neonGreen   :
              isLoss       ? COLORS.electricBlaze :
              isPush       ? COLORS.textMuted   :
              isVoid       ? COLORS.textSecondary : // neutral
              isUnresolved ? COLORS.goldElite   : // amber/diagnostic
                             COLORS.goldElite;    // pending → amber/live
            const statusLabel =
              isWin        ? "WON"        :
              isLoss       ? "LOST"       :
              isPush       ? "PUSH"       :
              isVoid       ? "VOID"       :
              isUnresolved ? "UNRESOLVED" :
                             "PENDING";
            const expanded = openId === p.id;
            const scoreStr = p.final_score ? Object.entries(p.final_score).map(([t, s]) => `${t} ${s}`).join(" · ") : "";
            return (
              <Pressable key={p.id} onPress={() => openAnalysis(p)} style={styles.card}>
                <View style={styles.cardTop}>
                  <Text style={styles.cardSport}>{p.sport}</Text>
                  <View style={[styles.statusPill, { backgroundColor: `${statusColor}22`, borderColor: statusColor }]}>
                    <Text style={[styles.statusText, { color: statusColor }]}>{statusLabel}</Text>
                  </View>
                </View>
                <Text style={styles.cardEvent}>{p.event}</Text>
                {p.event_time && (
                  <Text style={styles.cardTime}>{formatGameTime(p.event_time)}</Text>
                )}
                <Text style={styles.cardMarket}>{p.market}</Text>
                {!!scoreStr && <Text style={styles.cardScore}>Final: {scoreStr}</Text>}
                <View style={styles.cardMetrics}>
                  <Text style={styles.metric}>Lock {Math.round(p.lock_score)}</Text>
                  <Text style={styles.metricSep}>•</Text>
                  <Text style={styles.metric}>Win {Math.round(p.win_probability)}%</Text>
                  <Text style={styles.metricSep}>•</Text>
                  <Text style={styles.metric}>{p.book_odds > 0 ? `+${p.book_odds}` : p.book_odds}</Text>
                </View>

                {isLoss && (
                  <View style={styles.analysisRow}>
                    <Ionicons name={expanded ? "chevron-up" : "chevron-down"} size={14} color={COLORS.voltBlue} />
                    <Text style={styles.analysisCta}>
                      {expanded ? "Hide AI analysis" : "Why did this lose? Tap for AI analysis"}
                    </Text>
                  </View>
                )}

                {expanded && (
                  <View style={styles.analysisBox}>
                    {analyzing === p.id ? (
                      <ActivityIndicator color={COLORS.voltBlue} />
                    ) : (
                      <Text style={styles.analysisText}>
                        {analyses[p.id] ?? "Loading analysis..."}
                      </Text>
                    )}
                  </View>
                )}
              </Pressable>
            );
          })
        )}
      </ScrollView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: COLORS.bg },
  header: {
    paddingHorizontal: 20, paddingTop: 8, paddingBottom: 14,
    flexDirection: "row", justifyContent: "space-between", alignItems: "center",
  },
  backBtn: { width: 24 },
  title: { fontSize: 18, fontWeight: "900", color: COLORS.textPrimary, letterSpacing: 2 },

  statsCard: {
    marginHorizontal: 20, marginBottom: 12,
    flexDirection: "row", backgroundColor: COLORS.surface,
    borderRadius: 14, padding: 16,
  },
  statCol: { flex: 1, alignItems: "center" },
  statColDivider: { width: 1, backgroundColor: COLORS.borderDefault, marginHorizontal: 6 },
  statLabel: { color: COLORS.textMuted, fontSize: 9, letterSpacing: 1.2, fontWeight: "800" },
  statValue: { color: COLORS.textPrimary, fontSize: 22, fontWeight: "900", marginTop: 4 },
  statSub: { color: COLORS.textSecondary, fontSize: 10, marginTop: 2 },

  filterRow: {
    flexDirection: "row", gap: 8, paddingHorizontal: 20, marginBottom: 14,
  },
  chip: {
    paddingHorizontal: 12, paddingVertical: 6, borderRadius: 16,
    borderWidth: 1, borderColor: COLORS.borderDefault,
  },
  chipActive: { backgroundColor: COLORS.textPrimary, borderColor: COLORS.textPrimary },
  chipText: { color: COLORS.textSecondary, fontWeight: "700", fontSize: 12 },
  chipTextActive: { color: COLORS.bg },

  list: { paddingHorizontal: 20, paddingBottom: 30 },
  center: { paddingVertical: 80, alignItems: "center" },
  emptyTitle: { color: COLORS.textPrimary, fontSize: 16, fontWeight: "800", marginTop: 12 },
  emptyMsg: { color: COLORS.textMuted, fontSize: 13, marginTop: 6, textAlign: "center", paddingHorizontal: 24 },

  card: {
    backgroundColor: COLORS.surfaceElevated ?? COLORS.surface,
    borderRadius: 14, padding: 14, marginBottom: 10,
    borderWidth: 1, borderColor: COLORS.borderDefault,
    // μ-closure UI3 (2026-06): History result rows get ambient
    // depth so WIN/LOSS/PUSH/VOID states pop off the deep-navy
    // environment.
    shadowColor: COLORS.voltBlue,
    shadowOpacity: 0.10,
    shadowRadius: 8,
    shadowOffset: { width: 0, height: 3 },
    elevation: 2,
  },
  cardTop: { flexDirection: "row", justifyContent: "space-between", alignItems: "center", marginBottom: 6 },
  cardSport: { color: COLORS.textSecondary, fontSize: 10, fontWeight: "800", letterSpacing: 1.2 },
  statusPill: { paddingHorizontal: 8, paddingVertical: 3, borderRadius: 6, borderWidth: 1 },
  statusText: { fontSize: 10, fontWeight: "900", letterSpacing: 1 },
  cardEvent: { color: COLORS.textPrimary, fontSize: 13, fontWeight: "700", marginBottom: 2 },
  cardTime: { color: COLORS.voltBlue, fontSize: 11, fontWeight: "800", letterSpacing: 0.3, marginBottom: 4 },
  cardMarket: { color: COLORS.textPrimary, fontSize: 14, fontWeight: "800", marginBottom: 6 },
  cardScore: { color: COLORS.textSecondary, fontSize: 11, marginBottom: 6 },
  cardMetrics: { flexDirection: "row", alignItems: "center", gap: 6 },
  metric: { color: COLORS.textSecondary, fontSize: 11, fontWeight: "700" },
  metricSep: { color: COLORS.textMuted, fontSize: 10 },

  analysisRow: {
    flexDirection: "row", alignItems: "center", gap: 6,
    marginTop: 10, paddingTop: 10, borderTopWidth: 1, borderTopColor: COLORS.borderDefault,
  },
  analysisCta: { color: COLORS.voltBlue, fontSize: 11, fontWeight: "700" },
  analysisBox: {
    marginTop: 10, padding: 12, backgroundColor: COLORS.bg, borderRadius: 10,
    borderWidth: 1, borderColor: COLORS.borderDefault,
  },
  analysisText: { color: COLORS.textPrimary, fontSize: 12, lineHeight: 18 },
});
