/**
 * SignalEnginePanel — PerksLocks Signal Engine breakdown (Phase A).
 *
 * Renders the 0-100 Signal Score plus the six universal component
 * contributions (Form / Matchup / Volume / Injury / Market / Value)
 * as signed bars with the real-number evidence lines underneath.
 *
 * Data source: `pick.signal_engine` computed server-side by
 * `backend/services/signal_engine` on the pick-detail endpoint.
 * Hides itself entirely when the block is absent (legacy picks).
 */
import React, { useState } from "react";
import { View, Text, StyleSheet, Pressable } from "react-native";
import type { Pick } from "@/src/lib/api";
import { COLORS } from "@/src/theme";

const GRADE_TINTS: Record<string, string> = {
  Elite: COLORS.neonGreen,
  Strong: "#86EFAC",
  Moderate: COLORS.voltBlue,
  Weak: "#FCD34D",
  Fade: COLORS.electricBlaze,
};

export function SignalEnginePanel({ pick }: { pick: Pick }) {
  const [expanded, setExpanded] = useState<string | null>(null);
  const se = pick.signal_engine;
  if (!se || !Array.isArray(se.components)) return null;

  // 2026-07-19 fix (v2): show the RAW absolute score, not the
  // per-sport percentile rank. Raw reflects the pick's actual
  // evidence strength (e.g. 66 = "moderate positive edge"). Rank
  // is a relative sort key that made real +5% edges look like
  // "FADE 31/100" simply because 200+ other tennis picks were
  // above it in a compressed distribution.
  const displayScore = (typeof pick.signal_score_raw === "number")
    ? Math.round(pick.signal_score_raw)
    : Math.round(se.score);
  // Grade from raw score bands — matches the signal-engine's own
  // grade classification. Also matches the card badge colour.
  const derivedGrade = (
    displayScore >= 80 ? "Elite"
    : displayScore >= 65 ? "Strong"
    : displayScore >= 55 ? "Moderate"
    : displayScore >= 40 ? "Weak"
    : "Fade"
  );
  const tint = GRADE_TINTS[derivedGrade] || COLORS.voltBlue;
  // Only components that actually had data get a row.
  const rows = se.components.filter((c) => c.found);
  if (rows.length === 0) return null;

  return (
    <View style={styles.card} testID="signal-engine-panel">
      <View style={styles.headerRow}>
        <Text style={styles.title}>📡 SIGNAL ENGINE</Text>
        <View style={[styles.gradePill, { borderColor: tint + "66", backgroundColor: tint + "14" }]}>
          <Text style={[styles.gradeText, { color: tint }]}>{derivedGrade.toUpperCase()}</Text>
        </View>
      </View>

      <View style={styles.scoreRow}>
        <Text style={[styles.scoreBig, { color: tint }]}>{displayScore}</Text>
        <Text style={styles.scoreOutOf}>/100</Text>
        <Text style={styles.scoreLabel}>SIGNAL SCORE</Text>
      </View>

      {rows
        .slice()
        .sort((a, b) => b.points - a.points)
        .map((c) => {
          const pos = c.points >= 0;
          const width = Math.min(100, (Math.abs(c.points) / c.max) * 100);
          const barColor = pos ? COLORS.neonGreen : COLORS.electricBlaze;
          const open = expanded === c.key;
          return (
            <Pressable
              key={c.key}
              onPress={() => setExpanded(open ? null : c.key)}
              style={styles.compBlock}
              testID={`signal-comp-${c.key}`}
            >
              <View style={styles.compRow}>
                <Text style={styles.compLabel}>{c.label}</Text>
                <View style={styles.barTrack}>
                  <View style={styles.barCenter} />
                  <View
                    style={[
                      styles.barFill,
                      pos ? styles.barFillPos : styles.barFillNeg,
                      { width: `${width / 2}%`, backgroundColor: barColor },
                    ]}
                  />
                </View>
                <Text style={[styles.compPoints, { color: barColor }]}>
                  {pos ? "+" : ""}
                  {c.points}
                </Text>
              </View>
              {(open || Math.abs(c.points) >= 3) &&
                c.details.slice(0, open ? 4 : 1).map((d, i) => (
                  <Text key={i} style={styles.detailText}>
                    · {d}
                  </Text>
                ))}
            </Pressable>
          );
        })}

      <Text style={styles.footnote}>
        Six independent signals scored against a neutral 50. Tap a row for the underlying numbers.
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  card: {
    backgroundColor: COLORS.surface,
    padding: 16,
    borderRadius: 14,
    borderWidth: 1,
    borderColor: COLORS.borderDefault,
    marginTop: 22,
  },
  headerRow: { flexDirection: "row", alignItems: "center", justifyContent: "space-between" },
  title: { color: COLORS.textMuted, fontSize: 10, fontWeight: "800", letterSpacing: 1.6 },
  gradePill: { paddingHorizontal: 8, paddingVertical: 3, borderRadius: 8, borderWidth: 1 },
  gradeText: { fontSize: 9, fontWeight: "900", letterSpacing: 1.1 },

  scoreRow: { flexDirection: "row", alignItems: "flex-end", marginTop: 10, marginBottom: 12 },
  scoreBig: { fontSize: 40, fontWeight: "900", letterSpacing: -1.5, lineHeight: 42 },
  scoreOutOf: { color: COLORS.textMuted, fontSize: 15, fontWeight: "800", marginBottom: 4, marginLeft: 2 },
  scoreLabel: {
    color: COLORS.textMuted, fontSize: 9, fontWeight: "800",
    letterSpacing: 1.4, marginLeft: 10, marginBottom: 8,
  },

  compBlock: { marginVertical: 5 },
  compRow: { flexDirection: "row", alignItems: "center", gap: 10 },
  compLabel: { width: 66, color: COLORS.textSecondary, fontSize: 12, fontWeight: "700" },
  barTrack: {
    flex: 1, height: 8, backgroundColor: "rgba(255,255,255,0.06)",
    borderRadius: 4, overflow: "hidden", flexDirection: "row",
  },
  barCenter: {
    position: "absolute", left: "50%", top: 0, bottom: 0, width: 1,
    backgroundColor: "rgba(255,255,255,0.25)",
  },
  barFill: { position: "absolute", top: 0, bottom: 0, borderRadius: 4 },
  barFillPos: { left: "50%" },
  barFillNeg: { right: "50%" },
  compPoints: { width: 40, textAlign: "right", fontWeight: "900", fontSize: 13, letterSpacing: -0.3 },
  detailText: {
    color: COLORS.textMuted, fontSize: 11, lineHeight: 16,
    marginLeft: 76, marginTop: 3,
  },
  footnote: {
    color: COLORS.textMuted, fontSize: 10, lineHeight: 14,
    marginTop: 12, fontStyle: "italic",
  },
});
