import { useEffect, useState, useCallback } from "react";
import {
  View, Text, StyleSheet, ScrollView, ActivityIndicator,
  RefreshControl, Pressable, Alert,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { Ionicons } from "@expo/vector-icons";
import { Stack } from "expo-router";

import { api } from "@/src/lib/api";
import { COLORS } from "@/src/theme";

const sign = (n: number) => (n > 0 ? "+" : "");
const fmt = (n: number, d = 2) => (Number.isFinite(n) ? n.toFixed(d) : "—");

type Summary = Awaited<ReturnType<typeof api.myAnalyticsSummary>>;
type SportRows = Awaited<ReturnType<typeof api.myAnalyticsBySport>>;
type MarketRows = Awaited<ReturnType<typeof api.myAnalyticsByMarket>>;
type BetList = Awaited<ReturnType<typeof api.listMyBets>>;

export default function MyBetsScreen() {
  const [summary, setSummary] = useState<Summary | null>(null);
  const [bySport, setBySport] = useState<SportRows | null>(null);
  const [byMarket, setByMarket] = useState<MarketRows | null>(null);
  const [pending, setPending] = useState<BetList | null>(null);
  const [history, setHistory] = useState<BetList | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [tab, setTab] = useState<"pending" | "history">("pending");

  const load = useCallback(async () => {
    try {
      const [s, sp, mk, p, h] = await Promise.all([
        api.myAnalyticsSummary().catch(() => null),
        api.myAnalyticsBySport().catch(() => null),
        api.myAnalyticsByMarket().catch(() => null),
        api.listMyBets({ status: "pending", limit: 200 }).catch(() => null),
        api.myAnalyticsHistory(100).catch(() => null),
      ]);
      setSummary(s);
      setBySport(sp);
      setByMarket(mk);
      setPending(p);
      setHistory(h);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const onRefresh = useCallback(() => {
    setRefreshing(true);
    load();
  }, [load]);

  const removeBet = useCallback(async (betId: string) => {
    Alert.alert("Untrack Bet", "Remove this pending bet from your log?", [
      { text: "Cancel", style: "cancel" },
      {
        text: "Remove",
        style: "destructive",
        onPress: async () => {
          try {
            await api.deleteMyBet(betId);
            load();
          } catch (e: any) {
            Alert.alert("Could not remove", e?.message ?? "Try again in a moment.");
          }
        },
      },
    ]);
  }, [load]);

  if (loading) {
    return (
      <SafeAreaView style={styles.screen} edges={["top"]}>
        <View style={styles.center}>
          <ActivityIndicator size="large" color={COLORS.neonGreen} />
        </View>
      </SafeAreaView>
    );
  }

  const roiColor =
    (summary?.roi_pct ?? 0) > 0 ? COLORS.neonGreen :
    (summary?.roi_pct ?? 0) < 0 ? COLORS.electricBlaze : COLORS.textMuted;

  return (
    <SafeAreaView style={styles.screen} edges={["top"]}>
      <Stack.Screen options={{ headerShown: false }} />
      <ScrollView
        contentContainerStyle={styles.scroll}
        refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={COLORS.neonGreen} />}
      >
        <View style={styles.header}>
          <Text style={styles.headerTitle}>My Bets</Text>
          <Text style={styles.headerSub}>
            Personal ROI for the picks you&apos;ve tracked. Nothing else counts.
          </Text>
        </View>

        {/* ── Summary row ────────────────────────────────────────── */}
        <View style={styles.summaryRow}>
          <StatBox label="ROI"
            value={summary ? `${sign(summary.roi_pct)}${fmt(summary.roi_pct, 2)}%` : "—"}
            color={roiColor} />
          <StatBox label="Net Units"
            value={summary ? `${sign(summary.pnl_units)}${fmt(summary.pnl_units, 2)}u` : "—"}
            color={roiColor} />
          <StatBox label="Hit Rate"
            value={summary ? `${fmt(summary.hit_rate_pct, 1)}%` : "—"}
            color={COLORS.textPrimary} />
        </View>
        <View style={styles.summaryRow}>
          <StatBox label="Bets Tracked" value={String(summary?.total_bets ?? 0)} color={COLORS.textPrimary} />
          <StatBox label="W-L-P" value={summary ? `${summary.won}-${summary.lost}-${summary.push}` : "—"} color={COLORS.textPrimary} />
          <StatBox label="Pending" value={String(summary?.pending ?? 0)} color={COLORS.electricBlaze} />
        </View>

        {/* ── Tab switch: Pending vs History ─────────────────────── */}
        <View style={styles.tabRow}>
          <Pressable
            style={[styles.tabBtn, tab === "pending" && styles.tabBtnActive]}
            onPress={() => setTab("pending")}
          >
            <Text style={[styles.tabBtnText, tab === "pending" && styles.tabBtnTextActive]}>
              PENDING ({pending?.count ?? 0})
            </Text>
          </Pressable>
          <Pressable
            style={[styles.tabBtn, tab === "history" && styles.tabBtnActive]}
            onPress={() => setTab("history")}
          >
            <Text style={[styles.tabBtnText, tab === "history" && styles.tabBtnTextActive]}>
              HISTORY ({history?.count ?? 0})
            </Text>
          </Pressable>
        </View>

        {/* ── Bet list ───────────────────────────────────────────── */}
        {(tab === "pending" ? pending?.bets : history?.bets)?.length ? (
          (tab === "pending" ? pending!.bets : history!.bets).map((b: any) => (
            <BetRow
              key={b.id}
              bet={b}
              onRemove={tab === "pending" ? () => removeBet(b.id) : undefined}
            />
          ))
        ) : (
          <View style={styles.empty}>
            <Ionicons name="checkmark-circle-outline" size={40} color={COLORS.textMuted} />
            <Text style={styles.emptyTxt}>
              {tab === "pending"
                ? "No pending bets. Tap Track on any card to log a bet."
                : "No settled bets yet."}
            </Text>
          </View>
        )}

        {/* ── By-Sport breakdown ─────────────────────────────────── */}
        {(bySport?.rows?.length ?? 0) > 0 && (
          <View style={styles.section}>
            <Text style={styles.sectionTitle}>By Sport</Text>
            {bySport!.rows.map((r) => (
              <BreakdownRow
                key={r.sport}
                label={r.sport}
                n={r.n} won={r.won} lost={r.lost}
                hit={r.hit_rate_pct} pnl={r.pnl_units} roi={r.roi_pct}
              />
            ))}
          </View>
        )}

        {/* ── By-Market breakdown ────────────────────────────────── */}
        {(byMarket?.rows?.length ?? 0) > 0 && (
          <View style={styles.section}>
            <Text style={styles.sectionTitle}>By Market</Text>
            {byMarket!.rows.slice(0, 10).map((r) => (
              <BreakdownRow
                key={r.market}
                label={r.market}
                n={r.n} won={r.won} lost={r.lost}
                hit={r.hit_rate_pct} pnl={r.pnl_units} roi={r.roi_pct}
              />
            ))}
          </View>
        )}
      </ScrollView>
    </SafeAreaView>
  );
}

/* ── Sub-components ──────────────────────────────────────────────── */
function StatBox({ label, value, color }: { label: string; value: string; color: string }) {
  return (
    <View style={styles.statBox}>
      <Text style={styles.statLabel}>{label}</Text>
      <Text style={[styles.statValue, { color }]}>{value}</Text>
    </View>
  );
}

function BetRow({ bet, onRemove }: { bet: any; onRemove?: () => void }) {
  const st = bet.status as string;
  const pnl = bet.pnl_units ?? 0;
  const stColor =
    st === "won" ? COLORS.neonGreen :
    st === "lost" ? COLORS.electricBlaze :
    st === "push" ? COLORS.textMuted :
    "#FBBF24"; // pending amber

  const oddsStr = typeof bet.odds_at_bet === "number"
    ? (bet.odds_at_bet > 0 ? `+${bet.odds_at_bet}` : String(bet.odds_at_bet))
    : "—";

  return (
    <View style={styles.betRow}>
      <View style={{ flex: 1 }}>
        <Text style={styles.betEvent} numberOfLines={1}>
          {bet.event ?? bet.selection ?? "—"}
        </Text>
        <Text style={styles.betMeta} numberOfLines={1}>
          {bet.sport} · {bet.market} · {oddsStr} · {bet.stake_units}u
          {bet.bet_type === "parlay" ? ` · ${bet.parlay_legs?.length ?? 0}-leg parlay` : ""}
        </Text>
      </View>
      <View style={styles.betRightCol}>
        <Text style={[styles.betStatus, { color: stColor }]}>
          {st.toUpperCase()}
        </Text>
        {st !== "pending" && (
          <Text style={[styles.betPnl, { color: pnl >= 0 ? COLORS.neonGreen : COLORS.electricBlaze }]}>
            {pnl >= 0 ? "+" : ""}{fmt(pnl, 2)}u
          </Text>
        )}
        {onRemove && (
          <Pressable onPress={onRemove} hitSlop={8}>
            <Ionicons name="close-circle" size={20} color={COLORS.textMuted} />
          </Pressable>
        )}
      </View>
    </View>
  );
}

function BreakdownRow({ label, n, won, lost, hit, pnl, roi }: {
  label: string; n: number; won: number; lost: number;
  hit: number; pnl: number; roi: number;
}) {
  const good = roi >= 0;
  return (
    <View style={styles.breakdownRow}>
      <Text style={styles.breakdownLabel} numberOfLines={1}>{label}</Text>
      <View style={styles.breakdownStats}>
        <Text style={styles.breakdownMeta}>{n} bets · {won}-{lost} · {fmt(hit, 1)}%</Text>
        <Text style={[styles.breakdownRoi, { color: good ? COLORS.neonGreen : COLORS.electricBlaze }]}>
          {sign(roi)}{fmt(roi, 1)}% · {sign(pnl)}{fmt(pnl, 2)}u
        </Text>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: "transparent" },
  center: { flex: 1, alignItems: "center", justifyContent: "center" },
  scroll: { paddingBottom: 32 },
  header: { paddingHorizontal: 16, paddingTop: 12, paddingBottom: 16 },
  headerTitle: { color: COLORS.textPrimary, fontSize: 26, fontWeight: "900", letterSpacing: 0.5 },
  headerSub: { color: COLORS.textMuted, fontSize: 13, marginTop: 4 },
  summaryRow: { flexDirection: "row", paddingHorizontal: 12, gap: 8, marginBottom: 8 },
  statBox: {
    flex: 1, padding: 12, borderRadius: 10,
    backgroundColor: "rgba(255,255,255,0.04)",
    borderWidth: 1, borderColor: COLORS.borderDefault,
  },
  statLabel: { color: COLORS.textMuted, fontSize: 10, fontWeight: "800", letterSpacing: 1.2 },
  statValue: { fontSize: 20, fontWeight: "900", marginTop: 4 },
  tabRow: { flexDirection: "row", paddingHorizontal: 16, marginTop: 12, marginBottom: 8, gap: 8 },
  tabBtn: {
    flex: 1, paddingVertical: 10, borderRadius: 8, alignItems: "center",
    backgroundColor: "rgba(255,255,255,0.03)",
    borderWidth: 1, borderColor: COLORS.borderDefault,
  },
  tabBtnActive: { backgroundColor: COLORS.neonGreen + "22", borderColor: COLORS.neonGreen },
  tabBtnText: { color: COLORS.textMuted, fontSize: 11, fontWeight: "900", letterSpacing: 1.2 },
  tabBtnTextActive: { color: COLORS.neonGreen },
  betRow: {
    marginHorizontal: 12, marginVertical: 4,
    paddingHorizontal: 14, paddingVertical: 12,
    borderRadius: 10, borderWidth: 1, borderColor: COLORS.borderDefault,
    backgroundColor: "rgba(255,255,255,0.03)",
    flexDirection: "row", alignItems: "center", gap: 12,
  },
  betEvent: { color: COLORS.textPrimary, fontSize: 14, fontWeight: "700" },
  betMeta: { color: COLORS.textMuted, fontSize: 12, marginTop: 2 },
  betRightCol: { alignItems: "flex-end", gap: 4, minWidth: 68 },
  betStatus: { fontSize: 11, fontWeight: "900", letterSpacing: 1.1 },
  betPnl: { fontSize: 14, fontWeight: "800" },
  empty: { alignItems: "center", paddingVertical: 40, paddingHorizontal: 24 },
  emptyTxt: { color: COLORS.textMuted, fontSize: 13, marginTop: 10, textAlign: "center" },
  section: { paddingHorizontal: 12, paddingTop: 20, paddingBottom: 8 },
  sectionTitle: { color: COLORS.textPrimary, fontSize: 15, fontWeight: "800", marginBottom: 8, paddingHorizontal: 4 },
  breakdownRow: {
    paddingHorizontal: 12, paddingVertical: 10,
    borderBottomWidth: 1, borderBottomColor: COLORS.borderDefault,
    flexDirection: "row", justifyContent: "space-between", alignItems: "center",
  },
  breakdownLabel: { color: COLORS.textPrimary, fontSize: 13, fontWeight: "700", flex: 1 },
  breakdownStats: { alignItems: "flex-end" },
  breakdownMeta: { color: COLORS.textMuted, fontSize: 11 },
  breakdownRoi: { fontSize: 13, fontWeight: "800", marginTop: 2 },
});
