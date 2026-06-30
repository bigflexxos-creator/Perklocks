/**
 * MLB HR small CTA — compact one-line chip under the MLB section that
 * routes to the full HR slate (/hr). Was previously an inline banner
 * showing the top 5 picks, but the user reported "It's blocking app
 * need just small tab under mlb" — so this is now a minimal-footprint
 * button matching the NRFI/YRFI CTA style. Tap → full slate screen.
 */
import React from "react";
import { StyleSheet, Text, TouchableOpacity, View } from "react-native";
import { useRouter } from "expo-router";
import { COLORS } from "@/src/theme";

export function MLBHRBanner() {
  const router = useRouter();
  return (
    <TouchableOpacity
      onPress={() => router.push("/hr" as any)}
      style={styles.btn}
      activeOpacity={0.8}
      testID="mlb-hr-cta"
    >
      <Text style={styles.icon}>💣</Text>
      <View style={{ flex: 1 }}>
        <Text style={styles.title}>HR PICKS</Text>
        <Text style={styles.sub}>Top 5 of the day · park × pitcher × wind × form</Text>
      </View>
      <Text style={styles.chevron}>›</Text>
    </TouchableOpacity>
  );
}

const styles = StyleSheet.create({
  btn: {
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
    paddingHorizontal: 14,
    paddingVertical: 12,
    backgroundColor: COLORS.surface,
    borderRadius: 12,
    borderWidth: 1,
    borderColor: COLORS.borderDefault,
    marginBottom: 12,
  },
  icon:    { fontSize: 20 },
  title:   { color: COLORS.textPrimary, fontSize: 13, fontWeight: "900",
             letterSpacing: 0.6 },
  sub:     { color: COLORS.textMuted, fontSize: 11, marginTop: 2 },
  chevron: { color: COLORS.textMuted, fontSize: 20, fontWeight: "300" },
});
