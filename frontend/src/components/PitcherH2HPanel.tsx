/**
 * PitcherH2HPanel — MLB strikeout pick only.
 *
 * Shows the pitcher's historical K performance vs the opposing team:
 *   • Season K avg vs that team
 *   • Total starts vs that team this season
 *   • Last 5 starts vs that team (date, IP, K)
 *
 * Self-fetches on mount via api.pitcherH2H(pick.id). Silently renders
 * nothing if the pick isn't an MLB strikeout market, or if the endpoint
 * has no data to return.
 */
import React, { useEffect, useState } from "react";
import { View, Text, StyleSheet, ActivityIndicator } from "react-native";
import { COLORS } from "@/src/theme";
import { api, Pick } from "@/src/lib/api";

type H2H = Awaited<ReturnType<typeof api.pitcherH2H>>;

export function PitcherH2HPanel({ pick }: { pick: Pick }) {
  const isMlbStrikeout =
    pick?.sport === "MLB" &&
    typeof pick?.market === "string" &&
    /strikeout/i.test(pick.market);

  const [data, setData] = useState<H2H | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!isMlbStrikeout) return;
    let cancelled = false;
    setLoading(true);
    setErr(null);
    api.pitcherH2H(pick.id)
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setErr(e?.message || "Failed to load H2H"); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [isMlbStrikeout, pick?.id]);

  if (!isMlbStrikeout) return null;

  if (loading) {
    return (
      <View>
        <Text style={styles.section}>PITCHER vs TEAM (H2H)</Text>
        <View style={styles.card}>
          <View style={styles.loadingRow}>
            <ActivityIndicator size="small" color={COLORS.voltBlue} />
            <Text style={styles.loadingTxt}>Pulling pitcher&apos;s K history vs opponent…</Text>
          </View>
        </View>
      </View>
    );
  }

  // Hard error from backend (pitcher not found, etc.) — don't render the card.
  if (err || !data || !data.ok) return null;

  const seasonAvg = data.season_avg_k ?? 0;
  const seasonStarts = data.season_starts ?? 0;
  const vsAvg = data.vs_team_avg_k ?? 0;
  const vsStarts = data.vs_team_starts ?? 0;
  const recent = data.vs_team_recent ?? [];

  // Hide entirely when we have neither season nor H2H data — no value to show.
  if (seasonStarts === 0 && vsStarts === 0) return null;

  // Compare H2H avg vs season avg to give context.
  const delta = vsStarts > 0 ? vsAvg - seasonAvg : 0;
  const trendColor =
    delta > 0.4 ? COLORS.neonGreen :
    delta < -0.4 ? COLORS.electricBlaze :
    COLORS.textSecondary;
  const trendLabel =
    delta > 0.4 ? "HOT vs OPP" :
    delta < -0.4 ? "STRUGGLES vs OPP" :
    "ON PAR vs OPP";

  return (
    <View>
      <Text style={styles.section}>PITCHER vs TEAM (H2H)</Text>
      <View style={styles.card}>
        <View style={styles.titleRow}>
          <Text style={styles.pitcher} numberOfLines={1}>{data.pitcher}</Text>
          <Text style={styles.vs}>vs</Text>
          <Text style={styles.team} numberOfLines={1}>{data.opp_team}</Text>
        </View>

        {/* ── Bento stats grid ───────────────────────────────── */}
        <View style={styles.bento}>
          <View style={styles.bentoCell}>
            <Text style={styles.bentoLabel}>H2H K AVG</Text>
            <Text style={[styles.bentoValueBig, { color: COLORS.voltBlue }]}>
              {vsStarts > 0 ? vsAvg.toFixed(1) : "—"}
            </Text>
            <Text style={styles.bentoSub}>
              {vsStarts > 0 ? `${vsStarts} start${vsStarts === 1 ? "" : "s"}` : "no history"}
            </Text>
          </View>

          <View style={styles.bentoDivider} />

          <View style={styles.bentoCell}>
            <Text style={styles.bentoLabel}>SEASON K AVG</Text>
            <Text style={[styles.bentoValueBig, { color: COLORS.textPrimary }]}>
              {seasonStarts > 0 ? seasonAvg.toFixed(1) : "—"}
            </Text>
            <Text style={styles.bentoSub}>
              {seasonStarts} start{seasonStarts === 1 ? "" : "s"}
            </Text>
          </View>
        </View>

        {vsStarts > 0 && (
          <View style={[styles.trendPill, { borderColor: trendColor + "55" }]}>
            <Text style={[styles.trendTxt, { color: trendColor }]}>
              {trendLabel} · {delta > 0 ? "+" : ""}{delta.toFixed(1)} K vs season avg
            </Text>
          </View>
        )}

        {recent.length > 0 && (
          <View style={styles.recentBlock}>
            <Text style={styles.recentTitle}>RECENT STARTS vs {data.opp_team.toUpperCase()}</Text>
            {recent.map((g, idx) => (
              <View key={`${g.date}-${idx}`} style={styles.recentRow}>
                <Text style={styles.recentDate}>
                  {formatShortDate(g.date)}
                </Text>
                <View style={styles.recentMid}>
                  <Text style={styles.recentMidTxt}>{g.ip} IP</Text>
                </View>
                <View style={[styles.kPill, { borderColor: kColor(g.k) + "66" }]}>
                  <Text style={[styles.kPillTxt, { color: kColor(g.k) }]}>{g.k} K</Text>
                </View>
              </View>
            ))}
          </View>
        )}

        {vsStarts === 0 && seasonStarts > 0 && (
          <Text style={styles.noteTxt}>
            No prior starts vs {data.opp_team} this season — using season-wide K rate as context.
          </Text>
        )}
      </View>
    </View>
  );
}

function formatShortDate(iso: string): string {
  // "2026-06-18" → "JUN 18"
  if (!iso || iso.length < 10) return iso;
  const months = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"];
  const m = Number(iso.slice(5, 7));
  const d = Number(iso.slice(8, 10));
  if (!m || !d) return iso;
  return `${months[m - 1]} ${d}`;
}

function kColor(k: number): string {
  if (k >= 7) return COLORS.neonGreen;
  if (k >= 5) return COLORS.voltBlue;
  if (k >= 3) return COLORS.textPrimary;
  return COLORS.electricBlaze;
}

const styles = StyleSheet.create({
  section: {
    color: COLORS.textMuted,
    fontSize: 10,
    fontWeight: "800",
    letterSpacing: 1.6,
    marginTop: 22,
    marginBottom: 10,
  },
  card: {
    backgroundColor: COLORS.surface,
    padding: 16,
    borderRadius: 14,
    borderWidth: 1,
    borderColor: COLORS.borderDefault,
  },
  loadingRow: { flexDirection: "row", alignItems: "center", gap: 10 },
  loadingTxt: { color: COLORS.textSecondary, fontSize: 12, fontWeight: "600" },

  titleRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    marginBottom: 14,
    flexWrap: "wrap",
  },
  pitcher: {
    color: COLORS.textPrimary,
    fontSize: 15,
    fontWeight: "900",
    letterSpacing: -0.3,
    maxWidth: "45%",
  },
  vs: {
    color: COLORS.textMuted,
    fontSize: 11,
    fontWeight: "700",
    letterSpacing: 1,
  },
  team: {
    color: COLORS.voltBlue,
    fontSize: 14,
    fontWeight: "800",
    letterSpacing: -0.2,
    maxWidth: "45%",
  },

  bento: {
    flexDirection: "row",
    alignItems: "stretch",
    backgroundColor: COLORS.bg,
    borderRadius: 12,
    paddingVertical: 12,
    paddingHorizontal: 8,
    borderWidth: 1,
    borderColor: COLORS.borderDefault,
  },
  bentoCell: { flex: 1, alignItems: "center", paddingHorizontal: 8 },
  bentoDivider: { width: 1, backgroundColor: COLORS.borderDefault, marginVertical: 4 },
  bentoLabel: {
    color: COLORS.textMuted,
    fontSize: 9,
    fontWeight: "800",
    letterSpacing: 1.2,
    marginBottom: 6,
  },
  bentoValueBig: { fontSize: 30, fontWeight: "900", letterSpacing: -1, lineHeight: 32 },
  bentoSub: { color: COLORS.textMuted, fontSize: 10, fontWeight: "700", marginTop: 4 },

  trendPill: {
    alignSelf: "flex-start",
    marginTop: 12,
    paddingHorizontal: 10,
    paddingVertical: 6,
    borderRadius: 14,
    borderWidth: 1,
  },
  trendTxt: { fontSize: 10, fontWeight: "900", letterSpacing: 0.8 },

  recentBlock: { marginTop: 14, paddingTop: 12, borderTopWidth: 1, borderTopColor: COLORS.borderDefault },
  recentTitle: {
    color: COLORS.textMuted,
    fontSize: 9,
    fontWeight: "800",
    letterSpacing: 1.2,
    marginBottom: 8,
  },
  recentRow: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingVertical: 6,
    gap: 8,
  },
  recentDate: {
    color: COLORS.textPrimary,
    fontSize: 12,
    fontWeight: "800",
    letterSpacing: 0.5,
    minWidth: 56,
  },
  recentMid: { flex: 1, alignItems: "center" },
  recentMidTxt: { color: COLORS.textSecondary, fontSize: 11, fontWeight: "600" },
  kPill: {
    paddingHorizontal: 10,
    paddingVertical: 4,
    borderRadius: 10,
    borderWidth: 1,
    minWidth: 48,
    alignItems: "center",
  },
  kPillTxt: { fontSize: 12, fontWeight: "900", letterSpacing: 0.3 },

  noteTxt: {
    color: COLORS.textMuted,
    fontSize: 11,
    lineHeight: 16,
    marginTop: 12,
    fontStyle: "italic",
  },
});
