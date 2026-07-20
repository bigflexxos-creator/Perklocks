import React, { useRef, useState } from "react";
import { View, Text, StyleSheet, ScrollView, Pressable, Alert, Modal } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { Ionicons } from "@expo/vector-icons";
import { Stack, useRouter } from "expo-router";
import { COLORS } from "@/src/theme";
import { useBetSlip, computeParlay, MAX_SLIP_SIZE } from "@/src/contexts/BetSlipContext";
import { Pick, api } from "@/src/lib/api";
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
  // 2026-07-21 — inline modal instead of Alert.alert. Alert with buttons
  // is unreliable on React Native Web: on the /slip screen, tapping
  // CLEAR fired the Alert but the "Clear" button in the Alert popup
  // silently no-op'd (native-only handler). Modal works everywhere.
  const [clearConfirm, setClearConfirm] = useState(false);

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

  // ── Track the whole slip as a parlay in My Bets (2026-07-21) ──────
  // Sends bet_type=parlay + all pick_ids to /user/bets/track. Server
  // computes combined odds automatically. Prompts for stake first via
  // an Alert on native; native prompt-with-buttons is unreliable on
  // web so on web we just use 1u default (users can edit in My Bets
  // by untrack+retrack if needed).
  const onTrackToMyBets = async () => {
    if (slip.count === 0 || busy) return;
    if (slip.count < 2) {
      Alert.alert("Need ≥ 2 legs", "Parlays require at least 2 picks.");
      return;
    }
    setBusy(true);
    try {
      const legIds = slip.picks.map((p) => p.id);
      await api.trackBet({
        pick_id: legIds[0],
        bet_type: "parlay",
        stake_units: 1.0,
        parlay_legs: legIds,
      });
      // Auto-clear the slip on success — user has already committed
      // the parlay to My Bets, so keeping the same picks in the slip
      // would only invite double-tracking. Feedback via router push
      // so the user lands directly on My Bets and sees the new entry.
      slip.clear();
      router.replace("/(tabs)/my-bets");
    } catch (e: any) {
      Alert.alert("Track failed", String(e?.message || e));
    } finally {
      setBusy(false);
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
            onPress={() => slip.count > 0 && setClearConfirm(true)}
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

            {/* ── Track parlay to My Bets (2026-07-21) ────────────────
                Only visible when the slip has ≥ 2 legs (a parlay). Logs
                the whole slip as bet_type=parlay so it auto-grades in
                My Bets when all legs settle. */}
            {slip.count >= 2 && (
              <Pressable
                onPress={onTrackToMyBets}
                disabled={busy}
                testID="slip-track-mybets"
                style={({ pressed }) => [
                  styles.trackAllBtn,
                  (pressed || busy) && { opacity: 0.65 },
                ]}
              >
                <Ionicons name="wallet" size={16} color={COLORS.bg} />
                <Text style={styles.trackAllTxt}>
                  TRACK {slip.count}-LEG PARLAY IN MY BETS
                </Text>
              </Pressable>
            )}
            <Text style={styles.helper}>
              SHARE opens the system share sheet (Gambly, Messages, more) with a PNG of this slip.
              Structured text is also copied to your clipboard so any target can paste it.
            </Text>
          </>
        )}
      </ScrollView>

      {/* ── Clear Slip confirmation (2026-07-21) ────────────────────
          Web Alert with buttons was silently no-op'ing the Clear
          action; Modal renders identically on iOS / Android / web. */}
      <Modal
        visible={clearConfirm}
        transparent
        animationType="fade"
        onRequestClose={() => setClearConfirm(false)}
      >
        <Pressable style={styles.modalBackdrop} onPress={() => setClearConfirm(false)}>
          <Pressable style={styles.modalSheet} onPress={(e) => e.stopPropagation()}>
            <Text style={styles.modalTitle}>Clear Slip?</Text>
            <Text style={styles.modalMeta}>
              Remove all {slip.count} pick{slip.count === 1 ? "" : "s"} from your slip? This can&apos;t be undone.
            </Text>
            <View style={styles.confirmRow}>
              <Pressable style={styles.cancelBtn} onPress={() => setClearConfirm(false)}>
                <Text style={styles.cancelBtnText}>Keep Picks</Text>
              </Pressable>
              <Pressable
                style={styles.destructiveBtn}
                onPress={() => {
                  slip.clear();
                  setClearConfirm(false);
                }}
              >
                <Text style={styles.destructiveBtnText}>Clear All</Text>
              </Pressable>
            </View>
          </Pressable>
        </Pressable>
      </Modal>
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
  // ── Track parlay in My Bets ─ full-width button below share row
  trackAllBtn: {
    marginTop: 10, paddingVertical: 14, paddingHorizontal: 16,
    borderRadius: 10, alignItems: "center",
    flexDirection: "row", justifyContent: "center", gap: 8,
    backgroundColor: COLORS.neonGreen,
  },
  trackAllTxt: {
    color: COLORS.bg, fontSize: 13, fontWeight: "900", letterSpacing: 1.3,
  },
  // ── Clear-slip confirmation modal styles ──────────────────────────
  modalBackdrop: {
    flex: 1, backgroundColor: "rgba(0,0,0,0.75)",
    alignItems: "center", justifyContent: "center", padding: 24,
  },
  modalSheet: {
    width: "100%", maxWidth: 400,
    backgroundColor: "#111", borderRadius: 16,
    borderWidth: 1, borderColor: COLORS.borderDefault,
    padding: 20,
  },
  modalTitle: {
    color: COLORS.textPrimary, fontSize: 18, fontWeight: "900",
    letterSpacing: 0.3,
  },
  modalMeta: { color: COLORS.textMuted, fontSize: 13, marginTop: 8, lineHeight: 18 },
  confirmRow: { flexDirection: "row", gap: 12, marginTop: 20, alignItems: "center" },
  cancelBtn: { flex: 1, paddingVertical: 12, alignItems: "center" },
  cancelBtnText: { color: COLORS.textMuted, fontSize: 13, fontWeight: "700" },
  destructiveBtn: {
    flex: 1, paddingVertical: 12, borderRadius: 8, alignItems: "center",
    backgroundColor: COLORS.electricBlaze,
  },
  destructiveBtnText: {
    color: "#000", fontSize: 13, fontWeight: "900", letterSpacing: 1.1,
  },
  bookBtn: { flex: 1, minWidth: 90, flexDirection: "row", alignItems: "center", justifyContent: "center",
    gap: 6, paddingVertical: 12, backgroundColor: COLORS.surface, borderRadius: 10,
    borderWidth: 1, borderColor: COLORS.voltBlue },
  bookText: { color: COLORS.textPrimary, fontWeight: "900", fontSize: 12, letterSpacing: 0.5 },
  helper: { color: COLORS.textMuted, fontSize: 11, lineHeight: 16, marginTop: 6, textAlign: "center" },
});
