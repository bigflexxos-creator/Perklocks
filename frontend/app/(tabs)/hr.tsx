/**
 * MLB Home-Run tab.
 *
 * Renders the per-game HR slate built by /api/mlb/hr-slate. Each card
 * shows the matchup header (away @ home, venue, pitchers, park HR
 * factor, wind, temp, roof), followed by the top 3-5 batter HR picks
 * with their grade chip, HR probability, and the bullet-point
 * "why this pick?" rationale (park / pitcher HR/9 / batter ISO / form /
 * wind / temp / platoon).
 *
 * Auto-refreshes when the user pulls down. Backend caches each slate
 * for 25 min, so repeated taps are sub-100ms.
 */
import React, { useCallback, useEffect, useState } from "react";
import {
  ActivityIndicator, RefreshControl, ScrollView, StyleSheet,
  Text, View,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { Stack } from "expo-router";
import { Ionicons } from "@expo/vector-icons";
import { api, type HRSlateResponse, type GameHRSlate, type HRHitter } from "@/src/lib/api";
import { COLORS } from "@/src/theme";

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
          <Text style={styles.hitterScore}>{pick.hr_score.toFixed(0)}</Text>
          <Text style={styles.hitterPct}>{(pick.hr_probability * 100).toFixed(1)}%</Text>
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

      {/* Conditions row */}
      <View style={styles.condRow}>
        <View style={styles.condChip}>
          <Ionicons name="baseball" size={11} color={COLORS.textSecondary} />
          <Text style={styles.condText}>
            Park {game.park_hr_factor.toFixed(2)}
          </Text>
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

      {/* Pitcher row */}
      <View style={styles.pitcherRow}>
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

      {/* Hitter picks */}
      {game.picks.length === 0 ? (
        <Text style={styles.emptyText}>No qualifying HR picks for this game.</Text>
      ) : (
        game.picks.map((p) => (
          <HitterRow key={`${game.game_id}-${p.batter_id}`} pick={p} />
        ))
      )}
    </View>
  );
}

export default function HRTab() {
  const [data, setData] = useState<HRSlateResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

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

  return (
    <SafeAreaView style={styles.safe} edges={["top"]}>
      <Stack.Screen options={{ headerShown: false }} />
      <View style={styles.header}>
        <Ionicons name="baseball-outline" size={20} color={COLORS.goldElite} />
        <Text style={styles.headerTitle}>HOME RUNS</Text>
        {data && (
          <Text style={styles.headerSub}>
            {data.games.length} games · {data.total_picks} picks
          </Text>
        )}
      </View>
      {loading ? (
        <ActivityIndicator color={COLORS.voltBlue} style={{ marginTop: 60 }} />
      ) : error ? (
        <ScrollView
          contentContainerStyle={styles.center}
          refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={COLORS.voltBlue} />}
        >
          <Text style={styles.errorText}>{error}</Text>
          <Text style={styles.hintText}>Pull down to retry.</Text>
        </ScrollView>
      ) : !data || data.games.length === 0 ? (
        <ScrollView
          contentContainerStyle={styles.center}
          refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={COLORS.voltBlue} />}
        >
          <Text style={styles.emptyTitle}>No MLB games today.</Text>
          <Text style={styles.hintText}>Check back later — slate refreshes every 25 min.</Text>
        </ScrollView>
      ) : (
        <ScrollView
          style={{ flex: 1 }}
          contentContainerStyle={styles.scroll}
          refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={COLORS.voltBlue} />}
        >
          <Text style={styles.intro}>
            Daily HR slate · model blends park HR factor, pitcher HR/9, batter
            ISO + barrel %, recent form, wind direction + temperature, and
            handedness platoon. Top 3–5 batters per game shown.
          </Text>
          {data.games.map((g) => <GameCard key={g.game_id} game={g} />)}
          <View style={{ height: 32 }} />
        </ScrollView>
      )}
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe:   { flex: 1, backgroundColor: COLORS.background },
  header: { flexDirection: "row", alignItems: "center", gap: 10,
            paddingHorizontal: 18, paddingTop: 6, paddingBottom: 10,
            borderBottomWidth: 1, borderBottomColor: COLORS.borderDefault },
  headerTitle: { color: COLORS.textPrimary, fontSize: 17, fontWeight: "900",
                 letterSpacing: 0.8 },
  headerSub:   { color: COLORS.textMuted, fontSize: 11, marginLeft: "auto",
                 fontWeight: "600" },
  scroll: { paddingHorizontal: 12, paddingTop: 12 },
  intro:  { color: COLORS.textSecondary, fontSize: 11.5, lineHeight: 16,
            marginBottom: 14, paddingHorizontal: 4 },
  center: { flexGrow: 1, alignItems: "center", justifyContent: "center",
            paddingHorizontal: 24, gap: 8 },
  errorText: { color: "#fca5a5", fontSize: 13, textAlign: "center" },
  emptyTitle: { color: COLORS.textPrimary, fontSize: 15, fontWeight: "700" },
  hintText:   { color: COLORS.textMuted, fontSize: 12, textAlign: "center" },

  gameCard: { backgroundColor: COLORS.surface, borderRadius: 14, padding: 14,
              marginBottom: 14, borderWidth: 1, borderColor: COLORS.borderDefault },
  gameHeader: { flexDirection: "row", justifyContent: "space-between",
                alignItems: "center" },
  gameTitle:  { color: COLORS.textPrimary, fontSize: 15, fontWeight: "800",
                letterSpacing: 0.3 },
  gameTime:   { color: COLORS.textMuted, fontSize: 11, fontWeight: "600" },
  gameVenue:  { color: COLORS.textSecondary, fontSize: 11, marginTop: 2 },

  condRow: { flexDirection: "row", flexWrap: "wrap", gap: 6, marginTop: 8 },
  condChip: { flexDirection: "row", alignItems: "center", gap: 4,
              paddingHorizontal: 7, paddingVertical: 3, borderRadius: 4,
              backgroundColor: "rgba(255,255,255,0.05)",
              borderWidth: 1, borderColor: COLORS.borderDefault },
  condText:  { color: COLORS.textSecondary, fontSize: 10.5, fontWeight: "700" },
  windNote:  { color: COLORS.goldElite, fontSize: 10.5, fontWeight: "700",
               paddingLeft: 4, alignSelf: "center" },

  pitcherRow:  { marginTop: 8, gap: 2,
                 paddingTop: 8, borderTopWidth: 1,
                 borderTopColor: "rgba(255,255,255,0.04)" },
  pitcherText: { color: COLORS.textSecondary, fontSize: 11, fontWeight: "600" },

  hitter: { marginTop: 10, paddingTop: 10, borderTopWidth: 1,
            borderTopColor: "rgba(255,255,255,0.04)" },
  hitterHeader: { flexDirection: "row", alignItems: "center" },
  gradeChip:  { paddingHorizontal: 7, paddingVertical: 3, borderRadius: 4,
                minWidth: 32, alignItems: "center" },
  gradeText:  { color: "#0a0a0a", fontWeight: "900", fontSize: 11.5,
                letterSpacing: 0.4 },
  hitterName: { color: COLORS.textPrimary, fontSize: 13, fontWeight: "800" },
  hitterSub:  { color: COLORS.textMuted, fontSize: 10.5, marginTop: 1 },
  hitterScore:{ color: COLORS.textPrimary, fontSize: 16, fontWeight: "900",
                lineHeight: 18 },
  hitterPct:  { color: COLORS.goldElite, fontSize: 10, fontWeight: "700" },

  bullets: { marginTop: 6, gap: 2, paddingLeft: 42 },
  bullet:  { color: COLORS.textSecondary, fontSize: 11, lineHeight: 15 },

  emptyText: { color: COLORS.textMuted, fontSize: 11.5, marginTop: 10,
               fontStyle: "italic" },
});
