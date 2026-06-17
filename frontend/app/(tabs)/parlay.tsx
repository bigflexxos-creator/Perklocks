import React, { useCallback, useEffect, useMemo, useState } from "react";
import {
  View, Text, StyleSheet, ScrollView, ActivityIndicator,
  RefreshControl, Pressable,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { Ionicons } from "@expo/vector-icons";
import { useRouter } from "expo-router";
import { COLORS, GRADE_COLORS } from "@/src/theme";
import { api, Pick, LineType, PickFilters, ParlayCard } from "@/src/lib/api";
import { LineTypeToggle } from "@/src/components/LineTypeToggle";
import { SportFilterBar } from "@/src/components/SportFilterBar";

// ─── Card label → accent colour mapping ───────────────────────────────
const CARD_ACCENTS: Record<ParlayCard["label"], string> = {
  SAFE: COLORS.neonGreen,
  BALANCED: COLORS.goldElite,
  AGGRESSIVE: COLORS.electricBlaze,
};
const CARD_ICONS: Record<ParlayCard["label"], any> = {
  SAFE: "shield-checkmark",
  BALANCED: "diamond",
  AGGRESSIVE: "flame",
};
const CARD_TAGLINES: Record<ParlayCard["label"], string> = {
  SAFE: "Highest survival probability",
  BALANCED: "Best risk-reward balance",
  AGGRESSIVE: "Bigger payouts, more variance",
};

// Health grade → colour
const GRADE_TINT: Record<string, string> = {
  A: COLORS.neonGreen,
  B: COLORS.goldElite,
  C: COLORS.voltBlue,
  D: COLORS.electricBlaze,
  F: COLORS.lossRed ?? "#FF3B5C",
};

export default function ParlayScreen() {
  const router = useRouter();
  const [parlays, setParlays] = useState<ParlayCard[]>([]);
  const [reason, setReason] = useState<string>("");
  const [mode, setMode] = useState<"standard" | "high_risk">("standard");
  const [legs, setLegs] = useState(3);
  const [sport, setSport] = useState<string>("mix");
  const [excludedSports, setExcludedSports] = useState<string[]>([]);
  const [lineType, setLineType] = useState<LineType>("both");
  const [filters, setFilters] = useState<PickFilters>({});
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  // Per-card refresh cursor: SAFE=card[0], BALANCED=card[1], AGGRESSIVE=card[2]
  const [rank, setRank] = useState(1);
  // Pinned leg IDs survive refresh
  const [lockedIds, setLockedIds] = useState<string[]>([]);

  const load = useCallback(
    async (n: number, m: "standard" | "high_risk", s: string, lt: LineType,
           excl: string[], f: PickFilters, r: number, locked: string[]) => {
      try {
        const res = await api.parlay(n, m, s, lt, excl, f, r, locked);
        setParlays(res.parlays || []);
        setReason(res.reason || "");
      } catch (e) {
        console.warn("parlay load", e);
      } finally {
        setLoading(false);
        setRefreshing(false);
      }
    },
    [],
  );

  useEffect(() => {
    setLoading(true);
    load(legs, mode, sport, lineType, excludedSports, filters, rank, lockedIds);
  }, [legs, mode, sport, lineType, excludedSports, filters, rank, lockedIds, load]);

  const onModeChange = (m: "standard" | "high_risk") => {
    setMode(m);
    setLegs(m === "high_risk" ? 10 : 3);
    setRank(1);
    setLockedIds([]);
  };

  const onRefresh = () => {
    setRefreshing(true);
    // Cycle to next-best candidate (rank=2,3,4...) — when rank reaches 4 reset.
    setRank((r) => (r >= 4 ? 1 : r + 1));
  };

  const togglePin = useCallback((legId: string) => {
    setLockedIds((prev) =>
      prev.includes(legId) ? prev.filter((id) => id !== legId) : [...prev, legId],
    );
    setRank(1);  // Pinning resets rank cursor
  }, []);

  const isHighRisk = mode === "high_risk";
  const legOptions = isHighRisk ? [10, 15, 20] : [2, 3, 4, 5];
  const accentColor = isHighRisk ? COLORS.electricBlaze : COLORS.goldElite;
  const SPORT_OPTIONS = useMemo(
    () => [
      { id: "mix", label: "MIX" },
      { id: "MLB", label: "MLB" },
      { id: "NBA", label: "NBA" },
      { id: "Tennis", label: "TENNIS" },
      { id: "NFL", label: "NFL" },
      { id: "Soccer", label: "SOCCER" },
      { id: "UFC", label: "UFC" },
      { id: "KBO", label: "KBO" },
    ],
    [],
  );

  return (
    <SafeAreaView style={styles.safe} edges={["top"]}>
      <View style={styles.header}>
        <View>
          <Text style={styles.brand}>AUTO PARLAY</Text>
          <Text style={[styles.tag, { color: accentColor }]}>
            {isHighRisk ? "HIGH RISK · 10-20 LEG LOTTERY" : "OPTIMIZER V1 · SAFE / BALANCED / AGGRESSIVE"}
          </Text>
        </View>
        <Pressable
          onPress={onRefresh}
          testID="parlay-refresh"
          hitSlop={10}
          style={({ pressed }) => [
            styles.refreshBadge,
            { borderColor: accentColor, opacity: pressed ? 0.6 : 1 },
          ]}
        >
          <Ionicons name="refresh" size={14} color={accentColor} />
          <Text style={[styles.refreshTxt, { color: accentColor }]}>
            {rank === 1 ? "REFRESH" : `RANK ${rank}`}
          </Text>
        </Pressable>
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
        <Text style={styles.legLabel}>TARGET LEGS</Text>
        {legOptions.map((n) => (
          <Pressable
            key={n}
            testID={`parlay-legs-${n}`}
            onPress={() => { setLegs(n); setRank(1); }}
            style={[styles.legChip, legs === n && styles.legChipActive]}
          >
            <Text style={[styles.legChipText, legs === n && styles.legChipTextActive]}>{n}</Text>
          </Pressable>
        ))}
      </View>

      <View style={styles.sportRowWrap}>
        <Text style={styles.legLabel}>SPORT</Text>
        <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.sportRow}>
          {SPORT_OPTIONS.map((opt) => {
            const active = sport === opt.id;
            const isMix = opt.id === "mix";
            return (
              <Pressable
                key={opt.id}
                testID={`parlay-sport-${opt.id}`}
                onPress={() => { setSport(opt.id); setRank(1); }}
                style={[
                  styles.sportChip,
                  active && (isMix ? styles.sportChipMixActive : styles.sportChipActive),
                ]}
              >
                {isMix && (
                  <Ionicons name="shuffle" size={11} color={active ? COLORS.bg : COLORS.textSecondary} style={{ marginRight: 4 }} />
                )}
                <Text style={[styles.sportChipText, active && styles.sportChipTextActive]}>{opt.label}</Text>
              </Pressable>
            );
          })}
        </ScrollView>
      </View>

      {sport === "mix" && (
        <View style={styles.excludeRowWrap}>
          <Text style={styles.legLabel}>EXCLUDE</Text>
          <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.sportRow}>
            {SPORT_OPTIONS.filter((o) => o.id !== "mix").map((opt) => {
              const excluded = excludedSports.includes(opt.id);
              return (
                <Pressable
                  key={`excl-${opt.id}`}
                  testID={`parlay-exclude-${opt.id}`}
                  onPress={() => {
                    setExcludedSports((prev) =>
                      excluded ? prev.filter((s) => s !== opt.id) : [...prev, opt.id],
                    );
                    setRank(1);
                  }}
                  style={[styles.excludeChip, excluded && styles.excludeChipActive]}
                >
                  {excluded && (
                    <Ionicons name="close-circle" size={12} color={COLORS.bg} style={{ marginRight: 4 }} />
                  )}
                  <Text style={[styles.excludeChipText, excluded && styles.excludeChipTextActive]}>{opt.label}</Text>
                </Pressable>
              );
            })}
            {excludedSports.length > 0 && (
              <Pressable onPress={() => setExcludedSports([])} style={styles.clearExcludeBtn} testID="parlay-exclude-clear">
                <Text style={styles.clearExcludeText}>CLEAR</Text>
              </Pressable>
            )}
          </ScrollView>
        </View>
      )}

      <LineTypeToggle value={lineType} onChange={(v) => { setLineType(v); setRank(1); }} testIDPrefix="parlay-line" />

      {sport !== "mix" && (
        <SportFilterBar sport={sport} filters={filters} onChange={(f) => { setFilters(f); setRank(1); }} />
      )}

      {lockedIds.length > 0 && (
        <View style={styles.pinnedBar}>
          <Ionicons name="lock-closed" size={11} color={COLORS.goldElite} />
          <Text style={styles.pinnedTxt}>{lockedIds.length} pinned · survives refresh</Text>
          <Pressable onPress={() => { setLockedIds([]); setRank(1); }} hitSlop={6}>
            <Text style={styles.pinnedClear}>CLEAR</Text>
          </Pressable>
        </View>
      )}

      <ScrollView
        contentContainerStyle={styles.content}
        refreshControl={
          <RefreshControl
            tintColor={COLORS.textPrimary}
            refreshing={refreshing}
            onRefresh={onRefresh}
          />
        }
        showsVerticalScrollIndicator={false}
      >
        {loading ? (
          <View style={styles.center}><ActivityIndicator color={COLORS.voltBlue} /></View>
        ) : parlays.length === 0 ? (
          <View style={styles.center}>
            <Ionicons name="layers-outline" size={48} color={COLORS.textMuted} />
            <Text style={styles.emptyTitle}>No parlay available</Text>
            <Text style={styles.emptyMsg}>
              {reason || "Not enough qualifying picks today (need Lock≥88, Edge≥+3%, positive ROI)."}
            </Text>
          </View>
        ) : (
          <>
            <Text style={styles.intro}>
              3 optimal parlays · scored on survival, edge, ROI, correlation & stability
            </Text>
            {parlays.map((card) => (
              <ParlayCardView
                key={card.label}
                card={card}
                lockedIds={lockedIds}
                onTogglePin={togglePin}
                onLegPress={(id) => router.push(`/pick/${id}`)}
              />
            ))}
            <Text style={styles.disclaimer}>
              Optimizer V1: scored on survival, edge, ROI, correlation & stability.
              Quality stops early — no filler legs. Tap 🔒 to pin a leg across refreshes.
              Parlays are high-variance — bet responsibly.
            </Text>
          </>
        )}
      </ScrollView>
    </SafeAreaView>
  );
}

// ──────────────────────────────────────────────────────────────────────
// Parlay Card — one of 3 (SAFE / BALANCED / AGGRESSIVE)
// ──────────────────────────────────────────────────────────────────────
function ParlayCardView({
  card, lockedIds, onTogglePin, onLegPress,
}: {
  card: ParlayCard;
  lockedIds: string[];
  onTogglePin: (legId: string) => void;
  onLegPress: (id: string) => void;
}) {
  const accent = CARD_ACCENTS[card.label];
  const iconName = CARD_ICONS[card.label];
  const tagline = CARD_TAGLINES[card.label];
  const gradeColor = GRADE_TINT[card.grade] || COLORS.textPrimary;

  return (
    <View style={[styles.cardWrap, { borderColor: accent }]}>
      {/* Header */}
      <View style={styles.cardHeader}>
        <View style={{ flexDirection: "row", alignItems: "center", gap: 8, flex: 1 }}>
          <Ionicons name={iconName} size={18} color={accent} />
          <Text style={[styles.cardLabel, { color: accent }]}>{card.label}</Text>
        </View>
        <View style={[styles.gradeBadge, { borderColor: gradeColor }]}>
          <Text style={[styles.gradeText, { color: gradeColor }]}>{card.grade}</Text>
        </View>
      </View>
      <Text style={styles.cardTagline}>{tagline}</Text>

      {/* Headline payout row */}
      <View style={styles.headlineRow}>
        <View style={{ flex: 1 }}>
          <Text style={[styles.headlineNum, { color: accent }]}>{card.combined_american_odds}</Text>
          <Text style={styles.headlineLbl}>parlay odds</Text>
        </View>
        <View style={[styles.headlineDivider, { backgroundColor: accent + "33" }]} />
        <View style={{ flex: 1, alignItems: "flex-end" }}>
          <Text style={[styles.headlineNum, { color: COLORS.neonGreen }]}>
            +${card.profit_on_100.toFixed(0)}
          </Text>
          <Text style={styles.headlineLbl}>profit on $100</Text>
        </View>
      </View>

      {/* Strength score band */}
      <View style={styles.strengthBand}>
        <View style={{ flex: 1 }}>
          <Text style={styles.strengthLabel}>PARLAY STRENGTH</Text>
          <View style={styles.strengthBarBg}>
            <View
              style={[
                styles.strengthBarFg,
                { width: `${Math.min(100, card.strength_score)}%`, backgroundColor: accent },
              ]}
            />
          </View>
        </View>
        <Text style={[styles.strengthValue, { color: accent }]}>{card.strength_score.toFixed(0)}</Text>
      </View>

      {/* Stat grid */}
      <View style={styles.statGrid}>
        <MiniStat label="HIT %" value={`${card.survival_pct.toFixed(0)}%`} accent={accent} />
        <MiniStat label="EDGE" value={`${card.avg_edge_pct >= 0 ? "+" : ""}${card.avg_edge_pct.toFixed(1)}%`} accent={accent} />
        <MiniStat label="ROI" value={`${card.avg_roi_pct >= 0 ? "+" : ""}${card.avg_roi_pct.toFixed(1)}%`} accent={accent} />
        <MiniStat label="DIVERSE" value={`${card.diversification_pct.toFixed(0)}%`} accent={accent} />
        <MiniStat label="LEGS" value={String(card.leg_count)} accent={accent} />
      </View>

      {/* Why this parlay */}
      <View style={styles.reasonsBlock}>
        <Text style={styles.reasonsHeader}>WHY THIS PARLAY</Text>
        {card.reasons.map((r, i) => {
          const isPositive = r.startsWith("+");
          const isWarn = r.startsWith("!");
          const tone = isPositive
            ? COLORS.neonGreen
            : isWarn
              ? COLORS.electricBlaze
              : COLORS.textMuted;
          return (
            <Text key={i} style={[styles.reasonLine, { color: tone }]} numberOfLines={2}>
              {r}
            </Text>
          );
        })}
      </View>

      {/* Legs */}
      <Text style={styles.legsHeader}>LEGS</Text>
      {card.legs.map((leg, idx) => {
        const lockColor = GRADE_COLORS[leg.grade as any] || COLORS.textMuted;
        const pinned = lockedIds.includes(leg.id);
        return (
          <Pressable
            key={leg.id}
            testID={`parlay-leg-${card.label}-${idx}`}
            onPress={() => onLegPress(leg.id)}
            style={({ pressed }) => [
              styles.legCard,
              pinned && styles.legCardPinned,
              pressed && { opacity: 0.85 },
            ]}
          >
            <View style={[styles.legNum, { backgroundColor: accent }]}>
              <Text style={styles.legNumText}>{idx + 1}</Text>
            </View>
            <View style={{ flex: 1 }}>
              <Text style={styles.legSport}>
                {leg.sport.toUpperCase()} · {leg.league}
                {leg.elite_player ? "  ⭐" : ""}
              </Text>
              <Text style={styles.legEvent} numberOfLines={1}>{leg.event}</Text>
              <Text style={styles.legMarket} numberOfLines={2}>{leg.market}</Text>
              <View style={styles.legMeta}>
                <Text style={[styles.legLock, { color: lockColor }]}>Lock {leg.lock_score}</Text>
                <Text style={styles.legEdge}>{leg.edge_percent >= 0 ? "+" : ""}{leg.edge_percent.toFixed(1)}% edge</Text>
                <Text style={styles.legOdds}>
                  {leg.book_odds > 0 ? `+${leg.book_odds}` : leg.book_odds}
                </Text>
              </View>
            </View>
            <Pressable
              hitSlop={10}
              onPress={(e) => { e.stopPropagation?.(); onTogglePin(leg.id); }}
              testID={`parlay-pin-${card.label}-${idx}`}
              style={styles.pinBtn}
            >
              <Ionicons
                name={pinned ? "lock-closed" : "lock-open-outline"}
                size={16}
                color={pinned ? COLORS.goldElite : COLORS.textMuted}
              />
            </Pressable>
          </Pressable>
        );
      })}
    </View>
  );
}

function MiniStat({ label, value, accent }: { label: string; value: string; accent: string }) {
  return (
    <View style={styles.miniStat}>
      <Text style={styles.miniLabel}>{label}</Text>
      <Text style={[styles.miniValue, { color: accent }]}>{value}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: COLORS.bg },
  header: { paddingHorizontal: 20, paddingTop: 8, paddingBottom: 14, flexDirection: "row", justifyContent: "space-between", alignItems: "flex-end" },
  brand: { fontSize: 22, fontWeight: "900", color: COLORS.textPrimary, letterSpacing: 3 },
  tag: { fontSize: 10, color: COLORS.goldElite, fontWeight: "800", letterSpacing: 1.4, marginTop: 4 },
  refreshBadge: {
    flexDirection: "row", alignItems: "center", gap: 6,
    paddingHorizontal: 10, height: 30, borderRadius: 15, borderWidth: 1,
  },
  refreshTxt: { fontSize: 10, fontWeight: "900", letterSpacing: 1.5 },
  modeRow: { flexDirection: "row", gap: 8, paddingHorizontal: 20, paddingBottom: 10 },
  modeBtn: { flex: 1, flexDirection: "row", alignItems: "center", justifyContent: "center", gap: 6, paddingVertical: 9, borderRadius: 10, borderWidth: 1, borderColor: COLORS.borderDefault },
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
  sportChip: { flexDirection: "row", alignItems: "center", paddingHorizontal: 12, height: 30, borderRadius: 15, borderWidth: 1, borderColor: COLORS.borderDefault, backgroundColor: "transparent" },
  sportChipActive: { backgroundColor: COLORS.textPrimary, borderColor: COLORS.textPrimary },
  sportChipMixActive: { backgroundColor: COLORS.voltBlue, borderColor: COLORS.voltBlue },
  sportChipText: { color: COLORS.textSecondary, fontSize: 11, fontWeight: "800", letterSpacing: 1 },
  sportChipTextActive: { color: COLORS.bg, fontWeight: "900" },
  excludeRowWrap: { flexDirection: "row", alignItems: "center", paddingHorizontal: 20, paddingBottom: 10, gap: 8 },
  excludeChip: { flexDirection: "row", alignItems: "center", paddingHorizontal: 11, height: 28, borderRadius: 14, borderWidth: 1, borderColor: COLORS.borderDefault, backgroundColor: "transparent" },
  excludeChipActive: { backgroundColor: COLORS.electricBlaze, borderColor: COLORS.electricBlaze },
  excludeChipText: { color: COLORS.textSecondary, fontSize: 10, fontWeight: "800", letterSpacing: 0.8 },
  excludeChipTextActive: { color: COLORS.bg, fontWeight: "900" },
  clearExcludeBtn: { paddingHorizontal: 10, alignSelf: "center" },
  clearExcludeText: { color: COLORS.textMuted, fontSize: 10, fontWeight: "800", letterSpacing: 1.2 },
  pinnedBar: {
    flexDirection: "row", alignItems: "center", gap: 8,
    marginHorizontal: 20, marginBottom: 8,
    paddingHorizontal: 12, paddingVertical: 8, borderRadius: 10,
    backgroundColor: "rgba(255, 215, 0, 0.08)",
    borderWidth: 1, borderColor: "rgba(255, 215, 0, 0.3)",
  },
  pinnedTxt: { flex: 1, color: COLORS.goldElite, fontSize: 11, fontWeight: "800", letterSpacing: 0.8 },
  pinnedClear: { color: COLORS.textMuted, fontSize: 10, fontWeight: "900", letterSpacing: 1.4 },
  content: { paddingHorizontal: 20, paddingTop: 8, paddingBottom: 30 },
  center: { paddingVertical: 80, alignItems: "center" },
  emptyTitle: { color: COLORS.textPrimary, fontSize: 16, fontWeight: "800", marginTop: 14 },
  emptyMsg: { color: COLORS.textMuted, fontSize: 13, marginTop: 6, textAlign: "center", paddingHorizontal: 30 },
  intro: { color: COLORS.textMuted, fontSize: 11, fontWeight: "700", letterSpacing: 0.6, marginBottom: 14, textAlign: "center" },
  cardWrap: {
    backgroundColor: COLORS.surface,
    borderRadius: 18,
    padding: 16,
    marginBottom: 16,
    borderWidth: 1.5,
  },
  cardHeader: {
    flexDirection: "row", alignItems: "center", justifyContent: "space-between",
    marginBottom: 4,
  },
  cardLabel: { fontSize: 14, fontWeight: "900", letterSpacing: 2.5 },
  cardTagline: { color: COLORS.textMuted, fontSize: 11, fontWeight: "700", marginBottom: 12 },
  gradeBadge: {
    minWidth: 30, height: 30, borderRadius: 8, borderWidth: 1.5,
    alignItems: "center", justifyContent: "center", paddingHorizontal: 6,
  },
  gradeText: { fontSize: 16, fontWeight: "900" },
  headlineRow: { flexDirection: "row", alignItems: "center", marginBottom: 14 },
  headlineDivider: { width: 1, height: 40, marginHorizontal: 14 },
  headlineNum: { fontSize: 28, fontWeight: "900", letterSpacing: -0.5 },
  headlineLbl: { color: COLORS.textMuted, fontSize: 9, fontWeight: "800", letterSpacing: 1.2, marginTop: 2 },
  strengthBand: { flexDirection: "row", alignItems: "center", gap: 12, marginBottom: 12 },
  strengthLabel: { color: COLORS.textMuted, fontSize: 9, fontWeight: "800", letterSpacing: 1.3, marginBottom: 4 },
  strengthBarBg: { height: 6, borderRadius: 3, backgroundColor: COLORS.borderDefault, overflow: "hidden" },
  strengthBarFg: { height: 6, borderRadius: 3 },
  strengthValue: { fontSize: 22, fontWeight: "900", minWidth: 38, textAlign: "right" },
  statGrid: { flexDirection: "row", justifyContent: "space-between", marginBottom: 12 },
  miniStat: { alignItems: "center" },
  miniLabel: { color: COLORS.textMuted, fontSize: 8, fontWeight: "800", letterSpacing: 1.2 },
  miniValue: { fontSize: 14, fontWeight: "900", marginTop: 3, letterSpacing: -0.2 },
  reasonsBlock: {
    paddingHorizontal: 12, paddingVertical: 10, marginBottom: 12,
    backgroundColor: COLORS.bg,
    borderRadius: 10,
  },
  reasonsHeader: { color: COLORS.textMuted, fontSize: 9, fontWeight: "800", letterSpacing: 1.4, marginBottom: 6 },
  reasonLine: { fontSize: 11, fontWeight: "700", lineHeight: 16, marginTop: 1 },
  legsHeader: { color: COLORS.textMuted, fontSize: 9, fontWeight: "800", letterSpacing: 1.4, marginBottom: 8 },
  legCard: {
    flexDirection: "row", gap: 12, padding: 12, marginBottom: 8,
    backgroundColor: COLORS.bg,
    borderRadius: 12,
    borderWidth: 1, borderColor: COLORS.borderDefault,
    alignItems: "center",
  },
  legCardPinned: { borderColor: COLORS.goldElite, backgroundColor: "rgba(255, 215, 0, 0.06)" },
  legNum: { width: 28, height: 28, borderRadius: 14, alignItems: "center", justifyContent: "center" },
  legNumText: { color: COLORS.bg, fontWeight: "900", fontSize: 13 },
  legSport: { color: COLORS.textMuted, fontSize: 9, fontWeight: "800", letterSpacing: 1.2 },
  legEvent: { color: COLORS.textSecondary, fontSize: 11, fontWeight: "600", marginTop: 2 },
  legMarket: { color: COLORS.textPrimary, fontSize: 13, fontWeight: "800", marginTop: 3, letterSpacing: -0.1 },
  legMeta: { flexDirection: "row", alignItems: "center", marginTop: 6, gap: 10 },
  legLock: { fontSize: 10, fontWeight: "900", letterSpacing: 0.8 },
  legEdge: { color: COLORS.neonGreen, fontSize: 10, fontWeight: "900", letterSpacing: 0.4 },
  legOdds: { color: COLORS.textPrimary, fontSize: 11, fontWeight: "900", marginLeft: "auto" },
  pinBtn: { padding: 6 },
  disclaimer: { color: COLORS.textMuted, fontSize: 11, lineHeight: 17, marginTop: 12, textAlign: "center" },
});
