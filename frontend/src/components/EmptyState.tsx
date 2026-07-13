/**
 * EmptyState — Milestone 1.2 (Empty-market UX polish).
 *
 * Reusable empty / error state used across every tab. Replaces the
 * inconsistent one-off "no data" blocks with a single visual language:
 *
 *   [icon]
 *   Big Title
 *   Softer helper message
 *   [OPTIONAL CTA button]
 *   Divider
 *   [OPTIONAL secondary hint text]
 *
 * When `variant="error"` the surface gets a subtle red tint + shows a
 * RETRY button when `onRetry` is provided.
 */
import React from "react";
import { StyleSheet, Text, TouchableOpacity, View } from "react-native";
import { Ionicons } from "@expo/vector-icons";
import { COLORS } from "../theme";

type Variant = "empty" | "error" | "info";

type Props = {
  icon?: keyof typeof Ionicons.glyphMap;
  title: string;
  message?: string;
  ctaLabel?: string;
  onCtaPress?: () => void;
  onRetry?: () => void;
  secondaryHint?: string;
  variant?: Variant;
  testID?: string;
};

const iconForVariant = (v: Variant): keyof typeof Ionicons.glyphMap => {
  if (v === "error") return "warning-outline";
  if (v === "info") return "information-circle-outline";
  return "lock-open-outline";
};

const colorForVariant = (v: Variant) => {
  if (v === "error") return { fg: "#ffb4b4", bg: "rgba(255,88,88,0.10)", border: "rgba(255,88,88,0.35)" };
  if (v === "info") return { fg: COLORS.voltBlue, bg: "rgba(0,122,255,0.08)", border: "rgba(0,122,255,0.28)" };
  return { fg: COLORS.textMuted, bg: COLORS.surfaceElevated, border: COLORS.borderDefault };
};

export const EmptyState: React.FC<Props> = ({
  icon,
  title,
  message,
  ctaLabel,
  onCtaPress,
  onRetry,
  secondaryHint,
  variant = "empty",
  testID,
}) => {
  const c = colorForVariant(variant);
  return (
    <View
      style={[styles.wrap, { backgroundColor: c.bg, borderColor: c.border }]}
      testID={testID}
    >
      <Ionicons name={icon || iconForVariant(variant)} size={42} color={c.fg} />
      <Text style={styles.title}>{title}</Text>
      {!!message && <Text style={styles.msg}>{message}</Text>}
      {(!!ctaLabel && !!onCtaPress) && (
        <TouchableOpacity
          onPress={onCtaPress}
          activeOpacity={0.85}
          style={styles.cta}
          testID={testID ? `${testID}-cta` : undefined}
        >
          <Text style={styles.ctaTxt}>{ctaLabel}</Text>
        </TouchableOpacity>
      )}
      {!!onRetry && (
        <TouchableOpacity
          onPress={onRetry}
          activeOpacity={0.85}
          style={[styles.cta, styles.ctaGhost]}
          testID={testID ? `${testID}-retry` : undefined}
        >
          <Ionicons name="refresh" size={14} color={COLORS.textPrimary} />
          <Text style={[styles.ctaTxt, { marginLeft: 6 }]}>RETRY</Text>
        </TouchableOpacity>
      )}
      {!!secondaryHint && (
        <>
          <View style={styles.divider} />
          <Text style={styles.hint}>{secondaryHint}</Text>
        </>
      )}
    </View>
  );
};

const styles = StyleSheet.create({
  wrap: {
    marginTop: 20,
    marginHorizontal: 4,
    padding: 22,
    borderRadius: 16,
    alignItems: "center",
    borderWidth: 1,
  },
  title: {
    color: COLORS.textPrimary,
    fontSize: 17,
    fontWeight: "800",
    marginTop: 12,
    textAlign: "center",
  },
  msg: {
    color: COLORS.textSecondary,
    fontSize: 13,
    lineHeight: 18,
    marginTop: 8,
    textAlign: "center",
    maxWidth: 340,
  },
  cta: {
    marginTop: 16,
    paddingHorizontal: 18,
    paddingVertical: 11,
    borderRadius: 10,
    backgroundColor: COLORS.voltBlue,
    flexDirection: "row",
    alignItems: "center",
  },
  ctaGhost: {
    backgroundColor: "rgba(255,255,255,0.06)",
    borderWidth: 1,
    borderColor: COLORS.borderDefault,
  },
  ctaTxt: {
    color: COLORS.textPrimary,
    fontSize: 12,
    fontWeight: "800",
    letterSpacing: 0.6,
  },
  divider: {
    marginTop: 16,
    height: 1,
    alignSelf: "stretch",
    backgroundColor: COLORS.borderDefault,
  },
  hint: {
    color: COLORS.textMuted,
    fontSize: 11,
    lineHeight: 15,
    marginTop: 10,
    textAlign: "center",
    fontStyle: "italic",
  },
});
