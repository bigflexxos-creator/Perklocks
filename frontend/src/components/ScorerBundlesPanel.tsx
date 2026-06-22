/**
 * ScorerBundlesPanel — synthesized goalscorer bundle markets.
 *
 * Surfaces backend-computed Poisson-inverted bundles for any Anytime Goal
 * Scorer pick:
 *   • 2+ Goals
 *   • Hat Trick (3+ Goals)
 *   • Goal + Assist (SGP)
 *
 * Bundle prices are MODEL ESTIMATES, not bookmaker lines — labelled clearly
 * so the user understands these are derived prices to size edge expectations.
 *
 * Non-eligible picks (non-soccer / non-anytime markets) silently return null
 * so the panel doesn't take up real estate on irrelevant detail screens.
 */
import React, { useEffect, useState } from "react";
import { View, Text, StyleSheet, ActivityIndicator } from "react-native";
import { COLORS } from "@/src/theme";
import { api, Pick } from "@/src/lib/api";

type BundlesResp = Awaited<ReturnType<typeof api.pickScorerBundles>>;

export function ScorerBundlesPanel({ pick }: { pick: Pick }) {
  const [data, setData] = useState<BundlesResp | null>(null);
  const [loading, setLoading] = useState(true);

  // Cheap pre-check — avoids a network round-trip for picks we know won't
  // qualify (saves 60+ ms on every non-soccer / non-goalscorer detail open).
  const marketL = (pick.market || "").toLowerCase();
  const couldBeEligible =
    pick.sport === "Soccer" &&
    marketL.includes("goal scorer") &&
    !marketL.includes("first") &&
    !marketL.includes("last");

  useEffect(() => {
    if (!couldBeEligible) {
      setLoading(false);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const r = await api.pickScorerBundles(pick.id);
        if (!cancelled) {
          setData(r);
          setLoading(false);
        }
      } catch {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [pick.id, couldBeEligible]);

  if (!couldBeEligible) return null;

  if (loading) {
    return (
      <View style={styles.card}>
        <ActivityIndicator color={COLORS.textMuted} />
      </View>
    );
  }
  if (!data || !data.eligible) return null;
  if (data.eligible && !data.synthesizable) {
    return (
      <View style={styles.card}>
        <Text style={styles.title}>Scorer Bundles</Text>
        <Text style={styles.note}>{data.note ?? "Not synthesizable for this market."}</Text>
      </View>
    );
  }
  const bundles = data.bundles ?? [];

  return (
    <View style={styles.card}>
      <View style={styles.headerRow}>
        <Text style={styles.title}>Scorer Bundles</Text>
        <Text style={styles.modelChip}>📐 MODEL</Text>
      </View>
      {data.player ? (
        <Text style={styles.subtitle} numberOfLines={1}>
          {data.player}
          {typeof data["expected_goals_λ"] === "number"
            ? `  ·  Expected goals λ ${data["expected_goals_λ"].toFixed(2)}`
            : ""}
        </Text>
      ) : null}
      <View style={styles.rows}>
        {bundles.map((b) => {
          const isPrimary = b.type === "primary";
          return (
            <View
              key={b.name}
              style={[
                styles.row,
                isPrimary && styles.rowPrimary,
              ]}
            >
              <View style={styles.rowLeft}>
                <Text style={[styles.rowName, isPrimary && styles.rowNamePrimary]}>
                  {b.name}
                </Text>
                {isPrimary ? (
                  <Text style={styles.bookTag}>BOOK LINE</Text>
                ) : (
                  <Text style={styles.synthTag}>MODEL</Text>
                )}
              </View>
              <View style={styles.rowRight}>
                <Text style={[styles.rowOdds, isPrimary && styles.rowOddsPrimary]}>
                  {b.fair_american}
                </Text>
                <Text style={styles.rowProb}>{b.probability.toFixed(1)}%</Text>
              </View>
            </View>
          );
        })}
      </View>
      <Text style={styles.footnote} numberOfLines={3}>
        {data.method}
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  card: {
    marginHorizontal: 16,
    marginTop: 12,
    padding: 14,
    borderRadius: 14,
    backgroundColor: COLORS.surface,
    borderWidth: 1,
    borderColor: COLORS.borderDefault,
  },
  headerRow: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    marginBottom: 4,
  },
  title: {
    color: COLORS.textPrimary,
    fontSize: 16,
    fontWeight: "800",
    letterSpacing: 0.2,
  },
  modelChip: {
    color: "#C4B5FD",
    fontSize: 10,
    fontWeight: "800",
    letterSpacing: 0.8,
    backgroundColor: "rgba(167,139,250,0.18)",
    paddingHorizontal: 6,
    paddingVertical: 2,
    borderRadius: 6,
    overflow: "hidden",
  },
  subtitle: {
    color: COLORS.textSecondary,
    fontSize: 12.5,
    fontWeight: "600",
    marginBottom: 10,
  },
  rows: {
    gap: 6,
  },
  row: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingVertical: 10,
    paddingHorizontal: 10,
    borderRadius: 10,
    backgroundColor: "rgba(255,255,255,0.04)",
    borderWidth: 1,
    borderColor: "rgba(255,255,255,0.10)",
  },
  rowPrimary: {
    backgroundColor: "rgba(99,102,241,0.14)",
    borderColor: "rgba(99,102,241,0.40)",
  },
  rowLeft: {
    flex: 1,
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
  },
  rowName: {
    color: COLORS.textPrimary,
    fontSize: 13.5,
    fontWeight: "700",
  },
  rowNamePrimary: {
    color: "#E0E7FF",
  },
  rowRight: {
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
  },
  rowOdds: {
    color: COLORS.textPrimary,
    fontSize: 14,
    fontWeight: "800",
    fontVariant: ["tabular-nums"],
    minWidth: 48,
    textAlign: "right",
  },
  rowOddsPrimary: {
    color: "#E0E7FF",
  },
  rowProb: {
    color: COLORS.textSecondary,
    fontSize: 12,
    fontWeight: "700",
    fontVariant: ["tabular-nums"],
    minWidth: 44,
    textAlign: "right",
  },
  bookTag: {
    color: "#67E8F9",
    fontSize: 9,
    fontWeight: "800",
    letterSpacing: 0.7,
    backgroundColor: "rgba(34,211,238,0.16)",
    paddingHorizontal: 5,
    paddingVertical: 1,
    borderRadius: 4,
    overflow: "hidden",
  },
  synthTag: {
    color: "#C4B5FD",
    fontSize: 9,
    fontWeight: "800",
    letterSpacing: 0.7,
    backgroundColor: "rgba(167,139,250,0.14)",
    paddingHorizontal: 5,
    paddingVertical: 1,
    borderRadius: 4,
    overflow: "hidden",
  },
  note: {
    color: COLORS.textSecondary,
    fontSize: 12,
    fontStyle: "italic",
    marginTop: 4,
  },
  footnote: {
    marginTop: 10,
    color: COLORS.textSecondary,
    fontSize: 11,
    lineHeight: 15,
    fontStyle: "italic",
  },
});
