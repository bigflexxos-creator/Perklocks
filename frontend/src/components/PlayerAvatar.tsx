/**
 * P3 — PlayerAvatar: verified headshot → provider headshot → team logo →
 * sport icon → initials.  Every image failure is an OPTIONAL failure —
 * it degrades to the next fallback and can never surface as an error.
 * Uses expo-image native memory/disk caching.
 */
import React, { useState } from "react";
import { View, Text, StyleSheet } from "react-native";
import { Image } from "expo-image";
import { Ionicons } from "@expo/vector-icons";
import { COLORS } from "@/src/theme";

const SPORT_ICON: Record<string, React.ComponentProps<typeof Ionicons>["name"]> = {
  NFL: "american-football-outline",
  CFB: "american-football-outline",
  MLB: "baseball-outline",
  NBA: "basketball-outline",
  SOCCER: "football-outline",
  TENNIS: "tennisball-outline",
};

export function PlayerAvatar({
  name, sport, headshotUrl, providerHeadshotUrl, teamLogoUrl, size = 52, testID,
}: {
  name?: string | null;
  sport?: string | null;
  headshotUrl?: string | null;          // verified
  providerHeadshotUrl?: string | null;  // unverified provider
  teamLogoUrl?: string | null;
  size?: number;
  testID?: string;
}) {
  const chain = [headshotUrl, providerHeadshotUrl, teamLogoUrl].filter(
    (u): u is string => typeof u === "string" && u.startsWith("http"),
  );
  const [idx, setIdx] = useState(0);
  const uri = chain[idx];
  const radius = size / 2;
  if (uri) {
    return (
      <Image
        testID={testID}
        source={{ uri }}
        style={{ width: size, height: size, borderRadius: radius, backgroundColor: COLORS.surfaceElevated }}
        contentFit="cover"
        cachePolicy="memory-disk"
        transition={120}
        onError={() => setIdx((i) => i + 1)}
        accessibilityLabel={name || "player"}
      />
    );
  }
  const icon = SPORT_ICON[String(sport || "").toUpperCase()];
  const initials = (name || "").split(/\s+/).filter(Boolean).map((w) => w[0]).join("").slice(0, 2).toUpperCase();
  return (
    <View testID={testID} style={[styles.fallback, { width: size, height: size, borderRadius: radius }]}>
      {initials ? (
        <Text style={[styles.initials, { fontSize: Math.max(11, size * 0.34) }]}>{initials}</Text>
      ) : icon ? (
        <Ionicons name={icon} size={size * 0.5} color={COLORS.textMuted} />
      ) : (
        <Ionicons name="person-outline" size={size * 0.5} color={COLORS.textMuted} />
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  fallback: {
    alignItems: "center", justifyContent: "center",
    backgroundColor: COLORS.surfaceElevated, borderWidth: 1, borderColor: COLORS.borderDefault,
  },
  initials: { color: COLORS.textSecondary, fontWeight: "900", letterSpacing: 0.5 },
});
