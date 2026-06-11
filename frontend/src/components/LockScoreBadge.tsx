import React from "react";
import { View, Text, StyleSheet } from "react-native";
import { COLORS, GRADE_COLORS } from "@/src/theme";

export function LockScoreBadge({ score, grade, size = 56 }: {
  score: number; grade: keyof typeof GRADE_COLORS; size?: number;
}) {
  const color = GRADE_COLORS[grade] || COLORS.textMuted;
  return (
    <View
      testID={`lock-score-badge-${Math.round(score)}`}
      style={[
        styles.badge,
        {
          width: size,
          height: size,
          borderRadius: size / 2,
          borderColor: color,
        },
      ]}
    >
      <Text style={[styles.score, { color, fontSize: size * 0.36 }]}>{Math.round(score)}</Text>
      <Text style={[styles.label, { fontSize: size * 0.16 }]}>LOCK</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  badge: {
    borderWidth: 2,
    backgroundColor: "rgba(0,0,0,0.6)",
    alignItems: "center",
    justifyContent: "center",
  },
  score: { fontWeight: "900", letterSpacing: -0.5, lineHeight: undefined },
  label: { color: COLORS.textMuted, fontWeight: "700", letterSpacing: 1.5, marginTop: -2 },
});
