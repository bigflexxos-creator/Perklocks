import React, { useEffect, useState } from "react";
import {
  View, Text, StyleSheet, ScrollView, ActivityIndicator, Pressable,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { Ionicons } from "@expo/vector-icons";
import { useLocalSearchParams, useRouter } from "expo-router";
import { COLORS, GRADE_COLORS } from "@/src/theme";
import { api, Pick } from "@/src/lib/api";

export default function PickDetail() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const router = useRouter();
  const [pick, setPick] = useState<Pick | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!id) return;
    api.pickDetail(id)
      .then(setPick)
      .catch((e) => setError(e?.message || "Failed to load pick"))
      .finally(() => setLoading(false));
  }, [id]);

  const isKiller = pick && pick.lock_score < 85;
  const gradeColor = pick ? GRADE_COLORS[pick.grade] : COLORS.textMuted;

  return (
    <SafeAreaView
      style={[styles.safe, isKiller && { backgroundColor: COLORS.killerBg }]}
      edges={["top", "bottom"]}
    >
      <View style={styles.headerBar}>
        <Pressable
          testID="back-button"
          onPress={() => router.back()}
          hitSlop={12}
          style={styles.backBtn}
        >
          <Ionicons name="chevron-back" size={22} color={COLORS.textPrimary} />
        </Pressable>
        <Text style={styles.headerTitle}>
          {isKiller ? "BET KILLER" : "PICK BREAKDOWN"}
        </Text>
        <View style={{ width: 32 }} />
      </View>

      <ScrollView contentContainerStyle={styles.content} showsVerticalScrollIndicator={false}>
        {loading ? (
          <View style={styles.center}>
            <ActivityIndicator color={COLORS.voltBlue} />
          </View>
        ) : error ? (
          <Text style={styles.error}>{error}</Text>
        ) : pick ? (
          <>
            <View style={styles.tagRow}>
              <View style={[styles.tag, { backgroundColor: `${gradeColor}20`, borderColor: gradeColor }]}>
                <Text style={[styles.tagText, { color: gradeColor }]}>{pick.grade.toUpperCase()}</Text>
              </View>
              <Text style={styles.metaText}>
                {pick.sport} · {pick.league}
              </Text>
            </View>

            <Text style={styles.event}>{pick.event}</Text>
            <Text style={styles.market}>{pick.market}</Text>

            <View style={[styles.scoreWrap, { borderColor: gradeColor }]}>
              <Text style={[styles.scoreBig, { color: gradeColor }]}>
                {Math.round(pick.lock_score)}
              </Text>
              <Text style={styles.scoreSub}>LOCK SCORE</Text>
              <Text style={styles.confidence}>Confidence: {pick.confidence}</Text>
            </View>

            <View style={styles.bento}>
              <BentoCell label="WIN PROBABILITY" value={`${pick.win_probability}%`} />
              <BentoCell label="BOOK IMPLIED" value={`${pick.implied_probability}%`} muted />
              <BentoCell
                label="EDGE %"
                value={`${pick.edge_percent > 0 ? "+" : ""}${pick.edge_percent}%`}
                color={pick.edge_percent > 0 ? COLORS.neonGreen : COLORS.electricBlaze}
              />
              <BentoCell
                label="BOOK ODDS"
                value={pick.book_odds > 0 ? `+${pick.book_odds}` : `${pick.book_odds}`}
              />
            </View>

            <Text style={styles.sectionLabel}>
              {isKiller ? "WHY TO AVOID" : "WHY THIS PICK"}
            </Text>
            <View style={styles.explainCard}>
              <Text style={styles.explainText}>
                {pick.explanation || "Generating AI breakdown…"}
              </Text>
            </View>

            <Text style={styles.sectionLabel}>FACTOR BREAKDOWN</Text>
            <View style={styles.factorsCard}>
              {Object.entries(pick.factors).map(([k, v]) => (
                <View key={k} style={styles.factorRow}>
                  <Text style={styles.factorName}>{k}</Text>
                  <View style={styles.factorBarTrack}>
                    <View style={[
                      styles.factorBarFill,
                      { width: `${v}%`, backgroundColor: gradeColor },
                    ]} />
                  </View>
                  <Text style={[styles.factorValue, { color: gradeColor }]}>{Math.round(v)}</Text>
                </View>
              ))}
            </View>

            <Text style={styles.sectionLabel}>KEY INSIGHTS</Text>
            <View style={styles.insightsCard}>
              {pick.key_insights.map((i, idx) => (
                <View key={idx} style={styles.bullet}>
                  <View style={[styles.bulletDot, { backgroundColor: gradeColor }]} />
                  <Text style={styles.bulletText}>{i}</Text>
                </View>
              ))}
            </View>

            <Text style={styles.disclaimer}>
              Probabilistic forecast — no bet is guaranteed. Always bet responsibly.
            </Text>
          </>
        ) : null}
      </ScrollView>
    </SafeAreaView>
  );
}

function BentoCell({ label, value, color = COLORS.textPrimary, muted }: {
  label: string; value: string; color?: string; muted?: boolean;
}) {
  return (
    <View style={[styles.bentoCell, muted && { backgroundColor: "transparent" }]}>
      <Text style={styles.bentoLabel}>{label}</Text>
      <Text style={[styles.bentoValue, { color }]}>{value}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: COLORS.bg },
  headerBar: {
    flexDirection: "row", alignItems: "center", justifyContent: "space-between",
    paddingHorizontal: 16, paddingBottom: 10,
  },
  backBtn: {
    width: 36, height: 36, borderRadius: 18,
    backgroundColor: COLORS.surface, alignItems: "center", justifyContent: "center",
    borderWidth: 1, borderColor: COLORS.borderDefault,
  },
  headerTitle: { color: COLORS.textPrimary, fontWeight: "900", letterSpacing: 2, fontSize: 13 },
  content: { paddingHorizontal: 20, paddingBottom: 30 },
  center: { paddingVertical: 80, alignItems: "center" },
  error: { color: COLORS.electricBlaze, textAlign: "center", marginTop: 40 },

  tagRow: { flexDirection: "row", alignItems: "center", gap: 10, marginTop: 8, marginBottom: 10 },
  tag: { paddingHorizontal: 10, paddingVertical: 5, borderRadius: 6, borderWidth: 1 },
  tagText: { fontSize: 10, fontWeight: "900", letterSpacing: 1.4 },
  metaText: { color: COLORS.textMuted, fontSize: 11, fontWeight: "700", letterSpacing: 0.8 },

  event: { color: COLORS.textSecondary, fontSize: 14, fontWeight: "600" },
  market: { color: COLORS.textPrimary, fontSize: 24, fontWeight: "900", letterSpacing: -0.5, marginTop: 6 },

  scoreWrap: {
    alignItems: "center", padding: 24, marginTop: 18, marginBottom: 20,
    borderRadius: 20, borderWidth: 1, backgroundColor: COLORS.surface,
  },
  scoreBig: { fontSize: 78, fontWeight: "900", letterSpacing: -3, lineHeight: 84 },
  scoreSub: { color: COLORS.textMuted, fontSize: 10, fontWeight: "800", letterSpacing: 2 },
  confidence: { color: COLORS.textSecondary, fontSize: 12, fontWeight: "700", marginTop: 8, letterSpacing: 0.5 },

  bento: { flexDirection: "row", flexWrap: "wrap", marginHorizontal: -6, marginBottom: 8 },
  bentoCell: {
    width: "50%", padding: 6,
  },
  bentoLabel: { color: COLORS.textMuted, fontSize: 9, fontWeight: "800", letterSpacing: 1.3 },
  bentoValue: { fontSize: 24, fontWeight: "900", marginTop: 6, letterSpacing: -0.5 },

  sectionLabel: { color: COLORS.textMuted, fontSize: 10, fontWeight: "800", letterSpacing: 1.6, marginTop: 22, marginBottom: 10 },
  explainCard: {
    backgroundColor: COLORS.surface, padding: 18, borderRadius: 14,
    borderWidth: 1, borderColor: COLORS.borderDefault,
  },
  explainText: { color: COLORS.textPrimary, fontSize: 13, lineHeight: 21 },

  factorsCard: {
    backgroundColor: COLORS.surface, padding: 16, borderRadius: 14,
    borderWidth: 1, borderColor: COLORS.borderDefault,
  },
  factorRow: { flexDirection: "row", alignItems: "center", marginVertical: 6, gap: 10 },
  factorName: { flex: 1.5, color: COLORS.textSecondary, fontSize: 12, fontWeight: "600" },
  factorBarTrack: { flex: 2, height: 6, backgroundColor: "rgba(255,255,255,0.06)", borderRadius: 3, overflow: "hidden" },
  factorBarFill: { height: "100%" },
  factorValue: { width: 32, textAlign: "right", fontWeight: "900", fontSize: 12, letterSpacing: -0.3 },

  insightsCard: {
    backgroundColor: COLORS.surface, padding: 16, borderRadius: 14,
    borderWidth: 1, borderColor: COLORS.borderDefault,
  },
  bullet: { flexDirection: "row", alignItems: "flex-start", marginVertical: 6 },
  bulletDot: { width: 6, height: 6, borderRadius: 3, marginTop: 7, marginRight: 10 },
  bulletText: { flex: 1, color: COLORS.textPrimary, fontSize: 13, lineHeight: 20 },

  disclaimer: {
    color: COLORS.textMuted, fontSize: 11, lineHeight: 16,
    marginTop: 22, textAlign: "center",
  },
});
