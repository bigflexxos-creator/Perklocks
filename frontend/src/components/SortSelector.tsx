import React from "react";
import { View, Text, Pressable, StyleSheet } from "react-native";
import { Ionicons } from "@expo/vector-icons";
import { COLORS } from "@/src/theme";
import { SortKey } from "@/src/lib/api";

const OPTIONS: { id: SortKey; label: string; icon: keyof typeof Ionicons.glyphMap }[] = [
  { id: "lock", label: "LOCK", icon: "lock-closed" },
  { id: "time", label: "TIME", icon: "time-outline" },
  { id: "edge", label: "EDGE", icon: "trending-up" },
];

type Props = {
  value: SortKey;
  onChange: (v: SortKey) => void;
  testIDPrefix?: string;
};

// Compact 3-way sort selector. Sits inline with the line type toggle on
// screens that surface a long list of picks (Locks, Under Lock). Wired to
// the backend's `sort` query param so sorting happens server-side and
// pagination/filtering stay consistent.
export function SortSelector({ value, onChange, testIDPrefix = "sort" }: Props) {
  return (
    <View style={styles.row}>
      <Text style={styles.label}>SORT</Text>
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
