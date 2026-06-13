import React from "react";
import { View, Text, StyleSheet, ScrollView, Pressable, Alert, Share, Platform } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { Ionicons } from "@expo/vector-icons";
import { Stack, useRouter } from "expo-router";
import { COLORS } from "@/src/theme";
import { useBetSlip, computeParlay, MAX_SLIP_SIZE } from "@/src/contexts/BetSlipContext";
import { Pick } from "@/src/lib/api";
import { SPORTSBOOKS, dominantSport, openSportsbookWithSlip } from "@/src/utils/sportsbook";

function buildShareText(picks: Pick[]): string {
  const parlay = computeParlay(picks);
  const header = `\ud83d\udd12 PerksLocks \u00b7 ${parlay.legCount}-Leg Parlay\nCombined: ${parlay.americanOdds}  \u00b7  $${parlay.payoutOn100.toFixed(0)} on $100\n`;
  const legs = picks
    .map((p, i) => {
      const odds = p.book_odds > 0 ? `+${p.book_odds}` : `${p.book_odds}`;
      return `${i + 1}. ${p.sport} \u00b7 ${p.market} (${odds})\n   ${p.event}`;
    })
    .join("\n");
  return `${header}\n${legs}\n\nLock scores avg: ${Math.round(
    picks.reduce((s, p) => s + p.lock_score, 0) / picks.length,
  )}`;
}

export default function SlipScreen() {
  const router = useRouter();
  const slip = useBetSlip();
  const parlay = computeParlay(slip.picks);
  const sport = dominantSport(slip.picks);

  const handleShare = async () => {
    if (slip.count === 0) return;
    const message = buildShareText(slip.picks);
    try {
      if (Platform.OS === "web") {
        // Try the Web Share API, fall back to clipboard.
        if (typeof (globalThis as any).navigator !== "undefined" && (globalThis as any).navigator.share) {
          await (globalThis as any).navigator.share({ title: "My PerksLocks Slip", text: message });
        } else if (typeof (globalThis as any).navigator !== "undefined" && (globalThis as any).navigator.clipboard) {
          await (globalThis as any).navigator.clipboard.writeText(message);
          Alert.alert("Copied!", "Slip copied to clipboard.");
        }
      } else {
        await Share.share({ message, title: "My PerksLocks Slip" });
      }
    } catch (e) {
      console.warn("share failed", e);
    }
  };

  return (
    <SafeAreaView style={styles.safe} edges={["top"]}>
      <Stack.Screen options={{ headerShown: false }} />
      <View style={styles.header}>
        <Pressable onPress={() => router.back()} hitSlop={10} style={styles.backBtn}>
          <Ionicons name="arrow-back" size={24} color={COLORS.textPrimary} />
        </Pressable>
        <Text style={styles.title}>MY BET SLIP</Text>
        <View style={styles.headerActions}>
          <Pressable
            onPress={handleShare}
            disabled={slip.count === 0}
            hitSlop={10}
            testID="slip-share"
          >
            <Ionicons
              name="share-outline"
              size={20}
              color={slip.count === 0 ? COLORS.textMuted : COLORS.textPrimary}
              style={{ opacity: slip.count === 0 ? 0.3 : 1 }}
            />
          </Pressable>
          <Pressable
            onPress={() => slip.count > 0 && Alert.alert("Clear slip?", "Remove all picks?", [
              { text: "Cancel", style: "cancel" },
              { text: "Clear", style: "destructive", onPress: () => slip.clear() },
            ])}
            hitSlop={10}
            testID="slip-clear"
          >
            <Text style={[styles.clearBtn, !slip.count && { opacity: 0.3 }]}>CLEAR</Text>
          </Pressable>
        </View>
      </View>

      <ScrollView contentContainerStyle={styles.content} showsVerticalScrollIndicator={false}>
        {slip.count === 0 ? (
          <View style={styles.empty}>
            <Ionicons name="receipt-outline" size={48} color={COLORS.textMuted} />
            <Text style={styles.emptyTitle}>Slip is empty</Text>
            <Text style={styles.emptyMsg}>
              Tap the + button on any pick to add it. Up to {MAX_SLIP_SIZE} picks.
            </Text>
          </View>
        ) : (
          <>
            <View style={styles.parlayCard}>
              <Text style={styles.parlayLabel}>{parlay.legCount}-LEG PARLAY · COMBINED</Text>
              <View style={styles.parlayRow}>
                <View style={{ flex: 1 }}>
                  <Text style={styles.parlayOdds}>{parlay.americanOdds}</Text>
                  <Text style={styles.parlaySub}>parlay odds</Text>
                </View>
                <View style={styles.divider} />
                <View style={{ flex: 1, alignItems: "flex-end" }}>
                  <Text style={styles.parlayPayout}>${parlay.payoutOn100.toFixed(0)}</Text>
                  <Text style={styles.parlaySub}>on $100 stake</Text>
                </View>
              </View>
              <Text style={styles.profitNote}>
                Profit: +${parlay.profitOn100.toFixed(2)} if all {parlay.legCount} legs hit
              </Text>
            </View>

            <Text style={styles.section}>PICKS ({slip.count} / {MAX_SLIP_SIZE})</Text>
            {slip.picks.map((p, i) => (
              <View key={p.id} style={styles.leg}>
                <View style={styles.legNum}>
                  <Text style={styles.legNumText}>{i + 1}</Text>
                </View>
                <View style={{ flex: 1 }}>
                  <Text style={styles.legSport}>{p.sport} · Lock {Math.round(p.lock_score)}</Text>
                  <Text style={styles.legMarket}>{p.market}</Text>
                  <Text style={styles.legEvent}>{p.event}</Text>
                </View>
                <View style={{ alignItems: "flex-end" }}>
                  <Text style={styles.legOdds}>{p.book_odds > 0 ? `+${p.book_odds}` : p.book_odds}</Text>
                  <Pressable onPress={() => slip.removePick(p.id)} hitSlop={8} style={{ marginTop: 6 }}>
                    <Ionicons name="trash-outline" size={16} color={COLORS.electricBlaze} />
                  </Pressable>
                </View>
              </View>
            ))}

            <Text style={[styles.section, { marginTop: 18 }]}>
              {sport ? `OPEN ${sport.toUpperCase()} ON` : "BUILD THIS PARLAY ON"}
            </Text>
            <View style={styles.bookGrid}>
              {SPORTSBOOKS.map((book) => {
                return (
                  <Pressable
                    key={book}
                    onPress={() => openSportsbookWithSlip(book, slip.picks)}
                    style={({ pressed }) => [styles.bookBtn, pressed && { opacity: 0.7 }]}
                    testID={`slip-book-${book}`}
                  >
                    <Ionicons name="open-outline" size={14} color={COLORS.textPrimary} />
                    <Text style={styles.bookText}>{book}</Text>
                  </Pressable>
                );
              })}
            </View>
            <Text style={styles.helper}>
              {sport
                ? `Opens ${sport} on the sportsbook. Add each leg using the matchup & line above.`
                : "Mixed-sport slip — opens the sportsbook home. Add each leg using the matchup & line above."}
            </Text>
          </>
        )}
      </ScrollView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: COLORS.bg },
  header: { paddingHorizontal: 20, paddingTop: 8, paddingBottom: 14,
    flexDirection: "row", justifyContent: "space-between", alignItems: "center" },
  backBtn: { width: 30 },
  title: { fontSize: 18, fontWeight: "900", color: COLORS.textPrimary, letterSpacing: 2 },
  clearBtn: { fontSize: 11, fontWeight: "900", color: COLORS.electricBlaze, letterSpacing: 1.4 },
  headerActions: { flexDirection: "row", alignItems: "center", gap: 14 },
  content: { paddingHorizontal: 20, paddingBottom: 30 },
  empty: { paddingVertical: 80, alignItems: "center" },
  emptyTitle: { color: COLORS.textPrimary, fontSize: 16, fontWeight: "800", marginTop: 14 },
  emptyMsg: { color: COLORS.textMuted, fontSize: 13, marginTop: 6, textAlign: "center", paddingHorizontal: 30 },

  parlayCard: {
    backgroundColor: COLORS.surface, borderRadius: 14, padding: 18,
    borderWidth: 2, borderColor: COLORS.goldElite, marginBottom: 16,
  },
  parlayLabel: { fontSize: 10, color: COLORS.goldElite, fontWeight: "900", letterSpacing: 1.6, marginBottom: 14 },
  parlayRow: { flexDirection: "row", alignItems: "center" },
  parlayOdds: { fontSize: 28, fontWeight: "900", color: COLORS.textPrimary },
  parlayPayout: { fontSize: 24, fontWeight: "900", color: COLORS.neonGreen },
  parlaySub: { fontSize: 10, color: COLORS.textMuted, fontWeight: "700", letterSpacing: 1, marginTop: 4 },
  divider: { width: 1, height: 40, backgroundColor: COLORS.borderDefault, marginHorizontal: 12 },
  profitNote: { fontSize: 11, color: COLORS.neonGreen, fontWeight: "700", marginTop: 14 },

  section: { color: COLORS.textMuted, fontSize: 11, fontWeight: "800", letterSpacing: 1.4, marginBottom: 10 },
  leg: { flexDirection: "row", backgroundColor: COLORS.surface, borderRadius: 12, padding: 12, marginBottom: 8,
    borderWidth: 1, borderColor: COLORS.borderDefault, alignItems: "center" },
  legNum: { width: 26, height: 26, borderRadius: 13, backgroundColor: COLORS.bg, alignItems: "center", justifyContent: "center", marginRight: 12 },
  legNumText: { color: COLORS.goldElite, fontSize: 12, fontWeight: "900" },
  legSport: { color: COLORS.textSecondary, fontSize: 10, fontWeight: "800", letterSpacing: 1, marginBottom: 2 },
  legMarket: { color: COLORS.textPrimary, fontSize: 13, fontWeight: "800" },
  legEvent: { color: COLORS.textMuted, fontSize: 11, marginTop: 2 },
  legOdds: { color: COLORS.textPrimary, fontSize: 14, fontWeight: "900" },

  bookGrid: { flexDirection: "row", gap: 8, flexWrap: "wrap", marginBottom: 8 },
  bookBtn: { flex: 1, minWidth: 90, flexDirection: "row", alignItems: "center", justifyContent: "center",
    gap: 6, paddingVertical: 12, backgroundColor: COLORS.surface, borderRadius: 10,
    borderWidth: 1, borderColor: COLORS.voltBlue },
  bookText: { color: COLORS.textPrimary, fontWeight: "900", fontSize: 12, letterSpacing: 0.5 },
  helper: { color: COLORS.textMuted, fontSize: 11, lineHeight: 16, marginTop: 6, textAlign: "center" },
});
