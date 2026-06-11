import React from "react";
import { ScrollView, Pressable, Text, StyleSheet, View } from "react-native";
import { COLORS } from "@/src/theme";

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
          return (
            <Pressable
              key={opt}
              testID={`${testIDPrefix}-${opt.toLowerCase()}`}
              onPress={() => onChange(opt)}
              style={[styles.chip, selected ? styles.chipActive : styles.chipInactive]}
            >
              <Text style={[styles.chipText, selected && styles.chipTextActive]}>{opt}</Text>
            </Pressable>
          );
        })}
      </ScrollView>
    </View>
  );
}

const styles = StyleSheet.create({
  wrapper: { height: 56, justifyContent: "center" },
  content: { paddingHorizontal: 16, gap: 8, alignItems: "center" },
  chip: {
    height: 36,
    paddingHorizontal: 16,
    borderRadius: 18,
    borderWidth: 1,
    alignItems: "center",
    justifyContent: "center",
    flexShrink: 0,
  },
  chipActive: { backgroundColor: COLORS.textPrimary, borderColor: COLORS.textPrimary },
  chipInactive: { backgroundColor: "transparent", borderColor: COLORS.borderDefault },
  chipText: { color: COLORS.textSecondary, fontSize: 13, fontWeight: "700", letterSpacing: 0.5 },
  chipTextActive: { color: COLORS.bg, fontWeight: "900" },
});
