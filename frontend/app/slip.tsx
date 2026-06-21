import React, { useRef, useState } from "react";
import { View, Text, StyleSheet, ScrollView, Pressable, Alert } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { Ionicons } from "@expo/vector-icons";
import { Stack, useRouter } from "expo-router";
import { COLORS } from "@/src/theme";
import { useBetSlip, computeParlay, MAX_SLIP_SIZE } from "@/src/contexts/BetSlipContext";
import { Pick } from "@/src/lib/api";
import { formatGameTime } from "@/src/lib/formatGameTime";
import { getDisplayLock, getDisplayLockRounded } from "@/src/lib/lockScore";
import { buildSlipText, shareSlip, saveSlipImage, copySlipText } from "@/src/lib/shareBetSlip";

function buildSharePayload(picks: Pick[]) {
  return {
    legs: picks.map((p) => ({
      sport: p.sport,
      league: (p as any).league,
      event: p.event,
      market: p.market,
      selection: p.market,
      book_odds: p.book_odds,
      bookmaker: (p as any).bookmaker || (p as any).book,
      confidence: getDisplayLock(p),
    })),
    combined_odds: computeParlay(picks).americanOdds,
    generated_at: new Date().toISOString(),
  };
}

export default function SlipScreen() {
  const router = useRouter();
  const slip = useBetSlip();
  const parlay = computeParlay(slip.picks);
  const cardRef = useRef<View>(null);
  const [busy, setBusy] = useState(false);

  const onShare = async () => {
    if (slip.count === 0 || busy) return;
    setBusy(true);
    try {
      const text = buildSlipText(buildSharePayload(slip.picks));
      await shareSlip(cardRef, text);
    } finally { setBusy(false); }
  };

  const onCopy = async () => {
    if (slip.count === 0) return;
    const ok = await copySlipText(buildSlipText(buildSharePayload(slip.picks)));
    if (ok) Alert.alert("Copied", "Bet slip copied to clipboard.");
  };

  const onSaveImg = async () => {
    if (slip.count === 0 || busy) return;
    setBusy(true);
    try { await saveSlipImage(cardRef); } finally { setBusy(false); }
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
            <View ref={cardRef} collapsable={false}>
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
                    <Text style={styles.legSport}>{p.sport} · Lock {getDisplayLockRounded(p)}</Text>
                    <Text style={styles.legMarket}>{p.market}</Text>
                    <Text style={styles.legEvent}>{p.event}</Text>
                    {p.event_time && (
                      <Text style={styles.legTime}>{formatGameTime(p.event_time)}</Text>
                    )}
                  </View>
                  <View style={{ alignItems: "flex-end" }}>
                    <Text style={styles.legOdds}>{p.book_odds > 0 ? `+${p.book_odds}` : p.book_odds}</Text>
                    <Pressable onPress={() => slip.removePick(p.id)} hitSlop={8} style={{ marginTop: 6 }}>
                      <Ionicons name="trash-outline" size={16} color={COLORS.electricBlaze} />
                    </Pressable>
                  </View>
                </View>
              ))}
            </View>

            {/* ── Share-to-Gambly action row ─────────────────────────── */}
            <View style={styles.shareRow}>
              <Pressable
                onPress={onCopy}
                testID="slip-copy"
                style={({ pressed }) => [styles.shareBtn, { borderColor: COLORS.textMuted }, pressed && { opacity: 0.6 }]}
              >
                <Ionicons name="copy-outline" size={14} color={COLORS.textPrimary} />
                <Text style={styles.shareTxt}>COPY</Text>
              </Pressable>
              <Pressable
                onPress={onSaveImg}
                disabled={busy}
                testID="slip-save-image"
                style={({ pressed }) => [styles.shareBtn, { borderColor: COLORS.voltBlue }, (pressed || busy) && { opacity: 0.6 }]}
              >
                <Ionicons name="download-outline" size={14} color={COLORS.voltBlue} />
                <Text style={[styles.shareTxt, { color: COLORS.voltBlue }]}>SAVE IMG</Text>
              </Pressable>
              <Pressable
                onPress={onShare}
                disabled={busy}
                testID="slip-share"
                style={({ pressed }) => [styles.shareBtnPrimary, (pressed || busy) && { opacity: 0.65 }]}
              >
                <Ionicons name="share-social-outline" size={14} color={COLORS.bg} />
                <Text style={[styles.shareTxt, { color: COLORS.bg }]}>SHARE TO GAMBLY</Text>
              </Pressable>
            </View>
            <Text style={styles.helper}>
              SHARE opens the system share sheet (Gambly, Messages, more) with a PNG of this slip.
              Structured text is also copied to your clipboard so any target can paste it.
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
  legTime: { color: COLORS.voltBlue, fontSize: 10, fontWeight: "800", letterSpacing: 0.4, marginTop: 3 },
  legOdds: { color: COLORS.textPrimary, fontSize: 14, fontWeight: "900" },

  bookGrid: { flexDirection: "row", gap: 8, flexWrap: "wrap", marginBottom: 8 },
  shareRow: {
    flexDirection: "row", gap: 8, flexWrap: "wrap",
    marginTop: 18, marginBottom: 6, alignItems: "center",
  },
  shareBtn: {
    flexDirection: "row", alignItems: "center", gap: 6,
    paddingHorizontal: 12, height: 36, borderRadius: 18, borderWidth: 1.5,
  },
  shareBtnPrimary: {
    flex: 1, minWidth: 140,
    flexDirection: "row", alignItems: "center", justifyContent: "center", gap: 6,
    paddingHorizontal: 14, height: 36, borderRadius: 18,
    backgroundColor: COLORS.goldElite,
  },
  shareTxt: { color: COLORS.textPrimary, fontSize: 11, fontWeight: "900", letterSpacing: 1.2 },
  bookBtn: { flex: 1, minWidth: 90, flexDirection: "row", alignItems: "center", justifyContent: "center",
    gap: 6, paddingVertical: 12, backgroundColor: COLORS.surface, borderRadius: 10,
    borderWidth: 1, borderColor: COLORS.voltBlue },
  bookText: { color: COLORS.textPrimary, fontWeight: "900", fontSize: 12, letterSpacing: 0.5 },
  helper: { color: COLORS.textMuted, fontSize: 11, lineHeight: 16, marginTop: 6, textAlign: "center" },
});
