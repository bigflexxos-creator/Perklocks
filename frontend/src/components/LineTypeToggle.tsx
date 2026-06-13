import React from "react";
import { View, Text, Pressable, StyleSheet } from "react-native";
import { COLORS } from "@/src/theme";
import { LineType } from "@/src/lib/api";

const OPTIONS: { id: LineType; label: string }[] = [
  { id: "main", label: "MAIN" },
  { id: "alt", label: "ALT" },
  { id: "both", label: "BOTH" },
];

type Props = {
  value: LineType;
  onChange: (v: LineType) => void;
  // Optional `testIDPrefix` so the same component can ship into multiple
  // screens without test-id collisions.
  testIDPrefix?: string;
};

// Compact 3-way segmented control: MAIN | ALT | BOTH. Used across Locks,
// Rollover, and Parlay tabs. Keeps the visual language consistent so users
// learn the filter once.
export function LineTypeToggle({ value, onChange, testIDPrefix = "line" }: Props) {
  return (
    <View style={styles.row}>
      <Text style={styles.label}>LINE</Text>
      <View style={styles.group}>
        {OPTIONS.map((opt, idx) => {
          const active = value === opt.id;
          return (
            <Pressable
              key={opt.id}
              testID={`${testIDPrefix}-${opt.id}`}
              onPress={() => onChange(opt.id)}
              style={[
                styles.segment,
                idx === 0 && styles.segmentFirst,
                idx === OPTIONS.length - 1 && styles.segmentLast,
                active && styles.segmentActive,
              ]}
            >
              <Text style={[styles.segmentText, active && styles.segmentTextActive]}>
                {opt.label}
              </Text>
            </Pressable>
          );
        })}
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  row: {
    flexDirection: "row",
    alignItems: "center",
    paddingHorizontal: 20,
    paddingBottom: 10,
    gap: 8,
  },
  label: {
    color: COLORS.textMuted,
    fontSize: 10,
    fontWeight: "800",
    letterSpacing: 1.3,
    marginRight: 4,
  },
  group: {
    flexDirection: "row",
    backgroundColor: COLORS.surface,
    borderRadius: 10,
    borderWidth: 1,
    borderColor: COLORS.borderDefault,
    overflow: "hidden",
  },
  segment: {
    minWidth: 56,
    paddingHorizontal: 12,
    paddingVertical: 7,
    alignItems: "center",
    justifyContent: "center",
    borderLeftWidth: 1,
    borderLeftColor: COLORS.borderDefault,
  },
  segmentFirst: { borderLeftWidth: 0 },
  segmentLast: {},
  segmentActive: { backgroundColor: COLORS.voltBlue },
  segmentText: {
    color: COLORS.textSecondary,
    fontSize: 11,
    fontWeight: "800",
    letterSpacing: 1.2,
  },
  segmentTextActive: { color: COLORS.bg, fontWeight: "900" },
});
