/**
 * MLB Home-Run slate screen.
 *
 * Two view modes (toggle in the header):
 *  • "Top 5 Today" (default) — flattens every game's HR picks into one
 *    pool, sorts by hr_score desc, surfaces the TOP 5 across the whole
 *    day. Each row shows the matchup context inline (venue, opp SP,
 *    park HR factor, wind / temp / roof).
 *  • "By Game" — original per-game layout with up to 5 picks per game.
 *
 * Per user feedback 2026-06-30: "I want the 3–5 for the whole day so
 * add option where app take the 5 best". Default mode is the flat
 * top-5; toggle is provided for power users who want the per-game view.
 *
 * Auto-refreshes on pull-down. Backend caches each slate for 25 min.
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
  api, type HRSlateResponse, type GameHRSlate, type HRHitter,
} from "@/src/lib/api";
import { COLORS } from "@/src/theme";
import { SkeletonList } from "@/src/components/Skeleton";
import { EmptyState } from "@/src/components/EmptyState";

type ViewMode = "topDay" | "byGame";

// Augmented pick that carries its game's context so we can render a
// stand-alone card in flat "Top of Day" view.
type FlatPick = HRHitter & {
  _game_id: string;
  _away: string;
  _home: string;
  _venue: string;
  _commence_time: string;
  _park_hr_factor: number;
  _park_hr_label: string;
  _wind_label: string;
  _temp_f: number | null;
  _wind_mph: number | null;
  _wind_deg: number | null;
  _roof: string;
  _pitcher_opponent: string;
  _pitcher_opponent_hr9: number | null;
};

function gradeColor(g: string): string {
  if (g === "A+" || g === "A") return "rgba(74, 222, 128, 0.92)";
  if (g === "B+" || g === "B") return "rgba(132, 204, 22, 0.85)";
  if (g === "C+" || g === "C") return "rgba(234, 179, 8, 0.85)";
  if (g === "D")               return "rgba(249, 115, 22, 0.78)";
  return "rgba(239, 68, 68, 0.80)";
}

function compassFromDeg(deg: number): string {
  const dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
  return dirs[Math.round(((deg % 360) / 45)) % 8];
}

// ── Flatten + rank helper ─────────────────────────────────────
function flattenTopOfDay(games: GameHRSlate[], topN = 5): FlatPick[] {
  const all: FlatPick[] = [];
  for (const g of games) {
    for (const p of g.picks) {
      const oppPitcher = p.is_home ? g.pitcher_away_name : g.pitcher_home_name;
      const oppPitcherHr9 = p.is_home ? g.pitcher_away_hr9 : g.pitcher_home_hr9;
      all.push({
        ...p,
        _game_id: g.game_id,
        _away: g.away_team,
        _home: g.home_team,
        _venue: g.venue,
        _commence_time: g.commence_time,
        _park_hr_factor: g.park_hr_factor,
        _park_hr_label: g.park_hr_label,
        _wind_label: g.wind_blowing_label,
        _temp_f: g.temp_f,
        _wind_mph: g.wind_mph,
        _wind_deg: g.wind_deg,
        _roof: g.roof_status,
        _pitcher_opponent: oppPitcher,
        _pitcher_opponent_hr9: oppPitcherHr9,
      });
    }
  }
  all.sort((a, b) => b.hr_score - a.hr_score);
  // 2026-07-02 fix — user: "should not have 3 ppl from same game in top".
  // Enforce ONE HR pick per game in the Top-N view. The Yankees lineup
  // (or any bopper team) was crowding out ATL, HOU, LAD, etc. Diversify
  // by taking the top HR-scored batter from each unique game_id first,
  // then fill remaining slots from the rest if we run short.
  const seenGames = new Set<string>();
  const diversified: FlatPick[] = [];
  for (const p of all) {
    if (seenGames.has(p._game_id)) continue;
    seenGames.add(p._game_id);
    diversified.push(p);
    if (diversified.length >= topN) break;
  }
  // If we don't have enough unique games (rare — happens on light MLB
  // slates), backfill with the next-best picks regardless of game.
  if (diversified.length < topN) {
    for (const p of all) {
      if (diversified.includes(p)) continue;
      diversified.push(p);
      if (diversified.length >= topN) break;
    }
  }
  return diversified;
}

// ── Single flat pick card (top-of-day mode) ───────────────────
function FlatPickCard({ pick, rank }: { pick: FlatPick; rank: number }) {
  const matchTime = pick._commence_time
    ? new Date(pick._commence_time).toLocaleTimeString([], {
        hour: "numeric", minute: "2-digit",
      })
    : "";
  return (
    <View style={styles.flatCard}>
      <View style={styles.flatHeaderRow}>
        <Text style={styles.flatRank}>#{rank}</Text>
        <View style={[styles.gradeChip, { backgroundColor: gradeColor(pick.grade) }]}>
          <Text style={styles.gradeText}>{pick.grade}</Text>
        </View>
        <View style={{ flex: 1, marginLeft: 10 }}>
          <Text style={styles.batterName}>
            {pick.batter_name}{pick.batter_hand ? ` (${pick.batter_hand})` : ""}
          </Text>
          <Text style={styles.batterSub}>
            {pick.team} {pick.is_home ? "vs" : "@"} {pick.opponent}
            {matchTime ? ` · ${matchTime}` : ""}
          </Text>
        </View>
        <View style={{ alignItems: "flex-end" }}>
          <Text style={styles.score}>{pick.hr_score.toFixed(0)}</Text>
          <Text style={styles.pct}>{(pick.hr_probability * 100).toFixed(1)}%</Text>
        </View>
      </View>

      {/* Matchup context chips */}
      <View style={styles.condRow}>
        <View style={styles.condChip}>
          <Ionicons name="location" size={10.5} color={COLORS.textSecondary} />
          <Text style={styles.condText}>{pick._venue.replace(/\s+at\s+.*$/, "")}</Text>
        </View>
        <View style={styles.condChip}>
          <Ionicons name="baseball" size={10.5} color={COLORS.textSecondary} />
          <Text style={styles.condText}>Park {pick._park_hr_factor.toFixed(2)}</Text>
        </View>
        {pick._temp_f != null && (
          <View style={styles.condChip}>
            <Ionicons name="thermometer" size={10.5} color={COLORS.textSecondary} />
            <Text style={styles.condText}>{Math.round(pick._temp_f)}°F</Text>
          </View>
        )}
        {pick._wind_mph != null && pick._wind_mph >= 4 && (
          <View style={styles.condChip}>
            <Ionicons name="navigate" size={10.5} color={COLORS.textSecondary} />
            <Text style={styles.condText}>
              {Math.round(pick._wind_mph)} mph{" "}
              {pick._wind_deg != null ? compassFromDeg(pick._wind_deg) : ""}
            </Text>
          </View>
        )}
        {pick._roof === "closed_dome" && (
          <View style={styles.condChip}>
            <Ionicons name="home" size={10.5} color={COLORS.textSecondary} />
            <Text style={styles.condText}>Dome</Text>
          </View>
        )}
      </View>

      {pick._wind_label ? (
        <Text style={styles.windNote}>💨 {pick._wind_label}</Text>
      ) : null}

      {pick._pitcher_opponent ? (
        <Text style={styles.pitcherText}>
          vs SP {pick._pitcher_opponent}
          {pick._pitcher_opponent_hr9 != null
            ? ` (HR/9 ${pick._pitcher_opponent_hr9.toFixed(2)})` : ""}
        </Text>
      ) : null}

      {pick.why_this_pick && pick.why_this_pick.length > 0 && (
        <View style={styles.bullets}>
          {pick.why_this_pick.slice(0, 6).map((b, i) => (
            <Text key={`hr-why-${pick.batter_id}-${i}`} style={styles.bullet}>• {b}</Text>
          ))}
        </View>
      )}
    </View>
  );
}

// ── Per-game card (by-game mode) ──────────────────────────────
function HitterRow({ pick }: { pick: HRHitter }) {
  return (
    <View style={styles.hitter}>
      <View style={styles.hitterHeader}>
        <View style={[styles.gradeChip, { backgroundColor: gradeColor(pick.grade) }]}>
          <Text style={styles.gradeText}>{pick.grade}</Text>
        </View>
        <View style={{ flex: 1, marginLeft: 10 }}>
          <Text style={styles.hitterName}>
            {pick.batter_name}{pick.batter_hand ? ` (${pick.batter_hand})` : ""}
          </Text>
          <Text style={styles.hitterSub}>
            {pick.team} {pick.is_home ? "vs" : "@"} {pick.opponent}
            {pick.season_hr != null && ` · ${pick.season_hr} HR season`}
            {pick.last_15_hrs != null && pick.last_15_games
              ? ` · ${pick.last_15_hrs} HR last ${pick.last_15_games}G` : ""}
          </Text>
        </View>
        <View style={{ alignItems: "flex-end" }}>
          <Text style={styles.score}>{pick.hr_score.toFixed(0)}</Text>
          <Text style={styles.pct}>{(pick.hr_probability * 100).toFixed(1)}%</Text>
        </View>
      </View>
      {pick.why_this_pick && pick.why_this_pick.length > 0 && (
        <View style={styles.bullets}>
          {pick.why_this_pick.slice(0, 6).map((b, i) => (
            <Text key={`hr-why-${pick.batter_id}-${i}`} style={styles.bullet}>• {b}</Text>
          ))}
        </View>
      )}
    </View>
  );
}

function GameCard({ game }: { game: GameHRSlate }) {
  const matchDate = game.commence_time
    ? new Date(game.commence_time).toLocaleTimeString([], {
        hour: "numeric", minute: "2-digit",
      })
    : "";
  return (
    <View style={styles.gameCard}>
      <View style={styles.gameHeader}>
        <Text style={styles.gameTitle}>
          {game.away_team} @ {game.home_team}
        </Text>
        <Text style={styles.gameTime}>{matchDate}</Text>
      </View>
      <Text style={styles.gameVenue}>{game.venue}</Text>
      <View style={styles.condRow}>
        <View style={styles.condChip}>
          <Ionicons name="baseball" size={11} color={COLORS.textSecondary} />
          <Text style={styles.condText}>Park {game.park_hr_factor.toFixed(2)}</Text>
        </View>
        {game.temp_f != null && (
          <View style={styles.condChip}>
            <Ionicons name="thermometer" size={11} color={COLORS.textSecondary} />
            <Text style={styles.condText}>{Math.round(game.temp_f)}°F</Text>
          </View>
        )}
        {game.wind_mph != null && game.wind_mph >= 4 && (
          <View style={styles.condChip}>
            <Ionicons name="navigate" size={11} color={COLORS.textSecondary} />
            <Text style={styles.condText}>
              {Math.round(game.wind_mph)} mph{" "}
              {game.wind_deg != null ? compassFromDeg(game.wind_deg) : ""}
            </Text>
          </View>
        )}
        {game.roof_status === "closed_dome" && (
          <View style={styles.condChip}>
            <Ionicons name="home" size={11} color={COLORS.textSecondary} />
            <Text style={styles.condText}>Dome</Text>
          </View>
        )}
        {game.wind_blowing_label ? (
          <Text style={styles.windNote}>{game.wind_blowing_label}</Text>
        ) : null}
      </View>
      <View style={styles.pitcherBlock}>
        {game.pitcher_away_name ? (
          <Text style={styles.pitcherText}>
            {game.away_team} SP: {game.pitcher_away_name}
            {game.pitcher_away_hr9 != null
              ? ` (HR/9 ${game.pitcher_away_hr9.toFixed(2)})` : ""}
          </Text>
        ) : null}
        {game.pitcher_home_name ? (
          <Text style={styles.pitcherText}>
            {game.home_team} SP: {game.pitcher_home_name}
            {game.pitcher_home_hr9 != null
              ? ` (HR/9 ${game.pitcher_home_hr9.toFixed(2)})` : ""}
          </Text>
        ) : null}
      </View>
      {game.picks.length === 0 ? (
        <Text style={styles.emptyInline}>No qualifying HR picks for this game.</Text>
      ) : (
        game.picks.map((p) => (
          <HitterRow key={`${game.game_id}-${p.batter_id}`} pick={p} />
        ))
      )}
    </View>
  );
}

// ── Screen ────────────────────────────────────────────────────
export default function HRTab() {
  const router = useRouter();
  const [data, setData] = useState<HRSlateResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<ViewMode>("topDay");

  const load = useCallback(async (refresh = false) => {
    try {
      setError(null);
      const res = await api.hrSlate({ refresh });
      setData(res);
    } catch (e: any) {
      setError(e?.message || "Failed to load HR slate");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => { load(false); }, [load]);

  const onRefresh = useCallback(() => {
    setRefreshing(true);
    load(true);
  }, [load]);

  // Compute top-of-day picks once per slate change.
  const topOfDay = useMemo(
    () => (data ? flattenTopOfDay(data.games, 5) : []),
    [data],
  );

  return (
    <SafeAreaView style={styles.safe} edges={["top"]}>
      <Stack.Screen options={{ headerShown: false }} />
      <View style={styles.header}>
        <Pressable
          onPress={() => router.back()}
          hitSlop={12}
          style={styles.backBtn}
          testID="hr-back"
        >
          <Ionicons name="chevron-back" size={22} color={COLORS.textPrimary} />
        </Pressable>
        <Ionicons name="baseball-outline" size={20} color={COLORS.goldElite} />
        <Text style={styles.headerTitle}>HR PICKS</Text>
        {data && (
          <Text style={styles.headerSub}>
            {mode === "topDay"
              ? `Top ${Math.min(5, topOfDay.length)} of the day`
              : `${data.games.length}g · ${data.total_picks} picks`}
          </Text>
        )}
      </View>

      {/* View mode toggle */}
      <View style={styles.toggleRow}>
        <Pressable
          onPress={() => setMode("topDay")}
          style={[styles.toggleBtn, mode === "topDay" && styles.toggleBtnActive]}
          testID="hr-toggle-top"
        >
          <Text style={[styles.toggleText, mode === "topDay" && styles.toggleTextActive]}>
            🔥 Top 5 Today
          </Text>
        </Pressable>
        <Pressable
          onPress={() => setMode("byGame")}
          style={[styles.toggleBtn, mode === "byGame" && styles.toggleBtnActive]}
          testID="hr-toggle-game"
        >
          <Text style={[styles.toggleText, mode === "byGame" && styles.toggleTextActive]}>
            📋 By Game
          </Text>
        </Pressable>
      </View>

      {loading ? (
        <View style={styles.scroll} testID="hr-skeleton">
          <SkeletonList count={4} />
        </View>
      ) : error ? (
        <ScrollView
          contentContainerStyle={styles.scroll}
          refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={COLORS.voltBlue} />}
        >
          <EmptyState
            variant="error"
            title="Couldn't load HR slate"
            message={error}
            onRetry={onRefresh}
            secondaryHint="Pull down to retry manually — slate refreshes every 25 min."
            testID="hr-error"
          />
        </ScrollView>
      ) : !data || data.games.length === 0 ? (
        <ScrollView
          contentContainerStyle={styles.scroll}
          refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={COLORS.voltBlue} />}
        >
          <EmptyState
            icon="baseball-outline"
            title="No MLB games today"
            message="The slate is quiet — no games to analyze right now."
            secondaryHint="Check back later — slate refreshes every 25 min."
            testID="hr-empty"
          />
        </ScrollView>
      ) : (
        <ScrollView
          style={{ flex: 1 }}
          contentContainerStyle={styles.scroll}
          refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={COLORS.voltBlue} />}
        >
          {mode === "topDay" ? (
            topOfDay.length === 0 ? (
              <Text style={styles.emptyInline}>No qualifying HR picks for the slate.</Text>
            ) : (
              <>
                <Text style={styles.intro}>
                  Top 5 batters across the whole slate · ranked by hr_score
                  (park × pitcher HR/9 × ISO × form × wind × temp).
                </Text>
                {topOfDay.map((p, i) => (
                  <FlatPickCard key={`${p._game_id}-${p.batter_id}`} pick={p} rank={i + 1} />
                ))}
              </>
            )
          ) : (
            <>
              <Text style={styles.intro}>
                Per-game breakdown · up to 5 batters per game.
              </Text>
              {data.games.map((g) => <GameCard key={g.game_id} game={g} />)}
            </>
          )}
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
  backBtn: { padding: 4, marginRight: -4 },
  headerTitle: { color: COLORS.textPrimary, fontSize: 17, fontWeight: "900", letterSpacing: 0.8 },
  headerSub:   { color: COLORS.textMuted, fontSize: 11, marginLeft: "auto", fontWeight: "600" },

  toggleRow: {
    flexDirection: "row", gap: 8,
    paddingHorizontal: 14, paddingVertical: 10,
    borderBottomWidth: 1, borderBottomColor: COLORS.borderDefault,
  },
  toggleBtn: {
    flex: 1, paddingVertical: 8, paddingHorizontal: 12, borderRadius: 8,
    backgroundColor: "rgba(255,255,255,0.04)", borderWidth: 1,
    borderColor: COLORS.borderDefault, alignItems: "center",
  },
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
  emptyInline:{ color: COLORS.textMuted, fontSize: 11.5, fontStyle: "italic", marginTop: 10 },
  hintText:   { color: COLORS.textMuted, fontSize: 12, textAlign: "center" },

  // Flat top-of-day card
  flatCard: { backgroundColor: COLORS.surface, borderRadius: 14, padding: 14,
              marginBottom: 12, borderWidth: 1, borderColor: COLORS.borderDefault },
  flatHeaderRow: { flexDirection: "row", alignItems: "center" },
  flatRank: { color: COLORS.goldElite, fontSize: 18, fontWeight: "900", width: 32, textAlign: "center" },
  batterName: { color: COLORS.textPrimary, fontSize: 14, fontWeight: "900" },
  batterSub:  { color: COLORS.textMuted, fontSize: 11, marginTop: 2 },

  // Per-game card
  gameCard: { backgroundColor: COLORS.surface, borderRadius: 14, padding: 14,
              marginBottom: 14, borderWidth: 1, borderColor: COLORS.borderDefault },
  gameHeader: { flexDirection: "row", justifyContent: "space-between", alignItems: "center" },
  gameTitle:  { color: COLORS.textPrimary, fontSize: 15, fontWeight: "800", letterSpacing: 0.3 },
  gameTime:   { color: COLORS.textMuted, fontSize: 11, fontWeight: "600" },
  gameVenue:  { color: COLORS.textSecondary, fontSize: 11, marginTop: 2 },

  // Shared
  condRow: { flexDirection: "row", flexWrap: "wrap", gap: 6, marginTop: 8 },
  condChip: { flexDirection: "row", alignItems: "center", gap: 4,
              paddingHorizontal: 7, paddingVertical: 3, borderRadius: 4,
              backgroundColor: "rgba(255,255,255,0.05)",
              borderWidth: 1, borderColor: COLORS.borderDefault },
  condText:  { color: COLORS.textSecondary, fontSize: 10.5, fontWeight: "700" },
  windNote:  { color: COLORS.goldElite, fontSize: 11, fontWeight: "700", marginTop: 6 },

  pitcherBlock: { marginTop: 8, gap: 2, paddingTop: 8,
                  borderTopWidth: 1, borderTopColor: "rgba(255,255,255,0.04)" },
  pitcherText:  { color: COLORS.textSecondary, fontSize: 11, fontWeight: "600", marginTop: 4 },

  hitter: { marginTop: 10, paddingTop: 10, borderTopWidth: 1,
            borderTopColor: "rgba(255,255,255,0.04)" },
  hitterHeader: { flexDirection: "row", alignItems: "center" },
  gradeChip:  { paddingHorizontal: 7, paddingVertical: 3, borderRadius: 4,
                minWidth: 32, alignItems: "center" },
  gradeText:  { color: "#0a0a0a", fontWeight: "900", fontSize: 11.5, letterSpacing: 0.4 },
  hitterName: { color: COLORS.textPrimary, fontSize: 13, fontWeight: "800" },
  hitterSub:  { color: COLORS.textMuted, fontSize: 10.5, marginTop: 1 },
  score:      { color: COLORS.textPrimary, fontSize: 16, fontWeight: "900", lineHeight: 18 },
  pct:        { color: COLORS.goldElite, fontSize: 10, fontWeight: "700" },

  bullets: { marginTop: 6, gap: 2, paddingLeft: 42 },
  bullet:  { color: COLORS.textSecondary, fontSize: 11, lineHeight: 15 },
});
