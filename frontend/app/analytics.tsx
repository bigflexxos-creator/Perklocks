import { useEffect, useState, useCallback } from "react";
import {
  View, Text, StyleSheet, ScrollView, ActivityIndicator,
  RefreshControl, Pressable,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { Ionicons } from "@expo/vector-icons";
import { router, Stack } from "expo-router";

import { api, AnalyticsRow } from "@/src/lib/api";
import { COLORS } from "@/src/theme";

type Performance = Awaited<ReturnType<typeof api.modelPerformance>>;
type Learned = Awaited<ReturnType<typeof api.learnedWeights>>;
type V2 = Awaited<ReturnType<typeof api.analyticsV2>>;

const sign = (n: number) => (n > 0 ? "+" : "");
const fmt = (n: number, d = 2) => (Number.isFinite(n) ? n.toFixed(d) : "—");

export default function AnalyticsScreen() {
  const [data, setData] = useState<Performance | null>(null);
  const [learned, setLearned] = useState<Learned | null>(null);
  const [v2, setV2] = useState<V2 | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [learning, setLearning] = useState(false);

  const load = useCallback(async () => {
    try {
      const [res, lw, v2res] = await Promise.all([
        api.modelPerformance(),
        api.learnedWeights().catch(() => null),
        api.analyticsV2().catch(() => null),
      ]);
      // Defensive WNBA stripping. WNBA was permanently disabled in a prior
      // release because it was destroying ROI (-31% Player Points). If a
      // stale cached response or downstream pipeline ever returns WNBA
      // rows again, drop them client-side so they never reappear on the
      // Analytics screen.
      const stripWnba = <T extends Record<string, any>>(rows: T[] | undefined): T[] =>
        (rows || []).filter((r) => {
          const flat = `${r.sport || ""} ${r.market || ""} ${r.market_label || ""} ${r.label || ""} ${r.name || ""}`.toUpperCase();
          return !flat.includes("WNBA");
        });
      const cleanRes = res ? {
        ...res,
        by_sport: stripWnba((res as any).by_sport),
        by_market: stripWnba((res as any).by_market),
      } as Performance : res;
      const cleanLw = lw ? {
        ...lw,
        buckets: stripWnba((lw as any).buckets),
      } as Learned : lw;
      const cleanV2 = v2res ? {
        ...v2res,
        market_rows: stripWnba((v2res as any).market_rows),
      } as V2 : v2res;
      setData(cleanRes);
      setLearned(cleanLw);
      setV2(cleanV2);
    } catch (e) {
      setData(null);
      setLearned(null);
      setV2(null);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const onRefresh = useCallback(() => { setRefreshing(true); load(); }, [load]);

  const onLearnNow = useCallback(async () => {
    setLearning(true);
    try {
      await api.learnNow();
      await api.analyticsV2Recompute().catch(() => null);
      await load();
    } catch {}
    setLearning(false);
  }, [load]);

  if (loading) {
    return (
      <SafeAreaView style={styles.safe}>
        <ActivityIndicator color={COLORS.voltBlue} style={{ marginTop: 80 }} />
      </SafeAreaView>
    );
  }
  if (!data || !data.totals) {
    return (
      <SafeAreaView style={styles.safe}>
        <Stack.Screen options={{ headerShown: false }} />
        <Header />
        <View style={styles.empty}>
          <Text style={styles.emptyTxt}>Unable to load analytics. Pull to retry.</Text>
        </View>
      </SafeAreaView>
    );
  }

  const t = data.totals;
  const roiColor = (t.roi_pct ?? 0) >= 0 ? COLORS.neonGreen : COLORS.electricBlaze;

  return (
    <SafeAreaView style={styles.safe} edges={["top"]}>
      <Stack.Screen options={{ headerShown: false }} />
      <Header />
      <ScrollView
        contentContainerStyle={styles.scroll}
        refreshControl={<RefreshControl tintColor={COLORS.voltBlue} refreshing={refreshing} onRefresh={onRefresh} />}
      >
        <Text style={styles.subhead}>MODEL PERFORMANCE · ALL SETTLED PICKS</Text>
        <Text style={styles.caption}>
          Every generated pick is auto-tracked at 1u flat stake. No manual logging.
        </Text>

        {/* ── Hero metrics ── */}
        <View style={styles.heroRow}>
          <HeroCard
            label="ROI"
            value={`${sign(t.roi_pct)}${fmt(t.roi_pct, 2)}%`}
            color={roiColor}
            sub={`${t.units_risked.toFixed(0)}u risked`}
          />
          <HeroCard
            label="Units Won"
            value={`${sign(t.units_won)}${fmt(t.units_won, 2)}u`}
            color={t.units_won >= 0 ? COLORS.neonGreen : COLORS.electricBlaze}
            sub={`${t.wins}W · ${t.losses}L · ${t.pushes}P`}
          />
        </View>

        <View style={styles.grid}>
          <StatTile label="Hit Rate" value={`${fmt(t.hit_rate, 1)}%`} hint={`${t.decisive} decided`} />
          <StatTile label="Avg Edge" value={`${sign(t.avg_edge_pct)}${fmt(t.avg_edge_pct, 1)}%`} hint="model vs market" />
          <StatTile label="Avg CLV" value={`${sign(t.avg_clv)}${fmt(t.avg_clv, 2)}`}
                    hint="implied prob Δ" muted={t.avg_clv === 0} />
          <StatTile label="+CLV %" value={`${fmt(t.positive_clv_pct, 0)}%`} hint="picks beat closing" muted={t.positive_clv_pct === 0} />
          <StatTile label="L7 Days" value={`${sign(t.units_profit_7d)}${fmt(t.units_profit_7d, 2)}u`}
                    valueColor={t.units_profit_7d >= 0 ? COLORS.neonGreen : COLORS.electricBlaze} />
          <StatTile label="L30 Days" value={`${sign(t.units_profit_30d)}${fmt(t.units_profit_30d, 2)}u`}
                    valueColor={t.units_profit_30d >= 0 ? COLORS.neonGreen : COLORS.electricBlaze} />
        </View>

        {/* ── Highlights ── */}
        <View style={styles.highlightRow}>
          {data.highlights.best_sport && (
            <Highlight icon="trophy" tint={COLORS.goldElite} title="Best Sport"
              value={data.highlights.best_sport.key}
              detail={`${sign(data.highlights.best_sport.roi)}${fmt(data.highlights.best_sport.roi, 1)}% ROI`} />
          )}
          {data.highlights.best_market && (
            <Highlight icon="trending-up" tint={COLORS.neonGreen} title="Best Market"
              value={data.highlights.best_market.key}
              detail={`${sign(data.highlights.best_market.roi)}${fmt(data.highlights.best_market.roi, 1)}% ROI`} />
          )}
          {data.highlights.worst_market && (
            <Highlight icon="trending-down" tint={COLORS.electricBlaze} title="Worst Market"
              value={data.highlights.worst_market.key}
              detail={`${sign(data.highlights.worst_market.roi)}${fmt(data.highlights.worst_market.roi, 1)}% ROI`} />
          )}
        </View>

        {/* ── Calibration audit ── */}
        <SectionHeader title="Confidence Calibration" hint="Does Lock Score predict reality?" />
        <View style={styles.tableHeader}>
          <Text style={[styles.th, { flex: 1.6 }]}>BAND</Text>
          <Text style={[styles.th, { flex: 0.7, textAlign: "right" }]}>N</Text>
          <Text style={[styles.th, { flex: 1, textAlign: "right" }]}>EXPECTED</Text>
          <Text style={[styles.th, { flex: 1, textAlign: "right" }]}>ACTUAL</Text>
          <Text style={[styles.th, { flex: 1, textAlign: "right" }]}>Δ</Text>
        </View>
        {data.calibration.map((c) => (
          <View key={c.band} style={styles.tableRow}>
            <Text style={[styles.td, { flex: 1.6, color: COLORS.textPrimary, fontWeight: "600" }]}>{c.band}</Text>
            <Text style={[styles.td, { flex: 0.7, textAlign: "right" }]}>{c.count}</Text>
            <Text style={[styles.td, { flex: 1, textAlign: "right" }]}>{fmt(c.avg_lock_score, 1)}%</Text>
            <Text style={[styles.td, { flex: 1, textAlign: "right", color: COLORS.textPrimary }]}>
              {fmt(c.actual_hit_rate, 1)}%
            </Text>
            <Text style={[
              styles.td, { flex: 1, textAlign: "right", fontWeight: "700",
                color: c.delta >= 0 ? COLORS.neonGreen : COLORS.electricBlaze }]}>
              {sign(c.delta)}{fmt(c.delta, 1)}
            </Text>
          </View>
        ))}

        {/* ── What the App Has Learned ── */}
        {learned && (learned.buckets?.length ?? 0) > 0 && (
          <>
            <View style={styles.sectionHeader}>
              <View>
                <Text style={styles.sectionTitle}>What the App Has Learned</Text>
                <Text style={styles.sectionHint}>
                  Auto-bias from {learned.sample_size} settled picks · ±8pp cap
                </Text>
              </View>
              <Pressable
                onPress={onLearnNow}
                disabled={learning}
                style={({ pressed }) => [styles.learnBtn, pressed && { opacity: 0.7 }]}
              >
                {learning ? (
                  <ActivityIndicator color={COLORS.voltBlue} size="small" />
                ) : (
                  <>
                    <Ionicons name="sync" size={14} color={COLORS.voltBlue} />
                    <Text style={styles.learnBtnTxt}>RELEARN</Text>
                  </>
                )}
              </Pressable>
            </View>

            {/* Active learned bucket weights */}
            {learned.buckets.filter(b => b.active).map((b) => (
              <View key={`${b.sport}-${b.market_label}`} style={styles.learnedRow}>
                <View style={{ flex: 1.7 }}>
                  <Text style={styles.breakdownKey}>{b.sport} · {b.market_label}</Text>
                  <Text style={styles.breakdownSub}>
                    n={b.n} · hit {fmt(b.hit_rate, 1)}% · ROI {sign(b.roi)}{fmt(b.roi, 1)}%
                  </Text>
                </View>
                <View style={{ width: 90, alignItems: "flex-end" }}>
                  <View style={[
                    styles.weightPill,
                    { backgroundColor: (b.weight >= 0 ? COLORS.neonGreen : COLORS.electricBlaze) + "22",
                      borderColor: (b.weight >= 0 ? COLORS.neonGreen : COLORS.electricBlaze) + "55" }]}>
                    <Text style={[styles.weightTxt,
                      { color: b.weight >= 0 ? COLORS.neonGreen : COLORS.electricBlaze }]}>
                      {sign(b.weight * 100)}{fmt(b.weight * 100, 1)}pp
                    </Text>
                  </View>
                  <Text style={styles.breakdownSub}>win-prob bias</Text>
                </View>
              </View>
            ))}

            {/* Inactive buckets (not enough data) — small list at bottom */}
            {learned.buckets.filter(b => !b.active).length > 0 && (
              <Text style={styles.footnote}>
                {learned.buckets.filter(b => !b.active).length} more bucket{learned.buckets.filter(b => !b.active).length === 1 ? "" : "s"} waiting for ≥{learned.settings?.min_samples ?? 10} picks before learning kicks in.
              </Text>
            )}
          </>
        )}

        {/* ── By Sport ── */}
        <SectionHeader title="By Sport" hint="ROI · Hit Rate · Edge" />
        {data.by_sport.map((r) => <BreakdownRow key={r.key} row={r} />)}

        {/* ── By Market ── */}
        <SectionHeader title="By Market" hint="≥5 picks shown" />
        {data.by_market.filter(r => r.count >= 5).map((r) => <BreakdownRow key={r.key} row={r} />)}

        {/* ── By Confidence ── */}
        <SectionHeader title="By Confidence Bucket" />
        {data.by_confidence.map((r) => <BreakdownRow key={r.key} row={r} />)}

        {/* ── Learning System v2 (NEW) ── */}
        {v2 && (v2.total_settled ?? 0) > 0 && (
          <>
            <SectionHeader
              title="Learning System v2"
              hint={`ROI 50% · CLV 25% · Calibration 20% · Volume 5% · ${v2.total_settled} settled`}
            />

            {/* Profit by Sport */}
            {(v2.profit_by_sport ?? []).map((s) => (
              <View key={`v2sport-${s.sport}`} style={styles.row}>
                <Text style={[styles.td, { flex: 1.4, fontWeight: "700" }]}>{s.sport}</Text>
                <Text style={[styles.td, { flex: 0.7, textAlign: "right" }]}>{s.n}</Text>
                <Text style={[styles.td, { flex: 1, textAlign: "right" }]}>
                  {fmt(s.hit_rate_pct, 1)}%
                </Text>
                <Text style={[styles.td, { flex: 1, textAlign: "right", fontWeight: "700",
                  color: s.roi_pct >= 0 ? COLORS.neonGreen : COLORS.electricBlaze }]}>
                  {sign(s.roi_pct)}{fmt(s.roi_pct, 1)}%
                </Text>
                <Text style={[styles.td, { flex: 1, textAlign: "right",
                  color: s.units_profit >= 0 ? COLORS.neonGreen : COLORS.electricBlaze }]}>
                  {sign(s.units_profit)}{fmt(s.units_profit, 1)}u
                </Text>
              </View>
            ))}

            {/* Calibration band — expected vs actual */}
            {(v2.band_calibration ?? []).length > 0 && (
              <>
                <SectionHeader title="Band Calibration" hint="Expected vs actual hit rate" />
                {v2.band_calibration!.map((b) => (
                  <View key={`band-${b.band}`} style={styles.row}>
                    <Text style={[styles.td, { flex: 1, fontWeight: "700" }]}>LOCK {b.band}</Text>
                    <Text style={[styles.td, { flex: 0.7, textAlign: "right" }]}>n={b.n}</Text>
                    <Text style={[styles.td, { flex: 1, textAlign: "right" }]}>
                      exp {fmt(b.expected, 0)}%
                    </Text>
                    <Text style={[styles.td, { flex: 1, textAlign: "right", fontWeight: "700" }]}>
                      act {fmt(b.actual, 1)}%
                    </Text>
                    <Text style={[styles.td, { flex: 1, textAlign: "right", fontWeight: "700",
                      color: b.gap > 10 ? COLORS.electricBlaze :
                             b.gap > 0  ? "#F2C744" : COLORS.neonGreen }]}>
                      {sign(-b.gap)}{fmt(-b.gap, 1)}pp
                    </Text>
                  </View>
                ))}
              </>
            )}

            {/* Top-ROI markets */}
            {(v2.market_rows ?? []).filter(r => r.n >= 5).length > 0 && (
              <>
                <SectionHeader title="Profit by Market" hint="ROI-sorted · ≥5 picks" />
                {(v2.market_rows ?? [])
                  .filter(r => r.n >= 5)
                  .sort((a, b) => b.roi - a.roi)
                  .slice(0, 12)
                  .map((r) => (
                    <View key={`v2m-${r.sport}-${r.market}`} style={styles.row}>
                      <View style={{ flex: 2 }}>
                        <Text style={styles.breakdownKey}>{r.sport} · {r.market}</Text>
                        <Text style={styles.breakdownSub}>
                          n={r.n} · hit {fmt(r.hit_rate, 1)}% · CLV {sign(r.clv_avg)}{fmt(r.clv_avg, 1)}
                        </Text>
                      </View>
                      <Text style={[styles.td, { flex: 1, textAlign: "right", fontWeight: "700",
                        color: r.roi >= 0 ? COLORS.neonGreen : COLORS.electricBlaze }]}>
                        {sign(r.roi)}{fmt(r.roi, 1)}%
                      </Text>
                    </View>
                  ))}
              </>
            )}

            {/* Active market weights */}
            {v2.market_weights && Object.keys(v2.market_weights).length > 0 && (
              <>
                <SectionHeader title="Market Filter Weights" hint="Boost (>1.0) · Decay (<1.0)" />
                {Object.entries(v2.market_weights).map(([m, w]) => (
                  <View key={`mw-${m}`} style={styles.row}>
                    <Text style={[styles.td, { flex: 2, fontWeight: "700" }]}>{m}</Text>
                    <Text style={[styles.td, { flex: 1, textAlign: "right", fontWeight: "800",
                      color: w > 1.0 ? COLORS.neonGreen : w < 1.0 ? COLORS.electricBlaze : COLORS.textSecondary }]}>
                      ×{fmt(w, 2)}
                    </Text>
                  </View>
                ))}
              </>
            )}

            {/* Changes log */}
            {(v2.changes_log ?? []).length > 0 && (
              <>
                <SectionHeader title="Learning Changes Log" hint="Most recent first" />
                {v2.changes_log!.slice(0, 10).map((c, i) => (
                  <View key={`log-${i}`} style={[styles.row, { flexDirection: "column", alignItems: "stretch" }]}>
                    <Text style={{ color: COLORS.textPrimary, fontWeight: "700", fontSize: 12 }}>
                      [{c.type}] {c.band ? `Band ${c.band}` : `${c.sport ?? ""} · ${c.market ?? ""}`}
                    </Text>
                    <Text style={{ color: COLORS.textMuted, fontSize: 11, marginTop: 2 }}>
                      {c.reason}
                    </Text>
                  </View>
                ))}
              </>
            )}
          </>
        )}

        <Text style={styles.footnote}>
          Updated {new Date(data.as_of).toLocaleString()}. CLV populates after a few daily refreshes.
        </Text>
      </ScrollView>
    </SafeAreaView>
  );
}

function Header() {
  return (
    <View style={styles.header}>
      <Pressable onPress={() => router.back()} hitSlop={12} style={styles.backBtn}>
        <Ionicons name="chevron-back" size={22} color={COLORS.textPrimary} />
      </Pressable>
      <Text style={styles.headerTitle}>Analytics</Text>
      <Pressable
        onPress={() => router.push("/strategy-lab")}
        hitSlop={12}
        style={styles.backBtn}
        testID="strategy-lab-btn"
      >
        <Ionicons name="flask-outline" size={22} color={COLORS.goldElite} />
      </Pressable>
    </View>
  );
}

function HeroCard({ label, value, sub, color }: {
  label: string; value: string; sub: string; color: string;
}) {
  return (
    <View style={styles.heroCard}>
      <Text style={styles.heroLabel}>{label}</Text>
      <Text style={[styles.heroValue, { color }]}>{value}</Text>
      <Text style={styles.heroSub}>{sub}</Text>
    </View>
  );
}

function StatTile({ label, value, hint, muted, valueColor }: {
  label: string; value: string; hint?: string; muted?: boolean; valueColor?: string;
}) {
  return (
    <View style={[styles.tile, muted && styles.tileMuted]}>
      <Text style={styles.tileLabel}>{label}</Text>
      <Text style={[styles.tileValue, valueColor ? { color: valueColor } : null]}>{value}</Text>
      {hint && <Text style={styles.tileHint}>{hint}</Text>}
    </View>
  );
}

function Highlight({ icon, tint, title, value, detail }: {
  icon: any; tint: string; title: string; value: string; detail: string;
}) {
  return (
    <View style={[styles.highlight, { borderColor: tint + "55" }]}>
      <Ionicons name={icon} size={18} color={tint} />
      <Text style={styles.highlightTitle}>{title}</Text>
      <Text style={styles.highlightValue} numberOfLines={1}>{value}</Text>
      <Text style={[styles.highlightDetail, { color: tint }]}>{detail}</Text>
    </View>
  );
}

function SectionHeader({ title, hint }: { title: string; hint?: string }) {
  return (
    <View style={styles.sectionHeader}>
      <Text style={styles.sectionTitle}>{title}</Text>
      {hint && <Text style={styles.sectionHint}>{hint}</Text>}
    </View>
  );
}

function BreakdownRow({ row }: { row: AnalyticsRow }) {
  const positive = row.roi >= 0;
  return (
    <View style={styles.breakdownRow}>
      <View style={{ flex: 1.6 }}>
        <Text style={styles.breakdownKey}>{row.key}</Text>
        <Text style={styles.breakdownSub}>{row.wins}W-{row.losses}L · {fmt(row.hit_rate, 1)}%</Text>
      </View>
      <View style={{ flex: 1, alignItems: "flex-end" }}>
        <Text style={[styles.breakdownROI, { color: positive ? COLORS.neonGreen : COLORS.electricBlaze }]}>
          {sign(row.roi)}{fmt(row.roi, 1)}%
        </Text>
        <Text style={styles.breakdownSub}>{sign(row.units)}{fmt(row.units, 2)}u</Text>
      </View>
      <View style={{ width: 60, alignItems: "flex-end" }}>
        <Text style={styles.breakdownEdge}>+{fmt(row.avg_edge, 1)}%</Text>
        <Text style={styles.breakdownSub}>edge</Text>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: COLORS.bg },
  header: {
    flexDirection: "row", alignItems: "center", justifyContent: "space-between",
    paddingHorizontal: 16, paddingVertical: 12,
    borderBottomWidth: StyleSheet.hairlineWidth, borderBottomColor: COLORS.borderDefault,
  },
  backBtn: { width: 32, height: 32, justifyContent: "center" },
  headerTitle: { color: COLORS.textPrimary, fontSize: 18, fontWeight: "700", letterSpacing: 0.3 },
  scroll: { padding: 16, paddingBottom: 64 },
  subhead: { color: COLORS.textSecondary, fontSize: 11, letterSpacing: 1.5, marginBottom: 4 },
  caption: { color: COLORS.textMuted, fontSize: 12, marginBottom: 16 },

  heroRow: { flexDirection: "row", gap: 12, marginBottom: 12 },
  heroCard: {
    flex: 1, backgroundColor: COLORS.surface, borderRadius: 16, padding: 16,
    borderWidth: 1, borderColor: COLORS.borderDefault,
  },
  heroLabel: { color: COLORS.textMuted, fontSize: 11, letterSpacing: 1.2, marginBottom: 8 },
  heroValue: { fontSize: 28, fontWeight: "800", letterSpacing: -0.5 },
  heroSub: { color: COLORS.textSecondary, fontSize: 12, marginTop: 4 },

  grid: { flexDirection: "row", flexWrap: "wrap", marginHorizontal: -4 },
  tile: {
    width: "33.333%", paddingHorizontal: 4, marginBottom: 8,
  },
  tileMuted: { opacity: 0.55 },
  tileLabel: { color: COLORS.textMuted, fontSize: 10, letterSpacing: 1.1, marginBottom: 4,
    backgroundColor: COLORS.surface, paddingHorizontal: 10, paddingTop: 10, borderTopLeftRadius: 12, borderTopRightRadius: 12 },
  tileValue: { color: COLORS.textPrimary, fontSize: 18, fontWeight: "700",
    backgroundColor: COLORS.surface, paddingHorizontal: 10 },
  tileHint: { color: COLORS.textMuted, fontSize: 10,
    backgroundColor: COLORS.surface, paddingHorizontal: 10, paddingBottom: 10, borderBottomLeftRadius: 12, borderBottomRightRadius: 12 },

  highlightRow: { flexDirection: "row", gap: 10, marginTop: 18 },
  highlight: { flex: 1, backgroundColor: COLORS.surface, borderRadius: 14, padding: 12, borderWidth: 1 },
  highlightTitle: { color: COLORS.textMuted, fontSize: 10, marginTop: 6, letterSpacing: 1.1 },
  highlightValue: { color: COLORS.textPrimary, fontSize: 13, fontWeight: "600", marginTop: 4 },
  highlightDetail: { fontSize: 13, fontWeight: "700", marginTop: 2 },

  sectionHeader: { marginTop: 26, marginBottom: 8, flexDirection: "row", alignItems: "baseline", justifyContent: "space-between" },
  sectionTitle: { color: COLORS.textPrimary, fontSize: 16, fontWeight: "700" },
  sectionHint: { color: COLORS.textMuted, fontSize: 11 },

  tableHeader: {
    flexDirection: "row", paddingHorizontal: 12, paddingVertical: 8,
    borderBottomWidth: StyleSheet.hairlineWidth, borderColor: COLORS.borderDefault,
  },
  th: { color: COLORS.textMuted, fontSize: 10, letterSpacing: 1.2 },
  tableRow: {
    flexDirection: "row", paddingHorizontal: 12, paddingVertical: 10,
    borderBottomWidth: StyleSheet.hairlineWidth, borderColor: COLORS.borderDefault,
  },
  td: { color: COLORS.textSecondary, fontSize: 12 },

  breakdownRow: {
    flexDirection: "row", alignItems: "center", paddingHorizontal: 12, paddingVertical: 12,
    backgroundColor: COLORS.surface, marginBottom: 6, borderRadius: 10,
  },
  breakdownKey: { color: COLORS.textPrimary, fontSize: 14, fontWeight: "600" },
  breakdownSub: { color: COLORS.textMuted, fontSize: 11, marginTop: 2 },
  breakdownROI: { fontSize: 15, fontWeight: "700" },
  breakdownEdge: { color: COLORS.voltBlue, fontSize: 13, fontWeight: "600" },

  learnBtn: {
    flexDirection: "row", alignItems: "center", gap: 6,
    paddingHorizontal: 10, paddingVertical: 6,
    borderWidth: 1, borderColor: COLORS.voltBlue + "55", borderRadius: 16,
  },
  learnBtnTxt: { color: COLORS.voltBlue, fontSize: 10, fontWeight: "700", letterSpacing: 1.2 },
  learnedRow: {
    flexDirection: "row", alignItems: "center", paddingHorizontal: 12, paddingVertical: 12,
    backgroundColor: COLORS.surface, marginBottom: 6, borderRadius: 10,
    borderLeftWidth: 3, borderLeftColor: COLORS.voltBlue,
  },
  weightPill: {
    paddingHorizontal: 10, paddingVertical: 4, borderRadius: 12, borderWidth: 1,
  },
  weightTxt: { fontSize: 13, fontWeight: "700" },

  empty: { padding: 32, alignItems: "center" },
  emptyTxt: { color: COLORS.textMuted, fontSize: 13 },
  footnote: { color: COLORS.textMuted, fontSize: 10, marginTop: 24, textAlign: "center" },
});
