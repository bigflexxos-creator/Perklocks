import React, { useCallback, useEffect, useState, useRef } from "react";
import {
  View, Text, StyleSheet, ScrollView, ActivityIndicator,
  RefreshControl, Pressable,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { Ionicons } from "@expo/vector-icons";
import { useRouter, useFocusEffect } from "expo-router";
import { COLORS, GRADE_COLORS, SPORTS } from "@/src/theme";
import { api, Pick, LineType, SortKey, PickFilters } from "@/src/lib/api";
import { LineTypeToggle } from "@/src/components/LineTypeToggle";
import { SortSelector } from "@/src/components/SortSelector";
import { ChipRow } from "@/src/components/ChipRow";
import { SportFilterBar } from "@/src/components/SportFilterBar";
import { formatGameTime } from "@/src/lib/formatGameTime";

export default function UnderOfTheDayScreen() {
  const router = useRouter();
  const [pick, setPick] = useState<Pick | null>(null);
  const [alternates, setAlternates] = useState<Pick[]>([]);
  const [pool, setPool] = useState<number>(0);
  const [lineType, setLineType] = useState<LineType>("both");
  const [sortKey, setSortKey] = useState<SortKey>("time");
  const [sport, setSport] = useState<string>("All");
  const [filters, setFilters] = useState<PickFilters>({});
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async (lt: LineType, sk: SortKey, sp: string, f: PickFilters) => {
    try {
      const res = await api.underOfTheDay(lt, sk, f, sp);
      // Defensive: hide KBO at the client even if production backend
      // hasn't deployed the sport removal yet.
      const isKbo = (p: any) => p && p.sport === "KBO";
      setPick(isKbo(res.pick) ? null : res.pick);
      setAlternates((res.alternates ?? []).filter((p: any) => !isKbo(p)));
      setPool(res.total_evaluated ?? 0);
    } catch (e) {
      console.warn("under load", e);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => { setLoading(true); load(lineType, sortKey, sport, filters); }, [lineType, sortKey, sport, filters, load]);

  // Re-fetch on every tab focus (throttled to once per 60s) so users don't
  // get stuck looking at picks from when they first opened the app hours ago.
  const lastFocusRefreshRef = useRef<number>(0);
  useFocusEffect(
    useCallback(() => {
      const now = Date.now();
      if (now - lastFocusRefreshRef.current > 60_000) {
        lastFocusRefreshRef.current = now;
        load(lineType, sortKey, sport, filters);
      }
    }, [load, lineType, sortKey, sport, filters]),
  );

  const onRefresh = () => { setRefreshing(true); load(lineType, sortKey, sport, filters); };

  return (
    <SafeAreaView style={styles.safe} edges={["top"]}>
      <View style={styles.header}>
        <View>
          <Text style={styles.brand}>UNDER OF THE DAY</Text>
          <Text style={styles.tag}>SAFEST ALT-UNDER · ALL SPORTS</Text>
        </View>
        <Ionicons name="trending-down" size={28} color={COLORS.voltBlue} />
      </View>

      <LineTypeToggle value={lineType} onChange={setLineType} testIDPrefix="under-line" />
      <SortSelector value={sortKey} onChange={setSortKey} testIDPrefix="under-sort" />
      <ChipRow
        options={SPORTS}
        active={sport}
        onChange={(s) => {
          setFilters((f) => ({ ...f, market: undefined, league: undefined }));
          setSport(s);
        }}
        testIDPrefix="under-sport-chip"
      />
      <SportFilterBar sport={sport} filters={filters} onChange={setFilters} />

      <ScrollView
        contentContainerStyle={styles.content}
        refreshControl={<RefreshControl tintColor={COLORS.textPrimary} refreshing={refreshing} onRefresh={onRefresh} />}
        showsVerticalScrollIndicator={false}
      >
        {loading ? (
          <View style={styles.center}><ActivityIndicator color={COLORS.voltBlue} /></View>
        ) : !pick ? (
          <View style={styles.center}>
            <Ionicons name="search-outline" size={48} color={COLORS.textMuted} />
            <Text style={styles.emptyTitle}>
              {lineType === "main" ? "No main-line Unders" :
               lineType === "alt" ? "No alt-Under locks" :
               "No Under locks today"}
            </Text>
            <Text style={styles.emptyMsg}>
              {lineType === "main"
                ? "Try ALT or BOTH for chalky alt-line Unders. Pull to refresh."
                : "Books don't always offer alt lines on every event. Pull to refresh."}
            </Text>
          </View>
        ) : (
          <>
            <Text style={styles.intro}>
              The single safest alt-Under bet across NBA, WNBA, MLB, KBO and Tennis.
              The line is set absurdly high — Under is the lock.
            </Text>

            {/* Hero card */}
            <Pressable onPress={() => router.push(`/pick/${pick.id}`)} style={styles.heroCard}>
              <View style={styles.heroHeader}>
                <View style={styles.heroBadge}>
                  <Ionicons name="trending-down" size={12} color={COLORS.voltBlue} />
                  <Text style={styles.heroBadgeText}>UNDER OF THE DAY</Text>
                </View>
                <Text style={styles.poolNote}>1 / {pool}</Text>
              </View>
              <Text style={styles.sportLine}>{pick.sport} · {pick.league}</Text>
              <Text style={styles.event}>{pick.event}</Text>
              {pick.event_time && (
                <Text style={styles.gameTime}>{formatGameTime(pick.event_time)}</Text>
              )}
              <Text style={styles.market}>{pick.market}</Text>
              <View style={styles.metricsRow}>
                <Metric label="🔒 LOCK" value={String(Math.round(pick.lock_score))} color={GRADE_COLORS[pick.grade]} />
                <View style={styles.metricDivider} />
                <Metric label="📊 WIN" value={`${Math.round(pick.win_probability)}%`} color={COLORS.neonGreen} />
                <View style={styles.metricDivider} />
                <Metric
                  label="⚡ EDGE"
                  value={`${pick.edge_percent > 0 ? "+" : ""}${pick.edge_percent}%`}
                  color={pick.edge_percent > 0 ? COLORS.neonGreen : COLORS.electricBlaze}
                />
                <View style={styles.metricDivider} />
                <Metric label="ODDS" value={pick.book_odds > 0 ? `+${pick.book_odds}` : `${pick.book_odds}`} />
              </View>
              <View style={styles.heroCtaRow}>
                <Text style={styles.heroCtaText}>VIEW FULL BREAKDOWN</Text>
                <Ionicons name="arrow-forward" size={14} color={COLORS.voltBlue} />
              </View>
            </Pressable>

            {alternates.length > 0 && (
              <>
                <Text style={styles.sectionLabel}>BACKUP UNDER LOCKS</Text>
                {alternates.map((alt) => (
                  <Pressable key={alt.id} onPress={() => router.push(`/pick/${alt.id}`)} style={styles.altCard}>
                    <View style={styles.altLeft}>
                      <Text style={styles.altSport}>{alt.sport}</Text>
                      <Text style={styles.altMarket}>{alt.market}</Text>
                      <Text style={styles.altEvent}>{alt.event}</Text>
                    </View>
                    <View style={styles.altRight}>
                      <Text style={styles.altOdds}>{alt.book_odds > 0 ? `+${alt.book_odds}` : alt.book_odds}</Text>
                      <Text style={styles.altWin}>{Math.round(alt.win_probability)}%</Text>
                    </View>
                  </Pressable>
                ))}
              </>
            )}

            <Text style={styles.disclaimer}>
              Alt-Unders use higher lines than standard props — book pays less because they hit
              ~85-92% of the time. High confidence, lower payout. Stake accordingly.
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
  tag: { fontSize: 10, color: COLORS.voltBlue, fontWeight: "800", letterSpacing: 1.6, marginTop: 4 },
  content: { paddingHorizontal: 20, paddingTop: 8, paddingBottom: 30 },
  center: { paddingVertical: 80, alignItems: "center" },
  emptyTitle: { color: COLORS.textPrimary, fontSize: 16, fontWeight: "800", marginTop: 14 },
  emptyMsg: { color: COLORS.textMuted, fontSize: 13, marginTop: 6, textAlign: "center", paddingHorizontal: 30 },
  intro: { color: COLORS.textSecondary, fontSize: 12, marginBottom: 14, lineHeight: 18 },

  heroCard: {
    backgroundColor: COLORS.surface, borderRadius: 16, padding: 18,
    borderColor: COLORS.voltBlue, borderWidth: 2, marginBottom: 18,
  },
  heroHeader: { flexDirection: "row", justifyContent: "space-between", alignItems: "center", marginBottom: 14 },
  heroBadge: {
    flexDirection: "row", alignItems: "center", gap: 6,
    paddingHorizontal: 10, paddingVertical: 5, borderRadius: 6, borderWidth: 1,
    backgroundColor: "rgba(56,189,248,0.12)", borderColor: COLORS.voltBlue,
  },
  heroBadgeText: { fontSize: 10, fontWeight: "900", letterSpacing: 1.4, color: COLORS.voltBlue },
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
  heroCtaRow: { flexDirection: "row", justifyContent: "flex-end", alignItems: "center", gap: 6 },
  heroCtaText: { fontSize: 11, fontWeight: "900", letterSpacing: 1.4, color: COLORS.voltBlue },

  sectionLabel: { color: COLORS.textMuted, fontSize: 11, fontWeight: "800", letterSpacing: 1.4, marginBottom: 10, marginTop: 4 },
  altCard: {
    backgroundColor: COLORS.surface, borderRadius: 12, padding: 14, marginBottom: 8,
    borderWidth: 1, borderColor: COLORS.borderDefault,
    flexDirection: "row", justifyContent: "space-between", alignItems: "center",
  },
  altLeft: { flex: 1, paddingRight: 10 },
  altRight: { alignItems: "flex-end" },
  altSport: { color: COLORS.textSecondary, fontSize: 9, fontWeight: "800", letterSpacing: 1.2, marginBottom: 4 },
  altMarket: { color: COLORS.textPrimary, fontSize: 13, fontWeight: "800", marginBottom: 2 },
  altEvent: { color: COLORS.textMuted, fontSize: 10 },
  altOdds: { color: COLORS.textPrimary, fontSize: 16, fontWeight: "900" },
  altWin: { color: COLORS.neonGreen, fontSize: 10, fontWeight: "800", marginTop: 2 },

  disclaimer: { color: COLORS.textMuted, fontSize: 11, lineHeight: 17, marginTop: 16, textAlign: "center", paddingHorizontal: 10 },
});
