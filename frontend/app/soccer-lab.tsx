/**
 * Soccer Lab — Global confidence-ranked soccer feed.
 *
 * Pulls every active soccer_* league dynamically from The Odds API
 * (`/api/soccer-lab/leagues`) and shows the top setups across ALL leagues
 * ranked by confidence (implied probability 1/decimal_odds * 100).
 *
 * Accessed via the "🌍 SOCCER LAB" button on the home screen sport filter.
 */
import React, { useCallback, useEffect, useState } from "react";
import {
  View, Text, StyleSheet, ScrollView, TouchableOpacity, ActivityIndicator,
  RefreshControl, FlatList,
} from "react-native";
import { SafeAreaView, useSafeAreaInsets } from "react-native-safe-area-context";
import { Stack, router as expoRouter, useLocalSearchParams } from "expo-router";
import { Ionicons } from "@expo/vector-icons";
import { COLORS } from "@/src/theme";
import { api } from "@/src/lib/api";
import { formatGameTime } from "@/src/lib/formatGameTime";
import { getDisplayLock } from "@/src/lib/lockScore";

type Feed = Awaited<ReturnType<typeof api.soccerLabFeed>>;
type Pick = Feed["picks"][number];

const SPORT_LABEL: Record<string, { title: string; icon: string }> = {
  Soccer: { title: "SOCCER LAB",  icon: "🌍" },
  MLB:    { title: "MLB LAB",     icon: "⚾" },
  Tennis: { title: "TENNIS LAB",  icon: "🎾" },
  UFC:    { title: "UFC LAB",     icon: "🥊" },
  NBA:    { title: "NBA LAB",     icon: "🏀" },
  NFL:    { title: "NFL LAB",     icon: "🏈" },
};

export default function SoccerLabScreen() {
  const insets = useSafeAreaInsets();
  const params = useLocalSearchParams<{ sport?: string }>();
  const sport = (params.sport as string) || "Soccer";
  const meta = SPORT_LABEL[sport] || { title: `${sport.toUpperCase()} LAB`, icon: "🏆" };
  const isSoccer = sport === "Soccer";
  const [feed, setFeed] = useState<Feed | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [leagueFilter, setLeagueFilter] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [leaguesMeta, setLeaguesMeta] = useState<{ count: number; age_sec: number } | null>(null);
  // Lock-score floor chip (user-facing min-lock filter for the lab feed).
  // The backend already filters at 78; this is a CLIENT-SIDE refinement
  // so users can quickly narrow to 95+ / 99+ without a separate API call.
  const [lockFloor, setLockFloor] = useState<number>(78);

  const load = useCallback(async () => {
    setError(null);
    try {
      // soccerLabFeed is the generic endpoint — supports any sport
      const feedRes = await api.soccerLabFeed(100, 78, sport);
      setFeed(feedRes);
      if (isSoccer) {
        try {
          const leaguesRes = await api.soccerLabLeagues();
          setLeaguesMeta({ count: leaguesRes.count, age_sec: leaguesRes.age_sec });
        } catch {}
      } else {
        // Use league_distribution count from feed for non-soccer sports
        setLeaguesMeta({ count: feedRes.league_distribution.length, age_sec: 0 });
      }
    } catch (e: any) {
      setError(e?.message || "Failed to load Lab");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [sport, isSoccer]);

  useEffect(() => {
    load();
  }, [load]);

  const onRefresh = useCallback(() => {
    setRefreshing(true);
    load();
  }, [load]);

  const picks = feed?.picks ?? [];
  // Apply league + lock-floor filters client-side.
  const filtered = picks.filter((p) => {
    if (leagueFilter && (p.league || "").toLowerCase() !== leagueFilter.toLowerCase()) {
      return false;
    }
    // Use displayed/governed lock if available — same canonical value
    // the cards render.
    const lock = getDisplayLock(p as any);
    if (typeof lock === "number" && lock < lockFloor) return false;
    return true;
  });

  return (
    <SafeAreaView style={styles.root} edges={["top", "left", "right"]}>
      <Stack.Screen options={{ headerShown: false }} />

      {/* Header */}
      <View style={styles.header}>
        <TouchableOpacity onPress={() => expoRouter.back()} style={styles.backBtn} hitSlop={12}>
          <Ionicons name="chevron-back" size={22} color={COLORS.textPrimary} />
        </TouchableOpacity>
        <View style={{ flex: 1, minWidth: 0 }}>
          <Text style={styles.headerTitle} numberOfLines={1} adjustsFontSizeToFit minimumFontScale={0.75}>
            {meta.icon} {meta.title}
          </Text>
          <Text style={styles.headerSub} numberOfLines={1}>
            {leaguesMeta && typeof leaguesMeta.count === "number"
              ? `${leaguesMeta.count} ${isSoccer ? "active leagues · auto-discovered" : "league" + (leaguesMeta.count === 1 ? "" : "s") + " · ranked by confidence"}`
              : "Loading…"}
          </Text>
        </View>
        <TouchableOpacity onPress={onRefresh} style={styles.refreshBtn} hitSlop={12}>
          <Ionicons name="refresh" size={20} color={COLORS.voltBlue} />
        </TouchableOpacity>
      </View>

      {/* Lock-score floor chip strip — quick filter to 90+/95+/99+.
          Mirrors the home-tab filter sheet so users can narrow the Lab
          to elite-only picks with one tap. */}
      <ScrollView
        horizontal
        showsHorizontalScrollIndicator={false}
        contentContainerStyle={styles.lockChipRow}
      >
        {[78, 85, 90, 92, 95, 97, 99].map((v) => (
          <TouchableOpacity
            key={v}
            onPress={() => setLockFloor(v)}
            style={[
              styles.lockChip,
              lockFloor === v && styles.lockChipActive,
            ]}
            hitSlop={6}
            testID={`lab-lock-floor-${v}`}
          >
            <Text style={[
              styles.lockChipText,
              lockFloor === v && styles.lockChipTextActive,
            ]}>
              {v === 78 ? "ALL" : `${v}+`}
            </Text>
          </TouchableOpacity>
        ))}
      </ScrollView>

      {/* League chips */}
      {feed?.league_distribution && feed.league_distribution.length > 1 && (
        <ScrollView
          horizontal
          showsHorizontalScrollIndicator={false}
          contentContainerStyle={styles.chipRow}
        >
          <Chip
            label="ALL"
            count={picks.length}
            active={leagueFilter == null}
            onPress={() => setLeagueFilter(null)}
          />
          {feed.league_distribution.slice(0, 12).map((lg) => (
            <Chip
              key={lg.league}
              label={lg.league}
              count={lg.count}
              active={leagueFilter === lg.league}
              onPress={() =>
                setLeagueFilter(leagueFilter === lg.league ? null : lg.league)
              }
            />
          ))}
        </ScrollView>
      )}

      {/* Body */}
      {loading ? (
        <View style={styles.loadingWrap}>
          <ActivityIndicator size="large" color={COLORS.voltBlue} />
          <Text style={styles.mutedText}>Scanning every active soccer league…</Text>
        </View>
      ) : error ? (
        <View style={styles.errorWrap}>
          <Text style={styles.errorText}>{error}</Text>
          <TouchableOpacity onPress={onRefresh} style={styles.retryBtn}>
            <Text style={styles.retryText}>RETRY</Text>
          </TouchableOpacity>
        </View>
      ) : filtered.length === 0 ? (
        <View style={styles.emptyWrap}>
          <Text style={styles.emptyTitle}>No pregame soccer setups</Text>
          <Text style={styles.emptySub}>
            All games are either started or below the lock-score gate.
            Check back closer to kickoff.
          </Text>
        </View>
      ) : (
        <FlatList
          data={filtered}
          keyExtractor={(p) => p.id}
          renderItem={({ item, index }) => <PickRow pick={item} rank={index + 1} />}
          contentContainerStyle={{ paddingBottom: insets.bottom + 24 }}
          refreshControl={
            <RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={COLORS.voltBlue} />
          }
          ItemSeparatorComponent={() => <View style={{ height: 10 }} />}
          ListHeaderComponent={
            <View style={styles.subHeader}>
              <Text style={styles.subHeaderText}>
                {filtered.length} setups ranked by confidence (1 / odds × 100)
              </Text>
            </View>
          }
        />
      )}
    </SafeAreaView>
  );
}

function Chip({
  label, count, active, onPress,
}: {
  label: string;
  count: number;
  active: boolean;
  onPress: () => void;
}) {
  return (
    <TouchableOpacity
      onPress={onPress}
      style={[styles.chip, active && styles.chipActive]}
      activeOpacity={0.7}
    >
      <Text style={[styles.chipText, active && styles.chipTextActive]} numberOfLines={1}>
        {label}
      </Text>
      <Text style={[styles.chipCount, active && styles.chipCountActive]}>{count}</Text>
    </TouchableOpacity>
  );
}

function PickRow({ pick, rank }: { pick: Pick; rank: number }) {
  const conf = Math.round(pick.confidence || 0);
  const lock = Math.round(getDisplayLock(pick as any));
  const tint = conf >= 70 ? COLORS.neonGreen : conf >= 55 ? COLORS.voltBlue : COLORS.textSecondary;
  return (
    <TouchableOpacity
      style={styles.row}
      activeOpacity={0.75}
      onPress={() => expoRouter.push(`/pick/${pick.id}` as any)}
    >
      <View style={styles.rowLeft}>
        <Text style={styles.rank}>{rank}</Text>
      </View>
      <View style={{ flex: 1 }}>
        <View style={styles.rowTop}>
          <Text style={styles.league} numberOfLines={1}>
            {pick.league}
          </Text>
          {pick.tier_v2 && (
            <View style={[styles.tierChip, { borderColor: tint + "55" }]}>
              <Text style={[styles.tierChipText, { color: tint }]} numberOfLines={1}>
                {pick.tier_v2}
              </Text>
            </View>
          )}
        </View>
        <Text style={styles.event} numberOfLines={1}>{pick.event}</Text>
        <Text style={styles.market} numberOfLines={1}>{pick.market}</Text>
        <View style={styles.metaRow}>
          <Text style={styles.metaText}>
            {pick.book_odds != null
              ? (pick.book_odds > 0 ? `+${pick.book_odds}` : `${pick.book_odds}`)
              : "—"}
          </Text>
          <Text style={styles.metaDot}>·</Text>
          <Text style={styles.metaText}>{formatGameTime(pick.event_time)}</Text>
        </View>
      </View>
      <View style={styles.rowRight}>
        <Text style={[styles.confValue, { color: tint }]}>{conf}</Text>
        <Text style={styles.confUnit}>CONF</Text>
        <Text style={styles.lockMini}>L {lock}</Text>
      </View>
    </TouchableOpacity>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: COLORS.bg },
  header: {
    flexDirection: "row",
    alignItems: "center",
    paddingHorizontal: 16,
    paddingTop: 14,
    paddingBottom: 14,
    gap: 12,
    borderBottomWidth: 1,
    borderBottomColor: COLORS.borderDefault,
  },
  backBtn: {
    width: 36,
    height: 36,
    borderRadius: 18,
    alignItems: "center",
    justifyContent: "center",
    backgroundColor: COLORS.surface,
    borderWidth: 1,
    borderColor: COLORS.borderDefault,
  },
  refreshBtn: {
    width: 36,
    height: 36,
    borderRadius: 18,
    alignItems: "center",
    justifyContent: "center",
    backgroundColor: COLORS.surface,
    borderWidth: 1,
    borderColor: COLORS.voltBlue + "55",
  },
  // ── Lock-score floor chip strip (sub-header) ──
  lockChipRow: {
    paddingHorizontal: 16,
    paddingTop: 10,
    paddingBottom: 6,
    gap: 6,
  },
  lockChip: {
    paddingVertical: 6,
    paddingHorizontal: 14,
    borderRadius: 16,
    borderWidth: 1,
    borderColor: COLORS.borderDefault,
    backgroundColor: "rgba(255,255,255,0.03)",
    minHeight: 32,
    justifyContent: "center",
    marginRight: 6,
  },
  lockChipActive: {
    borderColor: COLORS.goldElite,
    backgroundColor: "rgba(255,215,0,0.18)",
  },
  lockChipText: {
    color: COLORS.textSecondary,
    fontSize: 11,
    fontWeight: "800",
    letterSpacing: 0.6,
  },
  lockChipTextActive: {
    color: COLORS.goldElite,
  },
  headerTitle: {
    color: COLORS.textPrimary,
    fontSize: 18,
    fontWeight: "900",
    letterSpacing: -0.3,
    flexShrink: 1,
  },
  headerSub: {
    color: COLORS.textMuted,
    fontSize: 11.5,
    fontWeight: "700",
    letterSpacing: 0.4,
    marginTop: 3,
  },

  chipRow: {
    paddingHorizontal: 16,
    paddingVertical: 12,
    gap: 8,
  },
  chip: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    paddingHorizontal: 10,
    paddingVertical: 6,
    borderRadius: 16,
    borderWidth: 1,
    borderColor: COLORS.borderDefault,
    backgroundColor: COLORS.surface,
    maxWidth: 160,
  },
  chipActive: {
    backgroundColor: COLORS.neonGreen,
    borderColor: COLORS.neonGreen,
  },
  chipText: {
    color: COLORS.textSecondary,
    fontSize: 11,
    fontWeight: "800",
    letterSpacing: 0.4,
  },
  chipTextActive: { color: COLORS.bg },
  chipCount: {
    color: COLORS.textMuted,
    fontSize: 10,
    fontWeight: "800",
    fontVariant: ["tabular-nums"],
  },
  chipCountActive: { color: COLORS.bg, opacity: 0.7 },

  subHeader: {
    paddingHorizontal: 16,
    paddingBottom: 8,
  },
  subHeaderText: {
    color: COLORS.textMuted,
    fontSize: 10.5,
    fontWeight: "700",
    letterSpacing: 0.6,
  },

  row: {
    flexDirection: "row",
    gap: 10,
    padding: 12,
    marginHorizontal: 16,
    borderRadius: 12,
    backgroundColor: COLORS.surface,
    borderWidth: 1,
    borderColor: COLORS.borderDefault,
  },
  rowLeft: { width: 22, alignItems: "center", paddingTop: 4 },
  rank: {
    color: COLORS.textMuted,
    fontSize: 12,
    fontWeight: "900",
    fontVariant: ["tabular-nums"],
  },
  rowTop: { flexDirection: "row", alignItems: "center", gap: 8, marginBottom: 2 },
  league: {
    color: COLORS.textMuted,
    fontSize: 10,
    fontWeight: "800",
    letterSpacing: 0.8,
    flex: 1,
  },
  tierChip: {
    paddingHorizontal: 5,
    paddingVertical: 1,
    borderRadius: 3,
    borderWidth: 1,
  },
  tierChipText: {
    fontSize: 8.5,
    fontWeight: "900",
    letterSpacing: 0.6,
  },
  event: {
    color: COLORS.textPrimary,
    fontSize: 13,
    fontWeight: "800",
    letterSpacing: -0.2,
    marginTop: 1,
  },
  market: {
    color: COLORS.textSecondary,
    fontSize: 12,
    fontWeight: "600",
    marginTop: 3,
  },
  metaRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    marginTop: 5,
  },
  metaText: {
    color: COLORS.textMuted,
    fontSize: 10.5,
    fontWeight: "700",
    fontVariant: ["tabular-nums"],
  },
  metaDot: { color: COLORS.textMuted, fontSize: 10 },

  rowRight: {
    alignItems: "flex-end",
    justifyContent: "center",
    paddingLeft: 6,
  },
  confValue: {
    fontSize: 24,
    fontWeight: "900",
    letterSpacing: -0.5,
    fontVariant: ["tabular-nums"],
  },
  confUnit: {
    color: COLORS.textMuted,
    fontSize: 8,
    fontWeight: "800",
    letterSpacing: 0.8,
    marginTop: -2,
  },
  lockMini: {
    color: COLORS.textMuted,
    fontSize: 9.5,
    fontWeight: "700",
    fontVariant: ["tabular-nums"],
    marginTop: 6,
  },

  loadingWrap: {
    flex: 1,
    alignItems: "center",
    justifyContent: "center",
    gap: 16,
    paddingHorizontal: 24,
  },
  mutedText: {
    color: COLORS.textMuted,
    fontSize: 12,
    fontWeight: "600",
    textAlign: "center",
  },
  errorWrap: {
    flex: 1,
    alignItems: "center",
    justifyContent: "center",
    gap: 14,
    paddingHorizontal: 24,
  },
  errorText: {
    color: COLORS.electricBlaze,
    fontSize: 13,
    fontWeight: "700",
    textAlign: "center",
  },
  retryBtn: {
    paddingHorizontal: 16,
    paddingVertical: 8,
    borderRadius: 6,
    borderWidth: 1,
    borderColor: COLORS.voltBlue + "55",
    backgroundColor: COLORS.voltBlue + "10",
  },
  retryText: {
    color: COLORS.voltBlue,
    fontSize: 11,
    fontWeight: "900",
    letterSpacing: 1.0,
  },
  emptyWrap: {
    flex: 1,
    alignItems: "center",
    justifyContent: "center",
    gap: 10,
    paddingHorizontal: 28,
  },
  emptyTitle: {
    color: COLORS.textPrimary,
    fontSize: 15,
    fontWeight: "800",
  },
  emptySub: {
    color: COLORS.textSecondary,
    fontSize: 12,
    textAlign: "center",
    lineHeight: 18,
  },
});
