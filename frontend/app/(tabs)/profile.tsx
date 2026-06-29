import React, { useEffect, useState } from "react";
import {
  View, Text, StyleSheet, ScrollView, Pressable, ActivityIndicator, Alert, Platform,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { Ionicons } from "@expo/vector-icons";
import { useRouter } from "expo-router";
import { COLORS } from "@/src/theme";
import { useAuth } from "@/src/contexts/AuthContext";
import { api } from "@/src/lib/api";
import { APP_DATA_VERSION, forceClearAllCaches } from "@/src/lib/cachebust";

const BACKEND_URL_DISPLAY = (process.env.EXPO_PUBLIC_BACKEND_URL || "auto")
  .replace(/^https?:\/\//, "")
  .replace(/\/$/, "");

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

  // Force-clear all AsyncStorage caches and reload — Layer 1 of the cache-bust
  // system. User-facing escape hatch for when the phone shows stale data
  // ("hits visible on website but not on app", etc.). See cachebust.ts.
  const [clearing, setClearing] = useState(false);
  const onForceRefresh = () => {
    const doClear = async () => {
      setClearing(true);
      try {
        await forceClearAllCaches();
        if (Platform.OS === "web") {
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          const w: any = (typeof window !== "undefined" ? window : null);
          if (w?.location?.reload) w.location.reload();
          return;
        }
        // Native: route to Locks tab so its useFocusRefetch fires AND
        // surface a confirmation so the user knows the wipe completed.
        // Without this confirmation users tap the button repeatedly because
        // there's no visible "I did it" signal.
        router.replace("/(tabs)");
        // Small delay so the screen transition completes before the alert.
        setTimeout(() => {
          Alert.alert(
            "App data refreshed",
            "Cleared cached picks, bet slip, and preferences. The Locks tab is reloading fresh data from the server.",
          );
        }, 250);
      } finally {
        setClearing(false);
      }
    };
    if (Platform.OS === "web") {
      // Skip the confirm dialog on web — it's a single tap escape hatch.
      doClear();
      return;
    }
    Alert.alert(
      "Refresh app data?",
      "Clears cached picks, bet slip, and preferences from this device. You won't be signed out.",
      [
        { text: "Cancel", style: "cancel" },
        { text: "Refresh", style: "destructive", onPress: doClear },
      ],
    );
  };

  return (
    <SafeAreaView style={styles.safe} edges={["top"]}>
      <ScrollView
        style={styles.scrollView}
        contentContainerStyle={styles.content}
        showsVerticalScrollIndicator={false}
      >
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
              {stats.by_sport.filter((s) => s.sport !== "KBO").map((s) => (
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

        <Pressable
          testID="analytics-button"
          onPress={() => router.push("/analytics")}
          style={[styles.actionBtn, styles.actionBtnPrimary]}
        >
          <Ionicons name="stats-chart" size={18} color={COLORS.goldElite} />
          <Text style={[styles.actionText, { color: COLORS.goldElite }]}>MODEL PERFORMANCE & ROI</Text>
          <Ionicons name="chevron-forward" size={18} color={COLORS.textMuted} />
        </Pressable>

        {/* Admin Dashboard — owner-only. Hidden for regular users. */}
        {((user as { role?: string } | null)?.role === "admin") && (
          <Pressable
            testID="admin-button"
            onPress={() => router.push("/admin")}
            style={[styles.actionBtn, styles.actionBtnPrimary]}
          >
            <Ionicons name="shield-checkmark" size={18} color={COLORS.goldElite} />
            <Text style={[styles.actionText, { color: COLORS.goldElite }]}>ADMIN DASHBOARD</Text>
            <Ionicons name="chevron-forward" size={18} color={COLORS.textMuted} />
          </Pressable>
        )}

        <Pressable
          testID="history-button"
          onPress={() => router.push("/history")}
          style={styles.actionBtn}
        >
          <Ionicons name="time-outline" size={18} color={COLORS.voltBlue} />
          <Text style={styles.actionText}>PICK HISTORY & RESULTS</Text>
          <Ionicons name="chevron-forward" size={18} color={COLORS.textMuted} />
        </Pressable>

        <Pressable
          testID="force-refresh-button"
          onPress={onForceRefresh}
          disabled={clearing}
          style={[styles.actionBtn, clearing && { opacity: 0.5 }]}
        >
          {clearing ? (
            <ActivityIndicator size="small" color="#FFB300" />
          ) : (
            <Ionicons name="refresh-circle-outline" size={18} color="#FFB300" />
          )}
          <Text style={styles.actionText}>REFRESH APP DATA</Text>
          <Ionicons name="chevron-forward" size={18} color={COLORS.textMuted} />
        </Pressable>

        <Pressable testID="logout-button" onPress={onLogout} style={styles.logoutBtn}>
          <Ionicons name="log-out-outline" size={18} color={COLORS.electricBlaze} />
          <Text style={styles.logoutText}>SIGN OUT</Text>
        </Pressable>

        <Text style={styles.footerNote}>
          PerkLocks v1.0 · Daily auto-refresh at 06:00 UTC{"\n"}
          All picks are probabilistic — never guaranteed.{"\n\n"}
          Build: {APP_DATA_VERSION}  ·  Backend: {BACKEND_URL_DISPLAY}
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
  safe: { flex: 1, backgroundColor: "rgba(10,10,10,0.92)" },
  // CRITICAL (2026-06-29 v21): the Profile screen's content (~700px tall)
  // is much shorter than the viewport (~900px). Without an opaque
  // backgroundColor on the ScrollView itself, the empty area below the
  // last section was TRANSPARENT — letting the inactive Locks tab (the
  // last tab visited) bleed through. Locks doesn't have this bug
  // because its 200+ pick cards always overfill the viewport. Fix:
  //   • Give the ScrollView its OWN opaque background
  //   • Give content `flexGrow: 1` so its area extends to fill the
  //     viewport even when there isn't enough content to overflow
  scrollView: { flex: 1, backgroundColor: "rgba(10,10,10,0.92)" },
  content: {
    paddingHorizontal: 20, paddingTop: 8, paddingBottom: 30,
    flexGrow: 1,
  },
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
  actionBtn: {
    flexDirection: "row", alignItems: "center", gap: 12,
    padding: 16, marginTop: 16, borderRadius: 12,
    borderWidth: 1, borderColor: COLORS.borderDefault, backgroundColor: COLORS.surface,
  },
  actionBtnPrimary: { borderColor: "rgba(255,215,0,0.35)", backgroundColor: "rgba(255,215,0,0.06)" },
  actionText: { flex: 1, color: COLORS.textPrimary, fontWeight: "900", letterSpacing: 1.5, fontSize: 12 },
  logoutBtn: {
    flexDirection: "row", alignItems: "center", justifyContent: "center", gap: 10,
    padding: 16, marginTop: 24, borderRadius: 12,
    borderWidth: 1, borderColor: COLORS.dangerBorder, backgroundColor: COLORS.dangerSurface,
  },
  logoutText: { color: COLORS.electricBlaze, fontWeight: "900", letterSpacing: 2, fontSize: 13 },
  footerNote: {
    color: COLORS.textMuted, fontSize: 11, lineHeight: 17,
    marginTop: 24, textAlign: "center",
  },
});
