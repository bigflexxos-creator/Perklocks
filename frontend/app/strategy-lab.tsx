/**
 * Strategy Lab — Phase 3 learning dashboard.
 *
 * Shows the live Multi-Armed Bandit (Thompson sampling) arm states + a
 * 30-day back-test of every strategy. Each arm card displays:
 *   - posterior mean (the bandit's current belief about win rate)
 *   - Thompson sample (the noisy draw used for the most recent decision)
 *   - hit rate / ROI / units P&L
 *   - sparkline of the cumulative-units curve
 *
 * Pull-to-refresh re-pulls both feeds. No writes — read-only analytics.
 */
import React, { useEffect, useState, useCallback } from "react";
import {
  View, Text, StyleSheet, ScrollView, ActivityIndicator,
  RefreshControl, Pressable,
} from "react-native";
import Svg, { Polyline, Line } from "react-native-svg";
import { SafeAreaView } from "react-native-safe-area-context";
import { Ionicons } from "@expo/vector-icons";
import { router, Stack } from "expo-router";

import { api } from "@/src/lib/api";
import { COLORS } from "@/src/theme";

type BanditResp = Awaited<ReturnType<typeof api.bandit>>;
type BacktestResp = Awaited<ReturnType<typeof api.backtest>>;

const fmt = (n: number, d = 2) => (Number.isFinite(n) ? n.toFixed(d) : "—");
const sign = (n: number) => (n > 0 ? "+" : "");

// Same emoji-based UX as Phase 2 player form for visual consistency.
function tempBadge(post: number): { icon: string; color: string; label: string } {
  if (post >= 0.65) return { icon: "🔥", color: COLORS.neonGreen,    label: "HOT" };
  if (post >= 0.55) return { icon: "📈", color: COLORS.goldElite,    label: "WARM" };
  if (post >= 0.45) return { icon: "➖", color: COLORS.textMuted,    label: "NEUTRAL" };
  if (post >= 0.35) return { icon: "📉", color: COLORS.electricBlaze, label: "COOL" };
  return { icon: "🥶", color: COLORS.electricBlaze, label: "COLD" };
}

function Sparkline({ curve, color }: { curve: Array<[string, number]>; color: string }) {
  const w = 110, h = 36, pad = 2;
  if (!curve || curve.length < 2) {
    return <View style={{ width: w, height: h, justifyContent: "center" }}>
      <Text style={{ color: COLORS.textMuted, fontSize: 9, textAlign: "center" }}>—</Text>
    </View>;
  }
  const ys = curve.map(([, y]) => y);
  const minY = Math.min(...ys, 0);
  const maxY = Math.max(...ys, 0);
  const range = (maxY - minY) || 1;
  const dx = (w - pad * 2) / (curve.length - 1);
  const pts = curve.map(([, y], i) => {
    const x = pad + dx * i;
    const yy = h - pad - ((y - minY) / range) * (h - pad * 2);
    return `${x.toFixed(1)},${yy.toFixed(1)}`;
  }).join(" ");
  // Zero baseline
  const zeroY = h - pad - ((0 - minY) / range) * (h - pad * 2);
  return (
    <Svg width={w} height={h}>
      <Line x1={0} y1={zeroY} x2={w} y2={zeroY}
            stroke={COLORS.borderDefault} strokeWidth={0.5} strokeDasharray="2,2" />
      <Polyline points={pts} fill="none" stroke={color} strokeWidth={1.8} />
    </Svg>
  );
}

export default function StrategyLab() {
  const [bandit, setBandit] = useState<BanditResp | null>(null);
  const [bt, setBt] = useState<BacktestResp | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async () => {
    try {
      const [b, t] = await Promise.all([
        api.bandit().catch(() => null),
        api.backtest(30).catch(() => null),
      ]);
      setBandit(b);
      setBt(t);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const onRefresh = () => { setRefreshing(true); load(); };

  if (loading) {
    return (
      <SafeAreaView style={styles.safe}>
        <View style={styles.center}><ActivityIndicator color={COLORS.goldElite} /></View>
      </SafeAreaView>
    );
  }

  const arms = bandit?.arms || [];
  const btArms = bt?.arms || {};

  return (
    <SafeAreaView style={styles.safe} edges={["top"]}>
      <Stack.Screen options={{ headerShown: false }} />
      <View style={styles.header}>
        <Pressable onPress={() => router.back()} hitSlop={10} style={styles.backBtn}>
          <Ionicons name="arrow-back" size={22} color={COLORS.textPrimary} />
        </Pressable>
        <Text style={styles.title}>STRATEGY LAB</Text>
        <View style={{ width: 22 }} />
      </View>

      <ScrollView
        contentContainerStyle={styles.content}
        showsVerticalScrollIndicator={false}
        refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh}
                                         tintColor={COLORS.goldElite} />}
      >
        <Text style={styles.subtitle}>
          Multi-Armed Bandit (Thompson Sampling) · {arms.length} arms ·
          {bt?.n_picks ?? 0} settled picks · {bt?.window_days ?? 30}-day window
        </Text>
        <Text style={styles.explainer}>
          Each arm is a betting strategy. The bandit samples each arm&apos;s posterior
          win-rate every refresh; hotter arms get a {`±lift`} on matching picks&apos;
          lock scores. ROI &amp; max-drawdown come from the 30-day back-test.
        </Text>

        {arms.length === 0 && (
          <View style={styles.empty}>
            <Ionicons name="construct-outline" size={36} color={COLORS.textMuted} />
            <Text style={styles.emptyTxt}>
              Bandit warming up. Need at least one settled pick per arm.
            </Text>
          </View>
        )}

        {arms.map((a) => {
          const t = tempBadge(a.posterior_mean);
          const btRow = btArms[a.arm];
          const roi = btRow?.roi ?? a.roi ?? 0;
          const profitColor = (btRow?.units_profit ?? a.units_profit ?? 0) >= 0
            ? COLORS.neonGreen : COLORS.electricBlaze;
          return (
            <View key={a.arm} style={[styles.card, { borderColor: t.color + "55" }]}>
              <View style={styles.cardHead}>
                <Text style={styles.armIcon}>{t.icon}</Text>
                <View style={{ flex: 1 }}>
                  <Text style={styles.armName}>{a.arm.replace(/_/g, " ").toUpperCase()}</Text>
                  <Text style={styles.armDesc} numberOfLines={2}>{a.description}</Text>
                </View>
                <View style={[styles.tempChip, { backgroundColor: t.color + "22", borderColor: t.color }]}>
                  <Text style={[styles.tempChipTxt, { color: t.color }]}>{t.label}</Text>
                </View>
              </View>

              <View style={styles.metricsRow}>
                <View style={styles.metricCell}>
                  <Text style={styles.metricVal}>{fmt(a.posterior_mean * 100, 1)}%</Text>
                  <Text style={styles.metricLbl}>POSTERIOR</Text>
                </View>
                <View style={styles.metricCell}>
                  <Text style={styles.metricVal}>{a.n}</Text>
                  <Text style={styles.metricLbl}>SAMPLES</Text>
                </View>
                <View style={styles.metricCell}>
                  <Text style={[styles.metricVal, { color: profitColor }]}>
                    {sign(roi)}{fmt(roi, 1)}%
                  </Text>
                  <Text style={styles.metricLbl}>ROI</Text>
                </View>
                <View style={styles.metricCell}>
                  <Text style={styles.metricVal}>
                    {a.n > 0 ? fmt(a.wins * 100 / Math.max(1, a.wins + a.losses), 0) : "—"}%
                  </Text>
                  <Text style={styles.metricLbl}>HIT</Text>
                </View>
              </View>

              <View style={styles.curveRow}>
                <Sparkline curve={btRow?.curve || []} color={profitColor} />
                <View style={styles.curveStats}>
                  <Text style={styles.curveStat}>
                    P&amp;L:&nbsp;
                    <Text style={{ color: profitColor, fontWeight: "900" }}>
                      {sign(btRow?.units_profit ?? a.units_profit ?? 0)}
                      {fmt(btRow?.units_profit ?? a.units_profit ?? 0, 2)}u
                    </Text>
                  </Text>
                  <Text style={styles.curveStat}>
                    Max DD: -{fmt(btRow?.max_drawdown ?? 0, 2)}u
                  </Text>
                  <Text style={styles.curveStat}>
                    Sharpe: {sign(btRow?.sharpe ?? 0)}{fmt(btRow?.sharpe ?? 0, 2)}
                  </Text>
                </View>
              </View>

              <View style={styles.posteriorRow}>
                <Text style={styles.posteriorTxt}>
                  Beta(α={fmt(a.alpha, 0)}, β={fmt(a.beta, 0)}) · last sample = {fmt(a.posterior_thompson * 100, 1)}%
                </Text>
              </View>
            </View>
          );
        })}

        <Text style={styles.footer}>
          Strategies the bandit currently fades show negative ROI / cold streaks.
          As new picks settle, posteriors update and the lift on next refresh
          shifts automatically. No manual tuning required.
        </Text>
      </ScrollView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: COLORS.bg },
  center: { flex: 1, alignItems: "center", justifyContent: "center" },
  header: {
    flexDirection: "row", alignItems: "center", justifyContent: "space-between",
    paddingHorizontal: 16, paddingVertical: 12,
    borderBottomWidth: StyleSheet.hairlineWidth, borderBottomColor: COLORS.borderDefault,
  },
  backBtn: { padding: 4 },
  title: { color: COLORS.textPrimary, fontSize: 14, fontWeight: "900", letterSpacing: 1.8 },
  content: { padding: 16, paddingBottom: 60 },
  subtitle: { color: COLORS.textPrimary, fontSize: 12, fontWeight: "700", marginBottom: 6 },
  explainer: { color: COLORS.textMuted, fontSize: 11, lineHeight: 15, marginBottom: 16 },
  empty: { alignItems: "center", paddingVertical: 36, gap: 8 },
  emptyTxt: { color: COLORS.textMuted, fontSize: 12, textAlign: "center", paddingHorizontal: 24 },
  card: {
    backgroundColor: COLORS.surface, borderRadius: 14, borderWidth: 1,
    padding: 14, marginBottom: 12,
  },
  cardHead: { flexDirection: "row", alignItems: "center", gap: 10, marginBottom: 12 },
  armIcon: { fontSize: 22 },
  armName: { color: COLORS.textPrimary, fontSize: 13, fontWeight: "900", letterSpacing: 0.8 },
  armDesc: { color: COLORS.textMuted, fontSize: 11, lineHeight: 14, marginTop: 2 },
  tempChip: { paddingHorizontal: 9, paddingVertical: 4, borderRadius: 10, borderWidth: 1 },
  tempChipTxt: { fontSize: 9, fontWeight: "900", letterSpacing: 1.0 },
  metricsRow: { flexDirection: "row", gap: 8, marginBottom: 12 },
  metricCell: { flex: 1, alignItems: "center" },
  metricVal: { color: COLORS.textPrimary, fontSize: 14, fontWeight: "900" },
  metricLbl: { color: COLORS.textMuted, fontSize: 9, fontWeight: "700", marginTop: 2, letterSpacing: 0.6 },
  curveRow: {
    flexDirection: "row", alignItems: "center", gap: 12,
    paddingTop: 10, borderTopWidth: StyleSheet.hairlineWidth, borderTopColor: COLORS.borderDefault,
  },
  curveStats: { flex: 1, gap: 3 },
  curveStat: { color: COLORS.textMuted, fontSize: 10.5, fontWeight: "600" },
  posteriorRow: { marginTop: 8 },
  posteriorTxt: { color: COLORS.textMuted, fontSize: 9.5, letterSpacing: 0.3 },
  footer: { color: COLORS.textMuted, fontSize: 10, lineHeight: 14, textAlign: "center",
            paddingHorizontal: 16, marginTop: 8 },
});
