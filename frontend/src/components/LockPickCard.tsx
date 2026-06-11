import React from "react";
import { View, Text, StyleSheet, Pressable } from "react-native";
import { useRouter } from "expo-router";
import { COLORS, GRADE_COLORS } from "@/src/theme";
import { LockScoreBadge } from "@/src/components/LockScoreBadge";
import { Pick } from "@/src/lib/api";

export function LockPickCard({ pick, variant = "lock" }: { pick: Pick; variant?: "lock" | "killer" }) {
  const router = useRouter();
  const isKiller = variant === "killer";
  const gradeColor = GRADE_COLORS[pick.grade] || COLORS.textMuted;
  const edgeColor = pick.edge_percent > 0 ? COLORS.neonGreen : COLORS.electricBlaze;
  return (
    <Pressable
      testID={`pick-card-${pick.id}`}
      onPress={() => router.push(`/pick/${pick.id}`)}
      style={({ pressed }) => [
        styles.card,
        isKiller && styles.cardKiller,
        pressed && { opacity: 0.85, transform: [{ scale: 0.98 }] },
      ]}
    >
      <View style={styles.header}>
        <View style={{ flex: 1, paddingRight: 70 }}>
          <View style={styles.tagRow}>
            <View style={[styles.tag, isKiller && styles.tagKiller]}>
              <Text style={[styles.tagText, isKiller && { color: COLORS.electricBlaze }]}>
                {pick.sport.toUpperCase()}
              </Text>
            </View>
            <Text style={styles.league} numberOfLines={1}>{pick.league}</Text>
          </View>
          <Text style={styles.event} numberOfLines={1}>{pick.event}</Text>
          <Text style={styles.market} numberOfLines={2}>{pick.market}</Text>
        </View>
        <LockScoreBadge score={pick.lock_score} grade={pick.grade} />
      </View>

      <View style={styles.metricsRow}>
        <Metric label="WIN PROB" value={`${pick.win_probability}%`} color={COLORS.textPrimary} />
        <Metric label="IMPLIED" value={`${pick.implied_probability}%`} color={COLORS.textSecondary} />
        <Metric
          label="EDGE"
          value={`${pick.edge_percent > 0 ? "+" : ""}${pick.edge_percent}%`}
          color={edgeColor}
        />
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
            { width: `${Math.min(100, pick.win_probability)}%`, backgroundColor: gradeColor },
          ]}
        />
      </View>
      <View style={styles.footer}>
        <Text style={[styles.gradeText, { color: gradeColor }]}>{pick.grade.toUpperCase()}</Text>
        <Text style={styles.confidence}>Confidence: {pick.confidence}</Text>
      </View>
    </Pressable>
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
  cardKiller: {
    backgroundColor: COLORS.killerSurface,
    borderColor: COLORS.killerBorder,
  },
  header: { flexDirection: "row", justifyContent: "space-between" },
  tagRow: { flexDirection: "row", alignItems: "center", gap: 8, marginBottom: 8 },
  tag: {
    paddingHorizontal: 8,
    paddingVertical: 3,
    borderRadius: 4,
    backgroundColor: "rgba(255,255,255,0.08)",
  },
  tagKiller: { backgroundColor: "rgba(255,59,48,0.15)" },
  tagText: { color: COLORS.textPrimary, fontSize: 10, fontWeight: "800", letterSpacing: 1.2 },
  league: { color: COLORS.textMuted, fontSize: 11, fontWeight: "600", flex: 1 },
  event: { color: COLORS.textSecondary, fontSize: 12, marginBottom: 4, fontWeight: "500" },
  market: { color: COLORS.textPrimary, fontSize: 17, fontWeight: "800", letterSpacing: -0.3 },
  metricsRow: { flexDirection: "row", justifyContent: "space-between", marginTop: 18, marginBottom: 12 },
  metric: { flex: 1 },
  metricLabel: { color: COLORS.textMuted, fontSize: 9, fontWeight: "800", letterSpacing: 1.3 },
  metricValue: { fontSize: 16, fontWeight: "900", marginTop: 3, letterSpacing: -0.3 },
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
  confidence: { fontSize: 10, color: COLORS.textMuted, fontWeight: "700", letterSpacing: 0.8 },
});
