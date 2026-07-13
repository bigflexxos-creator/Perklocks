/**
 * NFL Anytime-Touchdown (ATD) slate screen.
 *
 * Mirrors the MLB HR tab UX (per user request 2026-06-30: "I want to do
 * same thing with nfl for atd"). Backed by /api/nfl/atd/leaderboard,
 * which returns picks already ranked by td_probability across the slate.
 *
 * Modes:
 *  • "🔥 Top 5 Today" (default) — top 5 picks of the day with full
 *    rationale bullets and opportunity rating.
 *  • "📋 Full Board" — extended top-25 view for power users.
 *
 * Auto-refreshes on pull-down.
 */
import React, { useCallback, useEffect, useState } from "react";
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

type ViewMode = "top5" | "full";

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

function PickCard({ pick, rank }: { pick: NFLAtdPick; rank: number }) {
  const grade = gradeForProb(pick.td_probability);
  const opp = oppRatingChip(pick.opportunity_rating);
  return (
    <View style={styles.card}>
      <View style={styles.headerRow}>
        <Text style={styles.rank}>#{rank}</Text>
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
            {pick.weighted_touches_recent.toFixed(1)} touch/g
          </Text>
        </View>
        {pick.weighted_tds_recent > 0 && (
          <View style={styles.metaChip}>
            <Text style={styles.metaText}>
              {pick.weighted_tds_recent.toFixed(2)} TD/g
            </Text>
          </View>
        )}
        {pick.sample_games > 0 && (
          <View style={styles.metaChip}>
            <Text style={styles.metaText}>n={pick.sample_games}g</Text>
          </View>
        )}
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
  const router = useRouter();
  const [data, setData] = useState<NFLAtdLeaderboardResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<ViewMode>("top5");

  const load = useCallback(async () => {
    try {
      setError(null);
      // Pull a wide window (25), filter for display below.
      const res = await api.nflAtdLeaderboard(25, 0.30, "med");
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

  const picks = data?.picks ?? [];
  const displayed = mode === "top5" ? picks.slice(0, 5) : picks;

  return (
    <SafeAreaView style={styles.safe} edges={["top"]}>
      <Stack.Screen options={{ headerShown: false }} />
      <View style={styles.header}>
        <Pressable
          onPress={() => router.back()}
          hitSlop={12}
          style={styles.backBtn}
          testID="atd-back"
        >
          <Ionicons name="chevron-back" size={22} color={COLORS.textPrimary} />
        </Pressable>
        <Ionicons name="american-football-outline" size={20} color={COLORS.goldElite} />
        <Text style={styles.headerTitle}>ATD PICKS</Text>
        {data && (
          <Text style={styles.headerSub}>
            {mode === "top5" ? `Top ${displayed.length} of the day` : `${displayed.length} picks`}
          </Text>
        )}
      </View>

      <View style={styles.toggleRow}>
        <Pressable
          onPress={() => setMode("top5")}
          style={[styles.toggleBtn, mode === "top5" && styles.toggleBtnActive]}
          testID="atd-toggle-top"
        >
          <Text style={[styles.toggleText, mode === "top5" && styles.toggleTextActive]}>
            🔥 Top 5 Today
          </Text>
        </Pressable>
        <Pressable
          onPress={() => setMode("full")}
          style={[styles.toggleBtn, mode === "full" && styles.toggleBtnActive]}
          testID="atd-toggle-full"
        >
          <Text style={[styles.toggleText, mode === "full" && styles.toggleTextActive]}>
            📋 Full Board
          </Text>
        </Pressable>
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
      ) : displayed.length === 0 ? (
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
          <Text style={styles.intro}>
            {mode === "top5"
              ? "Top 5 anytime-TD picks across the slate · ranked by td_probability (recent touches/TDs · RB archetype · opponent matchup)."
              : "Full ATD board · all picks meeting minimum opportunity + probability filters."}
          </Text>
          {displayed.map((p, i) => (
            <PickCard key={`${p.player_id}-${i}`} pick={p} rank={i + 1} />
          ))}
          <View style={{ height: 32 }} />
        </ScrollView>
      )}
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe:   { flex: 1, backgroundColor: COLORS.background },
  header: { flexDirection: "row", alignItems: "center", gap: 10,
            paddingHorizontal: 14, paddingTop: 6, paddingBottom: 10,
            borderBottomWidth: 1, borderBottomColor: COLORS.borderDefault },
  backBtn:     { padding: 4, marginRight: -4 },
  headerTitle: { color: COLORS.textPrimary, fontSize: 17, fontWeight: "900", letterSpacing: 0.8 },
  headerSub:   { color: COLORS.textMuted, fontSize: 11, marginLeft: "auto", fontWeight: "600" },

  toggleRow: { flexDirection: "row", gap: 8, paddingHorizontal: 14, paddingVertical: 10,
               borderBottomWidth: 1, borderBottomColor: COLORS.borderDefault },
  toggleBtn: { flex: 1, paddingVertical: 8, paddingHorizontal: 12, borderRadius: 8,
               backgroundColor: "rgba(255,255,255,0.04)", borderWidth: 1,
               borderColor: COLORS.borderDefault, alignItems: "center" },
  toggleBtnActive: { backgroundColor: "rgba(74, 222, 128, 0.18)", borderColor: COLORS.goldElite },
  toggleText:      { color: COLORS.textSecondary, fontSize: 12, fontWeight: "700" },
  toggleTextActive:{ color: COLORS.textPrimary, fontWeight: "900" },

  scroll: { paddingHorizontal: 12, paddingTop: 12 },
  intro:  { color: COLORS.textSecondary, fontSize: 11.5, lineHeight: 16,
            marginBottom: 14, paddingHorizontal: 4 },
  center: { flexGrow: 1, alignItems: "center", justifyContent: "center",
            paddingHorizontal: 24, gap: 8 },
  errorText:  { color: "#fca5a5", fontSize: 13, textAlign: "center" },
  emptyTitle: { color: COLORS.textPrimary, fontSize: 15, fontWeight: "700" },
  hintText:   { color: COLORS.textMuted, fontSize: 12, textAlign: "center" },

  card: { backgroundColor: COLORS.surface, borderRadius: 14, padding: 14,
          marginBottom: 12, borderWidth: 1, borderColor: COLORS.borderDefault },
  headerRow: { flexDirection: "row", alignItems: "center" },
  rank:    { color: COLORS.goldElite, fontSize: 18, fontWeight: "900", width: 32, textAlign: "center" },
  gradeChip: { paddingHorizontal: 7, paddingVertical: 3, borderRadius: 4, minWidth: 32, alignItems: "center" },
  gradeText: { color: "#0a0a0a", fontWeight: "900", fontSize: 11.5, letterSpacing: 0.4 },
  name:  { color: COLORS.textPrimary, fontSize: 14, fontWeight: "900" },
  sub:   { color: COLORS.textMuted, fontSize: 11, marginTop: 2 },
  score: { color: COLORS.textPrimary, fontSize: 16, fontWeight: "900", lineHeight: 18 },
  pct:   { color: COLORS.goldElite, fontSize: 10, fontWeight: "700" },

  metaRow:  { flexDirection: "row", flexWrap: "wrap", gap: 6, marginTop: 8 },
  metaChip: { paddingHorizontal: 7, paddingVertical: 3, borderRadius: 4,
              backgroundColor: "rgba(255,255,255,0.05)", borderWidth: 1, borderColor: COLORS.borderDefault },
  metaText: { color: COLORS.textSecondary, fontSize: 10.5, fontWeight: "700" },

  bullets: { marginTop: 8, gap: 2 },
  bullet:  { color: COLORS.textSecondary, fontSize: 11.5, lineHeight: 16 },
});
