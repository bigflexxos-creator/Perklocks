/**
 * SimulatorPanel — Monte Carlo simulator output for a single MLB pick.
 *
 * Renders ONLY for MLB picks. Shows:
 *   • Headline:  "🎲 Simulator: 67.2% · 10,000 runs"
 *   • CI band:   95% Wilson confidence interval
 *   • Signal:    stronger / weaker / aligned vs. blended model
 *   • Lock lift: ±N points applied to lock_score from sim disagreement
 *
 * Reads sim_* fields straight off the pick (already computed at refresh
 * time). Falls back to fetching /api/picks/{id}/simulation on-demand if
 * the fields are missing — e.g. older picks generated before sim went live.
 */
import React, { useEffect, useState } from "react";
import { View, Text, StyleSheet, ActivityIndicator } from "react-native";
import { COLORS } from "@/src/theme";
import { api, Pick } from "@/src/lib/api";

export function SimulatorPanel({ pick }: { pick: Pick }) {
  const eligible = (pick.sport || "").toUpperCase() === "MLB";
  const hasInline = typeof pick.sim_win_probability === "number";
  const [sim, setSim] = useState<{
    sim_win_probability: number;
    sim_ci_lower: number;
    sim_ci_upper: number;
    sim_runs: number;
    sim_disagreement_with_model: number;
    sim_signal: "stronger" | "weaker" | "neutral";
  } | null>(hasInline ? {
    sim_win_probability: pick.sim_win_probability!,
    sim_ci_lower: pick.sim_ci_lower ?? 0,
    sim_ci_upper: pick.sim_ci_upper ?? 0,
    sim_runs: pick.sim_runs ?? 0,
    sim_disagreement_with_model: pick.sim_disagreement_with_model ?? 0,
    sim_signal: (pick.sim_signal as any) ?? "neutral",
  } : null);
  const [loading, setLoading] = useState(eligible && !hasInline);
  const [unavailable, setUnavailable] = useState(false);

  useEffect(() => {
    if (!eligible || hasInline) return;
    let cancelled = false;
    (async () => {
      try {
        const s = await api.pickSimulation(pick.id);
        if (!cancelled) {
          setSim(s);
          setLoading(false);
        }
      } catch {
        if (!cancelled) {
          setUnavailable(true);
          setLoading(false);
        }
      }
    })();
    return () => { cancelled = true; };
  }, [pick.id, eligible, hasInline]);

  if (!eligible) return null;
  if (unavailable) return null; // silently skip for unsupported MLB markets

  return (
    <View style={styles.wrap}>
      <View style={styles.headerRow}>
        <View style={styles.titleBlock}>
          <Text style={styles.sectionLabel}>MONTE CARLO SIMULATOR</Text>
          <Text style={styles.tagline}>
            10,000-run game simulation using MLB Stats API priors.
          </Text>
        </View>
        <View style={styles.insightChip}>
          <Text style={styles.insightChipText}>PHASE A</Text>
        </View>
      </View>

      {loading ? (
        <View style={styles.loadingRow}>
          <ActivityIndicator size="small" color={COLORS.voltBlue} />
          <Text style={styles.loadingText}>Running 10,000 simulations…</Text>
        </View>
      ) : sim ? (
        <SimulationBody sim={sim} lift={pick.sim_lock_lift} modelWp={pick.win_probability} />
      ) : null}
    </View>
  );
}

function SimulationBody({
  sim,
  lift,
  modelWp,
}: {
  sim: NonNullable<ReturnType<typeof useState<any>>[0]>;
  lift?: number;
  modelWp?: number;
}) {
  const wp = Math.round((sim.sim_win_probability ?? 0) * 10) / 10;
  const ciLo = Math.round(sim.sim_ci_lower ?? 0);
  const ciHi = Math.round(sim.sim_ci_upper ?? 0);
  const halfWidth = Math.round(((ciHi - ciLo) / 2) * 10) / 10;
  const signal = (sim.sim_signal ?? "neutral") as "stronger" | "weaker" | "neutral";
  const disagreement = sim.sim_disagreement_with_model ?? 0;

  const tint =
    signal === "stronger" ? COLORS.neonGreen :
    signal === "weaker" ? COLORS.electricBlaze :
    COLORS.voltBlue;
  const signalLabel =
    signal === "stronger" ? "STRONGER THAN MODEL" :
    signal === "weaker" ? "WEAKER THAN MODEL" :
    "ALIGNED WITH MODEL";

  return (
    <>
      {/* Headline */}
      <View style={styles.headlineRow}>
        <View style={[styles.bigScoreWrap, { borderColor: tint + "55" }]}>
          <Text style={[styles.bigScoreValue, { color: tint }]}>{wp.toFixed(1)}%</Text>
          <Text style={styles.bigScoreUnit}>± {halfWidth || 0}%</Text>
        </View>
        <View style={{ flex: 1 }}>
          <Text style={styles.headline}>Simulated Win Probability</Text>
          <Text style={styles.sub}>
            95% CI: <Text style={styles.subStrong}>{ciLo}% – {ciHi}%</Text>
          </Text>
          <Text style={styles.sub}>
            Runs: <Text style={styles.subStrong}>{(sim.sim_runs ?? 0).toLocaleString()}</Text>
          </Text>
          <View style={[styles.signalPill, { borderColor: tint + "55", backgroundColor: tint + "10" }]}>
            <View style={[styles.signalDot, { backgroundColor: tint }]} />
            <Text style={[styles.signalText, { color: tint }]}>{signalLabel}</Text>
          </View>
        </View>
      </View>

      {/* Comparison vs model */}
      {typeof modelWp === "number" && (
        <View style={styles.compareRow}>
          <View style={styles.compareCell}>
            <Text style={styles.compareLabel}>MODEL WP</Text>
            <Text style={styles.compareValue}>{Math.round(modelWp)}%</Text>
          </View>
          <View style={styles.compareDivider} />
          <View style={styles.compareCell}>
            <Text style={styles.compareLabel}>SIM WP</Text>
            <Text style={[styles.compareValue, { color: tint }]}>{wp.toFixed(1)}%</Text>
          </View>
          <View style={styles.compareDivider} />
          <View style={styles.compareCell}>
            <Text style={styles.compareLabel}>DELTA</Text>
            <Text style={[styles.compareValue, { color: tint }]}>
              {disagreement > 0 ? "+" : ""}{Math.round(disagreement * 10) / 10}
            </Text>
          </View>
        </View>
      )}

      {/* Lock score lift */}
      {typeof lift === "number" && Math.abs(lift) >= 0.1 && (
        <View style={[styles.liftBanner, { borderColor: tint + "55", backgroundColor: tint + "08" }]}>
          <Text style={[styles.liftText, { color: tint }]}>
            {lift > 0 ? "+" : ""}{lift.toFixed(2)} Lock Score from simulator
          </Text>
          <Text style={styles.liftSub}>
            Applied because the simulator {lift > 0 ? "agreed and reinforced" : "disagreed with"} the model.
          </Text>
        </View>
      )}

      <Text style={styles.footnote}>
        Hitter sims: per-AB Bernoulli using BA / HR-rate / RBI-rate over expected ABs.
        Pitcher sims: per-batter-faced K-rate over expected BF. Player data from the
        free MLB Stats API. 0 Odds credits used.
      </Text>
    </>
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
    justifyContent: "space-between",
    gap: 12,
  },
  titleBlock: { flex: 1 },
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
  insightChip: {
    paddingHorizontal: 7,
    paddingVertical: 3,
    borderRadius: 4,
    borderWidth: 1,
    borderColor: COLORS.voltBlue + "55",
    backgroundColor: COLORS.voltBlue + "12",
  },
  insightChipText: {
    color: COLORS.voltBlue,
    fontSize: 9,
    fontWeight: "900",
    letterSpacing: 1.2,
  },

  loadingRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
    paddingVertical: 16,
  },
  loadingText: { color: COLORS.textMuted, fontSize: 12, fontWeight: "600" },

  headlineRow: { flexDirection: "row", alignItems: "center", gap: 14, marginTop: 14 },
  bigScoreWrap: {
    width: 92,
    height: 92,
    borderRadius: 46,
    borderWidth: 1.5,
    alignItems: "center",
    justifyContent: "center",
  },
  bigScoreValue: { fontSize: 22, fontWeight: "900", letterSpacing: -0.5 },
  bigScoreUnit: {
    color: COLORS.textMuted,
    fontSize: 9,
    fontWeight: "800",
    letterSpacing: 0.6,
    marginTop: 2,
    fontVariant: ["tabular-nums"],
  },
  headline: {
    color: COLORS.textPrimary,
    fontSize: 14,
    fontWeight: "800",
    letterSpacing: -0.2,
  },
  sub: {
    color: COLORS.textSecondary,
    fontSize: 11,
    lineHeight: 16,
    marginTop: 3,
    fontVariant: ["tabular-nums"],
  },
  subStrong: { color: COLORS.textPrimary, fontWeight: "800" },

  signalPill: {
    alignSelf: "flex-start",
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    paddingHorizontal: 7,
    paddingVertical: 3,
    borderRadius: 4,
    borderWidth: 1,
    marginTop: 8,
  },
  signalDot: { width: 5, height: 5, borderRadius: 3 },
  signalText: { fontSize: 9, fontWeight: "900", letterSpacing: 0.8 },

  compareRow: {
    flexDirection: "row",
    alignItems: "center",
    marginTop: 16,
    paddingVertical: 10,
    paddingHorizontal: 6,
    backgroundColor: "rgba(255,255,255,0.02)",
    borderRadius: 10,
    borderWidth: 1,
    borderColor: COLORS.borderDefault,
  },
  compareCell: { flex: 1, alignItems: "center" },
  compareLabel: {
    color: COLORS.textMuted,
    fontSize: 9,
    fontWeight: "800",
    letterSpacing: 1.0,
  },
  compareValue: {
    color: COLORS.textPrimary,
    fontSize: 16,
    fontWeight: "900",
    marginTop: 3,
    fontVariant: ["tabular-nums"],
  },
  compareDivider: {
    width: 1,
    height: 28,
    backgroundColor: COLORS.borderDefault,
  },

  liftBanner: {
    marginTop: 12,
    padding: 10,
    borderRadius: 8,
    borderWidth: 1,
  },
  liftText: {
    fontSize: 12,
    fontWeight: "900",
    letterSpacing: 0.3,
  },
  liftSub: {
    color: COLORS.textMuted,
    fontSize: 10,
    fontWeight: "600",
    marginTop: 3,
    lineHeight: 14,
  },

  footnote: {
    color: COLORS.textMuted,
    fontSize: 10,
    lineHeight: 15,
    fontWeight: "500",
    marginTop: 14,
    fontStyle: "italic",
  },
});
