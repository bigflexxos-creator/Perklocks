import React, { useEffect, useState } from "react";
import {
  View, Text, StyleSheet, ScrollView, Pressable, ActivityIndicator,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { Ionicons } from "@expo/vector-icons";
import { useRouter } from "expo-router";
import { COLORS } from "@/src/theme";
import { useAuth } from "@/src/contexts/AuthContext";
import { api } from "@/src/lib/api";

export default function ProfileScreen() {
  const { user, signOut } = useAuth();
  const router = useRouter();
  const [stats, setStats] = useState<{
    date: string; total_picks: number; elite_count: number; avg_edge_percent: number;
    by_sport: { sport: string; count: number; avg_lock: number; avg_edge: number; elite_count: number }[];
  } | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api.stats().then(setStats).catch(() => {}).finally(() => setLoading(false));
  }, []);

  const onLogout = async () => {
    await signOut();
    router.replace("/(auth)/login");
  };

  return (
    <SafeAreaView style={styles.safe} edges={["top"]}>
      <ScrollView contentContainerStyle={styles.content} showsVerticalScrollIndicator={false}>
        <Text style={styles.title}>PROFILE</Text>

        <View style={styles.userCard}>
          <View style={styles.avatar}>
            <Text style={styles.avatarLetter}>{(user?.name || user?.email || "?")[0]?.toUpperCase()}</Text>
          </View>
          <View style={{ flex: 1 }}>
            <Text style={styles.name}>{user?.name || "Bettor"}</Text>
            <Text style={styles.email}>{user?.email}</Text>
          </View>
        </View>

        <Text style={styles.sectionLabel}>TODAY&apos;S BOARD</Text>
        {loading ? (
          <ActivityIndicator color={COLORS.voltBlue} style={{ marginTop: 20 }} />
        ) : stats ? (
          <>
            <View style={styles.statRow}>
              <BigStat label="TOTAL PICKS" value={stats.total_picks} />
              <BigStat label="ELITE LOCKS" value={stats.elite_count} color={COLORS.goldElite} />
              <BigStat
                label="AVG EDGE"
                value={`${stats.avg_edge_percent > 0 ? "+" : ""}${stats.avg_edge_percent}%`}
                color={COLORS.neonGreen}
              />
            </View>

            <Text style={styles.sectionLabel}>BY SPORT</Text>
            <View style={styles.sportList}>
              {stats.by_sport.map((s) => (
                <View key={s.sport} style={styles.sportRow}>
                  <Text style={styles.sportName}>{s.sport.toUpperCase()}</Text>
                  <View style={styles.sportMetrics}>
                    <Text style={styles.sportMetric}>{s.count} picks</Text>
                    <Text style={[styles.sportMetric, { color: COLORS.textPrimary }]}>
                      avg {s.avg_lock}
                    </Text>
                    <Text style={[styles.sportMetric, { color: COLORS.goldElite }]}>
                      {s.elite_count} elite
                    </Text>
                  </View>
                </View>
              ))}
            </View>
          </>
        ) : null}

        <Pressable testID="logout-button" onPress={onLogout} style={styles.logoutBtn}>
          <Ionicons name="log-out-outline" size={18} color={COLORS.electricBlaze} />
          <Text style={styles.logoutText}>SIGN OUT</Text>
        </Pressable>

        <Text style={styles.footerNote}>
          LockScore AI v1.0 · Daily auto-refresh at 06:00 UTC{"\n"}
          All picks are probabilistic — never guaranteed.
        </Text>
      </ScrollView>
    </SafeAreaView>
  );
}

function BigStat({ label, value, color = COLORS.textPrimary }: { label: string; value: any; color?: string }) {
  return (
    <View style={styles.bigStat}>
      <Text style={styles.bigStatLabel}>{label}</Text>
      <Text style={[styles.bigStatValue, { color }]}>{value}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: COLORS.bg },
  content: { paddingHorizontal: 20, paddingTop: 8, paddingBottom: 30 },
  title: { fontSize: 22, fontWeight: "900", color: COLORS.textPrimary, letterSpacing: 3, marginBottom: 18 },
  userCard: {
    flexDirection: "row", alignItems: "center", gap: 14, padding: 18,
    backgroundColor: COLORS.surface, borderRadius: 16,
    borderWidth: 1, borderColor: COLORS.borderDefault, marginBottom: 22,
  },
  avatar: {
    width: 52, height: 52, borderRadius: 26,
    backgroundColor: COLORS.voltBlue,
    alignItems: "center", justifyContent: "center",
  },
  avatarLetter: { color: COLORS.textPrimary, fontSize: 22, fontWeight: "900" },
  name: { color: COLORS.textPrimary, fontSize: 18, fontWeight: "800" },
  email: { color: COLORS.textMuted, fontSize: 13, marginTop: 2 },
  sectionLabel: { color: COLORS.textMuted, fontSize: 10, fontWeight: "800", letterSpacing: 1.5, marginBottom: 10, marginTop: 8 },
  statRow: { flexDirection: "row", gap: 10, marginBottom: 6 },
  bigStat: {
    flex: 1, padding: 14, borderRadius: 12,
    backgroundColor: COLORS.surface, borderWidth: 1, borderColor: COLORS.borderDefault,
  },
  bigStatLabel: { color: COLORS.textMuted, fontSize: 9, fontWeight: "800", letterSpacing: 1.3 },
  bigStatValue: { fontSize: 22, fontWeight: "900", marginTop: 4, letterSpacing: -0.5 },
  sportList: { marginTop: 4 },
  sportRow: {
    flexDirection: "row", justifyContent: "space-between", alignItems: "center",
    paddingVertical: 14, borderBottomWidth: 1, borderBottomColor: COLORS.borderDefault,
  },
  sportName: { color: COLORS.textPrimary, fontWeight: "900", letterSpacing: 1.3, fontSize: 13 },
  sportMetrics: { flexDirection: "row", gap: 12 },
  sportMetric: { color: COLORS.textSecondary, fontSize: 12, fontWeight: "700" },
  logoutBtn: {
    flexDirection: "row", alignItems: "center", justifyContent: "center", gap: 10,
    padding: 16, marginTop: 24, borderRadius: 12,
    borderWidth: 1, borderColor: COLORS.killerBorder, backgroundColor: COLORS.killerSurface,
  },
  logoutText: { color: COLORS.electricBlaze, fontWeight: "900", letterSpacing: 2, fontSize: 13 },
  footerNote: {
    color: COLORS.textMuted, fontSize: 11, lineHeight: 17,
    marginTop: 24, textAlign: "center",
  },
});
