/**
 * Skeleton primitives — Milestone 1.2 (Empty-market UX polish).
 *
 * Purpose: replace the plain `ActivityIndicator` spinner while data loads
 * with realistic shimmer placeholders that match the shape of the content
 * about to appear. Prevents the "blank frame" flicker between tab switch
 * and first paint, and gives the user tangible feedback that content is
 * on the way.
 *
 * Design: 60fps opacity pulse via react-native-reanimated. Every skeleton
 * respects the app's dark theme (COLORS.surfaceElevated with a subtle
 * lighter overlay). No layout thrash — same dimensions as the final
 * content so the picks slide into place without shifting.
 *
 * Usage:
 *   <SkeletonBlock w={140} h={16} />
 *   <SkeletonCircle size={28} />
 *   <PickCardSkeleton />            // pre-built for the main board
 */
import React, { useEffect } from "react";
import { StyleSheet, View, ViewStyle } from "react-native";
import Animated, {
  Easing,
  useAnimatedStyle,
  useSharedValue,
  withRepeat,
  withTiming,
} from "react-native-reanimated";
import { COLORS } from "../theme";

// Shared pulse — all skeletons in a tree tick together so it looks
// intentional rather than jittery.
const usePulseOpacity = () => {
  const v = useSharedValue(0.35);
  useEffect(() => {
    v.value = withRepeat(
      withTiming(0.85, { duration: 900, easing: Easing.inOut(Easing.ease) }),
      -1,
      true,
    );
    return () => {
      v.value = 0.35;
    };
  }, [v]);
  return useAnimatedStyle(() => ({ opacity: v.value }));
};

type BlockProps = {
  w?: number | "100%" | string;
  h?: number;
  radius?: number;
  style?: ViewStyle;
  testID?: string;
};

export const SkeletonBlock: React.FC<BlockProps> = ({
  w = "100%",
  h = 12,
  radius = 6,
  style,
  testID,
}) => {
  const pulse = usePulseOpacity();
  return (
    <Animated.View
      testID={testID}
      style={[
        styles.block,
        { width: w as any, height: h, borderRadius: radius },
        pulse,
        style,
      ]}
    />
  );
};

type CircleProps = { size: number; style?: ViewStyle; testID?: string };

export const SkeletonCircle: React.FC<CircleProps> = ({ size, style, testID }) => {
  const pulse = usePulseOpacity();
  return (
    <Animated.View
      testID={testID}
      style={[
        styles.block,
        { width: size, height: size, borderRadius: size / 2 },
        pulse,
        style,
      ]}
    />
  );
};

/**
 * PickCardSkeleton — matches the LockPickCard layout (icon + title +
 * meta row + lock badge). Sized so a stack of these takes the same
 * vertical space as the real card list, preventing layout shift.
 */
export const PickCardSkeleton: React.FC<{ testID?: string }> = ({ testID }) => {
  return (
    <View style={styles.cardWrap} testID={testID}>
      {/* Header row — team logos + lock badge */}
      <View style={styles.rowBetween}>
        <View style={styles.row}>
          <SkeletonCircle size={30} />
          <View style={{ width: 8 }} />
          <SkeletonCircle size={30} style={{ marginLeft: -14 }} />
          <View style={{ width: 12 }} />
          <View>
            <SkeletonBlock w={140} h={12} />
            <View style={{ height: 6 }} />
            <SkeletonBlock w={90} h={10} />
          </View>
        </View>
        <SkeletonBlock w={54} h={26} radius={13} />
      </View>
      {/* Market row */}
      <View style={{ height: 14 }} />
      <SkeletonBlock w={"85%"} h={14} />
      <View style={{ height: 8 }} />
      <SkeletonBlock w={"60%"} h={12} />
      {/* Bottom stats row */}
      <View style={{ height: 14 }} />
      <View style={styles.rowBetween}>
        <SkeletonBlock w={70} h={10} />
        <SkeletonBlock w={90} h={10} />
        <SkeletonBlock w={60} h={10} />
      </View>
    </View>
  );
};

/**
 * EventGroupSkeleton — matches the "GAME HEADER + 2 picks" shape used
 * by the main board's event-grouped view.
 */
export const EventGroupSkeleton: React.FC<{ picks?: number; testID?: string }> = ({
  picks = 2,
  testID,
}) => {
  return (
    <View style={styles.groupWrap} testID={testID}>
      {/* Event header */}
      <View style={styles.rowBetween}>
        <View style={styles.row}>
          <SkeletonBlock w={22} h={12} />
          <View style={{ width: 10 }} />
          <SkeletonBlock w={110} h={12} />
        </View>
        <SkeletonBlock w={64} h={10} />
      </View>
      <View style={{ height: 12 }} />
      {Array.from({ length: picks }).map((_, i) => (
        <View key={i} style={{ marginBottom: i < picks - 1 ? 10 : 0 }}>
          <PickCardSkeleton />
        </View>
      ))}
    </View>
  );
};

/**
 * SkeletonList — quick helper for pages that just need a vertical
 * stack of card placeholders (rollover, under, parlay etc.).
 */
export const SkeletonList: React.FC<{ count?: number; testID?: string }> = ({
  count = 4,
  testID,
}) => (
  <View testID={testID}>
    {Array.from({ length: count }).map((_, i) => (
      <View key={i} style={{ marginBottom: 12 }}>
        <PickCardSkeleton />
      </View>
    ))}
  </View>
);

const styles = StyleSheet.create({
  block: {
    backgroundColor: "rgba(255,255,255,0.10)",
  },
  cardWrap: {
    backgroundColor: COLORS.surfaceElevated,
    borderRadius: 14,
    padding: 14,
    borderWidth: 1,
    borderColor: COLORS.borderDefault,
  },
  groupWrap: {
    backgroundColor: COLORS.surface,
    borderRadius: 16,
    padding: 12,
    marginBottom: 14,
    borderWidth: 1,
    borderColor: COLORS.borderDefault,
  },
  row: { flexDirection: "row", alignItems: "center" },
  rowBetween: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
  },
});
