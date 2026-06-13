import React from "react";
import { View, Text, Pressable, StyleSheet, Platform } from "react-native";
import { Ionicons } from "@expo/vector-icons";
import { useRouter, usePathname } from "expo-router";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { COLORS } from "@/src/theme";
import { useBetSlip, computeParlay, MAX_SLIP_SIZE } from "@/src/contexts/BetSlipContext";

// Floating "View Slip" pill anchored above the bottom tab bar. Renders only
// when the slip has 1+ picks and we're not already on /slip.
export function BetSlipFab() {
  const { count, picks } = useBetSlip();
  const router = useRouter();
  const pathname = usePathname();
  const insets = useSafeAreaInsets();

  if (count === 0) return null;
  if (pathname === "/slip") return null;

  const parlay = computeParlay(picks);
  // Tab bar = 64 + insets.bottom. Sit 12px above it.
  const bottom = 64 + insets.bottom + 12;
  const full = count >= MAX_SLIP_SIZE;

  return (
    <View style={[styles.wrap, { bottom, pointerEvents: "box-none" }]}>
      <Pressable
        testID="bet-slip-fab"
        onPress={() => router.push("/slip")}
        style={({ pressed }) => [
          styles.pill,
          pressed && { transform: [{ scale: 0.97 }], opacity: 0.95 },
        ]}
      >
        <View style={styles.badge}>
          <Text style={styles.badgeText}>{count}</Text>
        </View>
        <View style={{ flex: 1 }}>
          <Text style={styles.label}>VIEW SLIP</Text>
          <Text style={styles.sub} numberOfLines={1}>
            {parlay.americanOdds} · ${parlay.payoutOn100.toFixed(0)} on $100
          </Text>
        </View>
        {full ? (
          <View style={styles.fullTag}>
            <Text style={styles.fullTagText}>FULL</Text>
          </View>
        ) : (
          <Ionicons name="chevron-forward" size={18} color={COLORS.bg} />
        )}
      </Pressable>
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: {
    position: "absolute",
    left: 16,
    right: 16,
    alignItems: "center",
    zIndex: 100,
  },
  pill: {
    flexDirection: "row",
    alignItems: "center",
    backgroundColor: COLORS.goldElite,
    paddingVertical: 10,
    paddingHorizontal: 14,
    borderRadius: 999,
    gap: 12,
    minWidth: 240,
    maxWidth: 360,
    ...Platform.select({
      ios: {
        shadowColor: "#000",
        shadowOpacity: 0.45,
        shadowRadius: 12,
        shadowOffset: { width: 0, height: 6 },
      },
      android: { elevation: 8 },
    }),
  },
  badge: {
    width: 32,
    height: 32,
    borderRadius: 16,
    backgroundColor: COLORS.bg,
    alignItems: "center",
    justifyContent: "center",
  },
  badgeText: {
    color: COLORS.goldElite,
    fontSize: 14,
    fontWeight: "900",
    letterSpacing: -0.3,
  },
  label: {
    color: COLORS.bg,
    fontSize: 13,
    fontWeight: "900",
    letterSpacing: 1.4,
  },
  sub: {
    color: COLORS.bg,
    fontSize: 11,
    fontWeight: "700",
    opacity: 0.75,
    marginTop: 1,
  },
  fullTag: {
    backgroundColor: COLORS.electricBlaze,
    paddingHorizontal: 8,
    paddingVertical: 4,
    borderRadius: 6,
  },
  fullTagText: {
    color: COLORS.textPrimary,
    fontSize: 10,
    fontWeight: "900",
    letterSpacing: 1,
  },
});
