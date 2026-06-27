import React, { useCallback, useEffect, useState } from "react";
import {
  View, Text, StyleSheet, ScrollView, ActivityIndicator,
  RefreshControl, Pressable,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { Ionicons } from "@expo/vector-icons";
import { useRouter } from "expo-router";
import { COLORS, GRADE_COLORS, SPORTS } from "@/src/theme";
import { api, Pick, LineType, PickFilters } from "@/src/lib/api";
import { LineTypeToggle } from "@/src/components/LineTypeToggle";
import { ChipRow } from "@/src/components/ChipRow";
import { SportFilterBar } from "@/src/components/SportFilterBar";
import { formatGameTime } from "@/src/lib/formatGameTime";
import { useFocusRefetch } from "@/src/lib/useFocusRefetch";
import { getDisplayLockRounded } from "@/src/lib/lockScore";

const RANK_LABELS = ["TOP PICK", "OPTION #2", "OPTION #3"];
const RANK_COLORS = [COLORS.goldElite, COLORS.voltBlue, COLORS.neonGreen];

export default function RolloverScreen() {
  const router = useRouter();
  const [picks, setPicks] = useState<Pick[]>([]);
  const [pool, setPool] = useState<number>(0);
  const [survivability, setSurvivability] = useState<any>(null);
  const [lineType, setLineType] = useState<LineType>("both");
  const [sport, setSport] = useState<string>("All");
  const [filters, setFilters] = useState<PickFilters>({});
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async (lt: LineType, sp: string, f: PickFilters) => {
    try {
      const res = await api.rollover(lt, f, sp);
      // Backend now returns an array of up to 3 picks.
      const arr = (res.picks && res.picks.length > 0)
        ? res.picks
        : (res.pick ? [res.pick] : []);
      setPicks(arr.filter((p: Pick) => p.sport !== "KBO"));
      setPool(res.total_evaluated ?? 0);
      setSurvivability((res as any).survivability ?? null);
    } catch (e) {
      console.warn("rollover load", e);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => { setLoading(true); load(lineType, sport, filters); }, [lineType, sport, filters, load]);

  // Smart refetch on focus — calls API again when the user returns to the
  // tab, but skips if last successful fetch was less than 30 s ago.
  useFocusRefetch(
    () => load(lineType, sport, filters),
    [load, lineType, sport, filters],
    30_000,
  );

  const onRefresh = () => { setRefreshing(true); load(lineType, sport, filters); };

  return (
    <SafeAreaView style={styles.safe} edges={["top"]}>
      <View style={styles.header}>
        <View>
          <Text style={styles.brand}>ROLLOVER</Text>
          <Text style={styles.tag}>3 SAFEST BETS · BOARD-WIDE</Text>
        </View>
        <Ionicons name="flash" size={28} color={COLORS.goldElite} />
      </View>

      <LineTypeToggle value={lineType} onChange={setLineType} testIDPrefix="rollover-line" />
      <ChipRow
        options={SPORTS}
        active={sport}
        onChange={(s) => {
          setFilters((f) => ({ ...f, market: undefined, league: undefined }));
          setSport(s);
        }}
        testIDPrefix="rollover-sport-chip"
      />
      <SportFilterBar sport={sport} filters={filters} onChange={setFilters} />

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
        ) : picks.length === 0 ? (
          <View style={styles.center}>
            <Ionicons name="hourglass-outline" size={48} color={COLORS.textMuted} />
            <Text style={styles.emptyTitle}>No qualifying picks</Text>
            <Text style={styles.emptyMsg}>
              No Lock 90+ non-soccer picks for today&apos;s slate yet. Pull to refresh.
            </Text>
          </View>
        ) : (
          <>
            <Text style={styles.intro}>
              The three highest-probability bets on the board, ranked. Soccer excluded.
              Each is scoped to today&apos;s slate only.
            </Text>

            {survivability && (
              <View style={styles.survBadge}>
                <Ionicons name="shield-checkmark" size={14} color={COLORS.neonGreen} />
                <Text style={styles.survText}>
                  Survivability V2 — odds ≥ {survivability.odds_floor}, edge ≥ {survivability.edge_floor}%, +{survivability.ev_cushion_pts}pt EV cushion
                  {survivability.rejected_chalk > 0 ? ` · ${survivability.rejected_chalk} chalk picks rejected` : ""}
                </Text>
              </View>
            )}

            {picks.map((p, idx) => (
              <RolloverCard
                key={p.id}
                pick={p}
                rank={idx}
                pool={pool}
                onPress={() => router.push(`/pick/${p.id}`)}
              />
            ))}

            <Text style={styles.disclaimer}>
              No bet is guaranteed. These rank by win probability + lock score with
              soccer excluded and player props favored for lower variance.
            </Text>
          </>
        )}
      </ScrollView>
    </SafeAreaView>
  );
}

function RolloverCard({ pick, rank, pool, onPress }: { pick: Pick; rank: number; pool: number; onPress: () => void }) {
  const gradeColor = GRADE_COLORS[pick.grade];
  const rankColor = RANK_COLORS[rank] ?? COLORS.textMuted;
  const rankLabel = RANK_LABELS[rank] ?? `OPTION #${rank + 1}`;
  const isTop = rank === 0;
  return (
    <Pressable
      onPress={onPress}
      testID={`rollover-card-${rank}`}
      style={({ pressed }) => [
        styles.card,
        { borderColor: rankColor, borderWidth: isTop ? 2 : 1 },
        pressed && { transform: [{ scale: 0.99 }] },
      ]}
    >
      <View style={styles.cardHeader}>
        <View style={[styles.rankBadge, { backgroundColor: `${rankColor}20`, borderColor: rankColor }]}>
          <Ionicons
            name={isTop ? "trophy" : "ribbon"}
            size={12}
            color={rankColor}
          />
          <Text style={[styles.rankBadgeText, { color: rankColor }]}>{rankLabel}</Text>
        </View>
        <Text style={styles.poolNote}>OF {pool}</Text>
      </View>

      <Text style={styles.sportLine}>{pick.sport} · {pick.league}</Text>
      <Text style={styles.event}>{pick.event}</Text>
      {pick.event_time && (
        <Text style={styles.gameTime}>{formatGameTime(pick.event_time)}</Text>
      )}
      <Text style={styles.market}>{pick.market}</Text>

      <View style={styles.metricsRow}>
        <Metric label="🔒 LOCK" value={String(getDisplayLockRounded(pick))} color={gradeColor} />
        <View style={styles.metricDivider} />
        <Metric label="📊 WIN" value={`${Math.round(pick.win_probability)}%`} color={COLORS.neonGreen} />
        <View style={styles.metricDivider} />
        <Metric
          label="⚡ EDGE"
          value={`${pick.edge_percent > 0 ? "+" : ""}${pick.edge_percent}%`}
          color={pick.edge_percent > 0 ? COLORS.neonGreen : COLORS.electricBlaze}
        />
        <View style={styles.metricDivider} />
        <Metric
          label="ODDS"
          value={pick.book_odds > 0 ? `+${pick.book_odds}` : `${pick.book_odds}`}
        />
      </View>

      <View style={styles.cardCtaRow}>
        <Text style={[styles.cardCtaText, { color: rankColor }]}>
          VIEW BREAKDOWN
        </Text>
        <Ionicons name="arrow-forward" size={14} color={rankColor} />
      </View>
    </Pressable>
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
  safe: { flex: 1, backgroundColor: "transparent" },
  header: {
    paddingHorizontal: 20, paddingTop: 8, paddingBottom: 14,
    flexDirection: "row", justifyContent: "space-between", alignItems: "flex-end",
  },
  brand: { fontSize: 22, fontWeight: "900", color: COLORS.textPrimary, letterSpacing: 3 },
  tag: { fontSize: 10, color: COLORS.goldElite, fontWeight: "800", letterSpacing: 1.8, marginTop: 4 },
  content: { paddingHorizontal: 20, paddingTop: 8, paddingBottom: 30 },
  center: { paddingVertical: 80, alignItems: "center" },
  emptyTitle: { color: COLORS.textPrimary, fontSize: 16, fontWeight: "800", marginTop: 14 },
  emptyMsg: { color: COLORS.textMuted, fontSize: 13, marginTop: 6, textAlign: "center", paddingHorizontal: 30 },
  intro: { color: COLORS.textSecondary, fontSize: 12, marginBottom: 14, lineHeight: 18 },

  card: {
    backgroundColor: COLORS.surface,
    borderRadius: 16, padding: 18,
    marginBottom: 14,
  },
  cardHeader: {
    flexDirection: "row", justifyContent: "space-between", alignItems: "center",
    marginBottom: 14,
  },
  rankBadge: {
    flexDirection: "row", alignItems: "center", gap: 6,
    paddingHorizontal: 10, paddingVertical: 5, borderRadius: 6, borderWidth: 1,
  },
  rankBadgeText: { fontSize: 10, fontWeight: "900", letterSpacing: 1.4 },
  poolNote: { color: COLORS.textMuted, fontSize: 10, fontWeight: "800", letterSpacing: 1.2 },

  sportLine: { color: COLORS.textSecondary, fontSize: 11, fontWeight: "700", letterSpacing: 1, marginBottom: 4 },
  event: { color: COLORS.textPrimary, fontSize: 14, fontWeight: "700", marginBottom: 4 },
  gameTime: { color: COLORS.voltBlue, fontSize: 11, fontWeight: "700", marginBottom: 10 },
  market: { color: COLORS.textPrimary, fontSize: 18, fontWeight: "900", letterSpacing: -0.3, marginBottom: 16 },

  metricsRow: {
    flexDirection: "row", alignItems: "center",
    backgroundColor: COLORS.bg, borderRadius: 10, padding: 12, marginBottom: 12,
  },
  metricDivider: { width: 1, alignSelf: "stretch", backgroundColor: COLORS.borderDefault, marginHorizontal: 4 },
  metricCell: { flex: 1, alignItems: "center" },
  metricLabel: { color: COLORS.textMuted, fontSize: 9, fontWeight: "800", letterSpacing: 1.2, marginBottom: 4 },
  metricValue: { fontSize: 18, fontWeight: "900", letterSpacing: -0.3 },

  cardCtaRow: {
    flexDirection: "row", justifyContent: "flex-end", alignItems: "center", gap: 6,
  },
  cardCtaText: { fontSize: 11, fontWeight: "900", letterSpacing: 1.4 },

  disclaimer: {
    color: COLORS.textMuted, fontSize: 11, lineHeight: 17, marginTop: 8,
    textAlign: "center", paddingHorizontal: 10,
  },
  survBadge: {
    flexDirection: "row", alignItems: "center", gap: 6,
    backgroundColor: COLORS.surface, borderRadius: 8,
    paddingHorizontal: 10, paddingVertical: 8,
    marginBottom: 12,
    borderWidth: 1, borderColor: COLORS.neonGreen,
  },
  survText: {
    color: COLORS.textSecondary, fontSize: 11, fontWeight: "600",
    flex: 1, letterSpacing: 0.2,
  },
});
