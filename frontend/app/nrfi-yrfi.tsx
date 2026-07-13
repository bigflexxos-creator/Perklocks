/**
 * NRFI / YRFI — MLB First-Inning Run Picks.
 *
 * Dedicated MLB sub-tab. Pulls from `GET /api/picks/nrfi-yrfi` and shows
 * each game's Poisson model output:
 *   • λ₁ (expected runs in 1st inning)
 *   • Pitcher / lineup / park factors
 *   • P(NRFI) vs P(YRFI)
 *   • Recommendation (NRFI or YRFI)
 *
 * Picks are intentionally kept off the main board so this is a discovery
 * surface — users explicitly visit when they want first-inning action.
 */
import React, { useCallback, useEffect, useState } from "react";
import {
  View, Text, StyleSheet, ScrollView, TouchableOpacity,
  RefreshControl,
} from "react-native";
import { SafeAreaView, useSafeAreaInsets } from "react-native-safe-area-context";
import { Stack, router as expoRouter } from "expo-router";
import { Ionicons } from "@expo/vector-icons";
import { COLORS } from "@/src/theme";
import { api } from "@/src/lib/api";
import { formatGameTime } from "@/src/lib/formatGameTime";
import { SkeletonList } from "@/src/components/Skeleton";
import { EmptyState } from "@/src/components/EmptyState";

type Feed = Awaited<ReturnType<typeof api.nrfiYrfi>>;
type Pick = Feed["picks"][number];

export default function NrfiYrfiScreen() {
  const insets = useSafeAreaInsets();
  const [feed, setFeed] = useState<Feed | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Tab filter — show only one side or both. Defaults to ALL.
  const [sideFilter, setSideFilter] = useState<"ALL" | "NRFI" | "YRFI">("ALL");

  const load = useCallback(async () => {
    setError(null);
    try {
      const res = await api.nrfiYrfi();
      setFeed(res);
    } catch (e: any) {
      setError(e?.message || "Failed to load NRFI/YRFI feed");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const visible = (feed?.picks || []).filter((p) =>
    sideFilter === "ALL" ? true : p.side === sideFilter,
  );

  return (
    <SafeAreaView style={styles.safe} edges={["top", "left", "right"]}>
      <Stack.Screen options={{ headerShown: false }} />

      {/* Header */}
      <View style={[styles.header, { paddingTop: insets.top > 0 ? 8 : 14 }]}>
        <TouchableOpacity onPress={() => expoRouter.back()} hitSlop={12}>
          <Ionicons name="chevron-back" size={26} color={COLORS.textPrimary} />
        </TouchableOpacity>
        <View style={{ flex: 1, marginLeft: 8 }}>
          <Text style={styles.title}>⚾ NRFI / YRFI</Text>
          <Text style={styles.subtitle}>
            Poisson model · pitcher × lineup × park
          </Text>
        </View>
      </View>

      {/* Side filter chips */}
      <View style={styles.chipRow}>
        {(["ALL", "NRFI", "YRFI"] as const).map((s) => (
          <TouchableOpacity
            key={s}
            style={[styles.chip, sideFilter === s && styles.chipActive]}
            onPress={() => setSideFilter(s)}
            activeOpacity={0.7}
          >
            <Text style={[styles.chipTxt, sideFilter === s && styles.chipTxtActive]}>
              {s}
            </Text>
          </TouchableOpacity>
        ))}
        <View style={{ flex: 1 }} />
        {feed && (
          <Text style={styles.countTxt}>
            {visible.length} {visible.length === 1 ? "pick" : "picks"}
          </Text>
        )}
      </View>

      <ScrollView
        contentContainerStyle={styles.content}
        refreshControl={
          <RefreshControl
            tintColor={COLORS.textPrimary}
            refreshing={refreshing}
            onRefresh={() => { setRefreshing(true); load(); }}
          />
        }
      >
        {loading ? (
          <SkeletonList count={3} testID="nrfi-skeleton" />
        ) : error ? (
          <EmptyState
            variant="error"
            title="Couldn't load NRFI/YRFI"
            message={error}
            onRetry={() => { setRefreshing(true); load(); }}
            testID="nrfi-error"
          />
        ) : visible.length === 0 ? (
          <EmptyState
            icon="baseball-outline"
            title="No first-inning picks right now"
            message="The model needs probable starters + lineup OPS to fire."
            secondaryHint="Check back closer to first pitch."
            testID="nrfi-empty"
          />
        ) : (
          visible.map((p) => <NrfiCard key={p.id} pick={p} />)
        )}
      </ScrollView>
    </SafeAreaView>
  );
}

function NrfiCard({ pick }: { pick: Pick }) {
  const isNrfi = pick.side === "NRFI";
  const sideColor = isNrfi ? COLORS.eliteLockBg || "#0ea5e9" : "#f59e0b";
  const inputs = pick.model_inputs;
  const output = pick.model_output;
  return (
    <View style={styles.card}>
      <View style={styles.cardTop}>
        <View style={[styles.sideBadge, { backgroundColor: sideColor }]}>
          <Text style={styles.sideBadgeTxt}>{pick.side}</Text>
        </View>
        <View style={{ flex: 1, marginLeft: 10 }}>
          <Text style={styles.matchTxt}>{pick.match}</Text>
          <Text style={styles.timeTxt}>{formatGameTime(pick.event_time)}</Text>
        </View>
        <View style={styles.lockPill}>
          <Text style={styles.lockNum}>{Math.round(pick.lock_score)}</Text>
          <Text style={styles.lockLbl}>LOCK</Text>
        </View>
      </View>

      <View style={styles.row}>
        <Stat label="P(WIN)" value={`${pick.win_probability.toFixed(1)}%`} />
        <Stat label="λ₁" value={output.expected_runs_1st_inning.toFixed(2)} />
        <Stat label="EDGE" value={`${pick.edge_percent > 0 ? "+" : ""}${pick.edge_percent.toFixed(1)}%`}
              valueColor={pick.edge_percent > 0 ? "#22c55e" : "#ef4444"} />
      </View>

      <View style={styles.divider} />

      <View style={styles.row}>
        <Stat small label="PITCHER" value={inputs.pitcher_factor.toFixed(2)}
              hint={inputs.pitcher_factor < 1 ? "favors NRFI" : "favors YRFI"} />
        <Stat small label="LINEUP"  value={inputs.lineup_top_factor.toFixed(2)}
              hint={inputs.lineup_top_factor < 1 ? "cold top" : "hot top"} />
        <Stat small label="PARK"    value={inputs.park_factor.toFixed(2)}
              hint={inputs.park_factor < 1 ? "pitcher" : "hitter"} />
      </View>

      {pick.key_insights?.length ? (
        <View style={styles.insightBox}>
          {pick.key_insights.slice(1, 2).map((insight, i) => (
            <Text key={i} style={styles.insightTxt}>• {insight}</Text>
          ))}
        </View>
      ) : null}
    </View>
  );
}

function Stat({
  label, value, valueColor, hint, small,
}: { label: string; value: string; valueColor?: string; hint?: string; small?: boolean }) {
  return (
    <View style={{ flex: 1 }}>
      <Text style={styles.statLbl}>{label}</Text>
      <Text style={[
        small ? styles.statValSmall : styles.statVal,
        valueColor ? { color: valueColor } : null,
      ]}>{value}</Text>
      {hint ? <Text style={styles.statHint}>{hint}</Text> : null}
    </View>
  );
}

const styles = StyleSheet.create({
  safe:    { flex: 1, backgroundColor: COLORS.bg },
  header:  { flexDirection: "row", alignItems: "center", paddingHorizontal: 16, paddingBottom: 12 },
  title:   { color: COLORS.textPrimary, fontSize: 22, fontWeight: "900", letterSpacing: 0.4 },
  subtitle:{ color: COLORS.textMuted, fontSize: 11, fontWeight: "600", marginTop: 2 },
  chipRow: { flexDirection: "row", paddingHorizontal: 16, marginBottom: 8, alignItems: "center" },
  chip:    {
    paddingHorizontal: 14, paddingVertical: 7, borderRadius: 14,
    backgroundColor: COLORS.surfaceAlt || "#1a1a24",
    marginRight: 8,
  },
  chipActive: { backgroundColor: COLORS.voltBlue || "#3b82f6" },
  chipTxt: { color: COLORS.textMuted, fontSize: 12, fontWeight: "800", letterSpacing: 0.6 },
  chipTxtActive: { color: "#fff" },
  countTxt: { color: COLORS.textMuted, fontSize: 11, fontWeight: "700" },
  content: { padding: 14, paddingBottom: 60 },
  center:  { paddingVertical: 40, alignItems: "center" },
  emptyCard: { padding: 24, alignItems: "center", backgroundColor: COLORS.surface || "#111119", borderRadius: 12 },
  emptyTitle: { color: COLORS.textPrimary, fontSize: 15, fontWeight: "800", marginTop: 8 },
  emptySub:   { color: COLORS.textMuted, fontSize: 12, marginTop: 6, textAlign: "center", paddingHorizontal: 12 },
  card: {
    backgroundColor: COLORS.surface || "#111119",
    borderRadius: 14, padding: 14, marginBottom: 10,
    borderWidth: 1, borderColor: COLORS.border || "rgba(255,255,255,0.06)",
  },
  cardTop: { flexDirection: "row", alignItems: "center", marginBottom: 12 },
  sideBadge: { paddingHorizontal: 10, paddingVertical: 5, borderRadius: 8 },
  sideBadgeTxt: { color: "#fff", fontSize: 11, fontWeight: "900", letterSpacing: 1 },
  matchTxt: { color: COLORS.textPrimary, fontSize: 14, fontWeight: "800" },
  timeTxt:  { color: COLORS.textMuted, fontSize: 11, fontWeight: "600", marginTop: 1 },
  lockPill: {
    alignItems: "center", paddingHorizontal: 10, paddingVertical: 5,
    backgroundColor: "rgba(59,130,246,0.15)", borderRadius: 8,
  },
  lockNum: { color: COLORS.voltBlue || "#3b82f6", fontSize: 16, fontWeight: "900" },
  lockLbl: { color: COLORS.textMuted, fontSize: 9, fontWeight: "800", letterSpacing: 1 },
  row: { flexDirection: "row" },
  divider: { height: 1, backgroundColor: COLORS.border || "rgba(255,255,255,0.06)", marginVertical: 10 },
  statLbl: { color: COLORS.textMuted, fontSize: 9, fontWeight: "800", letterSpacing: 1 },
  statVal: { color: COLORS.textPrimary, fontSize: 18, fontWeight: "900", marginTop: 2 },
  statValSmall: { color: COLORS.textPrimary, fontSize: 14, fontWeight: "800", marginTop: 2 },
  statHint: { color: COLORS.textMuted, fontSize: 9, fontWeight: "600", marginTop: 1, fontStyle: "italic" },
  insightBox: {
    backgroundColor: "rgba(255,255,255,0.03)",
    padding: 10, borderRadius: 8, marginTop: 8,
  },
  insightTxt: { color: COLORS.textMuted, fontSize: 11, lineHeight: 16 },
});
