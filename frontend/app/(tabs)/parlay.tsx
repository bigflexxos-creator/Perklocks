import React, { useCallback, useEffect, useState } from "react";
import {
  View, Text, StyleSheet, ScrollView, ActivityIndicator,
  RefreshControl, Pressable,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { Ionicons } from "@expo/vector-icons";
import { useRouter } from "expo-router";
import { COLORS, GRADE_COLORS } from "@/src/theme";
import { api, Pick, LineType } from "@/src/lib/api";
import { LineTypeToggle } from "@/src/components/LineTypeToggle";

export default function ParlayScreen() {
  const router = useRouter();
  const [data, setData] = useState<any>(null);
  const [mode, setMode] = useState<"standard" | "high_risk">("standard");
  const [legs, setLegs] = useState(3);
  const [sport, setSport] = useState<string>("mix");
  const [excludedSports, setExcludedSports] = useState<string[]>([]);
  const [lineType, setLineType] = useState<LineType>("both");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async (n: number, m: "standard" | "high_risk", s: string, lt: LineType, excl: string[]) => {
    try {
      const res = await api.parlay(n, m, s, lt, excl);
      setData(res);
    } catch (e) {
      console.warn("parlay load", e);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => { setLoading(true); load(legs, mode, sport, lineType, excludedSports); }, [legs, mode, sport, lineType, excludedSports, load]);

  const onModeChange = (m: "standard" | "high_risk") => {
    setMode(m);
    // Reset to a sensible default when switching modes.
    setLegs(m === "high_risk" ? 10 : 3);
  };

  const isHighRisk = mode === "high_risk";
  const legOptions = isHighRisk ? [10, 15, 20] : [2, 3, 4, 5];
  const accentColor = isHighRisk ? COLORS.electricBlaze : COLORS.goldElite;
  const SPORT_OPTIONS = [
    { id: "mix", label: "MIX" },
    { id: "MLB", label: "MLB" },
    { id: "NBA", label: "NBA" },
    { id: "WNBA", label: "WNBA" },
    { id: "NFL", label: "NFL" },
    { id: "Soccer", label: "SOCCER" },
    { id: "Tennis", label: "TENNIS" },
    { id: "UFC", label: "UFC" },
    { id: "KBO", label: "KBO" },
  ];

  return (
    <SafeAreaView style={styles.safe} edges={["top"]}>
      <View style={styles.header}>
        <View>
          <Text style={styles.brand}>AUTO PARLAY</Text>
          <Text style={[styles.tag, { color: accentColor }]}>
            {isHighRisk ? "HIGH RISK · LOCK 90+ LONGSHOT" : "ELITE LOCK PICKS · COMBINED"}
          </Text>
        </View>
        <Ionicons name={isHighRisk ? "flame" : "layers"} size={28} color={accentColor} />
      </View>

      <View style={styles.modeRow}>
        <Pressable
          testID="parlay-mode-standard"
          onPress={() => onModeChange("standard")}
          style={[styles.modeBtn, !isHighRisk && styles.modeBtnActive]}
        >
          <Text style={[styles.modeText, !isHighRisk && styles.modeTextActive]}>STANDARD</Text>
        </Pressable>
        <Pressable
          testID="parlay-mode-high-risk"
          onPress={() => onModeChange("high_risk")}
          style={[styles.modeBtn, isHighRisk && styles.modeBtnHighRiskActive]}
        >
          <Ionicons name="flame" size={12} color={isHighRisk ? COLORS.bg : COLORS.electricBlaze} />
          <Text style={[styles.modeText, isHighRisk && styles.modeTextHighRiskActive]}>HIGH RISK</Text>
        </Pressable>
      </View>

      <View style={styles.legSelector}>
        <Text style={styles.legLabel}>LEGS</Text>
        {legOptions.map((n) => (
          <Pressable
            key={n}
            testID={`parlay-legs-${n}`}
            onPress={() => setLegs(n)}
            style={[styles.legChip, legs === n && styles.legChipActive]}
          >
            <Text style={[styles.legChipText, legs === n && styles.legChipTextActive]}>{n}</Text>
          </Pressable>
        ))}
      </View>

      <View style={styles.sportRowWrap}>
        <Text style={styles.legLabel}>SPORT</Text>
        <ScrollView
          horizontal
          showsHorizontalScrollIndicator={false}
          contentContainerStyle={styles.sportRow}
        >
          {SPORT_OPTIONS.map((opt) => {
            const active = sport === opt.id;
            const isMix = opt.id === "mix";
            return (
              <Pressable
                key={opt.id}
                testID={`parlay-sport-${opt.id}`}
                onPress={() => setSport(opt.id)}
                style={[
                  styles.sportChip,
                  active && (isMix ? styles.sportChipMixActive : styles.sportChipActive),
                ]}
              >
                {isMix && (
                  <Ionicons
                    name="shuffle"
                    size={11}
                    color={active ? COLORS.bg : COLORS.textSecondary}
                    style={{ marginRight: 4 }}
                  />
                )}
                <Text
                  style={[
                    styles.sportChipText,
                    active && styles.sportChipTextActive,
                  ]}
                >
                  {opt.label}
                </Text>
              </Pressable>
            );
          })}
        </ScrollView>
      </View>

      {sport === "mix" && (
        <View style={styles.excludeRowWrap}>
          <Text style={styles.legLabel}>EXCLUDE</Text>
          <ScrollView
            horizontal
            showsHorizontalScrollIndicator={false}
            contentContainerStyle={styles.sportRow}
          >
            {SPORT_OPTIONS.filter((o) => o.id !== "mix").map((opt) => {
              const excluded = excludedSports.includes(opt.id);
              return (
                <Pressable
                  key={`excl-${opt.id}`}
                  testID={`parlay-exclude-${opt.id}`}
                  onPress={() =>
                    setExcludedSports((prev) =>
                      excluded
                        ? prev.filter((s) => s !== opt.id)
                        : [...prev, opt.id],
                    )
                  }
                  style={[styles.excludeChip, excluded && styles.excludeChipActive]}
                >
                  {excluded && (
                    <Ionicons
                      name="close-circle"
                      size={12}
                      color={COLORS.bg}
                      style={{ marginRight: 4 }}
                    />
                  )}
                  <Text
                    style={[
                      styles.excludeChipText,
                      excluded && styles.excludeChipTextActive,
                    ]}
                  >
                    {opt.label}
                  </Text>
                </Pressable>
              );
            })}
            {excludedSports.length > 0 && (
              <Pressable
                onPress={() => setExcludedSports([])}
                style={styles.clearExcludeBtn}
                testID="parlay-exclude-clear"
              >
                <Text style={styles.clearExcludeText}>CLEAR</Text>
              </Pressable>
            )}
          </ScrollView>
        </View>
      )}

      <LineTypeToggle value={lineType} onChange={setLineType} testIDPrefix="parlay-line" />

      <ScrollView
        contentContainerStyle={styles.content}
        refreshControl={<RefreshControl tintColor={COLORS.textPrimary} refreshing={refreshing} onRefresh={() => { setRefreshing(true); load(legs, mode, sport, lineType, excludedSports); }} />}
        showsVerticalScrollIndicator={false}
      >
        {loading ? (
          <View style={styles.center}><ActivityIndicator color={COLORS.voltBlue} /></View>
        ) : !data?.parlay ? (
          <View style={styles.center}>
            <Ionicons name="layers-outline" size={48} color={COLORS.textMuted} />
            <Text style={styles.emptyTitle}>No parlay available</Text>
            <Text style={styles.emptyMsg}>{data?.reason || "Not enough Lock 90+ picks today."}</Text>
          </View>
        ) : (
          <>
            <View style={styles.summaryCard}>
              <Text style={styles.sectionLabel}>COMBINED PAYOUT</Text>
              <View style={styles.payoutRow}>
                <View style={{ flex: 1 }}>
                  <Text style={styles.bigOdds}>{data.parlay.combined_american_odds}</Text>
                  <Text style={styles.smallLabel}>parlay odds</Text>
                </View>
                <View style={styles.divider} />
                <View style={{ flex: 1, alignItems: "flex-end" }}>
                  <Text style={styles.bigPayout}>${data.parlay.payout_on_100}</Text>
                  <Text style={styles.smallLabel}>on $100 stake</Text>
                </View>
              </View>
              <View style={styles.statsGrid}>
                <Stat label="LEGS" value={String(data.parlay.leg_count)} />
                <Stat label="PROFIT" value={`+$${data.parlay.profit_on_100}`} color={COLORS.neonGreen} />
                <Stat label="MODEL WIN %" value={`${data.parlay.combined_win_probability}%`} />
              </View>
            </View>

            {data.parlay.leg_count < legs && (
              <View style={styles.notice}>
                <Ionicons name="information-circle" size={14} color={COLORS.goldElite} />
                <Text style={styles.noticeText}>
                  Only {data.parlay.leg_count} Lock 90+ pick{data.parlay.leg_count === 1 ? "" : "s"} qualify today — showing best available.
                </Text>
              </View>
            )}

            <Text style={styles.sectionLabel}>PARLAY LEGS</Text>
            {data.parlay.legs.map((leg: Pick, idx: number) => {
              const gradeColor = GRADE_COLORS[leg.grade] || COLORS.textMuted;
              return (
                <Pressable
                  key={leg.id}
                  testID={`parlay-leg-${idx}`}
                  onPress={() => router.push(`/pick/${leg.id}`)}
                  style={({ pressed }) => [styles.legCard, pressed && { opacity: 0.85 }]}
                >
                  <View style={styles.legNum}><Text style={styles.legNumText}>{idx + 1}</Text></View>
                  <View style={{ flex: 1 }}>
                    <Text style={styles.legSport}>{leg.sport.toUpperCase()} · {leg.league}</Text>
                    <Text style={styles.legEvent} numberOfLines={1}>{leg.event}</Text>
                    <Text style={styles.legMarket} numberOfLines={2}>{leg.market}</Text>
                    <View style={styles.legMeta}>
                      <Text style={[styles.legLock, { color: gradeColor }]}>Lock {leg.lock_score}</Text>
                      <Text style={styles.legOdds}>
                        {leg.book_odds > 0 ? `+${leg.book_odds}` : leg.book_odds}
                      </Text>
                    </View>
                  </View>
                </Pressable>
              );
            })}

            <Text style={styles.disclaimer}>
              Auto-built from today&apos;s highest-rated picks. All legs must hit for the parlay to pay.
              Parlay variance is significant — each added leg multiplies risk. Bet responsibly.
            </Text>
          </>
        )}
      </ScrollView>
    </SafeAreaView>
  );
}

function Stat({ label, value, color = COLORS.textPrimary }: { label: string; value: string; color?: string }) {
  return (
    <View style={styles.statCell}>
      <Text style={styles.statLabel}>{label}</Text>
      <Text style={[styles.statValue, { color }]}>{value}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: COLORS.bg },
  header: { paddingHorizontal: 20, paddingTop: 8, paddingBottom: 14, flexDirection: "row", justifyContent: "space-between", alignItems: "flex-end" },
  brand: { fontSize: 22, fontWeight: "900", color: COLORS.textPrimary, letterSpacing: 3 },
  tag: { fontSize: 10, color: COLORS.goldElite, fontWeight: "800", letterSpacing: 1.8, marginTop: 4 },
  modeRow: {
    flexDirection: "row", gap: 8, paddingHorizontal: 20, paddingBottom: 10,
  },
  modeBtn: {
    flex: 1, flexDirection: "row", alignItems: "center", justifyContent: "center", gap: 6,
    paddingVertical: 9, borderRadius: 10,
    borderWidth: 1, borderColor: COLORS.borderDefault,
  },
  modeBtnActive: { backgroundColor: COLORS.goldElite, borderColor: COLORS.goldElite },
  modeBtnHighRiskActive: { backgroundColor: COLORS.electricBlaze, borderColor: COLORS.electricBlaze },
  modeText: { color: COLORS.textSecondary, fontSize: 11, fontWeight: "900", letterSpacing: 1.4 },
  modeTextActive: { color: COLORS.bg },
  modeTextHighRiskActive: { color: COLORS.bg },
  legSelector: { flexDirection: "row", alignItems: "center", paddingHorizontal: 20, gap: 8, paddingBottom: 8 },
  legLabel: { color: COLORS.textMuted, fontSize: 10, fontWeight: "800", letterSpacing: 1.3, marginRight: 4 },
  legChip: { width: 40, height: 36, borderRadius: 18, borderWidth: 1, borderColor: COLORS.borderDefault, alignItems: "center", justifyContent: "center" },
  legChipActive: { backgroundColor: COLORS.textPrimary, borderColor: COLORS.textPrimary },
  legChipText: { color: COLORS.textSecondary, fontWeight: "800" },
  legChipTextActive: { color: COLORS.bg, fontWeight: "900" },
  sportRowWrap: { flexDirection: "row", alignItems: "center", paddingHorizontal: 20, paddingBottom: 12, gap: 8 },
  sportRow: { gap: 6, paddingRight: 20 },
  sportChip: {
    flexDirection: "row", alignItems: "center",
    paddingHorizontal: 12, height: 30, borderRadius: 15,
    borderWidth: 1, borderColor: COLORS.borderDefault,
    backgroundColor: "transparent",
  },
  sportChipActive: { backgroundColor: COLORS.textPrimary, borderColor: COLORS.textPrimary },
  sportChipMixActive: { backgroundColor: COLORS.voltBlue, borderColor: COLORS.voltBlue },
  sportChipText: { color: COLORS.textSecondary, fontSize: 11, fontWeight: "800", letterSpacing: 1 },
  sportChipTextActive: { color: COLORS.bg, fontWeight: "900" },
  excludeRowWrap: { flexDirection: "row", alignItems: "center", paddingHorizontal: 20, paddingBottom: 10, gap: 8 },
  excludeChip: {
    flexDirection: "row", alignItems: "center",
    paddingHorizontal: 11, height: 28, borderRadius: 14,
    borderWidth: 1, borderColor: COLORS.borderDefault,
    backgroundColor: "transparent",
  },
  excludeChipActive: { backgroundColor: COLORS.electricBlaze, borderColor: COLORS.electricBlaze },
  excludeChipText: { color: COLORS.textSecondary, fontSize: 10, fontWeight: "800", letterSpacing: 0.8 },
  excludeChipTextActive: { color: COLORS.bg, fontWeight: "900" },
  clearExcludeBtn: { paddingHorizontal: 10, alignSelf: "center" },
  clearExcludeText: { color: COLORS.textMuted, fontSize: 10, fontWeight: "800", letterSpacing: 1.2 },
  content: { paddingHorizontal: 20, paddingTop: 8, paddingBottom: 30 },
  center: { paddingVertical: 80, alignItems: "center" },
  emptyTitle: { color: COLORS.textPrimary, fontSize: 16, fontWeight: "800", marginTop: 14 },
  emptyMsg: { color: COLORS.textMuted, fontSize: 13, marginTop: 6, textAlign: "center", paddingHorizontal: 30 },
  summaryCard: { backgroundColor: COLORS.surface, borderRadius: 20, padding: 22, borderWidth: 1, borderColor: COLORS.goldElite, marginBottom: 18 },
  payoutRow: { flexDirection: "row", alignItems: "center", marginVertical: 12 },
  divider: { width: 1, height: 60, backgroundColor: COLORS.borderDefault, marginHorizontal: 16 },
  bigOdds: { fontSize: 36, fontWeight: "900", color: COLORS.goldElite, letterSpacing: -1 },
  bigPayout: { fontSize: 36, fontWeight: "900", color: COLORS.neonGreen, letterSpacing: -1 },
  smallLabel: { color: COLORS.textMuted, fontSize: 10, fontWeight: "800", letterSpacing: 1.3, marginTop: 2 },
  statsGrid: { flexDirection: "row", marginTop: 8, gap: 12 },
  statCell: { flex: 1 },
  statLabel: { color: COLORS.textMuted, fontSize: 9, fontWeight: "800", letterSpacing: 1.3 },
  statValue: { fontSize: 18, fontWeight: "900", marginTop: 4, letterSpacing: -0.3 },
  sectionLabel: { color: COLORS.textMuted, fontSize: 10, fontWeight: "800", letterSpacing: 1.6, marginBottom: 10, marginTop: 6 },
  legCard: { flexDirection: "row", gap: 14, padding: 14, marginBottom: 10, backgroundColor: COLORS.surface, borderRadius: 14, borderWidth: 1, borderColor: COLORS.borderDefault },
  legNum: { width: 32, height: 32, borderRadius: 16, backgroundColor: COLORS.goldElite, alignItems: "center", justifyContent: "center" },
  legNumText: { color: COLORS.bg, fontWeight: "900", fontSize: 14 },
  legSport: { color: COLORS.textMuted, fontSize: 10, fontWeight: "800", letterSpacing: 1.2 },
  legEvent: { color: COLORS.textSecondary, fontSize: 12, fontWeight: "600", marginTop: 2 },
  legMarket: { color: COLORS.textPrimary, fontSize: 15, fontWeight: "800", marginTop: 4, letterSpacing: -0.2 },
  legMeta: { flexDirection: "row", justifyContent: "space-between", marginTop: 8 },
  legLock: { fontSize: 11, fontWeight: "900", letterSpacing: 1 },
  legOdds: { color: COLORS.textPrimary, fontSize: 13, fontWeight: "900" },
  disclaimer: { color: COLORS.textMuted, fontSize: 11, lineHeight: 17, marginTop: 18, textAlign: "center" },
  notice: { flexDirection: "row", alignItems: "center", gap: 8, paddingHorizontal: 12, paddingVertical: 10, marginBottom: 14, borderRadius: 12, backgroundColor: "rgba(255,215,0,0.08)", borderWidth: 1, borderColor: "rgba(255,215,0,0.25)" },
  noticeText: { flex: 1, color: COLORS.textSecondary, fontSize: 12, lineHeight: 17 },
});
