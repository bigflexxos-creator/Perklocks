import React, { useCallback, useEffect, useState } from "react";
import {
  View, Text, StyleSheet, ScrollView, ActivityIndicator,
  RefreshControl, Pressable,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { Ionicons } from "@expo/vector-icons";
import { useRouter } from "expo-router";
import { COLORS, GRADE_COLORS } from "@/src/theme";
import { api, Pick } from "@/src/lib/api";

function formatGameTime(iso: string): string {
  try {
    const dt = new Date(iso);
    const now = new Date();
    const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const tomorrowStart = new Date(todayStart);
    tomorrowStart.setDate(todayStart.getDate() + 1);
    const dayAfter = new Date(todayStart);
    dayAfter.setDate(todayStart.getDate() + 2);
    const time = dt.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
    if (dt >= todayStart && dt < tomorrowStart) return `Today · ${time}`;
    if (dt >= tomorrowStart && dt < dayAfter) return `Tomorrow · ${time}`;
    return `${dt.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" })} · ${time}`;
  } catch {
    return iso;
  }
}

export default function RolloverScreen() {
  const router = useRouter();
  const [pick, setPick] = useState<Pick | null>(null);
  const [composite, setComposite] = useState<number | null>(null);
  const [pool, setPool] = useState<number>(0);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async () => {
    try {
      const res = await api.rollover();
      setPick(res.pick);
      setComposite(res.composite_rank ?? null);
      setPool(res.total_evaluated ?? 0);
    } catch (e) {
      console.warn("rollover load", e);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const onRefresh = () => { setRefreshing(true); load(); };

  const gradeColor = pick ? GRADE_COLORS[pick.grade] : COLORS.textMuted;

  return (
    <SafeAreaView style={styles.safe} edges={["top"]}>
      <View style={styles.header}>
        <View>
          <Text style={styles.brand}>ROLLOVER</Text>
          <Text style={styles.tag}>SINGLE BEST BET · BOARD-WIDE</Text>
        </View>
        <Ionicons name="flash" size={28} color={COLORS.goldElite} />
      </View>

      <ScrollView
        contentContainerStyle={styles.content}
        refreshControl={<RefreshControl tintColor={COLORS.textPrimary} refreshing={refreshing} onRefresh={onRefresh} />}
        showsVerticalScrollIndicator={false}
        testID="rollover-scroll"
      >
        {loading ? (
          <View style={styles.center}>
            <ActivityIndicator color={COLORS.voltBlue} />
          </View>
        ) : !pick ? (
          <View style={styles.center}>
            <Ionicons name="hourglass-outline" size={48} color={COLORS.textMuted} />
            <Text style={styles.emptyTitle}>No games available</Text>
            <Text style={styles.emptyMsg}>No fixtures returned by the sports API for today.</Text>
          </View>
        ) : (
          <>
            <View style={styles.heroCard}>
              <View style={styles.heroBadgeRow}>
                <View style={[styles.gradeBadge, { borderColor: gradeColor, backgroundColor: `${gradeColor}15` }]}>
                  <Text style={[styles.gradeBadgeText, { color: gradeColor }]}>
                    {pick.grade.toUpperCase()}
                  </Text>
                </View>
                <Text style={styles.evaluated}>RANKED #1 OF {pool}</Text>
              </View>

              <Text style={styles.sportLine}>{pick.sport} · {pick.league}</Text>
              <Text style={styles.event}>{pick.event}</Text>
              {pick.event_time && (
                <Text style={styles.gameTime}>{formatGameTime(pick.event_time)}</Text>
              )}
              <Text style={styles.market}>{pick.market}</Text>

              <View style={styles.scoreBlock}>
                <Text style={[styles.bigScore, { color: gradeColor }]}>{Math.round(pick.lock_score)}</Text>
                <Text style={styles.scoreLabel}>LOCK SCORE</Text>
                {composite !== null && (
                  <Text style={styles.composite}>Composite Rank: {composite}</Text>
                )}
              </View>

              <View style={styles.metricsGrid}>
                <Metric label="WIN PROB" value={`${pick.win_probability}%`} />
                <Metric label="BOOK IMPLIED" value={`${pick.implied_probability}%`} />
                <Metric
                  label="EDGE"
                  value={`${pick.edge_percent > 0 ? "+" : ""}${pick.edge_percent}%`}
                  color={pick.edge_percent > 0 ? COLORS.neonGreen : COLORS.electricBlaze}
                />
                <Metric
                  label="BOOK ODDS"
                  value={pick.book_odds > 0 ? `+${pick.book_odds}` : `${pick.book_odds}`}
                />
              </View>
            </View>

            <View style={styles.insightsCard}>
              <Text style={styles.sectionLabel}>WHY IT&apos;S THE BOARD&apos;S BEST</Text>
              {pick.key_insights.map((i, idx) => (
                <View key={idx} style={styles.bullet}>
                  <View style={[styles.bulletDot, { backgroundColor: gradeColor }]} />
                  <Text style={styles.bulletText}>{i}</Text>
                </View>
              ))}
            </View>

            <Pressable
              testID="rollover-cta-button"
              onPress={() => router.push(`/pick/${pick.id}`)}
              style={({ pressed }) => [
                styles.cta,
                pressed && { transform: [{ scale: 0.98 }] },
              ]}
            >
              <Text style={styles.ctaText}>VIEW FULL AI BREAKDOWN</Text>
              <Ionicons name="arrow-forward" size={16} color={COLORS.bg} />
            </Pressable>

            <Text style={styles.disclaimer}>
              No bet is a guaranteed winner. The Rollover represents the single highest expected-value
              opportunity across today&apos;s entire board based on Lock Score + Edge%.
            </Text>
          </>
        )}
      </ScrollView>
    </SafeAreaView>
  );
}

function Metric({ label, value, color = COLORS.textPrimary }: { label: string; value: string; color?: string }) {
  return (
    <View style={styles.metricCell}>
      <Text style={styles.metricLabel}>{label}</Text>
      <Text style={[styles.metricValue, { color }]}>{value}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: COLORS.bg },
  header: {
    paddingHorizontal: 20, paddingTop: 8, paddingBottom: 14,
    flexDirection: "row", justifyContent: "space-between", alignItems: "flex-end",
  },
  brand: { fontSize: 22, fontWeight: "900", color: COLORS.textPrimary, letterSpacing: 3 },
  tag: { fontSize: 10, color: COLORS.goldElite, fontWeight: "800", letterSpacing: 1.8, marginTop: 4 },
  content: { paddingHorizontal: 20, paddingTop: 8, paddingBottom: 30 },
  center: { paddingVertical: 80, alignItems: "center" },
  emptyTitle: { color: COLORS.textPrimary, fontSize: 16, fontWeight: "800", marginTop: 14 },
  emptyMsg: { color: COLORS.textMuted, fontSize: 13, marginTop: 6, textAlign: "center" },

  heroCard: {
    backgroundColor: COLORS.surface,
    borderRadius: 20, padding: 22,
    borderWidth: 1, borderColor: COLORS.borderDefault,
    marginBottom: 16,
  },
  heroBadgeRow: {
    flexDirection: "row", justifyContent: "space-between", alignItems: "center", marginBottom: 14,
  },
  gradeBadge: {
    paddingHorizontal: 10, paddingVertical: 5, borderRadius: 6, borderWidth: 1,
  },
  gradeBadgeText: { fontSize: 10, fontWeight: "900", letterSpacing: 1.5 },
  evaluated: { color: COLORS.textMuted, fontSize: 10, fontWeight: "800", letterSpacing: 1.2 },
  sportLine: { color: COLORS.textSecondary, fontSize: 12, fontWeight: "700", letterSpacing: 1, marginBottom: 4 },
  event: { color: COLORS.textPrimary, fontSize: 16, fontWeight: "700", marginBottom: 8 },
  gameTime: { color: COLORS.voltBlue, fontSize: 12, fontWeight: "700", letterSpacing: 0.3, marginBottom: 12 },
  market: { color: COLORS.textPrimary, fontSize: 22, fontWeight: "900", letterSpacing: -0.5, marginBottom: 20 },

  scoreBlock: { alignItems: "center", marginBottom: 18 },
  bigScore: { fontSize: 72, fontWeight: "900", letterSpacing: -3, lineHeight: 78 },
  scoreLabel: { color: COLORS.textMuted, fontSize: 10, fontWeight: "800", letterSpacing: 2, marginTop: -2 },
  composite: { color: COLORS.textSecondary, fontSize: 11, marginTop: 8, fontWeight: "600" },

  metricsGrid: { flexDirection: "row", flexWrap: "wrap", marginHorizontal: -6 },
  metricCell: {
    width: "50%", paddingHorizontal: 6, paddingVertical: 8,
  },
  metricLabel: { color: COLORS.textMuted, fontSize: 10, fontWeight: "800", letterSpacing: 1.3 },
  metricValue: { fontSize: 22, fontWeight: "900", marginTop: 4, letterSpacing: -0.5 },

  insightsCard: {
    backgroundColor: COLORS.surface, borderRadius: 16, padding: 18,
    borderWidth: 1, borderColor: COLORS.borderDefault, marginBottom: 18,
  },
  sectionLabel: { color: COLORS.textMuted, fontSize: 10, fontWeight: "800", letterSpacing: 1.6, marginBottom: 12 },
  bullet: { flexDirection: "row", alignItems: "flex-start", marginBottom: 10 },
  bulletDot: { width: 6, height: 6, borderRadius: 3, marginTop: 7, marginRight: 10 },
  bulletText: { flex: 1, color: COLORS.textPrimary, fontSize: 13, lineHeight: 20 },

  cta: {
    backgroundColor: COLORS.textPrimary, borderRadius: 12, paddingVertical: 16,
    flexDirection: "row", alignItems: "center", justifyContent: "center", gap: 8,
  },
  ctaText: { color: COLORS.bg, fontSize: 13, fontWeight: "900", letterSpacing: 1.8 },

  disclaimer: {
    color: COLORS.textMuted, fontSize: 11, lineHeight: 17, marginTop: 18,
    textAlign: "center", paddingHorizontal: 10,
  },
});
