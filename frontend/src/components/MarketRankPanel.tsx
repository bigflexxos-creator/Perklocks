/**
 * MarketRankPanel — "Other markets in this match" insight card.
 *
 * For any pick on a match where parallel markets exist (e.g., a soccer match
 * with Double Chance + Over 1.5 + BTTS), surface the ranked alternatives so
 * the user sees if their current pick is the BEST setup or if there's a
 * stronger market available.
 *
 * Tap an alternative row to navigate to that pick's detail.
 */
import React, { useEffect, useState } from "react";
import { View, Text, StyleSheet, TouchableOpacity, ActivityIndicator } from "react-native";
import { router as expoRouter } from "expo-router";
import { COLORS } from "@/src/theme";
import { api, Pick } from "@/src/lib/api";

type Rank = Awaited<ReturnType<typeof api.pickMarketRank>>;

export function MarketRankPanel({ pick }: { pick: Pick }) {
  const [data, setData] = useState<Rank | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await api.pickMarketRank(pick.id);
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
  }, [pick.id]);

  if (loading) {
    return (
      <View style={styles.wrap}>
        <View style={styles.headerRow}>
          <Text style={styles.sectionLabel}>MARKET COMPETITION</Text>
          <View style={styles.shadowChip}>
            <Text style={styles.shadowChipText}>RANKED</Text>
          </View>
        </View>
        <View style={styles.loadingRow}>
          <ActivityIndicator size="small" color={COLORS.voltBlue} />
          <Text style={styles.muted}>Ranking parallel markets…</Text>
        </View>
      </View>
    );
  }

  if (!data || !data.ranked || data.ranked.length <= 1) {
    return null; // Hide when only the current pick exists
  }

  const ranked = data.ranked;
  const best = ranked[0];
  const currentIdx = ranked.findIndex((r) => r.is_current);
  const currentIsBest = currentIdx === 0;

  return (
    <View style={styles.wrap}>
      <View style={styles.headerRow}>
        <View style={{ flex: 1 }}>
          <Text style={styles.sectionLabel}>MARKET COMPETITION</Text>
          <Text style={styles.tagline}>
            All available markets for this match, ranked by setup quality.
          </Text>
        </View>
        <View style={styles.shadowChip}>
          <Text style={styles.shadowChipText}>RANKED</Text>
        </View>
      </View>

      {/* Verdict */}
      <View style={[
        styles.verdictBox,
        { borderColor: currentIsBest ? COLORS.neonGreen + "55" : "#F59E0B55" },
      ]}>
        <Text style={[
          styles.verdictLabel,
          { color: currentIsBest ? COLORS.neonGreen : "#F59E0B" },
        ]}>
          {currentIsBest ? "✓ BEST SETUP IN THIS MATCH" : "ALTERNATIVE STRONGER"}
        </Text>
        <Text style={styles.verdictDetail}>
          {currentIsBest
            ? `Your pick scores ${best.market_score.toFixed(0)} \u2014 leads ${ranked.length - 1} other market${ranked.length > 2 ? "s" : ""}.`
            : `${best.short_market} scores ${best.market_score.toFixed(0)} vs your pick at ${(ranked[currentIdx]?.market_score ?? 0).toFixed(0)}.`}
        </Text>
      </View>

      {/* Ranked rows */}
      <View style={styles.list}>
        {ranked.slice(0, 4).map((r, idx) => {
          const isBest = idx === 0;
          const tint = isBest ? COLORS.neonGreen : COLORS.textSecondary;
          return (
            <TouchableOpacity
              key={r.id}
              activeOpacity={r.is_current ? 1 : 0.7}
              onPress={() => {
                if (!r.is_current) expoRouter.push(`/pick/${r.id}` as any);
              }}
              style={[
                styles.row,
                r.is_current && { borderColor: COLORS.voltBlue + "55", backgroundColor: COLORS.voltBlue + "08" },
              ]}
            >
              <View style={[styles.rankBadge, { borderColor: tint + "55" }]}>
                <Text style={[styles.rankBadgeText, { color: tint }]}>
                  {idx + 1}
                </Text>
              </View>
              <View style={{ flex: 1 }}>
                <View style={styles.rowTop}>
                  <Text style={styles.rowMarket} numberOfLines={1}>
                    {r.short_market}
                  </Text>
                  {isBest && (
                    <View style={styles.bestChip}>
                      <Text style={styles.bestChipText}>BEST</Text>
                    </View>
                  )}
                  {r.is_current && (
                    <View style={styles.currentChip}>
                      <Text style={styles.currentChipText}>CURRENT</Text>
                    </View>
                  )}
                </View>
                <Text style={styles.rowSub} numberOfLines={1}>
                  {r.market}
                </Text>
              </View>
              <View style={styles.rowRight}>
                <Text style={[styles.rowLock, { color: tint }]}>
                  {(r.lock_score_v2 ?? r.lock_score ?? 0).toFixed(0)}
                </Text>
                <Text style={styles.rowLockUnit}>LOCK</Text>
              </View>
            </TouchableOpacity>
          );
        })}
      </View>

      <Text style={styles.footnote}>
        Score = prob·30% + edge·35% + survival·20% − var·10% − counter·5%
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: {
    backgroundColor: COLORS.surface,
    borderRadius: 14,
    padding: 16,
    borderWidth: 1,
    borderColor: COLORS.borderDefault,
    marginTop: 8,
  },
  headerRow: {
    flexDirection: "row",
    alignItems: "flex-start",
    gap: 12,
  },
  sectionLabel: {
    color: COLORS.textMuted,
    fontSize: 10,
    fontWeight: "800",
    letterSpacing: 1.6,
  },
  tagline: {
    color: COLORS.textSecondary,
    fontSize: 12,
    fontWeight: "500",
    lineHeight: 17,
    marginTop: 6,
  },
  shadowChip: {
    paddingHorizontal: 7,
    paddingVertical: 3,
    borderRadius: 4,
    borderWidth: 1,
    borderColor: COLORS.voltBlue + "55",
    backgroundColor: COLORS.voltBlue + "12",
  },
  shadowChipText: {
    color: COLORS.voltBlue,
    fontSize: 9,
    fontWeight: "900",
    letterSpacing: 1.2,
  },
  loadingRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
    paddingVertical: 14,
  },
  muted: { color: COLORS.textMuted, fontSize: 11, fontWeight: "600" },

  verdictBox: {
    marginTop: 14,
    borderWidth: 1,
    borderRadius: 10,
    padding: 12,
    backgroundColor: "rgba(255,255,255,0.02)",
  },
  verdictLabel: {
    fontSize: 11,
    fontWeight: "900",
    letterSpacing: 1.0,
  },
  verdictDetail: {
    color: COLORS.textSecondary,
    fontSize: 11.5,
    lineHeight: 16,
    marginTop: 4,
  },

  list: { marginTop: 14, gap: 8 },
  row: {
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
    paddingVertical: 10,
    paddingHorizontal: 10,
    borderRadius: 10,
    borderWidth: 1,
    borderColor: COLORS.borderDefault,
    backgroundColor: "rgba(255,255,255,0.02)",
  },
  rankBadge: {
    width: 24,
    height: 24,
    borderRadius: 12,
    borderWidth: 1.5,
    alignItems: "center",
    justifyContent: "center",
  },
  rankBadgeText: { fontSize: 12, fontWeight: "900" },
  rowTop: {
    flexDirection: "row",
    alignItems: "center",
    gap: 7,
  },
  rowMarket: {
    color: COLORS.textPrimary,
    fontSize: 13,
    fontWeight: "800",
    letterSpacing: -0.2,
  },
  rowSub: {
    color: COLORS.textMuted,
    fontSize: 10.5,
    fontWeight: "600",
    marginTop: 2,
  },
  bestChip: {
    paddingHorizontal: 5,
    paddingVertical: 1,
    borderRadius: 3,
    backgroundColor: COLORS.neonGreen + "20",
    borderWidth: 1,
    borderColor: COLORS.neonGreen + "55",
  },
  bestChipText: {
    color: COLORS.neonGreen,
    fontSize: 8,
    fontWeight: "900",
    letterSpacing: 0.8,
  },
  currentChip: {
    paddingHorizontal: 5,
    paddingVertical: 1,
    borderRadius: 3,
    backgroundColor: COLORS.voltBlue + "20",
    borderWidth: 1,
    borderColor: COLORS.voltBlue + "55",
  },
  currentChipText: {
    color: COLORS.voltBlue,
    fontSize: 8,
    fontWeight: "900",
    letterSpacing: 0.8,
  },
  rowRight: { alignItems: "flex-end" },
  rowLock: {
    fontSize: 18,
    fontWeight: "900",
    letterSpacing: -0.3,
    fontVariant: ["tabular-nums"],
  },
  rowLockUnit: {
    color: COLORS.textMuted,
    fontSize: 8,
    fontWeight: "800",
    letterSpacing: 0.8,
    marginTop: -2,
  },

  footnote: {
    color: COLORS.textMuted,
    fontSize: 9.5,
    fontWeight: "600",
    fontStyle: "italic",
    letterSpacing: 0.3,
    marginTop: 12,
    textAlign: "center",
  },
});
