/**
 * CLV Dashboard — proves the app is +EV.
 *
 * Feature (2026-07-27): shows the collective model performance so
 * users can see the picks board is winning at a rate that justifies
 * betting on it. Data is aggregated over the WHOLE picks board (not
 * personal bets). Personal bets live under /(tabs)/my-bets.tsx.
 *
 * Pulled from:
 *   GET /api/me/performance         — headline blocks
 *   GET /api/me/performance/by-sport
 *   GET /api/me/performance/by-band
 *   GET /api/me/performance/trend?days=30
 */
import React, { useCallback, useEffect, useState } from "react";
import {
  View, Text, StyleSheet, ScrollView, Pressable, ActivityIndicator,
  RefreshControl,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { Ionicons } from "@expo/vector-icons";
import { useRouter } from "expo-router";
import { COLORS } from "@/src/theme";
import { api, PerfSummary, PerfBySport, PerfByBand, PerfTrendRow } from "@/src/lib/api";

const WINDOWS = [
  { label: "7D", days: 7 },
  { label: "30D", days: 30 },
  { label: "90D", days: 90 },
  { label: "ALL", days: undefined as number | undefined },
];

export default function CLVDashboardScreen() {
  const router = useRouter();
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [windowDays, setWindowDays] = useState<number>(30);

  const [perf, setPerf] = useState<{
    all_time: PerfSummary; recent: PerfSummary; high_conviction: PerfSummary;
  } | null>(null);
  const [bySport, setBySport] = useState<PerfBySport[]>([]);
  const [byBand, setByBand] = useState<PerfByBand[]>([]);
  const [trend, setTrend] = useState<PerfTrendRow[]>([]);
  const [cumulative, setCumulative] = useState<number>(0);

  const load = useCallback(async () => {
    try {
      const [p, s, b, t] = await Promise.all([
        api.mePerformance(windowDays),
        api.mePerformanceBySport(windowDays),
        api.mePerformanceByBand(windowDays),
        api.mePerformanceTrend(Math.max(windowDays, 30)),
      ]);
      setPerf(p);
      setBySport(s.rows || []);
      setByBand(b.rows || []);
      setTrend(t.rows || []);
      setCumulative(t.cumulative_units || 0);
    } catch {
      // Silent fail — screen will still render with empty state.
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [windowDays]);

  useEffect(() => { load(); }, [load]);
  const onRefresh = () => { setRefreshing(true); load(); };

  return (
    <SafeAreaView style={styles.safe} edges={["top"]}>
      <View style={styles.header}>
        <Pressable onPress={() => router.back()} hitSlop={16} testID="back-btn">
          <Ionicons name="chevron-back" size={26} color={COLORS.textPrimary} />
        </Pressable>
        <Text style={styles.title}>MODEL EDGE</Text>
        <View style={{ width: 26 }} />
      </View>

      <ScrollView
        contentContainerStyle={styles.content}
        refreshControl={
          <RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={COLORS.voltBlue} />
        }
      >
        {/* Window Selector */}
        <View style={styles.windowRow}>
          {WINDOWS.map((w) => {
            const active = (w.days ?? -1) === windowDays || (!w.days && windowDays === 0);
            return (
              <Pressable
                key={w.label}
                onPress={() => setWindowDays(w.days ?? 0)}
                style={[styles.windowChip, active && styles.windowChipActive]}
              >
                <Text style={[styles.windowChipText, active && styles.windowChipTextActive]}>
                  {w.label}
                </Text>
              </Pressable>
            );
          })}
        </View>

        {loading ? (
          <ActivityIndicator color={COLORS.voltBlue} size="large" style={{ marginTop: 40 }} />
        ) : (
          <>
            {/* Headline Blocks */}
            {perf && (
              <View style={styles.headlineRow}>
                <HeadlineCard
                  label={windowDays === 0 ? "ALL TIME" : `LAST ${windowDays}D`}
                  hitRate={windowDays === 0 ? perf.all_time.hit_rate_pct : perf.recent.hit_rate_pct}
                  roi={windowDays === 0 ? perf.all_time.roi_pct : perf.recent.roi_pct}
                  clv={windowDays === 0 ? perf.all_time.clv_avg_pct : perf.recent.clv_avg_pct}
                  n={windowDays === 0 ? perf.all_time.n : perf.recent.n}
                />
                <HeadlineCard
                  label="HIGH LOCK (85+)"
                  hitRate={perf.high_conviction.hit_rate_pct}
                  roi={perf.high_conviction.roi_pct}
                  clv={perf.high_conviction.clv_avg_pct}
                  n={perf.high_conviction.n}
                  highlight
                />
              </View>
            )}

            {/* Cumulative Units Chart */}
            {trend.length > 0 && (
              <View style={styles.trendCard}>
                <View style={styles.trendHeader}>
                  <Text style={styles.sectionLabel}>PROFIT CURVE</Text>
                  <Text style={[styles.trendCumulative, {
                    color: cumulative >= 0 ? COLORS.neonGreen : COLORS.electricBlaze,
                  }]}>
                    {cumulative >= 0 ? "+" : ""}{cumulative.toFixed(2)}u
                  </Text>
                </View>
                <SparkLine data={trend.map((r) => r.cumulative_profit)} />
                <View style={styles.trendFooter}>
                  <Text style={styles.trendLabel}>
                    {trend[0]?.day.slice(5)} → {trend[trend.length - 1]?.day.slice(5)}
                  </Text>
                  <Text style={styles.trendLabel}>{trend.length} days</Text>
                </View>
              </View>
            )}

            {/* By Lock Band */}
            {byBand.length > 0 && (
              <>
                <Text style={styles.sectionLabel}>HIT RATE BY LOCK BAND</Text>
                <View style={styles.bandList}>
                  {byBand.map((b) => (
                    <BandRow key={b.band} row={b} />
                  ))}
                </View>
              </>
            )}

            {/* By Sport */}
            {bySport.length > 0 && (
              <>
                <Text style={styles.sectionLabel}>BY SPORT</Text>
                <View style={styles.bandList}>
                  {bySport
                    .filter((s) => s.n >= 10)
                    .map((s) => (
                      <SportRow key={s.sport} row={s} />
                    ))}
                </View>
              </>
            )}

            <Text style={styles.footerNote}>
              CLV avg = weighted difference between pick&apos;s book_odds and
              closing_odds. Positive CLV means we&apos;re getting better
              prices than the closing line — the strongest predictor of
              long-term winning.
            </Text>
          </>
        )}
      </ScrollView>
    </SafeAreaView>
  );
}

// ── Sub-components ──────────────────────────────────────────────────
function HeadlineCard({
  label, hitRate, roi, clv, n, highlight,
}: {
  label: string;
  hitRate: number; roi: number; clv: number; n: number;
  highlight?: boolean;
}) {
  const roiColor = roi >= 0 ? COLORS.neonGreen : COLORS.electricBlaze;
  const clvColor = clv >= 0 ? COLORS.neonGreen : COLORS.electricBlaze;
  return (
    <View style={[styles.headline, highlight && styles.headlineHighlight]}>
      <Text style={styles.headlineLabel}>{label}</Text>
      <Text style={styles.headlineHit}>{hitRate.toFixed(1)}%</Text>
      <Text style={styles.headlineHitSub}>HIT RATE · {n.toLocaleString()} PICKS</Text>
      <View style={styles.headlineRow2}>
        <View style={styles.headlineMiniStat}>
          <Text style={styles.headlineMiniLabel}>ROI</Text>
          <Text style={[styles.headlineMiniValue, { color: roiColor }]}>
            {roi >= 0 ? "+" : ""}{roi.toFixed(1)}%
          </Text>
        </View>
        <View style={styles.headlineMiniStat}>
          <Text style={styles.headlineMiniLabel}>CLV</Text>
          <Text style={[styles.headlineMiniValue, { color: clvColor }]}>
            {clv >= 0 ? "+" : ""}{clv.toFixed(2)}%
          </Text>
        </View>
      </View>
    </View>
  );
}

function BandRow({ row }: { row: PerfByBand }) {
  const roiColor = row.roi_pct >= 0 ? COLORS.neonGreen : COLORS.electricBlaze;
  // Bar width by hit-rate, capped to 100
  const barWidth = Math.max(3, Math.min(row.hit_rate_pct, 100));
  return (
    <View style={styles.bandRow}>
      <View style={styles.bandHeader}>
        <Text style={styles.bandName}>{row.band}</Text>
        <View style={styles.bandRight}>
          <Text style={styles.bandHit}>{row.hit_rate_pct.toFixed(1)}%</Text>
          <Text style={[styles.bandRoi, { color: roiColor }]}>
            {row.roi_pct >= 0 ? "+" : ""}{row.roi_pct.toFixed(1)}%
          </Text>
        </View>
      </View>
      <View style={styles.bandBarTrack}>
        <View style={[styles.bandBarFill, { width: `${barWidth}%` }]} />
      </View>
      <Text style={styles.bandMeta}>{row.n} picks · {row.won}W / {row.lost}L</Text>
    </View>
  );
}

function SportRow({ row }: { row: PerfBySport }) {
  const roiColor = row.roi_pct >= 0 ? COLORS.neonGreen : COLORS.electricBlaze;
  const clvColor = row.clv_avg_pct >= 0 ? COLORS.neonGreen : COLORS.textMuted;
  return (
    <View style={styles.sportRow}>
      <View style={{ flex: 1 }}>
        <Text style={styles.sportName}>{row.sport}</Text>
        <Text style={styles.sportMeta}>{row.n} · {row.won}W / {row.lost}L</Text>
      </View>
      <View style={styles.sportStatsGroup}>
        <View style={styles.sportStat}>
          <Text style={styles.sportStatLabel}>HIT</Text>
          <Text style={styles.sportStatValue}>{row.hit_rate_pct.toFixed(1)}%</Text>
        </View>
        <View style={styles.sportStat}>
          <Text style={styles.sportStatLabel}>ROI</Text>
          <Text style={[styles.sportStatValue, { color: roiColor }]}>
            {row.roi_pct >= 0 ? "+" : ""}{row.roi_pct.toFixed(1)}%
          </Text>
        </View>
        <View style={styles.sportStat}>
          <Text style={styles.sportStatLabel}>CLV</Text>
          <Text style={[styles.sportStatValue, { color: clvColor }]}>
            {row.clv_avg_pct >= 0 ? "+" : ""}{row.clv_avg_pct.toFixed(2)}%
          </Text>
        </View>
      </View>
    </View>
  );
}

function SparkLine({ data }: { data: number[] }) {
  if (data.length < 2) return null;
  const min = Math.min(...data);
  const max = Math.max(...data);
  const range = max - min || 1;
  // 12 slots max
  const step = 100 / (data.length - 1);
  return (
    <View style={styles.sparkTrack}>
      {data.map((v, i) => {
        const h = ((v - min) / range) * 60 + 4;
        const positive = v >= 0;
        return (
          <View
            key={i}
            style={[
              styles.sparkBar,
              {
                height: h,
                backgroundColor: positive ? COLORS.neonGreen : COLORS.electricBlaze,
                left: `${i * step}%`,
              },
            ]}
          />
        );
      })}
    </View>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: COLORS.background },
  header: {
    flexDirection: "row", alignItems: "center", justifyContent: "space-between",
    paddingHorizontal: 16, paddingVertical: 12,
    borderBottomWidth: 1, borderBottomColor: COLORS.borderDefault,
  },
  title: {
    color: COLORS.textPrimary, fontSize: 18, fontWeight: "900", letterSpacing: 2.5,
  },
  content: { padding: 16, paddingBottom: 40 },
  // Window
  windowRow: { flexDirection: "row", gap: 8, marginBottom: 16 },
  windowChip: {
    flex: 1, paddingVertical: 8,
    borderWidth: 1, borderColor: COLORS.borderDefault, borderRadius: 10,
    alignItems: "center", backgroundColor: COLORS.surface,
  },
  windowChipActive: {
    borderColor: COLORS.voltBlue, backgroundColor: "rgba(76,171,255,0.12)",
  },
  windowChipText: { color: COLORS.textMuted, fontWeight: "800", letterSpacing: 1.2, fontSize: 12 },
  windowChipTextActive: { color: COLORS.voltBlue },
  // Headline
  headlineRow: { flexDirection: "row", gap: 10, marginBottom: 16 },
  headline: {
    flex: 1, padding: 14, borderRadius: 14,
    backgroundColor: COLORS.surface,
    borderWidth: 1, borderColor: COLORS.borderDefault,
  },
  headlineHighlight: {
    borderColor: "rgba(255,215,0,0.5)",
    backgroundColor: "rgba(255,215,0,0.05)",
  },
  headlineLabel: {
    color: COLORS.textMuted, fontSize: 9, fontWeight: "800", letterSpacing: 1.2,
  },
  headlineHit: {
    color: COLORS.textPrimary, fontSize: 30, fontWeight: "900", marginTop: 6,
    letterSpacing: -1,
  },
  headlineHitSub: {
    color: COLORS.textMuted, fontSize: 9, fontWeight: "700", letterSpacing: 1, marginTop: 2,
  },
  headlineRow2: {
    flexDirection: "row", gap: 12, marginTop: 12,
    paddingTop: 10, borderTopWidth: 1, borderTopColor: COLORS.borderDefault,
  },
  headlineMiniStat: { flex: 1 },
  headlineMiniLabel: {
    color: COLORS.textMuted, fontSize: 8, fontWeight: "800", letterSpacing: 1,
  },
  headlineMiniValue: { fontSize: 15, fontWeight: "900", marginTop: 2 },
  // Trend
  trendCard: {
    padding: 14, borderRadius: 14, marginBottom: 20,
    backgroundColor: COLORS.surface,
    borderWidth: 1, borderColor: COLORS.borderDefault,
  },
  trendHeader: {
    flexDirection: "row", justifyContent: "space-between", alignItems: "center",
    marginBottom: 8,
  },
  trendCumulative: { fontSize: 20, fontWeight: "900", letterSpacing: -0.5 },
  trendFooter: {
    flexDirection: "row", justifyContent: "space-between", marginTop: 8,
  },
  trendLabel: {
    color: COLORS.textMuted, fontSize: 10, fontWeight: "700", letterSpacing: 0.8,
  },
  sparkTrack: {
    height: 76, position: "relative",
  },
  sparkBar: {
    position: "absolute", bottom: 0, width: 4, borderRadius: 2,
  },
  // Section
  sectionLabel: {
    color: COLORS.textMuted, fontSize: 10, fontWeight: "800", letterSpacing: 1.5,
    marginTop: 4, marginBottom: 10,
  },
  bandList: { marginBottom: 20 },
  bandRow: {
    padding: 12, marginBottom: 8, borderRadius: 10,
    backgroundColor: COLORS.surface,
    borderWidth: 1, borderColor: COLORS.borderDefault,
  },
  bandHeader: { flexDirection: "row", justifyContent: "space-between", marginBottom: 6 },
  bandName: {
    color: COLORS.textPrimary, fontSize: 13, fontWeight: "900", letterSpacing: 1.5,
  },
  bandRight: { flexDirection: "row", gap: 12 },
  bandHit: { color: COLORS.textPrimary, fontWeight: "800", fontSize: 13 },
  bandRoi: { fontWeight: "900", fontSize: 13 },
  bandBarTrack: {
    height: 6, backgroundColor: COLORS.borderDefault, borderRadius: 3, overflow: "hidden",
  },
  bandBarFill: {
    height: 6, backgroundColor: COLORS.voltBlue, borderRadius: 3,
  },
  bandMeta: {
    color: COLORS.textMuted, fontSize: 10, fontWeight: "700", marginTop: 6, letterSpacing: 0.5,
  },
  // Sport rows
  sportRow: {
    flexDirection: "row", alignItems: "center",
    padding: 12, marginBottom: 8, borderRadius: 10,
    backgroundColor: COLORS.surface,
    borderWidth: 1, borderColor: COLORS.borderDefault,
  },
  sportName: {
    color: COLORS.textPrimary, fontSize: 13, fontWeight: "900", letterSpacing: 1.2,
  },
  sportMeta: {
    color: COLORS.textMuted, fontSize: 10, fontWeight: "700", marginTop: 2, letterSpacing: 0.4,
  },
  sportStatsGroup: { flexDirection: "row", gap: 12 },
  sportStat: { alignItems: "flex-end" },
  sportStatLabel: {
    color: COLORS.textMuted, fontSize: 8, fontWeight: "800", letterSpacing: 1,
  },
  sportStatValue: {
    color: COLORS.textPrimary, fontSize: 13, fontWeight: "900", marginTop: 2,
  },
  footerNote: {
    color: COLORS.textMuted, fontSize: 11, lineHeight: 17,
    marginTop: 12, marginBottom: 20, textAlign: "center", paddingHorizontal: 12,
  },
});
