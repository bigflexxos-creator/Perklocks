import React, { useCallback, useEffect, useState } from "react";
import {
  View, Text, StyleSheet, ScrollView, RefreshControl, ActivityIndicator,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { Ionicons } from "@expo/vector-icons";
import { COLORS, SPORTS } from "@/src/theme";
import { api, Pick } from "@/src/lib/api";
import { LockPickCard } from "@/src/components/LockPickCard";
import { ChipRow } from "@/src/components/ChipRow";

export default function KillerScreen() {
  const [picks, setPicks] = useState<Pick[]>([]);
  const [sport, setSport] = useState("All");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async (s: string) => {
    try {
      const res = await api.betKiller(s);
      setPicks(res.picks);
    } catch (e) {
      console.warn("killer load", e);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => { setLoading(true); load(sport); }, [sport, load]);

  return (
    <SafeAreaView style={[styles.safe]} edges={["top"]}>
      <View style={styles.warningBanner}>
        <Ionicons name="warning" size={20} color={COLORS.electricBlaze} />
        <View style={{ flex: 1 }}>
          <Text style={styles.bannerTitle}>BET KILLER</Text>
          <Text style={styles.bannerSub}>Bets the model says to AVOID — Lock Score below 85.</Text>
        </View>
      </View>

      <ChipRow options={SPORTS} active={sport} onChange={setSport} testIDPrefix="killer-chip" />

      <ScrollView
        contentContainerStyle={styles.content}
        refreshControl={
          <RefreshControl
            tintColor={COLORS.electricBlaze}
            refreshing={refreshing}
            onRefresh={() => { setRefreshing(true); load(sport); }}
          />
        }
        showsVerticalScrollIndicator={false}
        testID="killer-scroll"
      >
        {loading ? (
          <View style={styles.center}>
            <ActivityIndicator color={COLORS.electricBlaze} />
          </View>
        ) : picks.length === 0 ? (
          <View style={styles.center}>
            <Ionicons name="shield-checkmark-outline" size={48} color={COLORS.textMuted} />
            <Text style={styles.emptyTitle}>No games available</Text>
            <Text style={styles.emptyMsg}>No {sport === "All" ? "" : sport + " "}fixtures returned by the sports API for today.</Text>
          </View>
        ) : (
          picks.map((p) => <LockPickCard key={p.id} pick={p} variant="killer" />)
        )}
      </ScrollView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: COLORS.killerBg },
  warningBanner: {
    flexDirection: "row", alignItems: "center", gap: 14,
    paddingHorizontal: 20, paddingVertical: 16,
    backgroundColor: COLORS.killerSurface,
    borderBottomWidth: 1, borderBottomColor: COLORS.killerBorder,
  },
  bannerTitle: { color: COLORS.electricBlaze, fontWeight: "900", letterSpacing: 2.5, fontSize: 16 },
  bannerSub: { color: COLORS.textSecondary, fontSize: 11, marginTop: 2, fontWeight: "600" },
  content: { paddingHorizontal: 20, paddingTop: 10, paddingBottom: 24 },
  center: { paddingVertical: 80, alignItems: "center" },
  emptyTitle: { color: COLORS.textPrimary, fontSize: 16, fontWeight: "800", marginTop: 14 },
  emptyMsg: { color: COLORS.textMuted, fontSize: 13, marginTop: 6, textAlign: "center" },
});
