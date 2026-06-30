/**
 * MLB HR Banner — surfaced inside the Locks tab when sport === "MLB".
 *
 * Replaces the standalone HR tab (per user request 2026-06-30). Pulls the
 * full slate from /api/mlb/hr-slate, flattens every game's picks into one
 * pool, sorts by hr_score desc, and renders the top-5 across THE WHOLE
 * DAY (not 5 per game).
 *
 * Tap on any row drills into the full HR slate screen (app/hr.tsx).
 */
import React, { useCallback, useEffect, useState } from "react";
import {
  ActivityIndicator, Pressable, StyleSheet, Text, View,
} from "react-native";
import { useRouter } from "expo-router";
import { Ionicons } from "@expo/vector-icons";
import { api, type HRHitter, type GameHRSlate } from "@/src/lib/api";
import { COLORS } from "@/src/theme";

// Local typing for "pick + game context" pair so we can show the matchup
// in the banner row.
type ScoredPick = HRHitter & {
  _game_id: string;
  _away: string;
  _home: string;
  _venue: string;
  _commence_time: string;
  _park_factor: number;
  _wind_label: string;
};

function gradeColor(g: string): string {
  if (g === "A+" || g === "A") return "rgba(74, 222, 128, 0.92)";
  if (g === "B+" || g === "B") return "rgba(132, 204, 22, 0.85)";
  if (g === "C+" || g === "C") return "rgba(234, 179, 8, 0.85)";
  if (g === "D")               return "rgba(249, 115, 22, 0.78)";
  return "rgba(239, 68, 68, 0.80)";
}

export function MLBHRBanner() {
  const router = useRouter();
  const [picks, setPicks] = useState<ScoredPick[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  const load = useCallback(async () => {
    try {
      setError(false);
      const res = await api.hrSlate();
      // Flatten + tag each pick with game context so the banner row
      // can show "vs OPP @ Venue" without us re-walking the games array.
      const all: ScoredPick[] = [];
      for (const g of res.games as GameHRSlate[]) {
        for (const p of g.picks) {
          all.push({
            ...p,
            _game_id: g.game_id,
            _away: g.away_team,
            _home: g.home_team,
            _venue: g.venue,
            _commence_time: g.commence_time,
            _park_factor: g.park_hr_factor,
            _wind_label: g.wind_blowing_label,
          });
        }
      }
      all.sort((a, b) => b.hr_score - a.hr_score);
      setPicks(all.slice(0, 5));
    } catch {
      setError(true);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  if (loading) {
    return (
      <View style={styles.wrap}>
        <View style={styles.header}>
          <Ionicons name="baseball" size={16} color={COLORS.goldElite} />
          <Text style={styles.title}>TOP 5 HR PICKS · TODAY</Text>
        </View>
        <ActivityIndicator color={COLORS.voltBlue} style={{ marginVertical: 12 }} />
      </View>
    );
  }
  if (error || !picks) {
    return null;  // silent failure — don't block the MLB slate
  }
  if (picks.length === 0) {
    return (
      <View style={styles.wrap}>
        <View style={styles.header}>
          <Ionicons name="baseball" size={16} color={COLORS.goldElite} />
          <Text style={styles.title}>TOP 5 HR PICKS · TODAY</Text>
        </View>
        <Text style={styles.empty}>No qualifying HR picks today.</Text>
      </View>
    );
  }

  return (
    <Pressable
      onPress={() => router.push("/hr" as any)}
      style={({ pressed }) => [styles.wrap, pressed && { opacity: 0.85 }]}
      testID="mlb-hr-banner"
    >
      <View style={styles.header}>
        <Ionicons name="baseball" size={16} color={COLORS.goldElite} />
        <Text style={styles.title}>TOP 5 HR PICKS · TODAY</Text>
        <Text style={styles.headerHint}>tap for full slate ›</Text>
      </View>
      <Text style={styles.subhead}>
        Park HR factor · pitcher HR/9 · ISO · barrel% · last-15 form · wind · temp
      </Text>
      {picks.map((p, i) => (
        <View key={`${p._game_id}-${p.batter_id}`} style={styles.row}>
          <Text style={styles.rank}>{i + 1}</Text>
          <View style={[styles.grade, { backgroundColor: gradeColor(p.grade) }]}>
            <Text style={styles.gradeText}>{p.grade}</Text>
          </View>
          <View style={{ flex: 1, marginLeft: 10 }}>
            <Text style={styles.name} numberOfLines={1}>
              {p.batter_name}{p.batter_hand ? ` (${p.batter_hand})` : ""}
            </Text>
            <Text style={styles.sub} numberOfLines={1}>
              {p.team} {p.is_home ? "vs" : "@"} {p.opponent}
              {p.season_hr ? ` · ${p.season_hr} HR season` : ""}
              {p.last_15_hrs ? ` · ${p.last_15_hrs} HR last ${p.last_15_games}G` : ""}
            </Text>
            {(p.why_this_pick && p.why_this_pick.length > 0) && (
              <Text style={styles.why} numberOfLines={2}>
                {p.why_this_pick.slice(0, 3).join(" · ")}
              </Text>
            )}
          </View>
          <View style={{ alignItems: "flex-end" }}>
            <Text style={styles.score}>{p.hr_score.toFixed(0)}</Text>
            <Text style={styles.pct}>{(p.hr_probability * 100).toFixed(1)}%</Text>
          </View>
        </View>
      ))}
    </Pressable>
  );
}

const styles = StyleSheet.create({
  wrap: {
    backgroundColor: COLORS.surface,
    borderRadius: 14,
    padding: 14,
    marginBottom: 14,
    borderWidth: 1,
    borderColor: COLORS.borderDefault,
  },
  header: { flexDirection: "row", alignItems: "center", gap: 6 },
  title:  { color: COLORS.textPrimary, fontSize: 12.5, fontWeight: "900", letterSpacing: 0.8 },
  headerHint: { marginLeft: "auto", color: COLORS.textMuted, fontSize: 10, fontWeight: "700" },
  subhead: { color: COLORS.textMuted, fontSize: 10.5, marginTop: 4, marginBottom: 8,
             lineHeight: 14 },
  empty:   { color: COLORS.textMuted, fontSize: 11.5, fontStyle: "italic",
             marginTop: 8 },
  row: {
    flexDirection: "row", alignItems: "center",
    paddingVertical: 10,
    borderTopWidth: 1, borderTopColor: "rgba(255,255,255,0.04)",
  },
  rank: {
    color: COLORS.textMuted, fontSize: 13, fontWeight: "800",
    width: 18, textAlign: "center",
  },
  grade: {
    paddingHorizontal: 7, paddingVertical: 3, borderRadius: 4,
    minWidth: 32, alignItems: "center", marginLeft: 4,
  },
  gradeText: {
    color: "#0a0a0a", fontWeight: "900", fontSize: 11.5, letterSpacing: 0.4,
  },
  name:  { color: COLORS.textPrimary, fontSize: 13, fontWeight: "800" },
  sub:   { color: COLORS.textMuted, fontSize: 10.5, marginTop: 1 },
  why:   { color: COLORS.textSecondary, fontSize: 10.5, marginTop: 2, lineHeight: 14 },
  score: { color: COLORS.textPrimary, fontSize: 16, fontWeight: "900", lineHeight: 18 },
  pct:   { color: COLORS.goldElite, fontSize: 10, fontWeight: "700" },
});
