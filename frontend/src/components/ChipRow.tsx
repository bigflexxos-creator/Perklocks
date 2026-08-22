import React, { useEffect, useRef } from "react";
import {
  ScrollView, Pressable, Text, StyleSheet, View, Animated, Platform,
} from "react-native";
import { COLORS, getSportColor } from "@/src/theme";

/**
 * ChipRow — Locks Mockup 2026-08-22 visual upgrade.
 *
 * Sport-color neon accents (per mockup §3):
 *   • Each sport chip's active state uses its own neon accent
 *     (NFL=green, MLB=blue, NBA=purple, CFB=orange, NHL=cyan,
 *     Tennis=lime, Soccer=cyan). "All" uses luminous gold.
 *   • Inactive chips: dark elevated surface + thin neutral border.
 *   • Active chip: sport-tinted background + colored border + soft
 *     outer glow. A subtle scale-in animation on state change so
 *     the selection feels "alive" without being distracting.
 *
 * Public contract is unchanged (options / active / onChange /
 * testIDPrefix) so the Locks screen doesn't need to be rewired.
 */
export function ChipRow({
  options,
  active,
  onChange,
  testIDPrefix = "chip",
}: {
  options: readonly string[];
  active: string;
  onChange: (v: string) => void;
  testIDPrefix?: string;
}) {
  return (
    <View style={styles.wrapper}>
      <ScrollView
        horizontal
        showsHorizontalScrollIndicator={false}
        contentContainerStyle={styles.content}
      >
        {options.map((opt) => {
          const selected = opt === active;
          const color = getSportColor(opt);
          return (
            <AnimatedChip
              key={opt}
              label={opt}
              selected={selected}
              accent={color.accent}
              soft={color.soft}
              border={color.border}
              glow={color.glow}
              onPress={() => onChange(opt)}
              testID={`${testIDPrefix}-${opt.toLowerCase()}`}
            />
          );
        })}
      </ScrollView>
    </View>
  );
}

function AnimatedChip({
  label, selected, accent, soft, border, glow, onPress, testID,
}: {
  label: string;
  selected: boolean;
  accent: string;
  soft: string;
  border: string;
  glow: string;
  onPress: () => void;
  testID: string;
}) {
  // Smooth glow transition when selection changes — cheap Animated.timing
  // driven by `selected`. Skip on web where the Animated shadow value
  // triggers a full paint per frame on Chrome.
  const glowAnim = useRef(new Animated.Value(selected ? 1 : 0)).current;
  useEffect(() => {
    Animated.timing(glowAnim, {
      toValue: selected ? 1 : 0,
      duration: 220,
      useNativeDriver: false,
    }).start();
  }, [selected, glowAnim]);

  // Press feedback — subtle scale down on tap.
  const scale = useRef(new Animated.Value(1)).current;

  return (
    <Animated.View style={{ transform: [{ scale }] }}>
      <Pressable
        testID={testID}
        onPress={onPress}
        onPressIn={() => Animated.spring(scale, { toValue: 0.96, useNativeDriver: true, speed: 40 }).start()}
        onPressOut={() => Animated.spring(scale, { toValue: 1, useNativeDriver: true, speed: 40 }).start()}
        style={[
          styles.chip,
          selected
            ? {
                backgroundColor: soft,
                borderColor: border,
                borderWidth: 1.4,
                ...Platform.select({
                  ios: {
                    shadowColor: glow,
                    shadowOpacity: 0.55,
                    shadowRadius: 8,
                    shadowOffset: { width: 0, height: 0 },
                  },
                  android: { elevation: 6 },
                  default: {
                    shadowColor: glow,
                    shadowOpacity: 0.55,
                    shadowRadius: 8,
                    shadowOffset: { width: 0, height: 0 },
                  },
                }),
              }
            : styles.chipInactive,
        ]}
      >
        <Text
          style={[
            styles.chipText,
            selected && { color: accent, fontWeight: "900", letterSpacing: 0.9 },
          ]}
        >
          {label.toUpperCase()}
        </Text>
      </Pressable>
    </Animated.View>
  );
}

const styles = StyleSheet.create({
  wrapper: { height: 56, justifyContent: "center" },
  content: { paddingHorizontal: 16, gap: 8, alignItems: "center" },
  chip: {
    height: 34,
    paddingHorizontal: 14,
    borderRadius: 17,
    borderWidth: 1,
    alignItems: "center",
    justifyContent: "center",
    flexShrink: 0,
  },
  chipInactive: {
    backgroundColor: "rgba(255,255,255,0.03)",
    borderColor: "rgba(255,255,255,0.14)",
  },
  chipText: {
    color: COLORS.textSecondary,
    fontSize: 12,
    fontWeight: "700",
    letterSpacing: 0.7,
  },
});
