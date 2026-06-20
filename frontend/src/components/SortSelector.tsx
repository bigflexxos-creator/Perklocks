import React from "react";
import { View, Text, Pressable, StyleSheet, ScrollView } from "react-native";
import { Ionicons } from "@expo/vector-icons";
import { COLORS } from "@/src/theme";
import { SortKey, SortDirection } from "@/src/lib/api";

const OPTIONS: { id: SortKey; label: string; icon: keyof typeof Ionicons.glyphMap }[] = [
  { id: "lock", label: "LOCK", icon: "lock-closed" },
  { id: "time", label: "TIME", icon: "time-outline" },
  { id: "edge", label: "EDGE", icon: "trending-up" },
  { id: "win",  label: "WIN %", icon: "trophy" },
  { id: "implied", label: "ODDS", icon: "stats-chart" },
];

type Props = {
  value: SortKey;
  onChange: (v: SortKey) => void;
  /** Direction control — desc puts highest values at the TOP (default,
   *  matches "best lock first" intent). asc flips to lowest-first which
   *  is useful for hunting weakest picks. */
  direction?: SortDirection;
  onDirectionChange?: (d: SortDirection) => void;
  testIDPrefix?: string;
};

// Compact sort selector. Horizontal scroll lets it stay on a single row
// regardless of how many options exist or screen width.
export function SortSelector({
  value,
  onChange,
  direction = "desc",
  onDirectionChange,
  testIDPrefix = "sort",
}: Props) {
  // Direction toggle now applies to ALL sort keys including TIME — user
  // spec: "the time need to be like edge odds where I can sort time".
  // For time, asc = soonest-first, desc = latest-first. We render
  // sport-specific labels below so the meaning stays obvious.
  const showDirToggle = !!onDirectionChange;
  const dirLabels = value === "time"
    ? { asc: "SOON→LATE", desc: "LATE→SOON" }
    : { asc: "LOW→HIGH", desc: "HIGH→LOW" };
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
        {showDirToggle ? (
          <Pressable
            testID={`${testIDPrefix}-direction`}
            onPress={() =>
              onDirectionChange!(direction === "desc" ? "asc" : "desc")
            }
            style={styles.dirBtn}
            hitSlop={8}
            accessibilityLabel={
              value === "time"
                ? (direction === "asc" ? "Soonest first" : "Latest first")
                : (direction === "desc" ? "Highest first" : "Lowest first")
            }
          >
            <Ionicons
              name={direction === "desc" ? "arrow-down" : "arrow-up"}
              size={13}
              color={COLORS.goldElite}
            />
            <Text style={styles.dirText}>
              {direction === "desc" ? dirLabels.desc : dirLabels.asc}
            </Text>
          </Pressable>
        ) : null}
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
  scroll: { paddingRight: 20, gap: 8, alignItems: "center" },
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
  // Direction toggle — small button to flip the sort order (high→low ↔ low→high).
  dirBtn: {
    flexDirection: "row",
    alignItems: "center",
    gap: 4,
    paddingHorizontal: 9,
    paddingVertical: 7,
    borderRadius: 10,
    borderWidth: 1,
    borderColor: COLORS.goldElite,
    backgroundColor: "rgba(255,215,0,0.08)",
  },
  dirText: {
    color: COLORS.goldElite,
    fontSize: 10,
    fontWeight: "900",
    letterSpacing: 0.8,
  },
});

