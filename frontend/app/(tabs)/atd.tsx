/**
 * NFL Anytime-Touchdown (ATD) slate screen.
 *
 * 2026-06-25 · restructured to match the MLB HR presentation pattern
 * with one CRITICAL difference: the Top 5 has NO per-game
 * diversification quota (see comment on `topFive` below).  Both
 * SECTION 1 · TOP 5 and SECTION 2 · BY GAME are rendered on ONE
 * scrolling screen — no toggle.  Picks that appear in Top 5 are
 * excluded from BY GAME so the two sections never duplicate.
 *
 * Backed by /api/nfl/atd/leaderboard which returns picks already
 * ranked by td_probability across the slate.
 */
import React, { useCallback, useEffect, useMemo, useState } from "react";
import {
  RefreshControl, ScrollView, StyleSheet,
  Text, View, Pressable,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { Stack, useRouter } from "expo-router";
import { Ionicons } from "@expo/vector-icons";
import { api, type NFLAtdLeaderboardResponse, type NFLAtdPick } from "@/src/lib/api";
import { COLORS } from "@/src/theme";
import { SkeletonList } from "@/src/components/Skeleton";
import { EmptyState } from "@/src/components/EmptyState";
import { safeBack } from "@/src/utils/safeBack";

function gradeForProb(p: number): string {
  if (p >= 0.70) return "A+";
  if (p >= 0.60) return "A";
  if (p >= 0.52) return "B+";
  if (p >= 0.45) return "B";
  if (p >= 0.38) return "C+";
  return "C";
}
function gradeColor(g: string): string {
  if (g === "A+" || g === "A") return "rgba(74, 222, 128, 0.92)";
  if (g === "B+" || g === "B") return "rgba(132, 204, 22, 0.85)";
  if (g === "C+" || g === "C") return "rgba(234, 179, 8, 0.85)";
  return "rgba(239, 68, 68, 0.80)";
}
function oppRatingChip(r: string): { bg: string; fg: string; label: string } {
  if (r === "high") return { bg: "rgba(74, 222, 128, 0.18)",  fg: "#86efac", label: "HIGH OPP" };
  if (r === "med")  return { bg: "rgba(234, 179, 8, 0.18)",   fg: "#fde68a", label: "MED OPP" };
  return                  { bg: "rgba(239, 68, 68, 0.18)",   fg: "#fca5a5", label: "LOW OPP" };
}

/** Canonical game key from (team, opponent) — matches the pair regardless of home/away. */
function gameKey(pick: NFLAtdPick): string {
  const a = (pick.team || "").trim();
  const b = (pick.opponent || "").trim();
  if (!b) return a || "unknown";
  return [a, b].sort().join(" @ ");
}
function gameTitle(pick: NFLAtdPick): string {
  const t = (pick.team || "").trim();
  const o = (pick.opponent || "").trim();
  if (!o) return t || "TBD";
  return `${t} vs ${o}`;
}

function PickCard({ pick, rank, hideRank = false }: { pick: NFLAtdPick; rank: number; hideRank?: boolean }) {
  const grade = gradeForProb(pick.td_probability);
  const opp = oppRatingChip(pick.opportunity_rating);
  return (
    <View style={styles.card}>
      <View style={styles.headerRow}>
        {!hideRank && <Text style={styles.rank}>#{rank}</Text>}
        <View style={[styles.gradeChip, { backgroundColor: gradeColor(grade) }]}>
          <Text style={styles.gradeText}>{grade}</Text>
        </View>
        <View style={{ flex: 1, marginLeft: 10 }}>
          <Text style={styles.name}>
            {pick.player_name}
            {pick.is_rb_archetype ? " 🏃" : ""}
          </Text>
          <Text style={styles.sub}>
            {pick.team}{pick.opponent ? ` vs ${pick.opponent}` : ""}
          </Text>
        </View>
        <View style={{ alignItems: "flex-end" }}>
          <Text style={styles.score}>{(pick.td_probability * 100).toFixed(0)}%</Text>
          <Text style={styles.pct}>conf {(pick.confidence * 100).toFixed(0)}%</Text>
        </View>
      </View>

      <View style={styles.metaRow}>
        <View style={[styles.metaChip, { backgroundColor: opp.bg, borderColor: opp.fg + "40" }]}>
          <Text style={[styles.metaText, { color: opp.fg }]}>{opp.label}</Text>
        </View>
        <View style={styles.metaChip}>
          <Text style={styles.metaText}>
            {pick.weighted_touches_recent.toFixed(1)} touches
          </Text>
        </View>
        <View style={styles.metaChip}>
          <Text style={styles.metaText}>
            {pick.weighted_tds_recent.toFixed(1)} recent TD
          </Text>
        </View>
      </View>

      {pick.reasons && pick.reasons.length > 0 && (
        <View style={styles.bullets}>
          {pick.reasons.slice(0, 5).map((b, i) => (
            <Text key={`r-${i}`} style={styles.bullet}>• {b}</Text>
          ))}
        </View>
      )}
    </View>
  );
}

export default function NFLAtdScreen() {
  useRouter();
  const [data, setData] = useState<NFLAtdLeaderboardResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setError(null);
      // Pull a wide window (60) so BY GAME sections have material even
      // after the true Top 5 are lifted out.
      const res = await api.nflAtdLeaderboard(60, 0.30, "med");
      setData(res);
    } catch (e: any) {
      setError(e?.message || "Failed to load ATD board");
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

  const picks: NFLAtdPick[] = useMemo(() => data?.picks ?? [], [data]);

  // ── TRUE Top 5 · NO per-game diversification ──────────────────
  // MLB HR enforces "one HR pick per game" in flattenTopOfDay() so a
  // hot Yankees lineup doesn't crowd out other games. That rule does
  // NOT apply here: if two players in the same event are the two
  // strongest ATD wagers on the slate, they both belong in Top 5.
  // Rank mathematically by td_probability descending (backend already
  // sorts, but we re-sort defensively).
  const topFive = useMemo(() => {
    const sorted = [...picks].sort((a, b) => b.td_probability - a.td_probability);
    return sorted.slice(0, 5);
  }, [picks]);

  // ── BY GAME · remaining picks, grouped ────────────────────────
  // Exclude every pick already surfaced in Top 5 by player_id so the
  // two sections never duplicate. Group by canonical (team, opponent)
  // pair. Within each game keep the highest td_probability first.
  const byGame = useMemo(() => {
    const topIds = new Set(topFive.map(p => p.player_id));
    const remaining = picks.filter(p => !topIds.has(p.player_id));
    const groups = new Map<string, { title: string; picks: NFLAtdPick[] }>();
    for (const p of remaining) {
      const k = gameKey(p);
      if (!groups.has(k)) {
        groups.set(k, { title: gameTitle(p), picks: [] });
      }
      groups.get(k)!.picks.push(p);
    }
    // Sort games by their strongest remaining pick (best-first) so
    // interesting matchups float up.
    const arr = Array.from(groups.entries()).map(([key, g]) => ({
      key,
      title: g.title,
      picks: g.picks.sort((a, b) => b.td_probability - a.td_probability),
    }));
    arr.sort((a, b) => (b.picks[0]?.td_probability ?? 0) - (a.picks[0]?.td_probability ?? 0));
    return arr;
  }, [picks, topFive]);

  return (
    <SafeAreaView style={styles.safe} edges={["top"]}>
      <Stack.Screen options={{ headerShown: false }} />
      <View style={styles.header}>
        <Pressable
          onPress={() => safeBack()}
          hitSlop={12}
          style={styles.backBtn}
          testID="atd-back"
        >
          <Ionicons name="chevron-back" size={22} color={COLORS.textPrimary} />
        </Pressable>
        <Ionicons name="american-football-outline" size={20} color={COLORS.goldElite} />
        <Text style={styles.headerTitle}>NFL ANYTIME TOUCHDOWNS</Text>
        {data && (
          <Text style={styles.headerSub}>
            {topFive.length + byGame.reduce((n, g) => n + g.picks.length, 0)} picks
          </Text>
        )}
      </View>

      {loading ? (
        <View style={styles.scroll} testID="atd-skeleton">
          <SkeletonList count={4} />
        </View>
      ) : error ? (
        <ScrollView
          contentContainerStyle={styles.scroll}
          refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={COLORS.voltBlue} />}
        >
          <EmptyState
            variant="error"
            title="Couldn't load ATD board"
            message={error}
            onRetry={onRefresh}
            secondaryHint="Pull down to retry manually."
            testID="atd-error"
          />
        </ScrollView>
      ) : picks.length === 0 ? (
        <ScrollView
          contentContainerStyle={styles.scroll}
          refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={COLORS.voltBlue} />}
        >
          <EmptyState
            icon="american-football-outline"
            title="No NFL ATD picks yet"
            message="Slate is built once probable usage data lands."
            secondaryHint="Check back closer to gameday."
            testID="atd-empty"
          />
        </ScrollView>
      ) : (
        <ScrollView
          style={{ flex: 1 }}
          contentContainerStyle={styles.scroll}
          refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={COLORS.voltBlue} />}
        >
          {/* ── SECTION 1 · TOP 5 ANYTIME TDS ─────────────────── */}
          <Text style={styles.sectionTitle}>🔥 TOP 5 TODAY</Text>
          <Text style={styles.intro}>
            True five highest-ranked ATD wagers across the slate — no per-game quota.
          </Text>
          {topFive.map((p, i) => (
            <PickCard key={`top-${p.player_id}-${i}`} pick={p} rank={i + 1} />
          ))}

          {/* ── SECTION 2 · BY GAME ────────────────────────────── */}
          {byGame.length > 0 && (
            <>
              <View style={{ height: 12 }} />
              <Text style={styles.sectionTitle}>🏈 BY GAME</Text>
              <Text style={styles.intro}>
                Remaining ATD candidates grouped by matchup, best pick first.
              </Text>
              {byGame.map((g) => (
                <View key={g.key} style={styles.gameGroup}>
                  <Text style={styles.gameGroupTitle}>{g.title}</Text>
                  {g.picks.map((p, i) => (
                    <PickCard key={`bg-${p.player_id}-${i}`} pick={p} rank={i + 1} hideRank />
                  ))}
                </View>
              ))}
            </>
          )}
          <View style={{ height: 32 }} />
        </ScrollView>
      )}
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: COLORS.deepBlack },
  header: {
    flexDirection: "row", alignItems: "center", paddingHorizontal: 14,
    paddingVertical: 12, borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: "#1e293b",
  },
  backBtn: { marginRight: 8, padding: 4 },
  headerTitle: {
    color: COLORS.textPrimary, fontSize: 17, fontWeight: "800",
    marginLeft: 8, letterSpacing: 0.5, flex: 1,
  },
  headerSub: { color: COLORS.textMuted, fontSize: 12 },
  scroll: { padding: 12, paddingBottom: 24 },
  sectionTitle: {
    color: COLORS.textPrimary, fontSize: 14, fontWeight: "800",
    letterSpacing: 1.0, marginTop: 8, marginBottom: 4,
  },
  intro: { color: COLORS.textMuted, fontSize: 12, marginBottom: 8 },
  gameGroup: { marginTop: 10 },
  gameGroupTitle: {
    color: COLORS.textPrimary, fontSize: 13, fontWeight: "700",
    letterSpacing: 0.4, marginBottom: 6, marginTop: 6,
  },
  card: {
    backgroundColor: "rgba(15,23,42,0.65)",
    borderRadius: 14, padding: 12,
    marginBottom: 10, borderWidth: 1, borderColor: "#1e293b",
  },
  headerRow: { flexDirection: "row", alignItems: "center" },
  rank: {
    color: COLORS.textPrimary, fontSize: 15, fontWeight: "800",
    width: 34,
  },
  gradeChip: {
    paddingHorizontal: 8, paddingVertical: 3, borderRadius: 8,
    minWidth: 32, alignItems: "center",
  },
  gradeText: { color: "#0f172a", fontSize: 13, fontWeight: "800" },
  name: { color: COLORS.textPrimary, fontSize: 15, fontWeight: "700" },
  sub: { color: COLORS.textMuted, fontSize: 12 },
  score: { color: COLORS.goldElite, fontSize: 20, fontWeight: "800" },
  pct: { color: COLORS.textMuted, fontSize: 11 },
  metaRow: { flexDirection: "row", flexWrap: "wrap", marginTop: 8 },
  metaChip: {
    paddingHorizontal: 8, paddingVertical: 3, borderRadius: 6,
    backgroundColor: "rgba(30,41,59,0.85)",
    borderWidth: StyleSheet.hairlineWidth, borderColor: "#334155",
    marginRight: 6, marginBottom: 6,
  },
  metaText: { color: COLORS.textPrimary, fontSize: 11, fontWeight: "600" },
  bullets: { marginTop: 8 },
  bullet: { color: COLORS.textMuted, fontSize: 12, marginBottom: 3 },
  toggleRow: {},
  toggleBtn: {},
  toggleBtnActive: {},
  toggleText: {},
  toggleTextActive: {},
});
