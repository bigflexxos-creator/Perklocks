import React from "react";
import { View, Text, Pressable, StyleSheet, ScrollView } from "react-native";
import { Ionicons } from "@expo/vector-icons";
import { COLORS } from "@/src/theme";
import { SortKey } from "@/src/lib/api";

const OPTIONS: { id: SortKey; label: string; icon: keyof typeof Ionicons.glyphMap }[] = [
  { id: "lock", label: "LOCK", icon: "lock-closed" },
  { id: "time", label: "TIME", icon: "time-outline" },
  { id: "edge", label: "EDGE", icon: "trending-up" },
  { id: "implied", label: "ODDS", icon: "stats-chart" },
];

type Props = {
  value: SortKey;
  onChange: (v: SortKey) => void;
  testIDPrefix?: string;
};

// Compact sort selector. Horizontal scroll lets it stay on a single row
// regardless of how many options exist or screen width.
export function SortSelector({ value, onChange, testIDPrefix = "sort" }: Props) {
  return (
    <View style={styles.row}>
      <Text style={styles.label}>SORT</Text>
      <ScrollView
        horizontal
        showsHorizontalScrollIndicator={false}
        contentContainerStyle={styles.scroll}
      >
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
                  active && styles.segmentActive,
                ]}
              >
                <Ionicons
                  name={opt.icon}
                  size={11}
                  color={active ? COLORS.bg : COLORS.textSecondary}
                />
                <Text style={[styles.segmentText, active && styles.segmentTextActive]}>
                  {opt.label}
                </Text>
              </Pressable>
            );
          })}
        </View>
      </ScrollView>
    </View>
  );
}

const styles = StyleSheet.create({
  row: {
    flexDirection: "row",
    alignItems: "center",
    paddingLeft: 20,
    paddingBottom: 10,
    gap: 8,
  },
  scroll: { paddingRight: 20 },
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
    flexDirection: "row",
    alignItems: "center",
    gap: 5,
    paddingHorizontal: 11,
    paddingVertical: 7,
    borderLeftWidth: 1,
    borderLeftColor: COLORS.borderDefault,
  },
  segmentFirst: { borderLeftWidth: 0 },
  segmentActive: { backgroundColor: COLORS.goldElite },
  segmentText: {
    color: COLORS.textSecondary,
    fontSize: 11,
    fontWeight: "800",
    letterSpacing: 1.1,
  },
  segmentTextActive: { color: COLORS.bg, fontWeight: "900" },
});
