/**
 * ProbabilityBreakdownPanel — Unified Probability Engine math viewer.
 *
 * Renders the three probability inputs (model v1 · model v2 · simulator)
 * with their blend weights, the post-blend `p_final`, the calibrated
 * output `p_calibrated`, and the resulting edge versus market-implied
 * probability. Also surfaces the `classification` (PREMIUM / GOOD / FADE)
 * and `stability_score` (how tightly the three inputs agree).
 *
 * Reads `GET /api/picks/{id}/probability` — see `probability_engine.py`
 * for the source of truth on how these numbers are computed.
 *
 * Auto-hides on network failure (additive UI metadata, never blocks
 * the deep-dive screen).
 */
import React, { useEffect, useState } from "react";
import { View, Text, StyleSheet, ActivityIndicator } from "react-native";
import { COLORS } from "@/src/theme";
import { api } from "@/src/lib/api";

type Probability = Awaited<ReturnType<typeof api.pickProbability>>;

const pct = (n: number | null | undefined, d = 1): string =>
  n == null || !Number.isFinite(n) ? "—" : `${(n * 100).toFixed(d)}%`;

export function ProbabilityBreakdownPanel({ pickId }: { pickId: string }) {
  const [data, setData] = useState<Probability | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await api.pickProbability(pickId);
        if (!cancelled) setData(res);
      } catch {
        if (!cancelled) setErr(true);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [pickId]);

  if (err) return null;
  if (loading) {
    return (
      <View style={styles.wrap}>
        <ActivityIndicator color={COLORS.voltBlue} />
      </View>
    );
  }
  if (!data) return null;

  const edgePct = data.edge * 100;
  const edgeColor =
    edgePct >= 3 ? "#86EFAC"
    : edgePct >= 0 ? COLORS.textPrimary
    : edgePct >= -1 ? "#FCD34D"
    : "#FCA5A5";

  const classColor =
    data.classification === "LOCK_99"  ? "#FFD700"        // gold for elite
    : data.classification === "PREMIUM" ? "#86EFAC"        // green
    : data.classification === "GOOD"    ? COLORS.voltBlue  // blue
    : data.classification === "NORMAL"  ? COLORS.textPrimary
    : data.classification === "FADE"    ? "#FCA5A5"        // red
    : COLORS.textPrimary;                                  // unknown — neutral

  // Pretty-print enum tokens like LOCK_99 → "LOCK 99" so the pill
  // never renders raw snake_case from the backend.
  const classLabel = (data.classification || "").replace(/_/g, " ");

  // Simulator may be null on non-simulated sports/markets — collapse
  // the bar gracefully when that's the case. We rely on `sim_probability
  // === null` as the canonical signal (truthful API) and fall back to
  // `sim_ran === false` for older snapshots.
  const hasSim = data.sim_probability != null && data.sim_ran !== false;

  // Prefer effective_weights (what was actually blended into p_final
  // on this specific pick) over the nominal weights, so the UI's
  // weight tags match reality. Falls back to nominal for older
  // backends that don't return effective_weights yet.
  const w = data.effective_weights ?? data.weights;

  return (
    <View style={styles.wrap} testID="probability-breakdown-panel">
      <View style={styles.headerRow}>
        <View style={{ flex: 1 }}>
          <Text style={styles.kicker}>UNIFIED PROBABILITY ENGINE</Text>
          <Text style={styles.title}>Probability Breakdown</Text>
          <Text style={styles.sub}>
            Three model inputs blended → calibrated → compared against the book&apos;s implied price.
          </Text>
        </View>
        <View style={[styles.classPill, { borderColor: classColor + "55", backgroundColor: classColor + "1A" }]}>
          <Text style={[styles.classText, { color: classColor }]}>
            {classLabel}
          </Text>
        </View>
      </View>

      {/* ── Inputs (v1 · v2 · sim) ───────────────────────────────── */}
      <ProbBar
        label="Model v1"
        sub="features-only"
        weight={w?.v1 ?? 0}
        prob={data.p_v1}
        tint="#7DD3FC"
      />
      <ProbBar
        label="Model v2"
        sub="learned + bandit"
        weight={w?.v2 ?? 0}
        prob={data.p_v2}
        tint={COLORS.voltBlue}
      />
      <ProbBar
        label="Simulator"
        sub={hasSim ? "Monte Carlo" : "not run"}
        weight={hasSim ? (w?.sim ?? 0) : 0}
        prob={hasSim ? (data.sim_probability as number) : 0}
        tint="#A78BFA"
        muted={!hasSim}
      />

      {/* ── Blend → Calibration → Final ──────────────────────────── */}
      <View style={styles.divider} />

      <BlendRow
        label="BLENDED p_final"
        hint="weighted average of inputs"
        prob={data.p_final}
        tint={COLORS.textPrimary}
      />
      <BlendRow
        label="CALIBRATED p_cal"
        hint={
          data.calibration?.fit_sample_size
            ? `isotonic fit · n=${data.calibration.fit_sample_size}`
            : "isotonic regression"
        }
        prob={data.p_calibrated}
        tint={COLORS.voltBlue}
      />
      <BlendRow
        label="MARKET IMPLIED"
        hint="from the book's odds"
        prob={data.implied_probability}
        tint={COLORS.textMuted}
      />

      {/* ── Edge readout ─────────────────────────────────────────── */}
      <View style={styles.edgeBlock}>
        <View>
          <Text style={styles.edgeKicker}>EDGE = p_cal − implied</Text>
          <Text style={styles.edgeFootnote}>
            {edgePct >= 3 ? "Strong overlay vs market"
            : edgePct >= 0 ? "Mild overlay"
            : edgePct >= -1 ? "Roughly fair price"
            : "Pricing in our favour weakening"}
          </Text>
        </View>
        <Text style={[styles.edgeValue, { color: edgeColor }]}>
          {edgePct >= 0 ? "+" : ""}{edgePct.toFixed(2)}pp
        </Text>
      </View>

      {/* ── Stability + footnote ─────────────────────────────────── */}
      {typeof data.stability_score === "number" && (
        <View style={styles.stabilityRow}>
          <Text style={styles.stabilityLabel}>STABILITY</Text>
          <View style={styles.stabilityBar}>
            <View
              style={[
                styles.stabilityFill,
                { width: `${Math.max(0, Math.min(100, data.stability_score * 100))}%` },
              ]}
            />
          </View>
          <Text style={styles.stabilityVal}>
            {(data.stability_score * 100).toFixed(0)}/100
          </Text>
        </View>
      )}

      <Text style={styles.footnote}>
        How tightly v1 / v2 / sim agree — higher = stronger consensus.
      </Text>
    </View>
  );
}

// ──────────────────────────────────────────────────────────────────────
// Sub-components
// ──────────────────────────────────────────────────────────────────────

function ProbBar({
  label, sub, weight, prob, tint, muted,
}: {
  label: string;
  sub: string;
  weight: number;
  prob: number;
  tint: string;
  muted?: boolean;
}) {
  const pctValue = Math.max(0, Math.min(100, prob * 100));
  return (
    <View style={[styles.probRow, muted && { opacity: 0.45 }]}>
      <View style={styles.probLabelCol}>
        <Text style={styles.probLabel}>{label}</Text>
        <Text style={styles.probSub}>{sub}</Text>
      </View>
      <View style={styles.probBarCol}>
        <View style={styles.probBarOuter}>
          <View
            style={[
              styles.probBarInner,
              { width: `${pctValue}%`, backgroundColor: tint },
            ]}
          />
        </View>
      </View>
      <View style={styles.probValCol}>
        <Text style={[styles.probVal, { color: tint }]}>{pct(prob, 1)}</Text>
        <Text style={styles.probWeight}>w={(weight * 100).toFixed(0)}%</Text>
      </View>
    </View>
  );
}

function BlendRow({
  label, hint, prob, tint,
}: {
  label: string;
  hint: string;
  prob: number;
  tint: string;
}) {
  return (
    <View style={styles.blendRow}>
      <View style={{ flex: 1 }}>
        <Text style={styles.blendLabel}>{label}</Text>
        <Text style={styles.blendHint}>{hint}</Text>
      </View>
      <Text style={[styles.blendVal, { color: tint }]}>
        {pct(prob, 2)}
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: {
    backgroundColor: COLORS.surface,
    borderRadius: 14,
    borderWidth: 1,
    borderColor: COLORS.borderDefault,
    padding: 16,
    marginVertical: 8,
  },
  headerRow: {
    flexDirection: "row",
    alignItems: "flex-start",
    justifyContent: "space-between",
    marginBottom: 14,
    gap: 10,
  },
  kicker: {
    color: COLORS.voltBlue,
    fontSize: 10,
    fontWeight: "900",
    letterSpacing: 1.4,
  },
  title: {
    color: COLORS.textPrimary,
    fontSize: 16,
    fontWeight: "900",
    marginTop: 3,
    letterSpacing: -0.3,
  },
  sub: {
    color: COLORS.textMuted,
    fontSize: 11,
    fontWeight: "600",
    marginTop: 3,
    lineHeight: 15,
  },
  classPill: {
    paddingHorizontal: 8,
    paddingVertical: 4,
    borderRadius: 5,
    borderWidth: 1,
  },
  classText: {
    fontSize: 9,
    fontWeight: "900",
    letterSpacing: 1.2,
  },

  probRow: {
    flexDirection: "row",
    alignItems: "center",
    paddingVertical: 6,
    gap: 10,
  },
  probLabelCol: { width: 96 },
  probLabel: {
    color: COLORS.textPrimary,
    fontSize: 12,
    fontWeight: "800",
  },
  probSub: {
    color: COLORS.textMuted,
    fontSize: 9,
    fontWeight: "600",
    marginTop: 1,
    letterSpacing: 0.2,
  },
  probBarCol: { flex: 1 },
  probBarOuter: {
    height: 8,
    backgroundColor: "rgba(255,255,255,0.05)",
    borderRadius: 4,
    overflow: "hidden",
  },
  probBarInner: { height: "100%", borderRadius: 4 },
  probValCol: { width: 70, alignItems: "flex-end" },
  probVal: {
    fontSize: 13,
    fontWeight: "900",
    fontVariant: ["tabular-nums"],
  },
  probWeight: {
    color: COLORS.textMuted,
    fontSize: 9,
    fontWeight: "700",
    marginTop: 1,
    fontVariant: ["tabular-nums"],
  },

  divider: {
    height: 1,
    backgroundColor: COLORS.borderDefault,
    marginVertical: 10,
  },
  blendRow: {
    flexDirection: "row",
    alignItems: "center",
    paddingVertical: 6,
  },
  blendLabel: {
    color: COLORS.textPrimary,
    fontSize: 11,
    fontWeight: "900",
    letterSpacing: 0.8,
  },
  blendHint: {
    color: COLORS.textMuted,
    fontSize: 9,
    fontWeight: "600",
    marginTop: 1,
    letterSpacing: 0.2,
  },
  blendVal: {
    fontSize: 16,
    fontWeight: "900",
    letterSpacing: -0.3,
    fontVariant: ["tabular-nums"],
  },

  edgeBlock: {
    marginTop: 12,
    paddingVertical: 12,
    paddingHorizontal: 14,
    borderRadius: 10,
    backgroundColor: "rgba(255,255,255,0.03)",
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center",
  },
  edgeKicker: {
    color: COLORS.textMuted,
    fontSize: 10,
    fontWeight: "900",
    letterSpacing: 1.2,
  },
  edgeFootnote: {
    color: COLORS.textPrimary,
    fontSize: 11,
    fontWeight: "700",
    marginTop: 3,
  },
  edgeValue: {
    fontSize: 22,
    fontWeight: "900",
    letterSpacing: -0.5,
    fontVariant: ["tabular-nums"],
  },

  stabilityRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
    marginTop: 12,
  },
  stabilityLabel: {
    color: COLORS.textMuted,
    fontSize: 9,
    fontWeight: "900",
    letterSpacing: 1.2,
    width: 78,
  },
  stabilityBar: {
    flex: 1,
    height: 5,
    backgroundColor: "rgba(255,255,255,0.05)",
    borderRadius: 3,
    overflow: "hidden",
  },
  stabilityFill: {
    height: "100%",
    backgroundColor: COLORS.voltBlue,
    borderRadius: 3,
  },
  stabilityVal: {
    color: COLORS.textPrimary,
    fontSize: 11,
    fontWeight: "800",
    width: 56,
    textAlign: "right",
    fontVariant: ["tabular-nums"],
  },
  footnote: {
    color: COLORS.textMuted,
    fontSize: 10,
    fontWeight: "600",
    fontStyle: "italic",
    marginTop: 4,
    lineHeight: 14,
  },
});
