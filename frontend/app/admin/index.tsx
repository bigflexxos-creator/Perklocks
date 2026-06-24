/**
 * Admin Dashboard — owner-only view of platform metrics and users.
 *
 * Visible at /admin. Auto-redirects to the home tab if the logged-in
 * user's role isn't "admin".
 *
 * Sections:
 *   • Headline tiles: total users · admins · suspended · new 24h ·
 *     new 7d · active 24h · parlays 24h · picks today
 *   • Top API users: ranked by `user_activity.api_calls`. Tap a row to
 *     drill into the user detail page (future).
 */
import React, { useEffect, useState, useCallback } from "react";
import {
  View, Text, StyleSheet, ScrollView, ActivityIndicator,
  RefreshControl, Pressable,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { Stack, router as expoRouter } from "expo-router";
import { Ionicons } from "@expo/vector-icons";
import { COLORS } from "@/src/theme";
import { useAuth } from "@/src/contexts/AuthContext";
import { api } from "@/src/lib/api";

type OverviewResp = {
  users:    { total: number; admins: number; suspended: number;
              new_24h: number; new_7d: number; active_24h: number };
  activity: { parlays_total: number; parlays_24h: number;
              picks_today: number };
  generated_at?: string;
};
type TopUser = {
  user_id: string; email: string | null; name: string | null;
  role: string; status: string; api_calls: number; last_call_at?: string;
};

export default function AdminDashboardScreen() {
  const { user } = useAuth();
  const [overview, setOverview] = useState<OverviewResp | null>(null);
  const [top, setTop] = useState<TopUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // Guard — non-admins shouldn't ever land here, but if they navigate
  // by URL we route them home.
  useEffect(() => {
    if (user && (user as any).role !== "admin") {
      expoRouter.replace("/");
    }
  }, [user]);

  const load = useCallback(async () => {
    setErr(null);
    try {
      const [o, t] = await Promise.all([
        api.request<OverviewResp>("/admin/overview"),
        api.request<{ top: TopUser[] }>("/admin/top-api-users?limit=25"),
      ]);
      setOverview(o);
      setTop(t?.top || []);
    } catch (e: any) {
      setErr(e?.message || "Failed to load");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const onRefresh = () => { setRefreshing(true); load(); };

  if (loading) {
    return (
      <SafeAreaView style={styles.root} edges={["top"]}>
        <Stack.Screen options={{ headerShown: false }} />
        <View style={styles.center}>
          <ActivityIndicator color={COLORS.goldElite} />
        </View>
      </SafeAreaView>
    );
  }

  const u = overview?.users;
  const a = overview?.activity;
  return (
    <SafeAreaView style={styles.root} edges={["top"]}>
      <Stack.Screen options={{ headerShown: false }} />
      {/* Header */}
      <View style={styles.header}>
        <Pressable onPress={() => expoRouter.back()} hitSlop={8} style={styles.backBtn}>
          <Ionicons name="chevron-back" size={22} color={COLORS.textPrimary} />
        </Pressable>
        <Text style={styles.title}>ADMIN DASHBOARD</Text>
        <Pressable onPress={onRefresh} hitSlop={8}>
          <Ionicons name="refresh" size={20} color={COLORS.voltBlue} />
        </Pressable>
      </View>

      <ScrollView
        contentContainerStyle={styles.content}
        refreshControl={
          <RefreshControl
            refreshing={refreshing}
            onRefresh={onRefresh}
            tintColor={COLORS.goldElite}
          />
        }
      >
        {err && (
          <View style={styles.errBox}>
            <Text style={styles.errText}>{err}</Text>
          </View>
        )}

        {/* ── Headline tiles ── */}
        <Text style={styles.sectionTitle}>USERS</Text>
        <View style={styles.tilesRow}>
          <Tile label="TOTAL"      value={u?.total ?? 0} accent={COLORS.goldElite} />
          <Tile label="ACTIVE 24h" value={u?.active_24h ?? 0} accent={COLORS.voltBlue} />
          <Tile label="NEW 24h"    value={u?.new_24h ?? 0} accent={COLORS.signalGreen} />
        </View>
        <View style={styles.tilesRow}>
          <Tile label="NEW 7d"     value={u?.new_7d ?? 0} />
          <Tile label="ADMINS"     value={u?.admins ?? 0} />
          <Tile label="SUSPENDED"  value={u?.suspended ?? 0}
                accent={u?.suspended ? COLORS.warmRed : undefined} />
        </View>

        <Text style={[styles.sectionTitle, { marginTop: 18 }]}>ACTIVITY</Text>
        <View style={styles.tilesRow}>
          <Tile label="PICKS TODAY"  value={a?.picks_today ?? 0} />
          <Tile label="PARLAYS 24h"  value={a?.parlays_24h ?? 0} />
          <Tile label="PARLAYS ALL"  value={a?.parlays_total ?? 0} />
        </View>

        {/* ── Top API consumers ── */}
        <Text style={[styles.sectionTitle, { marginTop: 22 }]}>
          TOP API USERS ({top.length})
        </Text>
        {top.length === 0 ? (
          <View style={styles.emptyBox}>
            <Text style={styles.emptyText}>
              No usage tracked yet. Tracker just turned on — start hitting endpoints and they&apos;ll appear here.
            </Text>
          </View>
        ) : (
          top.map((row, i) => (
            <View key={row.user_id} style={styles.userRow}>
              <View style={styles.rankBadge}>
                <Text style={styles.rankBadgeText}>{i + 1}</Text>
              </View>
              <View style={{ flex: 1, minWidth: 0 }}>
                <View style={{ flexDirection: "row", alignItems: "center", gap: 6 }}>
                  <Text style={styles.userEmail} numberOfLines={1}>
                    {row.email || row.user_id}
                  </Text>
                  {row.role === "admin" && (
                    <View style={styles.adminBadge}>
                      <Text style={styles.adminBadgeText}>ADMIN</Text>
                    </View>
                  )}
                  {row.status === "suspended" && (
                    <View style={[styles.adminBadge, { backgroundColor: COLORS.warmRed }]}>
                      <Text style={styles.adminBadgeText}>SUSPENDED</Text>
                    </View>
                  )}
                </View>
                <Text style={styles.userSub}>
                  {row.name || ""}
                  {row.last_call_at ? ` · last: ${row.last_call_at.slice(0, 16).replace("T", " ")}` : ""}
                </Text>
              </View>
              <Text style={styles.calls}>
                {row.api_calls.toLocaleString()}
              </Text>
            </View>
          ))
        )}

        <View style={{ height: 40 }} />
      </ScrollView>
    </SafeAreaView>
  );
}

function Tile({ label, value, accent }: {
  label: string; value: number; accent?: string;
}) {
  return (
    <View style={[styles.tile, accent ? { borderColor: accent } : null]}>
      <Text style={[styles.tileValue, accent ? { color: accent } : null]}>
        {value.toLocaleString()}
      </Text>
      <Text style={styles.tileLabel}>{label}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: COLORS.background },
  center: { flex: 1, alignItems: "center", justifyContent: "center" },
  header: {
    flexDirection: "row",
    alignItems: "center",
    paddingHorizontal: 14,
    paddingTop: 6,
    paddingBottom: 10,
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: COLORS.borderDefault,
    gap: 12,
  },
  backBtn: {
    width: 32, height: 32, alignItems: "center", justifyContent: "center",
  },
  title: {
    flex: 1, color: COLORS.textPrimary,
    fontSize: 14, fontWeight: "900", letterSpacing: 1.4,
  },
  content: { paddingHorizontal: 14, paddingTop: 12, paddingBottom: 20 },
  sectionTitle: {
    color: COLORS.textMuted, fontSize: 11.5, fontWeight: "900",
    letterSpacing: 1.4, marginBottom: 8,
  },
  tilesRow: {
    flexDirection: "row", gap: 8, marginBottom: 8,
  },
  tile: {
    flex: 1,
    backgroundColor: COLORS.surface,
    borderRadius: 10,
    borderWidth: 1,
    borderColor: COLORS.borderDefault,
    paddingVertical: 14,
    paddingHorizontal: 10,
    alignItems: "center",
  },
  tileValue: {
    color: COLORS.textPrimary, fontSize: 24, fontWeight: "900",
    letterSpacing: -0.5,
  },
  tileLabel: {
    color: COLORS.textMuted, fontSize: 10, fontWeight: "800",
    letterSpacing: 1.0, marginTop: 4,
  },
  userRow: {
    flexDirection: "row", alignItems: "center", gap: 10,
    paddingVertical: 12, paddingHorizontal: 12, borderRadius: 10,
    borderWidth: 1, borderColor: COLORS.borderDefault,
    backgroundColor: COLORS.surface, marginBottom: 6,
  },
  rankBadge: {
    width: 28, height: 28, borderRadius: 14,
    backgroundColor: "rgba(255,215,0,0.18)",
    alignItems: "center", justifyContent: "center",
  },
  rankBadgeText: {
    color: COLORS.goldElite, fontSize: 12, fontWeight: "900",
  },
  userEmail: {
    color: COLORS.textPrimary, fontSize: 13, fontWeight: "800",
    flexShrink: 1,
  },
  userSub: {
    color: COLORS.textMuted, fontSize: 11, fontWeight: "700",
    marginTop: 2,
  },
  calls: {
    color: COLORS.voltBlue, fontSize: 16, fontWeight: "900",
    minWidth: 60, textAlign: "right",
  },
  adminBadge: {
    backgroundColor: COLORS.goldElite,
    paddingHorizontal: 6, paddingVertical: 2,
    borderRadius: 4,
  },
  adminBadgeText: {
    color: "#000", fontSize: 9, fontWeight: "900", letterSpacing: 0.6,
  },
  errBox: {
    backgroundColor: "rgba(255,68,68,0.12)",
    borderRadius: 10, padding: 12, marginBottom: 12,
    borderWidth: 1, borderColor: COLORS.warmRed,
  },
  errText: { color: COLORS.warmRed, fontWeight: "700" },
  emptyBox: {
    paddingVertical: 24, paddingHorizontal: 14,
    borderRadius: 10, borderWidth: 1,
    borderColor: COLORS.borderDefault,
    backgroundColor: COLORS.surface,
    alignItems: "center",
  },
  emptyText: { color: COLORS.textMuted, fontSize: 12, textAlign: "center" },
});
