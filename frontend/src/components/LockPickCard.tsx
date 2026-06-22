import React from "react";
import { View, Text, StyleSheet, Pressable } from "react-native";
import { useRouter } from "expo-router";
import { COLORS, GRADE_COLORS } from "@/src/theme";
import { Pick } from "@/src/lib/api";
import { formatGameTime } from "@/src/lib/formatGameTime";
import { useMLBLive } from "@/src/contexts/MLBLiveContext";
import { getDisplayLock } from "@/src/lib/lockScore";

export function LockPickCard({ pick }: { pick: Pick }) {
  const router = useRouter();
  const gradeColor = GRADE_COLORS[pick.grade] || COLORS.textMuted;
  const edgeColor = pick.edge_percent > 0 ? COLORS.neonGreen : COLORS.electricBlaze;
  // Live MLB score lookup — null for non-MLB picks (zero cost when missing).
  // Pass event_time so a multi-game series doesn't show yesterday's FINAL
  // on tomorrow's matching matchup card.
  const live = useMLBLive(
    pick.sport === "MLB" ? pick.event : null,
    pick.sport === "MLB" ? (pick.event_time as string | null) : null,
  );
  const showLiveBadge = !!live && (live.is_live || live.is_final);

  // Lock V2 (Deep Thinking) shadow scores.
  //
  // CRITICAL: V2 is in SHADOW MODE — its data must not contradict V1 on
  // the home card. Showing a gold APEX border + "RARE LOCK" chip on a
  // pick whose V1 grade is "Pass" is the inconsistency the user flagged.
  // So we only allow V2 UI elements to surface when they AGREE with V1:
  // V2 chip + APEX border only render when the V1 lock_score is already
  // in the Strong Lock band (95+). Otherwise treat V2 as silent data
  // available solely in the pick detail "Deep Thinking" panel.
  // ── Single source of truth: see /src/lib/lockScore.ts ──
  // Backend now also canonicalizes lock_score = max(v1, v2) at READ time
  // (server.py `_canonicalize_lock_score`), so `pick.lock_score` from the
  // wire is already the right number. We keep `getDisplayLock` as defense
  // in depth in case any pick slips through with v2 > v1.
  const displayLock = getDisplayLock(pick);
  // Anything that referenced pick.lock_score for visual logic now uses
  // displayLock so the badge / progress bar / strong-lock gates all
  // match the headline number.
  const v1IsStrong = displayLock >= 95;
  const v2Tier = v1IsStrong ? pick.tier_v2 : undefined;
  const isApex = v1IsStrong && !!pick.is_apex;
  const v2Lock = pick.lock_score_v2 ?? null;
  const tierColor =
    v2Tier === "Apex Lock"    ? "#FFD700"
  : v2Tier === "Rare Lock"    ? COLORS.neonGreen
  : v2Tier === "Strong Lock"  ? COLORS.voltBlue
  : v2Tier === "Elite Setup"  ? COLORS.textSecondary
  : COLORS.textMuted;
  // "Almost Apex" hint suppressed for non-Strong-Lock picks (same reason).
  const nearMiss = v1IsStrong
    && !isApex
    && v2Lock != null
    && v2Lock >= 97
    && Array.isArray(pick.apex_blockers)
    && pick.apex_blockers.length > 0;
  const firstBlocker = nearMiss ? pick.apex_blockers![0] : null;

  return (
    <Pressable
      testID={`pick-card-${pick.id}`}
      onPress={() => router.push(`/pick/${pick.id}`)}
      style={({ pressed }) => [
        styles.card,
        isApex && styles.cardApex,
        pressed && { opacity: 0.85, transform: [{ scale: 0.98 }] },
      ]}
    >
      <View style={styles.header}>
        <View style={{ flex: 1 }}>
          <View style={styles.tagRow}>
            <View style={styles.tag}>
              <Text style={styles.tagText}>
                {pick.sport.toUpperCase()}
              </Text>
            </View>
            {pick.elite_player && (
              <View style={styles.eliteTag}>
                <Text style={styles.eliteTagText}>⭐ ELITE</Text>
              </View>
            )}
            {(pick as any).is_extra && (
              <View style={styles.extraTag}>
                <Text style={styles.extraTagText}>📡 EXTENDED</Text>
              </View>
            )}
            <Text style={styles.league} numberOfLines={1}>{pick.league}</Text>
            <View style={[styles.gradePill, { borderColor: gradeColor, backgroundColor: gradeColor + "18" }]}>
              <Text style={[styles.gradePillText, { color: gradeColor }]} numberOfLines={1}>
                {pick.grade.toUpperCase()}
              </Text>
            </View>
            {/* Lock V2 tier chip — shadow engine verdict at a glance */}
            {v2Tier && (
              <View style={[styles.v2Chip, { borderColor: tierColor + "66", backgroundColor: tierColor + "12" }]}>
                {isApex && <Text style={styles.apexIcon}>⚡</Text>}
                <Text style={[styles.v2ChipText, { color: tierColor }]} numberOfLines={1}>
                  {isApex ? "APEX" : v2Tier === "Rare Lock" ? "RARE"
                          : v2Tier === "Strong Lock" ? "STRONG"
                          : v2Tier === "Elite Setup" ? "ELITE" : v2Tier.toUpperCase()}
                </Text>
              </View>
            )}
          </View>
          <Text style={styles.event} numberOfLines={1}>{pick.event}</Text>
          {pick.event_time && (
            <Text style={styles.gameTime}>{formatGameTime(pick.event_time)}</Text>
          )}
          {showLiveBadge && live && (
            <View style={[styles.liveBadge, live.is_final ? styles.liveBadgeFinal : styles.liveBadgeLive]}>
              <View style={[styles.liveDot, { backgroundColor: live.is_final ? COLORS.textMuted : COLORS.neonGreen }]} />
              <Text style={[styles.liveBadgeText, { color: live.is_final ? COLORS.textMuted : COLORS.neonGreen }]}>
                {live.is_final ? "FINAL" : "LIVE"} · {live.away_score ?? 0}-{live.home_score ?? 0}
              </Text>
            </View>
          )}
          {/* SIM EDGE chip — appears when the Monte Carlo simulator returns
              ≥85% win probability AND it's at least 5pp stronger than the
              blended model. Signals a high-confidence "the math really
              loves this" finding. */}
          {typeof pick.sim_win_probability === "number" &&
           pick.sim_win_probability >= 85 &&
           (pick.sim_disagreement_with_model ?? 0) >= 5 && (
            <View style={styles.simEdgeBadge}>
              <Text style={styles.simEdgeIcon}>🎲</Text>
              <Text style={styles.simEdgeText}>SIM EDGE · {pick.sim_win_probability.toFixed(0)}%</Text>
            </View>
          )}
          <Text style={styles.market} numberOfLines={2}>{pick.market}</Text>
          {(pick as any).model_line === true && (
            <Text style={styles.modelLineText} numberOfLines={1}>
              📐 Model line — synthesized from market O/U
            </Text>
          )}
          {nearMiss && firstBlocker && (
            <Text style={styles.nearMissText} numberOfLines={1}>
              ⚡ Almost Apex — blocked by {firstBlocker}
            </Text>
          )}
        </View>
      </View>

      {/* Lock v3 — Stacked badge hero row: Bet Quality / Expected Win / Edge */}
      <View style={styles.heroBadgeRow}>
        <HeroBadge
          icon="🔒"
          value={`${Math.round(displayLock)}`}
          label="LOCK"
          sub="BET QUALITY"
          color={gradeColor}
        />
        <HeroBadge
          icon="📊"
          value={`${pick.win_probability}%`}
          label="WIN"
          sub="EXPECTED"
          color={COLORS.textPrimary}
        />
        <HeroBadge
          icon="⚡"
          value={`${pick.edge_percent > 0 ? "+" : ""}${pick.edge_percent}%`}
          label="EDGE"
          sub="VALUE"
          color={edgeColor}
        />
      </View>

      <View style={styles.secondaryRow}>
        <Metric label="IMPLIED" value={`${pick.implied_probability}%`} color={COLORS.textSecondary} />
        <View style={styles.secondaryDivider} />
        <Metric
          label="ODDS"
          value={pick.book_odds > 0 ? `+${pick.book_odds}` : `${pick.book_odds}`}
          color={COLORS.textPrimary}
        />
      </View>

      <View style={styles.progressTrack}>
        <View
          style={[
            styles.progressFill,
            { width: `${Math.min(100, displayLock)}%`, backgroundColor: gradeColor },
          ]}
        />
      </View>
      <View style={styles.footer}>
        <Text style={styles.lockNote}>Lock = Bet Quality · Win = Expected Hit Rate</Text>
        <Text style={styles.confidence}>{pick.confidence}</Text>
      </View>
    </Pressable>
  );
}

function HeroBadge({
  icon, value, label, sub, color,
}: { icon: string; value: string; label: string; sub: string; color: string }) {
  return (
    <View style={[styles.heroBadge, { borderColor: color + "55", backgroundColor: color + "10" }]}>
      <Text style={styles.heroIcon}>{icon}</Text>
      <Text style={[styles.heroValue, { color }]} numberOfLines={1}>{value}</Text>
      <Text style={styles.heroLabel}>{label}</Text>
      <Text style={styles.heroSub}>{sub}</Text>
    </View>
  );
}

function Metric({ label, value, color }: { label: string; value: string; color: string }) {
  return (
    <View style={styles.metric}>
      <Text style={styles.metricLabel}>{label}</Text>
      <Text style={[styles.metricValue, { color }]} numberOfLines={1}>{value}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  card: {
    backgroundColor: COLORS.surface,
    borderRadius: 16,
    padding: 18,
    borderWidth: 1,
    borderColor: COLORS.borderDefault,
    marginBottom: 14,
  },
  cardApex: {
    borderColor: "#FFD700",
    borderWidth: 1.5,
    shadowColor: "#FFD700",
    shadowOpacity: 0.25,
    shadowOffset: { width: 0, height: 0 },
    shadowRadius: 8,
  },
  v2Chip: {
    flexDirection: "row",
    alignItems: "center",
    gap: 3,
    paddingHorizontal: 6,
    paddingVertical: 2,
    borderRadius: 4,
    borderWidth: 1,
  },
  v2ChipText: {
    fontSize: 8.5,
    fontWeight: "900",
    letterSpacing: 0.9,
  },
  apexIcon: { fontSize: 9 },
  nearMissText: {
    color: "#FFD700",
    fontSize: 10.5,
    fontWeight: "700",
    letterSpacing: 0.3,
    marginTop: 6,
    opacity: 0.85,
  },
  modelLineText: {
    color: COLORS.textMuted,
    fontSize: 10.5,
    fontWeight: "600",
    letterSpacing: 0.2,
    marginTop: 4,
    fontStyle: "italic",
  },
  header: { flexDirection: "row", justifyContent: "space-between" },
  tagRow: { flexDirection: "row", alignItems: "center", gap: 8, marginBottom: 8 },
  tag: {
    paddingHorizontal: 8,
    paddingVertical: 3,
    borderRadius: 4,
    backgroundColor: "rgba(255,255,255,0.08)",
  },
  tagText: { color: COLORS.textPrimary, fontSize: 10, fontWeight: "800", letterSpacing: 1.2 },
  extraTag: {
    paddingHorizontal: 6, paddingVertical: 2, borderRadius: 4,
    borderWidth: 1, borderColor: COLORS.voltBlue + "66",
    backgroundColor: COLORS.voltBlue + "18",
  },
  extraTagText: {
    color: COLORS.voltBlue, fontSize: 9, fontWeight: "800",
    letterSpacing: 0.8,
  },
  league: { color: COLORS.textMuted, fontSize: 11, fontWeight: "600", flex: 1 },
  event: { color: COLORS.textSecondary, fontSize: 12, marginBottom: 2, fontWeight: "500" },
  gameTime: { color: COLORS.voltBlue, fontSize: 11, fontWeight: "700", letterSpacing: 0.3, marginBottom: 6 },
  liveBadge: {
    flexDirection: "row", alignItems: "center", alignSelf: "flex-start",
    gap: 6, paddingHorizontal: 8, paddingVertical: 3,
    borderRadius: 4, borderWidth: 1, marginBottom: 6,
  },
  liveBadgeLive: {
    backgroundColor: "rgba(0,255,170,0.10)",
    borderColor: "rgba(0,255,170,0.45)",
  },
  liveBadgeFinal: {
    backgroundColor: "rgba(255,255,255,0.04)",
    borderColor: "rgba(255,255,255,0.18)",
  },
  liveDot: { width: 6, height: 6, borderRadius: 3 },
  liveBadgeText: { fontSize: 10, fontWeight: "800", letterSpacing: 0.8, fontVariant: ["tabular-nums"] },
  // SIM EDGE — high-confidence simulator agreement chip
  simEdgeBadge: {
    alignSelf: "flex-start",
    flexDirection: "row",
    alignItems: "center",
    gap: 4,
    paddingHorizontal: 7,
    paddingVertical: 3,
    borderRadius: 5,
    borderWidth: 1,
    borderColor: "rgba(167, 139, 250, 0.55)",
    backgroundColor: "rgba(167, 139, 250, 0.12)",
    marginTop: 4,
  },
  simEdgeIcon: { fontSize: 10 },
  simEdgeText: { color: "#C4B5FD", fontSize: 9, fontWeight: "900", letterSpacing: 0.9 },
  market: { color: COLORS.textPrimary, fontSize: 17, fontWeight: "800", letterSpacing: -0.3 },
  metricsRow: { flexDirection: "row", justifyContent: "space-between", marginTop: 18, marginBottom: 12 },
  metric: { flex: 1 },
  metricLabel: { color: COLORS.textMuted, fontSize: 9, fontWeight: "800", letterSpacing: 1.3 },
  metricValue: { fontSize: 16, fontWeight: "900", marginTop: 3, letterSpacing: -0.3 },

  heroBadgeRow: {
    flexDirection: "row",
    gap: 8,
    marginTop: 16,
    marginBottom: 12,
  },
  heroBadge: {
    flex: 1,
    paddingVertical: 12,
    paddingHorizontal: 8,
    borderRadius: 12,
    borderWidth: 1,
    alignItems: "center",
    justifyContent: "center",
  },
  heroIcon: { fontSize: 14, marginBottom: 2 },
  heroValue: { fontSize: 22, fontWeight: "900", letterSpacing: -0.6, marginTop: 2 },
  heroLabel: {
    color: COLORS.textPrimary,
    fontSize: 9,
    fontWeight: "900",
    letterSpacing: 1.4,
    marginTop: 4,
  },
  heroSub: {
    color: COLORS.textMuted,
    fontSize: 8,
    fontWeight: "700",
    letterSpacing: 1.0,
    marginTop: 1,
  },

  secondaryRow: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingVertical: 6,
    paddingHorizontal: 4,
    marginBottom: 10,
  },
  secondaryDivider: {
    width: 1,
    height: 22,
    backgroundColor: COLORS.borderDefault,
  },

  gradePill: {
    paddingHorizontal: 8,
    paddingVertical: 3,
    borderRadius: 4,
    borderWidth: 1,
  },
  gradePillText: {
    fontSize: 9,
    fontWeight: "900",
    letterSpacing: 1.2,
  },
  eliteTag: {
    paddingHorizontal: 7,
    paddingVertical: 2,
    borderRadius: 4,
    backgroundColor: "#FFD70022",
    borderWidth: 1,
    borderColor: "#FFD700",
  },
  eliteTagText: {
    color: "#FFD700",
    fontSize: 9,
    fontWeight: "900",
    letterSpacing: 1.0,
  },
  progressTrack: {
    height: 4,
    backgroundColor: "rgba(255,255,255,0.06)",
    borderRadius: 2,
    overflow: "hidden",
  },
  progressFill: { height: "100%", borderRadius: 2 },
  footer: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center",
    marginTop: 10,
  },
  gradeText: { fontSize: 11, fontWeight: "900", letterSpacing: 1.5 },
  lockNote: {
    color: COLORS.textMuted,
    fontSize: 9,
    fontWeight: "700",
    letterSpacing: 0.4,
    flex: 1,
  },
  confidence: { fontSize: 10, color: COLORS.textMuted, fontWeight: "700", letterSpacing: 0.8 },
});
