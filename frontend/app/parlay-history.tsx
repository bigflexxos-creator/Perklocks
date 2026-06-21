/**
 * Parlay History — Save-on-Tap viewer.
 * Lists all parlays the user has saved, grouped by status with quick
 * filter pills. Each card shows per-leg progress and live status.
 */
import React, { useCallback, useEffect, useState } from "react";
import {
  View, Text, StyleSheet, ScrollView, ActivityIndicator,
  RefreshControl, Pressable, Alert,
} from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { Ionicons } from "@expo/vector-icons";
import { Stack, useRouter } from "expo-router";
import { COLORS } from "@/src/theme";
import { api } from "@/src/lib/api";

type Status = "live" | "won" | "lost" | "all";

const FILTERS: { id: Status; label: string; icon: any; color: string }[] = [
  { id: "live", label: "Live",   icon: "flame",     color: COLORS.electricBlaze },
  { id: "won",  label: "Won",    icon: "trophy",    color: COLORS.neonGreen },
  { id: "lost", label: "Lost",   icon: "close-circle-outline", color: COLORS.textMuted },
  { id: "all",  label: "All",    icon: "albums-outline",       color: COLORS.textPrimary },
];

const STATUS_COLOR: Record<string, string> = {
  live: COLORS.electricBlaze,
  won:  COLORS.neonGreen,
  lost: COLORS.textMuted,
};
const LEG_ICON: Record<string, any> = {
  won: "checkmark-circle",
  lost: "close-circle",
  void: "remove-circle",
  pending: "time-outline",
};
const LEG_COLOR: Record<string, string> = {
  won: COLORS.neonGreen,
  lost: "#FF3B5C",
  void: COLORS.textMuted,
  pending: COLORS.voltBlue,
};

export default function ParlayHistoryScreen() {
  const insets = useSafeAreaInsets();
  const router = useRouter();
  const [filter, setFilter] = useState<Status>("live");
  const [parlays, setParlays] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async (status: Status = filter) => {
    try {
      const res = await api.parlayHistory(status);
      setParlays(res.parlays || []);
    } catch (e: any) {
      console.warn("parlayHistory error", e);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [filter]);

  useEffect(() => { setLoading(true); load(filter); }, [filter, load]);

  const onRefresh = useCallback(() => { setRefreshing(true); load(filter); }, [filter, load]);

  const onDelete = useCallback(async (id: string) => {
    Alert.alert("Remove parlay?", "This removes it from your history.", [
      { text: "Cancel", style: "cancel" },
      { text: "Remove", style: "destructive", onPress: async () => {
        try { await api.deleteParlay(id); setParlays(p => p.filter(x => x.id !== id)); }
        catch (e: any) { Alert.alert("Failed", String(e?.message || e)); }
      }},
    ]);
  }, []);

  return (
    <View style={[styles.root, { paddingTop: insets.top + 6 }]}>
      <Stack.Screen options={{ headerShown: false }} />
      <View style={styles.header}>
        <Pressable onPress={() => router.back()} hitSlop={12} style={styles.backBtn}>
          <Ionicons name="chevron-back" size={22} color={COLORS.textPrimary} />
        </Pressable>
        <View style={{ flex: 1, minWidth: 0 }}>
          <Text style={styles.title} numberOfLines={1} adjustsFontSizeToFit minimumFontScale={0.75}>
            🏆 PARLAY HISTORY
          </Text>
          <Text style={styles.sub}>{parlays.length} saved · tracking live legs</Text>
        </View>
      </View>

      <View style={styles.filterRow}>
        {FILTERS.map(f => (
          <Pressable key={f.id} onPress={() => setFilter(f.id)}
            style={[styles.filterPill, filter === f.id && { borderColor: f.color, backgroundColor: f.color + "1A" }]}
            testID={`parlay-history-filter-${f.id}`}>
            <Ionicons name={f.icon} size={14} color={filter === f.id ? f.color : COLORS.textMuted} />
            <Text style={[styles.filterTxt, filter === f.id && { color: f.color }]}>{f.label}</Text>
          </Pressable>
        ))}
      </View>

      {loading ? (
        <View style={styles.center}><ActivityIndicator color={COLORS.electricBlaze} /></View>
      ) : (
        <ScrollView
          contentContainerStyle={{ padding: 14, paddingBottom: 80 }}
          refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={COLORS.electricBlaze} />}
        >
          {parlays.length === 0 ? (
            <View style={styles.empty}>
              <Ionicons name="bookmark-outline" size={42} color={COLORS.textMuted} />
              <Text style={styles.emptyTitle}>No saved parlays yet</Text>
              <Text style={styles.emptySub}>
                Go to the Parlay tab and tap SAVE on any card you want to track.
              </Text>
            </View>
          ) : (
            parlays.map(p => <ParlayCard key={p.id} parlay={p} onDelete={() => onDelete(p.id)} />)
          )}
        </ScrollView>
      )}
    </View>
  );
}

function ParlayCard({ parlay, onDelete }: { parlay: any; onDelete: () => void }) {
  const accent = STATUS_COLOR[parlay.status] || COLORS.textPrimary;
  const oddsLabel = parlay.combined_odds > 0 ? `+${parlay.combined_odds}` : `${parlay.combined_odds}`;
  return (
    <View style={[styles.card, { borderColor: accent + "55" }]}>
      <View style={styles.cardHead}>
        <View style={[styles.statusChip, { backgroundColor: accent + "22", borderColor: accent }]}>
          <Text style={[styles.statusTxt, { color: accent }]}>{parlay.status.toUpperCase()}</Text>
        </View>
        <Text style={styles.odds}>{oddsLabel}</Text>
        {parlay.payout != null && parlay.status === "won" && (
          <Text style={styles.payout}>+${parlay.payout.toFixed(2)}</Text>
        )}
        <Pressable hitSlop={10} onPress={onDelete} style={{ marginLeft: "auto" }}>
          <Ionicons name="trash-outline" size={16} color={COLORS.textMuted} />
        </Pressable>
      </View>

      <View style={styles.progress}>
        <Text style={styles.progressTxt}>
          {parlay.legs_won}/{parlay.legs.length} legs hit · {parlay.legs_pending} pending · {parlay.legs_lost} lost
        </Text>
      </View>

      {parlay.legs.map((leg: any, i: number) => {
        const ic = LEG_ICON[leg.status] || LEG_ICON.pending;
        const ic_color = LEG_COLOR[leg.status] || LEG_COLOR.pending;
        return (
          <View key={i} style={styles.leg}>
            <Ionicons name={ic} size={18} color={ic_color} />
            <View style={{ flex: 1, marginLeft: 8 }}>
              <Text style={styles.legSport}>
                {(leg.sport || "").toUpperCase()} · {leg.league}
              </Text>
              <Text style={styles.legMarket} numberOfLines={1}>{leg.market}</Text>
            </View>
            <Text style={styles.legOdds}>
              {leg.book_odds > 0 ? `+${leg.book_odds}` : leg.book_odds}
            </Text>
          </View>
        );
      })}
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: COLORS.bg },
  header: {
    flexDirection: "row", alignItems: "center", gap: 12,
    paddingHorizontal: 16, paddingTop: 12, paddingBottom: 12,
    borderBottomWidth: 1, borderBottomColor: COLORS.borderDefault,
  },
  backBtn: { padding: 4 },
  title: { color: COLORS.textPrimary, fontSize: 18, fontWeight: "900", letterSpacing: -0.3 },
  sub: { color: COLORS.textMuted, fontSize: 11.5, marginTop: 3, fontWeight: "700", letterSpacing: 0.4 },
  filterRow: {
    flexDirection: "row", paddingHorizontal: 14, paddingVertical: 12, gap: 8,
    borderBottomWidth: 1, borderBottomColor: COLORS.borderDefault,
  },
  filterPill: {
    flexDirection: "row", alignItems: "center", gap: 5,
    paddingHorizontal: 12, paddingVertical: 7, borderRadius: 16,
    borderWidth: 1, borderColor: COLORS.borderDefault,
  },
  filterTxt: { color: COLORS.textMuted, fontSize: 12, fontWeight: "700", letterSpacing: 0.5 },
  center: { flex: 1, alignItems: "center", justifyContent: "center" },
  empty: { alignItems: "center", padding: 40, gap: 8 },
  emptyTitle: { color: COLORS.textPrimary, fontSize: 16, fontWeight: "800" },
  emptySub: { color: COLORS.textMuted, fontSize: 12, textAlign: "center", maxWidth: 280 },
  card: {
    backgroundColor: COLORS.surface, borderWidth: 1, borderRadius: 12,
    padding: 12, marginBottom: 12,
  },
  cardHead: { flexDirection: "row", alignItems: "center", gap: 10, marginBottom: 6 },
  statusChip: {
    paddingHorizontal: 8, paddingVertical: 3, borderRadius: 4, borderWidth: 1,
  },
  statusTxt: { fontSize: 10, fontWeight: "900", letterSpacing: 1 },
  odds: { color: COLORS.textPrimary, fontSize: 18, fontWeight: "900" },
  payout: { color: COLORS.neonGreen, fontSize: 14, fontWeight: "800" },
  progress: { marginBottom: 8 },
  progressTxt: { color: COLORS.textMuted, fontSize: 11.5, fontWeight: "600" },
  leg: {
    flexDirection: "row", alignItems: "center",
    paddingVertical: 6, borderTopWidth: 1, borderTopColor: COLORS.borderDefault,
  },
  legSport: { color: COLORS.textMuted, fontSize: 10, fontWeight: "700", letterSpacing: 0.5 },
  legMarket: { color: COLORS.textPrimary, fontSize: 12.5, fontWeight: "600", marginTop: 1 },
  legOdds: { color: COLORS.textPrimary, fontSize: 12, fontWeight: "800" },
});
