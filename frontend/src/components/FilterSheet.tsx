import React, { useState, useEffect } from "react";
import {
  View, Text, Modal, Pressable, StyleSheet, Platform, ScrollView,
} from "react-native";
import Slider from "@react-native-community/slider";
import { Ionicons } from "@expo/vector-icons";
import { COLORS } from "@/src/theme";
import { PickFilters, LineType } from "@/src/lib/api";
import { SortSelector, SortKey } from "@/src/components/SortSelector";
import { LineTypeToggle } from "@/src/components/LineTypeToggle";

type Props = {
  visible: boolean;
  onClose: () => void;
  filters: PickFilters;
  onApply: (next: PickFilters) => void;
  // Optional: bring LINE + SORT controls into the sheet. User spec:
  // "the line and sort should [be] in the filters" — keeps the home
  // header clean (no permanent stats row, no permanent sort/line bar).
  lineType?: LineType;
  onLineTypeChange?: (v: LineType) => void;
  sortKey?: SortKey;
  onSortKeyChange?: (v: SortKey) => void;
  sortDir?: "asc" | "desc";
  onSortDirChange?: (v: "asc" | "desc") => void;
};

// Bottom-sheet style filter for lock score and implied probability. Single
// slider per dimension (min lock, min implied, max implied) keeps the UI
// simple while letting power users tighten the board.
export function FilterSheet({
  visible, onClose, filters, onApply,
  lineType, onLineTypeChange,
  sortKey, onSortKeyChange,
  sortDir = "desc", onSortDirChange,
}: Props) {
  // Local state so users can scrub without firing API calls on every drag.
  const [minLock, setMinLock] = useState<number>(filters.minLock ?? 85);
  const [minImplied, setMinImplied] = useState<number>(filters.minImplied ?? 0);
  const [maxImplied, setMaxImplied] = useState<number>(filters.maxImplied ?? 100);

  // Sync local state when the sheet opens with current external values.
  useEffect(() => {
    if (visible) {
      setMinLock(filters.minLock ?? 85);
      setMinImplied(filters.minImplied ?? 0);
      setMaxImplied(filters.maxImplied ?? 100);
    }
  }, [visible, filters.minLock, filters.minImplied, filters.maxImplied]);

  const reset = () => {
    setMinLock(85);
    setMinImplied(0);
    setMaxImplied(100);
  };

  const apply = () => {
    onApply({
      minLock: minLock > 85 ? minLock : undefined,
      minImplied: minImplied > 0 ? minImplied : undefined,
      maxImplied: maxImplied < 100 ? maxImplied : undefined,
    });
    onClose();
  };

  const activeCount = [
    minLock > 85,
    minImplied > 0,
    maxImplied < 100,
  ].filter(Boolean).length;

  return (
    <Modal visible={visible} transparent animationType="slide" onRequestClose={onClose}>
      <Pressable style={styles.backdrop} onPress={onClose} />
      <View style={styles.sheet}>
        <View style={styles.handle} />
        <View style={styles.header}>
          <Text style={styles.title}>FILTERS</Text>
          <Pressable onPress={onClose} hitSlop={10}>
            <Ionicons name="close" size={22} color={COLORS.textSecondary} />
          </Pressable>
        </View>

        <ScrollView showsVerticalScrollIndicator={false} style={{ maxHeight: 520 }}>

        {/* ── LINE TYPE — moved from the home header per user spec ── */}
        {lineType && onLineTypeChange && (
          <View style={styles.section}>
            <View style={styles.sectionHeader}>
              <View style={styles.sectionTitleRow}>
                <Ionicons name="git-branch-outline" size={14} color={COLORS.electricBlaze} />
                <Text style={styles.sectionTitle}>LINE TYPE</Text>
              </View>
            </View>
            <LineTypeToggle value={lineType} onChange={onLineTypeChange} testIDPrefix="filter-line" />
          </View>
        )}

        {/* ── SORT BY — moved from the home header per user spec ── */}
        {sortKey && onSortKeyChange && (
          <View style={styles.section}>
            <View style={styles.sectionHeader}>
              <View style={styles.sectionTitleRow}>
                <Ionicons name="swap-vertical" size={14} color={COLORS.neonGreen} />
                <Text style={styles.sectionTitle}>SORT BY</Text>
              </View>
            </View>
            <SortSelector
              value={sortKey}
              onChange={onSortKeyChange}
              direction={sortDir}
              onDirectionChange={onSortDirChange}
              testIDPrefix="filter-sort"
            />
          </View>
        )}

        {/* Lock score floor */}
        <View style={styles.section}>
          <View style={styles.sectionHeader}>
            <View style={styles.sectionTitleRow}>
              <Ionicons name="lock-closed" size={14} color={COLORS.goldElite} />
              <Text style={styles.sectionTitle}>MIN LOCK SCORE</Text>
            </View>
            <Text style={styles.value}>{Math.round(minLock)}+</Text>
          </View>
          <Slider
            testID="filter-min-lock"
            style={styles.slider}
            minimumValue={85}
            maximumValue={99}
            step={1}
            value={minLock}
            onValueChange={setMinLock}
            minimumTrackTintColor={COLORS.goldElite}
            maximumTrackTintColor={COLORS.borderDefault}
            thumbTintColor={Platform.OS === "android" ? COLORS.goldElite : undefined}
          />
          <View style={styles.scaleRow}>
            <Text style={styles.scaleLabel}>85 GOOD</Text>
            <Text style={styles.scaleLabel}>92 STRONG</Text>
            <Text style={styles.scaleLabel}>95 ELITE</Text>
          </View>
        </View>

        {/* Implied probability range */}
        <View style={styles.section}>
          <View style={styles.sectionHeader}>
            <View style={styles.sectionTitleRow}>
              <Ionicons name="stats-chart" size={14} color={COLORS.voltBlue} />
              <Text style={styles.sectionTitle}>IMPLIED ODDS RANGE</Text>
            </View>
            <Text style={styles.value}>
              {Math.round(minImplied)}% – {Math.round(maxImplied)}%
            </Text>
          </View>
          <Text style={styles.sublabel}>MIN IMPLIED</Text>
          <Slider
            testID="filter-min-implied"
            style={styles.slider}
            minimumValue={0}
            maximumValue={95}
            step={5}
            value={minImplied}
            onValueChange={(v) => setMinImplied(Math.min(v, maxImplied - 5))}
            minimumTrackTintColor={COLORS.voltBlue}
            maximumTrackTintColor={COLORS.borderDefault}
            thumbTintColor={Platform.OS === "android" ? COLORS.voltBlue : undefined}
          />
          <Text style={styles.sublabel}>MAX IMPLIED</Text>
          <Slider
            testID="filter-max-implied"
            style={styles.slider}
            minimumValue={5}
            maximumValue={100}
            step={5}
            value={maxImplied}
            onValueChange={(v) => setMaxImplied(Math.max(v, minImplied + 5))}
            minimumTrackTintColor={COLORS.voltBlue}
            maximumTrackTintColor={COLORS.borderDefault}
            thumbTintColor={Platform.OS === "android" ? COLORS.voltBlue : undefined}
          />
          <Text style={styles.hint}>
            Lower implied = bigger payouts. Higher implied = chalkier safer picks.
          </Text>
        </View>

        </ScrollView>

        <View style={styles.actions}>
          <Pressable onPress={reset} style={styles.resetBtn} testID="filter-reset">
            <Text style={styles.resetText}>RESET</Text>
          </Pressable>
          <Pressable onPress={apply} style={styles.applyBtn} testID="filter-apply">
            <Text style={styles.applyText}>
              APPLY{activeCount > 0 ? ` · ${activeCount}` : ""}
            </Text>
          </Pressable>
        </View>
      </View>
    </Modal>
  );
}

// Compact filter trigger button that shows active filter count.
export function FilterButton({
  onPress,
  activeCount,
  testID,
}: {
  onPress: () => void;
  activeCount: number;
  testID?: string;
}) {
  const active = activeCount > 0;
  return (
    <Pressable
      onPress={onPress}
      testID={testID}
      style={[styles.fbBtn, active && styles.fbBtnActive]}
    >
      <Ionicons
        name="options-outline"
        size={13}
        color={active ? COLORS.bg : COLORS.textSecondary}
      />
      <Text style={[styles.fbText, active && styles.fbTextActive]}>
        FILTER{active ? ` · ${activeCount}` : ""}
      </Text>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  backdrop: {
    flex: 1,
    backgroundColor: "rgba(0,0,0,0.55)",
  },
  sheet: {
    backgroundColor: COLORS.surface,
    paddingTop: 6,
    paddingBottom: 28,
    paddingHorizontal: 20,
    borderTopLeftRadius: 22,
    borderTopRightRadius: 22,
    borderTopWidth: 1,
    borderColor: COLORS.borderDefault,
  },
  handle: {
    alignSelf: "center",
    width: 40,
    height: 4,
    borderRadius: 2,
    backgroundColor: COLORS.borderDefault,
    marginBottom: 12,
  },
  header: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingBottom: 16,
    borderBottomWidth: 1,
    borderColor: COLORS.borderDefault,
  },
  title: {
    color: COLORS.textPrimary,
    fontSize: 14,
    fontWeight: "900",
    letterSpacing: 2,
  },
  section: { paddingVertical: 16 },
  sectionHeader: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    marginBottom: 4,
  },
  sectionTitleRow: { flexDirection: "row", alignItems: "center", gap: 6 },
  sectionTitle: {
    color: COLORS.textSecondary,
    fontSize: 11,
    fontWeight: "800",
    letterSpacing: 1.4,
  },
  value: {
    color: COLORS.textPrimary,
    fontSize: 14,
    fontWeight: "900",
  },
  slider: { width: "100%", height: 36 },
  scaleRow: {
    flexDirection: "row",
    justifyContent: "space-between",
    marginTop: 2,
  },
  scaleLabel: {
    color: COLORS.textMuted,
    fontSize: 10,
    fontWeight: "700",
    letterSpacing: 1,
  },
  sublabel: {
    color: COLORS.textMuted,
    fontSize: 10,
    fontWeight: "700",
    letterSpacing: 1,
    marginTop: 8,
  },
  hint: {
    color: COLORS.textMuted,
    fontSize: 11,
    marginTop: 8,
    lineHeight: 16,
  },
  actions: {
    flexDirection: "row",
    gap: 10,
    marginTop: 8,
    paddingTop: 12,
    borderTopWidth: 1,
    borderColor: COLORS.borderDefault,
  },
  resetBtn: {
    flex: 1,
    paddingVertical: 14,
    borderRadius: 10,
    borderWidth: 1,
    borderColor: COLORS.borderDefault,
    alignItems: "center",
  },
  resetText: {
    color: COLORS.textSecondary,
    fontWeight: "900",
    letterSpacing: 1.4,
    fontSize: 12,
  },
  applyBtn: {
    flex: 2,
    paddingVertical: 14,
    borderRadius: 10,
    backgroundColor: COLORS.voltBlue,
    alignItems: "center",
  },
  applyText: {
    color: COLORS.bg,
    fontWeight: "900",
    letterSpacing: 1.4,
    fontSize: 12,
  },
  fbBtn: {
    flexDirection: "row",
    alignItems: "center",
    gap: 5,
    paddingHorizontal: 11,
    paddingVertical: 7,
    borderRadius: 10,
    borderWidth: 1,
    borderColor: COLORS.borderDefault,
    backgroundColor: COLORS.surface,
  },
  fbBtnActive: {
    backgroundColor: COLORS.voltBlue,
    borderColor: COLORS.voltBlue,
  },
  fbText: {
    color: COLORS.textSecondary,
    fontSize: 11,
    fontWeight: "800",
    letterSpacing: 1.1,
  },
  fbTextActive: { color: COLORS.bg, fontWeight: "900" },
});
