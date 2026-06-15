import React from "react";
import { View, Text, StyleSheet, Pressable } from "react-native";
import { useRouter } from "expo-router";
import { COLORS, GRADE_COLORS } from "@/src/theme";
import { Pick } from "@/src/lib/api";

function formatGameTime(iso: string): string {
  try {
    const dt = new Date(iso);
    const now = new Date();
    const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const tomorrowStart = new Date(todayStart);
    tomorrowStart.setDate(todayStart.getDate() + 1);
    const dayAfter = new Date(todayStart);
    dayAfter.setDate(todayStart.getDate() + 2);
    const time = dt.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
    if (dt >= todayStart && dt < tomorrowStart) return `Today · ${time}`;
    if (dt >= tomorrowStart && dt < dayAfter) return `Tomorrow · ${time}`;
    return `${dt.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" })} · ${time}`;
  } catch {
    return iso;
  }
}

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
        <View style={{ flex: 1 }}>
          <View style={styles.tagRow}>
            <View style={[styles.tag, isKiller && styles.tagKiller]}>
              <Text style={[styles.tagText, isKiller && { color: COLORS.electricBlaze }]}>
                {pick.sport.toUpperCase()}
              </Text>
            </View>
            <Text style={styles.league} numberOfLines={1}>{pick.league}</Text>
            <View style={[styles.gradePill, { borderColor: gradeColor, backgroundColor: gradeColor + "18" }]}>
              <Text style={[styles.gradePillText, { color: gradeColor }]} numberOfLines={1}>
                {pick.grade.toUpperCase()}
              </Text>
            </View>
          </View>
          <Text style={styles.event} numberOfLines={1}>{pick.event}</Text>
          {pick.event_time && (
            <Text style={styles.gameTime}>{formatGameTime(pick.event_time)}</Text>
          )}
          <Text style={styles.market} numberOfLines={2}>{pick.market}</Text>
        </View>
      </View>

      {/* Lock v3 — Stacked badge hero row: Bet Quality / Expected Win / Edge */}
      <View style={styles.heroBadgeRow}>
        <HeroBadge
          icon="🔒"
          value={`${Math.round(pick.lock_score)}`}
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
            { width: `${Math.min(100, pick.lock_score)}%`, backgroundColor: gradeColor },
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
  event: { color: COLORS.textSecondary, fontSize: 12, marginBottom: 2, fontWeight: "500" },
  gameTime: { color: COLORS.voltBlue, fontSize: 11, fontWeight: "700", letterSpacing: 0.3, marginBottom: 6 },
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
