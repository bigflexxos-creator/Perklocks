/**
 * XGFormShadowCard — Live A/B card for the xG Form ±6pp lift.
 *
 * Rendered on the Analytics screen below the existing "Confidence
 * Calibration" table. Reads `GET /api/analytics/xg-form-shadow` and
 * shows hit rate per HOT / NEUTRAL / COLD bucket, the delta vs the
 * NEUTRAL baseline, and Brier-score comparison live-vs-shadow.
 *
 * Goal: let the user watch — daily — whether the ±6pp lift would have
 * actually improved board accuracy. Promote shadow → live ONLY when
 * HOT.delta ≥ +5pp AND COLD.delta ≤ −5pp AND n ≥ 30 in both buckets.
 *
 * Until then the card is purely informational; lift values live on the
 * pick payload as `understat_form.shadow_*` but never modify the
 * displayed `lock_score`.
 */
import React, { useEffect, useState, useCallback } from "react";
import { View, Text, StyleSheet, Pressable } from "react-native";
import { Ionicons } from "@expo/vector-icons";
import { COLORS } from "@/src/theme";
import { api, XGFormBucket } from "@/src/lib/api";

type ShadowReport = Awaited<ReturnType<typeof api.xgFormShadow>>;

export function XGFormShadowCard() {
  const [data, setData] = useState<ShadowReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await api.xgFormShadow();
      setData(res);
      setErr(false);
    } catch {
      setErr(true);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  if (err) return null;       // silent on 500/network — non-critical
  if (loading || !data) return null;

  const { HOT, COLD, NEUTRAL } = data.buckets;
  const totalSettled = HOT.n + COLD.n + NEUTRAL.n;

  // Sample-size guard. With N=0 we still render the scaffold + an
  // explainer so the user knows the test exists and is waiting on
  // settled goalscorer picks.
  const seedingPhase = totalSettled === 0;
  const hot_n_target = 30, cold_n_target = 30;
  const seedingPctHot   = Math.min(100, Math.round(((HOT.n   / hot_n_target)  * 100)));
  const seedingPctCold  = Math.min(100, Math.round(((COLD.n  / cold_n_target) * 100)));

  return (
    <View style={styles.wrap} testID="xg-form-shadow-card">
      <View style={styles.header}>
        <View>
          <Text style={styles.kicker}>xG FORM · LIVE A/B SHADOW</Text>
          <Text style={styles.title}>±6pp Lift Validation</Text>
          <Text style={styles.hint}>
            Does HOT form actually outperform NEUTRAL on settled scorer picks?
          </Text>
        </View>
        {data.promote_ready ? (
          <View style={[styles.statusPill, styles.statusReady]}>
            <Ionicons name="checkmark-circle" size={11} color="#86EFAC" />
            <Text style={styles.statusReadyTxt}>READY TO PROMOTE</Text>
          </View>
        ) : (
          <View style={[styles.statusPill, styles.statusShadow]}>
            <Text style={styles.statusShadowTxt}>SHADOW MODE</Text>
          </View>
        )}
      </View>

      {seedingPhase ? (
        <View style={styles.seedingBlock}>
          <Ionicons name="hourglass-outline" size={20} color={COLORS.textMuted} />
          <Text style={styles.seedingText}>
            Awaiting settled goalscorer picks. The test starts grading
            once today&apos;s soccer slate finalises.
          </Text>
        </View>
      ) : (
        <>
          {/* Header row */}
          <View style={styles.tableHeader}>
            <Text style={[styles.th, { flex: 1.4 }]}>FORM</Text>
            <Text style={[styles.th, { flex: 0.7, textAlign: "right" }]}>N</Text>
            <Text style={[styles.th, { flex: 1, textAlign: "right" }]}>HIT</Text>
            <Text style={[styles.th, { flex: 1, textAlign: "right" }]}>Δ vs NEU</Text>
            <Text style={[styles.th, { flex: 1, textAlign: "right" }]}>BRIER</Text>
          </View>

          <BucketRow icon="🔥" label="HOT"     tint="#FCA5A5" bucket={HOT}     showDelta />
          <BucketRow icon="📊" label="NEUTRAL" tint={COLORS.textPrimary} bucket={NEUTRAL} />
          <BucketRow icon="❄️" label="COLD"    tint="#93C5FD" bucket={COLD}    showDelta />
        </>
      )}

      {/* Progress towards promotion threshold */}
      <View style={styles.progressBlock}>
        <Text style={styles.progressLabel}>SAMPLE SIZE TO PROMOTE</Text>
        <View style={styles.progressRow}>
          <Text style={styles.progressTag}>🔥 HOT</Text>
          <View style={styles.progressBarOuter}>
            <View
              style={[
                styles.progressBarInner,
                { width: `${seedingPctHot}%`, backgroundColor: "#FCA5A5" },
              ]}
            />
          </View>
          <Text style={styles.progressVal}>{HOT.n}/{hot_n_target}</Text>
        </View>
        <View style={styles.progressRow}>
          <Text style={styles.progressTag}>❄️ COLD</Text>
          <View style={styles.progressBarOuter}>
            <View
              style={[
                styles.progressBarInner,
                { width: `${seedingPctCold}%`, backgroundColor: "#93C5FD" },
              ]}
            />
          </View>
          <Text style={styles.progressVal}>{COLD.n}/{cold_n_target}</Text>
        </View>
      </View>

      <Text style={styles.rule}>{data.promotion_rule}</Text>

      <Pressable onPress={load} style={styles.refreshBtn} hitSlop={6}>
        <Ionicons name="refresh" size={12} color={COLORS.textMuted} />
        <Text style={styles.refreshTxt}>Refresh</Text>
      </Pressable>
    </View>
  );
}

function BucketRow({
  icon, label, tint, bucket, showDelta,
}: {
  icon: string;
  label: string;
  tint: string;
  bucket: XGFormBucket;
  showDelta?: boolean;
}) {
  const hit = bucket.hit_rate ?? null;
  const delta = bucket.delta_hit_pp ?? null;
  const brier = bucket.brier_live ?? null;
  const brierShadow = bucket.brier_shadow ?? null;
  const brierBetter = brier != null && brierShadow != null && brierShadow < brier;

  return (
    <View style={styles.tableRow}>
      <Text style={[styles.td, { flex: 1.4 }]}>
        <Text style={{ fontSize: 12 }}>{icon}</Text>
        <Text style={[styles.tdLabel, { color: tint }]}>  {label}</Text>
      </Text>
      <Text style={[styles.td, { flex: 0.7, textAlign: "right" }]}>{bucket.n}</Text>
      <Text style={[styles.td, { flex: 1, textAlign: "right", color: COLORS.textPrimary }]}>
        {hit != null ? `${hit.toFixed(1)}%` : "—"}
      </Text>
      <Text style={[
        styles.td, { flex: 1, textAlign: "right", fontWeight: "700",
          color:
            !showDelta ? COLORS.textMuted
            : delta == null ? COLORS.textMuted
            : delta > 0 ? "#86EFAC"
            : delta < 0 ? "#FCA5A5"
            : COLORS.textPrimary,
        }]}>
        {showDelta && delta != null ? `${delta >= 0 ? "+" : ""}${delta.toFixed(1)}pp` : "—"}
      </Text>
      <Text style={[
        styles.td, { flex: 1, textAlign: "right",
          color: brierBetter ? "#86EFAC" : COLORS.textPrimary,
        }]}>
        {brier != null ? brier.toFixed(3) : "—"}
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
  header: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "flex-start",
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
    fontSize: 15,
    fontWeight: "900",
    letterSpacing: -0.2,
    marginTop: 3,
  },
  hint: {
    color: COLORS.textMuted,
    fontSize: 11,
    fontWeight: "600",
    marginTop: 3,
    lineHeight: 15,
  },
  statusPill: {
    paddingHorizontal: 8,
    paddingVertical: 3,
    borderRadius: 5,
    flexDirection: "row",
    alignItems: "center",
    gap: 4,
  },
  statusShadow: {
    backgroundColor: "rgba(125, 211, 252, 0.10)",
    borderWidth: 1,
    borderColor: "rgba(125, 211, 252, 0.40)",
  },
  statusShadowTxt: {
    color: "#7DD3FC",
    fontSize: 9,
    fontWeight: "900",
    letterSpacing: 1.0,
  },
  statusReady: {
    backgroundColor: "rgba(134, 239, 172, 0.12)",
    borderWidth: 1,
    borderColor: "rgba(134, 239, 172, 0.45)",
  },
  statusReadyTxt: {
    color: "#86EFAC",
    fontSize: 9,
    fontWeight: "900",
    letterSpacing: 1.0,
  },
  seedingBlock: {
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
    paddingVertical: 12,
    paddingHorizontal: 12,
    backgroundColor: "rgba(255, 255, 255, 0.03)",
    borderRadius: 8,
    marginBottom: 12,
  },
  seedingText: {
    color: COLORS.textMuted,
    fontSize: 12,
    fontWeight: "600",
    flex: 1,
    lineHeight: 16,
  },
  tableHeader: {
    flexDirection: "row",
    paddingVertical: 6,
    borderBottomWidth: 1,
    borderBottomColor: COLORS.borderDefault,
  },
  th: {
    color: COLORS.textMuted,
    fontSize: 9,
    fontWeight: "900",
    letterSpacing: 1.2,
  },
  tableRow: {
    flexDirection: "row",
    paddingVertical: 8,
    alignItems: "center",
  },
  td: {
    color: COLORS.textMuted,
    fontSize: 12,
    fontWeight: "700",
    fontVariant: ["tabular-nums"],
  },
  tdLabel: { fontWeight: "900", letterSpacing: 0.4 },
  progressBlock: {
    marginTop: 12,
    paddingTop: 10,
    borderTopWidth: 1,
    borderTopColor: COLORS.borderDefault,
  },
  progressLabel: {
    color: COLORS.textMuted,
    fontSize: 9,
    fontWeight: "900",
    letterSpacing: 1.2,
    marginBottom: 6,
  },
  progressRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    marginVertical: 3,
  },
  progressTag: {
    color: COLORS.textPrimary,
    fontSize: 10,
    fontWeight: "800",
    width: 60,
  },
  progressBarOuter: {
    flex: 1,
    height: 6,
    backgroundColor: "rgba(255, 255, 255, 0.06)",
    borderRadius: 3,
    overflow: "hidden",
  },
  progressBarInner: { height: "100%", borderRadius: 3 },
  progressVal: {
    color: COLORS.textMuted,
    fontSize: 10,
    fontWeight: "700",
    width: 44,
    textAlign: "right",
    fontVariant: ["tabular-nums"],
  },
  rule: {
    color: COLORS.textMuted,
    fontSize: 10,
    fontWeight: "600",
    fontStyle: "italic",
    marginTop: 10,
    lineHeight: 14,
  },
  refreshBtn: {
    flexDirection: "row",
    alignItems: "center",
    gap: 4,
    alignSelf: "flex-start",
    marginTop: 8,
    paddingVertical: 4,
    paddingHorizontal: 6,
  },
  refreshTxt: {
    color: COLORS.textMuted,
    fontSize: 10,
    fontWeight: "700",
    letterSpacing: 0.4,
  },
});
