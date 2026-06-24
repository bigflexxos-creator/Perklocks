/**
 * RiskMeterPanel — visualize the outcome distribution for a single pick.
 *
 * Renders a horizontal "risk meter" showing where the betting line sits
 * within the Monte Carlo simulator's projected stat distribution.
 *
 *   P10 ─────[ likely-range ]──────P90    ← simulated stat samples
 *        ▲                  ▲      ▲
 *        |                  |      |
 *      P50                 line   max
 *
 * Numbers come from /api/picks/{id}/simulation — specifically the
 * sim_pctl_p10/p25/p50/p75/p90 + sim_pctl_min/max fields plus the
 * sim_pctl_line/sim_pctl_line_quantile_pct pair the backend just
 * started returning.
 *
 * The panel HIDES itself if percentile fields are missing (older picks
 * or sports without a meaningful integer distribution — pure ML / win
 * markets are skipped).
 */
import React, { useEffect, useState } from "react";
import { View, Text, StyleSheet, ActivityIndicator } from "react-native";
import { COLORS } from "@/src/theme";
import { api, Pick } from "@/src/lib/api";

type SimResp = {
  sim_pctl_p10?: number; sim_pctl_p25?: number; sim_pctl_p50?: number;
  sim_pctl_p75?: number; sim_pctl_p90?: number;
  sim_pctl_min?: number; sim_pctl_max?: number;
  sim_pctl_line?: number;
  sim_pctl_line_quantile_pct?: number;
  sim_win_probability?: number;
  sim_ci_lower?: number;
  sim_ci_upper?: number;
  sim_is_under?: boolean;
  sim_runs?: number;
};

// Try to label the y-axis stat by reading the pick's market string.
// Falls back to a generic "stat" if no keyword matches.
function statLabelFor(market: string): string {
  const m = (market || "").toLowerCase();
  if (m.includes("strikeout")) return "Strikeouts";
  if (m.includes("hits + runs + rbi") || m.includes("h+r+rbi")) return "H+R+RBI";
  if (m.includes("total bases")) return "Total Bases";
  if (m.includes("rbi")) return "RBIs";
  if (m.includes("home run")) return "Home Runs";
  if (m.includes("outs recorded")) return "Outs Recorded";
  if (m.includes("hits")) return "Hits";
  if (m.includes("rebound")) return "Rebounds";
  if (m.includes("assist")) return "Assists";
  if (m.includes("points")) return "Points";
  if (m.includes("3-point") || m.includes("threes")) return "3-Pointers";
  return "Projected outcome";
}

// Decide a verdict label based on where the line sits in the distribution.
// Mirrors how a sharp would read the meter at a glance.
function verdictFor(
  lineQuantilePct: number,
  isUnder: boolean,
): { label: string; color: string; sub: string } {
  // For overs: low quantile = line is below most samples = strong over.
  // For unders: high quantile = line is above most samples = strong under.
  // Normalize so `winPct` always means "win % from this side of the line".
  const winPct = isUnder ? lineQuantilePct : 100 - lineQuantilePct;

  if (winPct >= 80) {
    return {
      label: "STRONG LOCK",
      color: COLORS.neonGreen,
      sub: `Line clears in ~${Math.round(winPct)}% of simulated outcomes`,
    };
  }
  if (winPct >= 65) {
    return {
      label: "LIKELY LOCK",
      color: COLORS.voltBlue,
      sub: `Line clears in ~${Math.round(winPct)}% of simulated outcomes`,
    };
  }
  if (winPct >= 50) {
    return {
      label: "LEAN",
      color: COLORS.amber || "#F2C94C",
      sub: `Slight edge — clears in ~${Math.round(winPct)}% of sims`,
    };
  }
  return {
    label: "COIN FLIP",
    color: COLORS.textMuted,
    sub: `Only ~${Math.round(winPct)}% of sims clear the line`,
  };
}

export function RiskMeterPanel({ pick }: { pick: Pick }) {
  const sport = (pick.sport || "").toUpperCase();
  const eligible = ["MLB", "NBA"].includes(sport);

  const inline: SimResp | null =
    typeof (pick as any).sim_pctl_p50 === "number"
      ? (pick as any)
      : null;

  const [sim, setSim] = useState<SimResp | null>(inline);
  const [loading, setLoading] = useState(eligible && !inline);
  const [unavailable, setUnavailable] = useState(false);

  useEffect(() => {
    if (!eligible || inline) return;
    let cancelled = false;
    (async () => {
      try {
        const res = await api.pickSimulation(pick.id);
        if (cancelled) return;
        // If the response is missing percentile data (older pick, sport
        // without distribution), hide the panel.
        if (typeof res?.sim_pctl_p50 !== "number") {
          setUnavailable(true);
        } else {
          setSim(res);
        }
      } catch {
        if (!cancelled) setUnavailable(true);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [pick.id, eligible, inline]);

  if (!eligible || unavailable) return null;
  if (loading) {
    return (
      <View style={styles.card}>
        <View style={styles.headerRow}>
          <Text style={styles.title}>RISK METER</Text>
          <ActivityIndicator size="small" color={COLORS.voltBlue} />
        </View>
        <Text style={styles.subhead}>Computing simulated outcomes…</Text>
      </View>
    );
  }
  if (!sim || typeof sim.sim_pctl_p50 !== "number") return null;

  const p10 = sim.sim_pctl_p10 ?? 0;
  const p25 = sim.sim_pctl_p25 ?? 0;
  const p50 = sim.sim_pctl_p50 ?? 0;
  const p75 = sim.sim_pctl_p75 ?? 0;
  const p90 = sim.sim_pctl_p90 ?? 0;
  const min = sim.sim_pctl_min ?? p10;
  const max = sim.sim_pctl_max ?? p90;
  const line = sim.sim_pctl_line ?? p50;
  const lineQ = sim.sim_pctl_line_quantile_pct ?? 50;
  const isUnder = !!sim.sim_is_under;

  // Build the axis range. Pad slightly so the markers don't sit flush
  // against the edges.
  const axisMin = Math.min(min, line, p10) - 0.5;
  const axisMax = Math.max(max, line, p90) + 0.5;
  const span = Math.max(1e-6, axisMax - axisMin);
  const pct = (v: number) =>
    Math.min(100, Math.max(0, ((v - axisMin) / span) * 100));

  const verdict = verdictFor(lineQ, isUnder);
  const statLabel = statLabelFor(pick.market || "");
  const sideLabel = isUnder ? "Under" : "Over";

  return (
    <View style={styles.card}>
      <View style={styles.headerRow}>
        <Text style={styles.title}>RISK METER</Text>
        <View style={[styles.verdictPill, { borderColor: verdict.color }]}>
          <Text style={[styles.verdictTxt, { color: verdict.color }]}>
            {verdict.label}
          </Text>
        </View>
      </View>

      <Text style={styles.subhead}>
        {statLabel} · {sim.sim_runs?.toLocaleString() ?? "—"} Monte Carlo runs
      </Text>

      {/* Outcome cone — P10/P25/P50/P75/P90 spread */}
      <View style={styles.meterWrap}>
        <View style={styles.track}>
          {/* P10-P90 "likely" band */}
          <View
            style={[
              styles.bandLikely,
              { left: `${pct(p10)}%`, width: `${pct(p90) - pct(p10)}%` },
            ]}
          />
          {/* P25-P75 "core" band */}
          <View
            style={[
              styles.bandCore,
              { left: `${pct(p25)}%`, width: `${pct(p75) - pct(p25)}%` },
            ]}
          />
          {/* Median tick */}
          <View style={[styles.medianTick, { left: `${pct(p50)}%` }]} />
          {/* Line marker */}
          <View style={[styles.lineMarker, { left: `${pct(line)}%`, borderColor: verdict.color }]}>
            <Text style={[styles.lineLabel, { color: verdict.color }]}>LINE</Text>
          </View>
        </View>

        {/* Axis labels */}
        <View style={styles.axisRow}>
          <Text style={styles.axisTick}>P10 · {p10}</Text>
          <Text style={styles.axisTick}>P50 · {p50}</Text>
          <Text style={styles.axisTick}>P90 · {p90}</Text>
        </View>
      </View>

      {/* Verdict subtext */}
      <Text style={styles.verdictSub}>{verdict.sub}</Text>

      {/* Detail row — quartile breakdown for the analytical user */}
      <View style={styles.statRow}>
        <Stat label="P25" value={p25} />
        <Stat label="MEDIAN" value={p50} highlight />
        <Stat label="P75" value={p75} />
        <Stat
          label={`${sideLabel.toUpperCase()} ${line}`}
          value={
            sim.sim_win_probability != null
              ? `${sim.sim_win_probability}%`
              : "—"
          }
          highlight
        />
      </View>
    </View>
  );
}

function Stat({
  label, value, highlight,
}: {
  label: string;
  value: number | string;
  highlight?: boolean;
}) {
  return (
    <View style={styles.statCell}>
      <Text style={styles.statLabel}>{label}</Text>
      <Text
        style={[
          styles.statValue,
          highlight && { color: COLORS.voltBlue, fontWeight: "900" },
        ]}
        numberOfLines={1}
        adjustsFontSizeToFit
      >
        {value}
      </Text>
    </View>
  );
}

const TRACK_HEIGHT = 18;

const styles = StyleSheet.create({
  card: {
    backgroundColor: COLORS.surface,
    borderRadius: 14,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: COLORS.borderDefault,
    padding: 16,
    marginBottom: 16,
  },
  headerRow: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    marginBottom: 6,
  },
  title: {
    color: COLORS.textPrimary,
    fontSize: 12,
    fontWeight: "900",
    letterSpacing: 1.4,
  },
  verdictPill: {
    paddingHorizontal: 10,
    paddingVertical: 4,
    borderRadius: 14,
    borderWidth: 1,
  },
  verdictTxt: {
    fontSize: 10,
    fontWeight: "900",
    letterSpacing: 1.1,
  },
  subhead: {
    color: COLORS.textMuted,
    fontSize: 11,
    marginBottom: 16,
  },
  meterWrap: {
    marginBottom: 8,
  },
  track: {
    height: TRACK_HEIGHT,
    backgroundColor: COLORS.bg,
    borderRadius: TRACK_HEIGHT / 2,
    position: "relative",
    overflow: "visible",
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: COLORS.borderDefault,
  },
  bandLikely: {
    position: "absolute",
    top: 0,
    bottom: 0,
    backgroundColor: COLORS.voltBlue + "33", // 20% opacity
    borderRadius: TRACK_HEIGHT / 2,
  },
  bandCore: {
    position: "absolute",
    top: 0,
    bottom: 0,
    backgroundColor: COLORS.voltBlue + "66", // 40% opacity
  },
  medianTick: {
    position: "absolute",
    top: -2,
    bottom: -2,
    width: 2,
    marginLeft: -1,
    backgroundColor: COLORS.textPrimary,
  },
  lineMarker: {
    position: "absolute",
    top: -10,
    bottom: -10,
    width: 24,
    marginLeft: -12,
    alignItems: "center",
    justifyContent: "center",
    borderLeftWidth: 2,
    borderRightWidth: 2,
  },
  lineLabel: {
    fontSize: 8,
    fontWeight: "900",
    letterSpacing: 0.8,
    position: "absolute",
    top: -14,
  },
  axisRow: {
    flexDirection: "row",
    justifyContent: "space-between",
    marginTop: 10,
  },
  axisTick: {
    color: COLORS.textMuted,
    fontSize: 10,
    fontWeight: "700",
    letterSpacing: 0.5,
  },
  verdictSub: {
    color: COLORS.textSecondary,
    fontSize: 12,
    marginTop: 6,
    marginBottom: 12,
    lineHeight: 16,
  },
  statRow: {
    flexDirection: "row",
    justifyContent: "space-between",
    paddingTop: 10,
    borderTopWidth: StyleSheet.hairlineWidth,
    borderTopColor: COLORS.borderDefault,
  },
  statCell: {
    flex: 1,
    alignItems: "center",
  },
  statLabel: {
    color: COLORS.textMuted,
    fontSize: 9,
    fontWeight: "800",
    letterSpacing: 1.1,
    marginBottom: 3,
  },
  statValue: {
    color: COLORS.textPrimary,
    fontSize: 13,
    fontWeight: "700",
    fontVariant: ["tabular-nums"],
  },
});
