/**
 * EvidencePanel — the user-facing surface of the Universal Evidence System.
 *
 * Renders the FOUR separated metrics the engine produces:
 *
 *     PROBABILITY · EDGE · EVIDENCE · LOCK
 *
 * Plus a one-line evidence verdict ("Backed by 3 high-confidence features
 * over 10,000 simulated outcomes") and a tier breakdown (HIGH / MED / LOW)
 * counts. Tap-to-expand reveals every feature with provenance.
 *
 * Data source: the `evidence_breakdown` field that the backend's
 * `evidence_engine.govern_pick()` writes onto every pick. If the field is
 * missing (very old picks not yet re-governed), the panel hides itself
 * gracefully rather than rendering a misleading half-card.
 */
import React, { useState } from "react";
import { View, Text, StyleSheet, Pressable } from "react-native";
import { COLORS } from "@/src/theme";
import { Pick } from "@/src/lib/api";

type EvidenceFeature = {
  name: string;
  category: string;
  value: any;
  sample_size: number;
  lookback_days: number;
  source: string;
  freshness_hours: number;
  importance: number;
  reliability: number;
  tier: "LOW" | "MEDIUM" | "HIGH";
  direction: "pro" | "con" | "neutral";
  passes_governor: boolean;
  reason?: string;
  explanation_text?: string | null;
};

type EvidenceBreakdown = {
  score?: number;
  multiplier?: number;
  lock_raw?: number;
  lock_governed?: number;
  tier_counts?: { HIGH: number; MEDIUM: number; LOW: number };
  top_features?: EvidenceFeature[];
  excluded_features?: EvidenceFeature[];
  dropped_insights?: string[];
  generated_at?: string;
};

type PickWithEvidence = Pick & {
  evidence_score?: number;
  lock_score_raw?: number;
  evidence_breakdown?: EvidenceBreakdown;
};

// ── Visual helpers ─────────────────────────────────────────────────
function tierColor(tier?: string): string {
  if (tier === "HIGH") return COLORS.neonGreen;
  if (tier === "MEDIUM") return COLORS.voltBlue;
  return COLORS.textMuted;
}

function evidenceColor(score?: number): string {
  if (score == null) return COLORS.textMuted;
  if (score >= 80) return COLORS.neonGreen;
  if (score >= 60) return COLORS.voltBlue;
  if (score >= 40) return "#F2C94C";
  return "#E07A5F";
}

function evidenceLabel(score?: number): string {
  if (score == null) return "—";
  if (score >= 80) return "HIGH";
  if (score >= 60) return "STRONG";
  if (score >= 40) return "MODERATE";
  if (score >= 20) return "LIMITED";
  return "LOW";
}

function evidenceVerdict(eb?: EvidenceBreakdown): string {
  const score = eb?.score ?? 0;
  const counts = eb?.tier_counts || { HIGH: 0, MEDIUM: 0, LOW: 0 };
  const passing = (eb?.top_features || []).filter(f => f.passes_governor).length;
  if (score === 0 || passing === 0) {
    return "Signal exists but supporting sample is limited.";
  }
  const top = (eb?.top_features || [])[0];
  if (top) {
    const sn = top.sample_size;
    const sampleTxt =
      top.category === "model" && sn >= 1000
        ? `${sn.toLocaleString()} simulated outcomes`
        : sn === 1
        ? "a single observation"
        : `${sn} observations`;
    return `Backed by ${passing} evidence-grade feature${passing === 1 ? "" : "s"} (top: ${top.name}, ${sampleTxt}).`;
  }
  return `Backed by ${counts.HIGH} HIGH + ${counts.MEDIUM} MEDIUM tier features.`;
}

// ── Main component ────────────────────────────────────────────────
export function EvidencePanel({ pick }: { pick: PickWithEvidence }) {
  const [expanded, setExpanded] = useState(false);

  const eb = pick.evidence_breakdown;
  const evScore = pick.evidence_score;

  // Hide the panel if we have NO evidence data at all — better than
  // rendering a misleading half-empty card on a legacy pick.
  if (evScore == null || !eb) return null;

  const evColor = evidenceColor(evScore);
  const evLabel = evidenceLabel(evScore);

  return (
    <View style={styles.card}>
      <View style={styles.headerRow}>
        <Text style={styles.title}>EVIDENCE</Text>
        <View style={[styles.evidencePill, { borderColor: evColor }]}>
          <Text style={[styles.evidencePillTxt, { color: evColor }]}>{evLabel}</Text>
        </View>
      </View>

      {/* The 4-metric strip — Probability · Edge · Evidence · Lock */}
      <View style={styles.metricsRow}>
        <Metric
          label="PROB"
          value={pick.win_probability != null ? `${Math.round(pick.win_probability)}%` : "—"}
          hint="Model win probability"
        />
        <Metric
          label="EDGE"
          value={
            pick.edge_percent != null
              ? `${pick.edge_percent > 0 ? "+" : ""}${pick.edge_percent.toFixed(1)}%`
              : "—"
          }
          hint="vs market consensus"
          tint={
            pick.edge_percent != null && pick.edge_percent > 0
              ? COLORS.neonGreen
              : pick.edge_percent != null && pick.edge_percent < 0
              ? "#E07A5F"
              : undefined
          }
        />
        <Metric
          label="EVIDENCE"
          value={`${evScore}`}
          hint="Provenance score"
          tint={evColor}
          highlight
        />
        <Metric
          label="LOCK"
          value={pick.lock_score != null ? `${Math.round(pick.lock_score)}` : "—"}
          hint={
            pick.lock_score_raw != null && pick.lock_score_raw !== pick.lock_score
              ? `Raw ${Math.round(pick.lock_score_raw)} × ${eb.multiplier?.toFixed(2)}`
              : "Confidence display"
          }
        />
      </View>

      {/* One-line verdict */}
      <Text style={styles.verdict}>{evidenceVerdict(eb)}</Text>

      {/* Tier counts */}
      <View style={styles.tierRow}>
        <TierBadge tier="HIGH"   count={eb.tier_counts?.HIGH   ?? 0} />
        <TierBadge tier="MEDIUM" count={eb.tier_counts?.MEDIUM ?? 0} />
        <TierBadge tier="LOW"    count={eb.tier_counts?.LOW    ?? 0} />
      </View>

      {/* Expand: top features */}
      <Pressable
        onPress={() => setExpanded(v => !v)}
        style={styles.expandRow}
        hitSlop={10}
      >
        <Text style={styles.expandTxt}>
          {expanded ? "Hide evidence detail" : "Show evidence detail"}
        </Text>
        <Text style={[styles.expandChevron, expanded && { transform: [{ rotate: "180deg" }] }]}>
          ▾
        </Text>
      </Pressable>

      {expanded && (eb.top_features || []).length > 0 && (
        <View style={styles.detailWrap}>
          {(eb.top_features || []).map((f, idx) => (
            <FeatureRow key={`top-${idx}`} feature={f} />
          ))}
          {(eb.excluded_features || []).length > 0 && (
            <>
              <Text style={styles.excludedHeader}>
                Excluded ({(eb.excluded_features || []).length}) — below evidence threshold
              </Text>
              {(eb.excluded_features || []).slice(0, 4).map((f, idx) => (
                <FeatureRow key={`x-${idx}`} feature={f} muted />
              ))}
            </>
          )}
        </View>
      )}
    </View>
  );
}

// ── Sub-components ────────────────────────────────────────────────
function Metric({
  label, value, hint, tint, highlight,
}: {
  label: string;
  value: string;
  hint: string;
  tint?: string;
  highlight?: boolean;
}) {
  return (
    <View style={styles.metricCell}>
      <Text style={styles.metricLabel}>{label}</Text>
      <Text
        style={[
          styles.metricValue,
          tint ? { color: tint } : null,
          highlight ? { fontWeight: "900" } : null,
        ]}
        numberOfLines={1}
        adjustsFontSizeToFit
      >
        {value}
      </Text>
      <Text style={styles.metricHint} numberOfLines={1}>{hint}</Text>
    </View>
  );
}

function TierBadge({ tier, count }: { tier: "HIGH" | "MEDIUM" | "LOW"; count: number }) {
  const color = tierColor(tier);
  return (
    <View style={[styles.tierBadge, { borderColor: color }]}>
      <Text style={[styles.tierBadgeNum, { color }]}>{count}</Text>
      <Text style={[styles.tierBadgeLbl, { color }]}>{tier}</Text>
    </View>
  );
}

function FeatureRow({ feature, muted }: { feature: EvidenceFeature; muted?: boolean }) {
  const color = tierColor(feature.tier);
  return (
    <View style={[styles.featureRow, muted && { opacity: 0.55 }]}>
      <View style={styles.featureMain}>
        <Text style={styles.featureName} numberOfLines={1}>{feature.name}</Text>
        <Text style={styles.featureSource} numberOfLines={1}>
          {feature.source} · n={feature.sample_size} · {feature.lookback_days}d window
        </Text>
      </View>
      <View style={[styles.featureTier, { borderColor: color }]}>
        <Text style={[styles.featureTierTxt, { color }]}>{feature.tier}</Text>
      </View>
    </View>
  );
}

// ── Styles ────────────────────────────────────────────────────────
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
    marginBottom: 14,
  },
  title: {
    color: COLORS.textPrimary,
    fontSize: 12,
    fontWeight: "900",
    letterSpacing: 1.4,
  },
  evidencePill: {
    paddingHorizontal: 10,
    paddingVertical: 4,
    borderRadius: 14,
    borderWidth: 1,
  },
  evidencePillTxt: {
    fontSize: 10,
    fontWeight: "900",
    letterSpacing: 1.1,
  },
  metricsRow: {
    flexDirection: "row",
    justifyContent: "space-between",
    paddingVertical: 4,
    marginBottom: 12,
  },
  metricCell: {
    flex: 1,
    alignItems: "center",
    paddingHorizontal: 2,
  },
  metricLabel: {
    color: COLORS.textMuted,
    fontSize: 9,
    fontWeight: "800",
    letterSpacing: 1.1,
    marginBottom: 4,
  },
  metricValue: {
    color: COLORS.textPrimary,
    fontSize: 18,
    fontWeight: "800",
    fontVariant: ["tabular-nums"],
    marginBottom: 3,
  },
  metricHint: {
    color: COLORS.textMuted,
    fontSize: 9,
    letterSpacing: 0.3,
    textAlign: "center",
  },
  verdict: {
    color: COLORS.textSecondary,
    fontSize: 12,
    lineHeight: 17,
    marginBottom: 12,
  },
  tierRow: {
    flexDirection: "row",
    gap: 8,
    marginBottom: 12,
  },
  tierBadge: {
    flex: 1,
    paddingVertical: 8,
    borderRadius: 10,
    borderWidth: 1,
    alignItems: "center",
  },
  tierBadgeNum: {
    fontSize: 18,
    fontWeight: "900",
    fontVariant: ["tabular-nums"],
  },
  tierBadgeLbl: {
    fontSize: 9,
    fontWeight: "800",
    letterSpacing: 1.0,
    marginTop: 1,
  },
  expandRow: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    paddingVertical: 6,
    marginTop: 4,
    borderTopWidth: StyleSheet.hairlineWidth,
    borderTopColor: COLORS.borderDefault,
  },
  expandTxt: {
    color: COLORS.voltBlue,
    fontSize: 11,
    fontWeight: "800",
    letterSpacing: 0.8,
    marginRight: 6,
  },
  expandChevron: {
    color: COLORS.voltBlue,
    fontSize: 10,
  },
  detailWrap: {
    marginTop: 8,
  },
  featureRow: {
    flexDirection: "row",
    alignItems: "center",
    paddingVertical: 6,
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: COLORS.borderDefault,
  },
  featureMain: {
    flex: 1,
    marginRight: 8,
  },
  featureName: {
    color: COLORS.textPrimary,
    fontSize: 12,
    fontWeight: "700",
    marginBottom: 2,
  },
  featureSource: {
    color: COLORS.textMuted,
    fontSize: 10,
  },
  featureTier: {
    paddingHorizontal: 8,
    paddingVertical: 2,
    borderRadius: 10,
    borderWidth: 1,
  },
  featureTierTxt: {
    fontSize: 9,
    fontWeight: "900",
    letterSpacing: 0.8,
  },
  excludedHeader: {
    color: COLORS.textMuted,
    fontSize: 10,
    fontWeight: "700",
    letterSpacing: 0.6,
    marginTop: 10,
    marginBottom: 4,
  },
});
