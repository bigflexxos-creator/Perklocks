/**
 * NFL Anytime-Touchdown (ATD) slate screen — MLB HR-style experience.
 *
 * 2026-06-11 · Restructured per user directive to mirror MLB HR PICKS:
 *  • Two view modes toggled at the top:
 *      🔥 TOP 5 TODAY  — true five highest-ranked ATD wagers across
 *                        the whole active slate.
 *      📋 BY GAME       — ATD candidates grouped by canonical matchup.
 *  • BY GAME is backed by /api/nfl/atd/by-game so the "one player =
 *    one ATD score" invariant is preserved (identical tie-breaking to
 *    the global leaderboard).
 *  • Real sportsbook ATD odds surfaced when a canonical publication
 *    row exists. Never synthesised.
 *
 * The canonical NFL ATD pipeline (nfl_atd_engine + xTD v2 + Bayesian
 * shrinkage) is UNCHANGED — this screen is a READ-only UX rewrite.
 */
import React, { useCallback, useEffect, useMemo, useState } from "react";
import {
  RefreshControl, ScrollView, StyleSheet,
  Text, View, Pressable,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { Stack, useRouter } from "expo-router";
import { Ionicons } from "@expo/vector-icons";
import {
  api,
  type NFLAtdLeaderboardResponse,
  type NFLAtdByGameResponse,
  type NFLAtdByGameGroup,
  type NFLAtdPick,
} from "@/src/lib/api";
import { COLORS } from "@/src/theme";
import { SkeletonList } from "@/src/components/Skeleton";
import { EmptyState } from "@/src/components/EmptyState";
import { safeBack } from "@/src/utils/safeBack";

type ViewMode = "topDay" | "byGame";

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

/** Format American odds → "+150" / "-210". Returns "" when missing. */
function fmtOdds(o?: number | null): string {
  if (o === null || o === undefined || !Number.isFinite(o)) return "";
  return o > 0 ? `+${Math.round(o)}` : `${Math.round(o)}`;
}

function PickCard({ pick, rank, hideRank = false }:
                  { pick: NFLAtdPick; rank: number; hideRank?: boolean }) {
  const grade = gradeForProb(pick.td_probability);
  const opp = oppRatingChip(pick.opportunity_rating);
  const oddsStr = fmtOdds(pick.book_odds);
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
            {pick.position ? ` · ${pick.position}` : ""}
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
        {oddsStr ? (
          <View style={[styles.metaChip, styles.oddsChip]}>
            <Text style={styles.oddsText}>ATD {oddsStr}</Text>
          </View>
        ) : null}
        {typeof pick.weighted_touches_recent === "number" && (
          <View style={styles.metaChip}>
            <Text style={styles.metaText}>
              {pick.weighted_touches_recent.toFixed(1)} touches
            </Text>
          </View>
        )}
        {typeof pick.weighted_tds_recent === "number" && (
          <View style={styles.metaChip}>
            <Text style={styles.metaText}>
              {pick.weighted_tds_recent.toFixed(1)} recent TD
            </Text>
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
  useRouter();
  const [top, setTop] = useState<NFLAtdLeaderboardResponse | null>(null);
  const [byGame, setByGame] = useState<NFLAtdByGameResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<ViewMode>("topDay");

  const load = useCallback(async () => {
    try {
      setError(null);
      // Fire BOTH endpoints in parallel — they hit the same canonical
      // NFL ATD publication rows so scores/odds match across views.
      const [tRes, gRes] = await Promise.all([
        api.nflAtdLeaderboard(60, 0.10, "low"),
        api.nflAtdByGame(5, 0.05, "low"),
      ]);
      setTop(tRes);
      setByGame(gRes);
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

  // ── TOP 5 · true whole-slate ranking, NO per-game diversification ──
  // MLB HR enforces "one HR pick per game" so a hot lineup doesn't
  // crowd out other games.  That rule does NOT apply here: if two
  // players in the same event are the two strongest ATD wagers on
  // the slate, they both belong in Top 5.  Backend is already
  // ranked by td_probability desc; we re-sort defensively.
  const topFive = useMemo(() => {
    const picks: NFLAtdPick[] = top?.picks ?? [];
    return [...picks]
      .sort((a, b) => b.td_probability - a.td_probability)
      .slice(0, 5);
  }, [top]);

  const games: NFLAtdByGameGroup[] = useMemo(
    () => byGame?.games ?? [],
    [byGame],
  );

  // Header summary counts adapt to current mode.
  const summary = useMemo(() => {
    if (mode === "topDay") {
      return `Top ${topFive.length} of ${top?.passed_filters ?? 0}`;
    }
    return `${games.length}g · ${byGame?.picks_returned ?? 0} picks`;
  }, [mode, topFive.length, top, games.length, byGame]);

  const emptyForCurrentMode =
    (mode === "topDay" && topFive.length === 0) ||
    (mode === "byGame" && games.length === 0);

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
        {(top || byGame) && (
          <Text style={styles.headerSub}>{summary}</Text>
        )}
      </View>

      {/* View mode toggle — MLB HR pattern */}
      <View style={styles.toggleRow}>
        <Pressable
          onPress={() => setMode("topDay")}
          style={[styles.toggleBtn, mode === "topDay" && styles.toggleBtnActive]}
          testID="atd-toggle-top"
        >
          <Text style={[styles.toggleText, mode === "topDay" && styles.toggleTextActive]}>
            🔥 Top 5 Today
          </Text>
        </Pressable>
        <Pressable
          onPress={() => setMode("byGame")}
          style={[styles.toggleBtn, mode === "byGame" && styles.toggleBtnActive]}
          testID="atd-toggle-game"
        >
          <Text style={[styles.toggleText, mode === "byGame" && styles.toggleTextActive]}>
            📋 By Game
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
      ) : emptyForCurrentMode ? (
        <ScrollView
          contentContainerStyle={styles.scroll}
          refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={COLORS.voltBlue} />}
        >
          <EmptyState
            icon="american-football-outline"
            title="No qualifying ATD picks"
            message={
              mode === "topDay"
                ? "The slate is quiet — no players clear the ATD model floor yet."
                : "No games with qualifying ATD candidates on today's slate."
            }
            secondaryHint="Check back closer to gameday — the slate refreshes automatically."
            testID="atd-empty"
          />
        </ScrollView>
      ) : (
        <ScrollView
          style={{ flex: 1 }}
          contentContainerStyle={styles.scroll}
          refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={COLORS.voltBlue} />}
        >
          {mode === "topDay" ? (
            <>
              <Text style={styles.sectionTitle}>🔥 TOP 5 TODAY</Text>
              <Text style={styles.intro}>
                True five highest-ranked ATD wagers across the slate —
                no per-game quota, real sportsbook odds when available.
              </Text>
              {topFive.map((p, i) => (
                <PickCard key={`top-${p.player_id}-${i}`} pick={p} rank={i + 1} />
              ))}
            </>
          ) : (
            <>
              <Text style={styles.sectionTitle}>📋 BY GAME</Text>
              <Text style={styles.intro}>
                ATD candidates grouped by matchup. One player = one ATD
                score across every view.
              </Text>
              {games.map((g) => (
                <View key={g.canonical_event_id || g.event} style={styles.gameGroup}>
                  <Text style={styles.gameGroupTitle}>{g.event}</Text>
                  {g.picks.map((p, i) => (
                    <PickCard
                      key={`bg-${g.canonical_event_id || g.event}-${p.player_id}-${i}`}
                      pick={p}
                      rank={i + 1}
                      hideRank
                    />
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
  toggleRow: {
    flexDirection: "row",
    paddingHorizontal: 14,
    paddingVertical: 10,
    gap: 8,
  },
  toggleBtn: {
    flex: 1,
    paddingVertical: 9,
    borderRadius: 10,
    borderWidth: 1,
    borderColor: "#1e293b",
    backgroundColor: "rgba(15,23,42,0.6)",
    alignItems: "center",
  },
  toggleBtnActive: {
    borderColor: COLORS.goldElite,
    backgroundColor: "rgba(255,215,0,0.10)",
  },
  toggleText: {
    color: COLORS.textMuted, fontSize: 13, fontWeight: "700",
    letterSpacing: 0.4,
  },
  toggleTextActive: { color: COLORS.textPrimary },
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
  oddsChip: {
    backgroundColor: "rgba(255,215,0,0.12)",
    borderColor: "rgba(255,215,0,0.45)",
  },
  oddsText: {
    color: COLORS.goldElite,
    fontSize: 11,
    fontWeight: "800",
    letterSpacing: 0.4,
  },
  bullets: { marginTop: 8 },
  bullet: { color: COLORS.textMuted, fontSize: 12, marginBottom: 3 },
});
