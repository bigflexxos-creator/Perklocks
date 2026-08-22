import React, { useEffect, useRef } from "react";
import { View, Text, StyleSheet, Animated, Pressable, Platform } from "react-native";
import { Ionicons } from "@expo/vector-icons";
import { COLORS } from "@/src/theme";

/**
 * PremiumHeader — Locks-Mockup 2026-08-22 shared header for the app's
 * primary tabs (Locks, Rollover, Parlay, My Bets, Profile).
 *
 * Visual language:
 *   • Luminous gold wordmark (bright #FFD700 with radial glow)
 *   • Optional tagline (small gold caps)
 *   • Optional right-side action (defaults to nothing; screens can
 *     pass a custom child element like an update pill or history btn)
 *   • Optional pulsing live-status dot + label (e.g. "Updated now")
 *
 * Backend/data untouched. Structure identical to each screen's
 * existing header so we don't disrupt layouts.
 */
export function PremiumHeader({
  title,
  tagline,
  subtitle,
  status,
  right,
}: {
  title: string;
  tagline?: string;
  subtitle?: string;
  status?: {
    /** Text label like "Updated just now" */
    label: string;
    /** Color of the pulsing dot (default: neonGreen) */
    color?: string;
  };
  right?: React.ReactNode;
}) {
  const pulse = useRef(new Animated.Value(0)).current;
  useEffect(() => {
    if (!status) return;
    const loop = Animated.loop(
      Animated.sequence([
        Animated.timing(pulse, { toValue: 1, duration: 900, useNativeDriver: true }),
        Animated.timing(pulse, { toValue: 0, duration: 900, useNativeDriver: true }),
      ]),
    );
    loop.start();
    return () => loop.stop();
  }, [pulse, status]);
  const dotOpacity = pulse.interpolate({ inputRange: [0, 1], outputRange: [0.55, 1] });

  return (
    <View style={styles.header}>
      <View style={{ flex: 1 }}>
        <Text style={styles.brand}>{title}</Text>
        {tagline && <Text style={styles.tagline}>{tagline}</Text>}
        {subtitle && <Text style={styles.subtitle}>{subtitle}</Text>}
        {status && (
          <View style={styles.statusRow}>
            <Animated.View
              style={[
                styles.statusDot,
                { backgroundColor: status.color || COLORS.neonGreen, opacity: dotOpacity, shadowColor: status.color || COLORS.neonGreen },
              ]}
            />
            <Text style={[styles.statusLabel, { color: status.color || COLORS.neonGreen }]}>
              {status.label}
            </Text>
          </View>
        )}
      </View>
      {right}
    </View>
  );
}

/**
 * Small circular icon button styled to match the Locks-screen refresh
 * button — black glass with luminous gold ring. Used as the standard
 * right-side header action across screens.
 */
export function GoldIconButton({
  icon,
  onPress,
  testID,
  disabled,
  label,
}: {
  icon: React.ComponentProps<typeof Ionicons>["name"];
  onPress: () => void;
  testID?: string;
  disabled?: boolean;
  label?: string;
}) {
  return (
    <Pressable
      testID={testID}
      onPress={onPress}
      disabled={disabled}
      style={[styles.goldIconBtn, disabled && { opacity: 0.6 }]}
      hitSlop={10}
    >
      {label ? (
        <Text style={styles.goldIconLabel}>{label}</Text>
      ) : (
        <Ionicons name={icon} size={20} color={COLORS.goldElite} />
      )}
    </Pressable>
  );
}

const styles = StyleSheet.create({
  header: {
    paddingHorizontal: 20, paddingTop: 8, paddingBottom: 14,
    flexDirection: "row", justifyContent: "space-between", alignItems: "flex-end",
  },
  brand: {
    fontSize: 30,
    fontWeight: "900",
    color: COLORS.goldElite,
    letterSpacing: 5,
    textShadowColor: "rgba(255,215,0,0.75)",
    textShadowOffset: { width: 0, height: 0 },
    textShadowRadius: 18,
  },
  tagline: {
    fontSize: 10,
    fontWeight: "800",
    color: COLORS.goldRich,
    letterSpacing: 3.2,
    marginTop: 4,
    marginBottom: 2,
    opacity: 0.95,
  },
  subtitle: {
    fontSize: 11.5,
    color: COLORS.textSecondary,
    fontWeight: "600",
    marginTop: 6,
    letterSpacing: 0.4,
  },
  statusRow: { flexDirection: "row", alignItems: "center", gap: 6, marginTop: 5 },
  statusDot: {
    width: 7, height: 7, borderRadius: 4,
    shadowOpacity: 0.85, shadowRadius: 5,
    shadowOffset: { width: 0, height: 0 },
  },
  statusLabel: { fontSize: 11, fontWeight: "800", letterSpacing: 0.5 },
  // Gold-ringed action button (used on right of screen headers).
  goldIconBtn: {
    minWidth: 44, height: 44, borderRadius: 22,
    backgroundColor: "rgba(0,0,0,0.65)",
    alignItems: "center", justifyContent: "center",
    borderWidth: 1.4, borderColor: "rgba(255,215,0,0.65)",
    paddingHorizontal: 10,
    shadowColor: "#FFD700",
    shadowOpacity: 0.55,
    shadowRadius: 10,
    shadowOffset: { width: 0, height: 0 },
    ...Platform.select({ android: { elevation: 6 } }),
  },
  goldIconLabel: {
    color: COLORS.goldElite,
    fontSize: 11,
    fontWeight: "900",
    letterSpacing: 1.3,
    fontVariant: ["tabular-nums"],
  },
});
