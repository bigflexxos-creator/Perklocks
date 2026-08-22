import React, { useCallback, useEffect, useState } from "react";
import {
  View, Text, StyleSheet, ScrollView,
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
import { PremiumHeader } from "@/src/components/PremiumHeader";
import { PickEventRow } from "@/src/components/PickEventRow";
import { SkeletonList } from "@/src/components/Skeleton";
import { EmptyState } from "@/src/components/EmptyState";
import { formatGameTime } from "@/src/lib/formatGameTime";
import { useFocusRefetch } from "@/src/lib/useFocusRefetch";
import { swrCacheRead, swrCacheWrite } from "@/src/lib/useSWR";
import { getDisplayLockRounded } from "@/src/lib/lockScore";

const RANK_LABELS = ["TOP PICK", "OPTION #2", "OPTION #3"];
const RANK_COLORS = [COLORS.goldElite, COLORS.voltBlue, COLORS.neonGreen];

// μ-closure P3 (2026-06) — SWR cache key builder.  Warm revisits with
// the SAME filters use the previous snapshot instantly; only a
// dep-change to filters that has never been loaded triggers the
// skeleton state.
type RolloverSnapshot = {
  picks: Pick[];
  pool: number;
  survivability: any;
};
const _rolloverKey = (lt: LineType, sp: string, f: PickFilters) =>
  `rollover|${lt}|${sp}|${JSON.stringify(f || {})}`;

export default function RolloverScreen() {
  const router = useRouter();
  // Seed synchronously from SWR cache (warm revisit → no skeleton).
  const [lineType, setLineType] = useState<LineType>("both");
  const [sport, setSport] = useState<string>("All");
  const [filters, setFilters] = useState<PickFilters>({});
  const _initialKey = _rolloverKey(lineType, sport, filters);
  const _initialSnap = swrCacheRead<RolloverSnapshot>(_initialKey);
  const [picks, setPicks] = useState<Pick[]>(_initialSnap?.picks ?? []);
  const [pool, setPool] = useState<number>(_initialSnap?.pool ?? 0);
  const [survivability, setSurvivability] = useState<any>(_initialSnap?.survivability ?? null);
  const [loading, setLoading] = useState(_initialSnap === undefined);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (lt: LineType, sp: string, f: PickFilters, silent: boolean = false) => {
    try {
      setError(null);
      const res = await api.rollover(lt, f, sp);
      // Backend now returns an array of up to 3 picks.
      const arr = (res.picks && res.picks.length > 0)
        ? res.picks
        : (res.pick ? [res.pick] : []);
      const nextPicks = arr.filter((p: Pick) => p.sport !== "KBO");
      const nextPool = res.total_evaluated ?? 0;
      const nextSurv = (res as any).survivability ?? null;
      setPicks(nextPicks);
      setPool(nextPool);
      setSurvivability(nextSurv);
      // Seed the SWR cache for the next warm revisit.
      swrCacheWrite<RolloverSnapshot>(
        _rolloverKey(lt, sp, f),
        { picks: nextPicks, pool: nextPool, survivability: nextSurv },
      );
    } catch (e: any) {
      console.warn("rollover load", e);
      if (!silent) setError(String(e?.message || "Couldn't load rollover picks."));
    } finally {
      if (!silent) setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    const key = _rolloverKey(lineType, sport, filters);
    const cached = swrCacheRead<RolloverSnapshot>(key);
    if (cached) {
      // Warm — paint immediately, kick off silent background refresh.
      setPicks(cached.picks);
      setPool(cached.pool);
      setSurvivability(cached.survivability);
      setLoading(false);
      load(lineType, sport, filters, true);
    } else {
      // Cold — full skeleton state.
      setLoading(true);
      load(lineType, sport, filters, false);
    }
  }, [lineType, sport, filters, load]);

  // Smart refetch on focus — calls API again when the user returns to the
  // tab, but skips if last successful fetch was less than 30 s ago.
  // Combined with the SWR seed above, the tab paint is INSTANT on warm
  // revisits and only a silent background refresh runs.
  useFocusRefetch(
    () => load(lineType, sport, filters, true),
    [load, lineType, sport, filters],
    30_000,
  );

  const onRefresh = () => { setRefreshing(true); load(lineType, sport, filters, false); };

  return (
    <SafeAreaView style={styles.safe} edges={["top"]}>
      <PremiumHeader
        title="ROLLOVER"
        tagline="3 SAFEST BETS · BOARD-WIDE"
        right={
          <View style={{ paddingRight: 4 }}>
            <Ionicons name="flash" size={28} color={COLORS.goldElite} />
          </View>
        }
      />

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
          <SkeletonList count={3} testID="rollover-skeleton" />
        ) : error ? (
          <EmptyState
            variant="error"
            title="Couldn't load rollover picks"
            message={error}
            onRetry={() => { setLoading(true); load(lineType, sport, filters); }}
            testID="rollover-error"
          />
        ) : picks.length === 0 ? (
          <EmptyState
            icon="hourglass-outline"
            title="No qualifying picks"
            message="No Lock 90+ non-soccer picks for today's slate yet."
            secondaryHint="Pull down to refresh, or try again in a few minutes as the slate develops."
            testID="rollover-empty"
          />
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
        // μ-closure UI3 (2026-06): rank-driven visual hierarchy —
        // #1 becomes the HERO card (thicker border, gold ring, subtle
        // luminous tint) while #2/#3 sit as supporting cards.
        isTop ? styles.cardHero : styles.cardSupport,
        { borderColor: rankColor, borderWidth: isTop ? 2 : 1 },
        pressed && { transform: [{ scale: 0.99 }] },
      ]}
    >
      <View style={styles.cardHeader}>
        <View style={[
          styles.rankBadge,
          {
            backgroundColor: isTop ? `${rankColor}2E` : `${rankColor}20`,
            borderColor: rankColor,
          },
        ]}>
          <Ionicons
            name={isTop ? "trophy" : "ribbon"}
            size={isTop ? 13 : 12}
            color={rankColor}
          />
          <Text style={[
            styles.rankBadgeText,
            { color: rankColor, fontSize: isTop ? 11 : 10 },
          ]}>{rankLabel}</Text>
        </View>
        <Text style={styles.poolNote}>OF {pool}</Text>
      </View>

      <Text style={styles.sportLine}>{pick.sport} · {pick.league}</Text>
      <PickEventRow pick={pick} size="card" />
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
  // Hero (#1) — luminous navy tint, gold ambient shadow, more air.
  cardHero: {
    backgroundColor: COLORS.surfaceElevated ?? COLORS.surface,
    padding: 20,
    marginBottom: 16,
    shadowColor: COLORS.goldElite,
    shadowOpacity: 0.28,
    shadowRadius: 18,
    shadowOffset: { width: 0, height: 6 },
    elevation: 6,
  },
  // Supporting (#2/#3) — softer surface, subtle blue ambient.
  cardSupport: {
    shadowColor: COLORS.voltBlue,
    shadowOpacity: 0.10,
    shadowRadius: 10,
    shadowOffset: { width: 0, height: 3 },
    elevation: 2,
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
