/**
 * /admin/pick-evidence/[id] — the Universal Evidence System inspector.
 *
 * Renders the complete audit trail for a single pick:
 *   • The 4 separated metrics (Probability / Edge / Evidence / Lock)
 *   • Raw vs governed lock score + the multiplier applied
 *   • Every feature with full envelope (value / sample_size / lookback /
 *     source / freshness / importance / reliability / tier / passes_governor)
 *   • Excluded features (and WHY they were excluded)
 *   • Dropped insights (hype-filtered)
 *   • Final UI-bound key_insights
 *
 * Reached either by deep-link or by tapping a "View evidence trail" link
 * from the regular /pick/[id] screen.
 *
 * This screen IS the success condition for rule #8 of the spec:
 * "No explanation can appear unless the app can prove where the data
 *  came from."
 */
import React, { useEffect, useState } from "react";
import {
  View, Text, StyleSheet, ScrollView, ActivityIndicator,
  TouchableOpacity, RefreshControl,
} from "react-native";
import { useLocalSearchParams, useRouter } from "expo-router";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { COLORS } from "@/src/theme";
import { api } from "@/src/lib/api";

type Feature = {
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

type Inspector = Awaited<ReturnType<typeof api.pickEvidenceInspector>>;

function tierColor(t: string): string {
  if (t === "HIGH")   return COLORS.neonGreen;
  if (t === "MEDIUM") return COLORS.voltBlue;
  return COLORS.textMuted;
}

export default function PickEvidenceInspector() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const router = useRouter();
  const insets = useSafeAreaInsets();
  const [data, setData] = useState<Inspector | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  const load = async () => {
    if (!id) return;
    setLoading(true);
    setErr(null);
    try {
      const out = await api.pickEvidenceInspector(id);
      setData(out);
    } catch (e: any) {
      setErr(e?.message || "Failed to load evidence");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); /* eslint-disable-next-line */ }, [id]);

  if (loading && !data) {
    return (
      <View style={[styles.center, { paddingTop: insets.top + 100 }]}>
        <ActivityIndicator size="large" color={COLORS.voltBlue} />
        <Text style={styles.loadingTxt}>Loading evidence trail…</Text>
      </View>
    );
  }

  if (err || !data) {
    return (
      <View style={[styles.center, { paddingTop: insets.top + 80 }]}>
        <Text style={styles.errTxt}>{err || "No data"}</Text>
        <TouchableOpacity onPress={() => router.back()} style={styles.backBtn}>
          <Text style={styles.backTxt}>← Back</Text>
        </TouchableOpacity>
      </View>
    );
  }

  const eb = data.evidence_breakdown || {};
  const top: Feature[]      = eb.top_features      || [];
  const excluded: Feature[] = eb.excluded_features || [];

  return (
    <ScrollView
      style={{ flex: 1, backgroundColor: COLORS.bg }}
      contentContainerStyle={{ paddingTop: insets.top + 12, paddingBottom: 60 }}
      refreshControl={<RefreshControl refreshing={loading} onRefresh={load} tintColor={COLORS.voltBlue} />}
    >
      {/* Header */}
      <View style={styles.headerWrap}>
        <TouchableOpacity onPress={() => router.back()} hitSlop={20}>
          <Text style={styles.backTxt}>← Back</Text>
        </TouchableOpacity>
        <Text style={styles.h1}>EVIDENCE INSPECTOR</Text>
        <Text style={styles.subhead} numberOfLines={2}>
          {data.market}
        </Text>
        <Text style={styles.event}>{data.event} · {data.sport}</Text>
      </View>

      {/* 4 metrics */}
      <View style={styles.metricsCard}>
        <MetricRow label="Probability" value={data.probability_pct != null ? `${data.probability_pct.toFixed(1)}%` : "—"} hint="Model win probability — never mutated" />
        <MetricRow label="Edge"        value={data.edge_pct != null ? `${data.edge_pct > 0 ? "+" : ""}${data.edge_pct.toFixed(1)}%` : "—"} hint="vs market consensus" />
        <MetricRow label="Evidence"    value={data.evidence_score != null ? `${data.evidence_score}` : "—"} hint={`× ${eb.multiplier?.toFixed(2) ?? "1.00"} multiplier`} highlight />
        <MetricRow
          label="Lock"
          value={data.lock_score != null ? `${data.lock_score.toFixed(1)}` : "—"}
          hint={
            data.lock_score_raw != null && data.lock_score_raw !== data.lock_score
              ? `Raw ${data.lock_score_raw.toFixed(1)} × ${eb.multiplier?.toFixed(2)} = ${data.lock_score?.toFixed(1)}`
              : "Confidence display"
          }
        />
      </View>

      {/* Tier counts */}
      <View style={styles.tierRow}>
        {(["HIGH", "MEDIUM", "LOW"] as const).map(t => (
          <View key={t} style={[styles.tierBox, { borderColor: tierColor(t) }]}>
            <Text style={[styles.tierNum, { color: tierColor(t) }]}>
              {eb.tier_counts?.[t] ?? 0}
            </Text>
            <Text style={[styles.tierLbl, { color: tierColor(t) }]}>{t}</Text>
          </View>
        ))}
      </View>

      {/* Top features */}
      <Text style={styles.sectionLbl}>TOP FEATURES (used in claims)</Text>
      <View style={styles.featList}>
        {top.length === 0 ? (
          <Text style={styles.emptyTxt}>No features above the evidence threshold.</Text>
        ) : top.map((f, i) => <FeatureCard key={`t-${i}`} feature={f} />)}
      </View>

      {/* Excluded features */}
      {excluded.length > 0 && (
        <>
          <Text style={styles.sectionLbl}>EXCLUDED (below threshold)</Text>
          <View style={styles.featList}>
            {excluded.map((f, i) => (
              <FeatureCard key={`x-${i}`} feature={f} muted />
            ))}
          </View>
        </>
      )}

      {/* Dropped insights */}
      {(eb.dropped_insights || []).length > 0 && (
        <>
          <Text style={styles.sectionLbl}>DROPPED INSIGHTS (hype-filtered)</Text>
          <View style={styles.dropList}>
            {(eb.dropped_insights || []).map((s: string, i: number) => (
              <Text key={`d-${i}`} style={styles.dropTxt}>• {s}</Text>
            ))}
          </View>
        </>
      )}

      {/* Final insights surfaced */}
      <Text style={styles.sectionLbl}>SURFACED INSIGHTS</Text>
      <View style={styles.surfList}>
        {(data.key_insights || []).map((s, i) => (
          <Text key={`s-${i}`} style={styles.surfTxt}>• {s}</Text>
        ))}
      </View>

      <Text style={styles.foot}>
        Generated at {eb.generated_at || "—"} · Pick ID {data.pick_id}
      </Text>
    </ScrollView>
  );
}

function MetricRow({
  label, value, hint, highlight,
}: { label: string; value: string; hint: string; highlight?: boolean }) {
  return (
    <View style={styles.metricRow}>
      <Text style={styles.metricLbl}>{label}</Text>
      <View style={{ flex: 1 }} />
      <View style={{ alignItems: "flex-end" }}>
        <Text
          style={[
            styles.metricValue,
            highlight && { color: COLORS.voltBlue, fontWeight: "900" },
          ]}
        >
          {value}
        </Text>
        <Text style={styles.metricHint}>{hint}</Text>
      </View>
    </View>
  );
}

function FeatureCard({ feature, muted }: { feature: Feature; muted?: boolean }) {
  const color = tierColor(feature.tier);
  return (
    <View style={[styles.featCard, muted && { opacity: 0.6 }]}>
      <View style={styles.featHead}>
        <Text style={styles.featName} numberOfLines={1}>{feature.name}</Text>
        <View style={[styles.featTier, { borderColor: color }]}>
          <Text style={[styles.featTierTxt, { color }]}>{feature.tier}</Text>
        </View>
      </View>
      <View style={styles.featMetaRow}>
        <FeatChip label="cat"     value={feature.category} />
        <FeatChip label="n"       value={`${feature.sample_size}`} />
        <FeatChip label="window"  value={`${feature.lookback_days}d`} />
        <FeatChip label="fresh"   value={`${feature.freshness_hours.toFixed(0)}h`} />
      </View>
      <View style={styles.featMetaRow}>
        <FeatChip label="imp"  value={feature.importance.toFixed(2)} />
        <FeatChip label="rel"  value={feature.reliability.toFixed(2)} />
        <FeatChip label="i×r"  value={(feature.importance * feature.reliability).toFixed(2)} />
        <FeatChip label="pass" value={feature.passes_governor ? "YES" : "NO"} />
      </View>
      <Text style={styles.featSource}>source: {feature.source}</Text>
      {feature.reason ? (
        <Text style={styles.featReason}>{feature.reason}</Text>
      ) : null}
    </View>
  );
}

function FeatChip({ label, value }: { label: string; value: string }) {
  return (
    <View style={styles.featChip}>
      <Text style={styles.featChipLbl}>{label}</Text>
      <Text style={styles.featChipVal}>{value}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  center: { flex: 1, alignItems: "center", justifyContent: "center", backgroundColor: COLORS.bg },
  loadingTxt: { color: COLORS.textMuted, marginTop: 10, fontSize: 13 },
  errTxt: { color: "#E07A5F", fontSize: 14, marginBottom: 16 },
  backBtn: { paddingHorizontal: 16, paddingVertical: 8, borderRadius: 8, borderWidth: 1, borderColor: COLORS.borderDefault },
  backTxt: { color: COLORS.voltBlue, fontSize: 13, fontWeight: "700", paddingHorizontal: 16, paddingVertical: 6 },
  headerWrap: { paddingHorizontal: 16, marginBottom: 12 },
  h1: { color: COLORS.textPrimary, fontSize: 18, fontWeight: "900", letterSpacing: 1.2, marginTop: 8 },
  subhead: { color: COLORS.textSecondary, fontSize: 13, marginTop: 6, fontWeight: "600" },
  event: { color: COLORS.textMuted, fontSize: 11, marginTop: 2 },
  metricsCard: {
    marginHorizontal: 16,
    backgroundColor: COLORS.surface,
    borderRadius: 14,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: COLORS.borderDefault,
    padding: 16,
    marginBottom: 12,
  },
  metricRow: {
    flexDirection: "row",
    alignItems: "center",
    paddingVertical: 8,
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: COLORS.borderDefault,
  },
  metricLbl: { color: COLORS.textSecondary, fontSize: 12, fontWeight: "700", letterSpacing: 0.7 },
  metricValue: { color: COLORS.textPrimary, fontSize: 16, fontWeight: "800", fontVariant: ["tabular-nums"] },
  metricHint: { color: COLORS.textMuted, fontSize: 10, marginTop: 1 },
  tierRow: { flexDirection: "row", paddingHorizontal: 16, gap: 8, marginBottom: 14 },
  tierBox: { flex: 1, paddingVertical: 12, borderRadius: 10, borderWidth: 1, alignItems: "center" },
  tierNum: { fontSize: 22, fontWeight: "900", fontVariant: ["tabular-nums"] },
  tierLbl: { fontSize: 10, fontWeight: "800", letterSpacing: 1.0, marginTop: 2 },
  sectionLbl: {
    color: COLORS.textMuted, fontSize: 10, fontWeight: "800", letterSpacing: 1.3,
    marginTop: 16, marginBottom: 8, paddingHorizontal: 16,
  },
  featList: { paddingHorizontal: 16, gap: 8 },
  featCard: {
    backgroundColor: COLORS.surface,
    borderRadius: 12,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: COLORS.borderDefault,
    padding: 12,
    marginBottom: 8,
  },
  featHead: { flexDirection: "row", alignItems: "center", marginBottom: 8 },
  featName: { color: COLORS.textPrimary, fontSize: 13, fontWeight: "800", flex: 1, marginRight: 8 },
  featTier: { paddingHorizontal: 8, paddingVertical: 2, borderRadius: 8, borderWidth: 1 },
  featTierTxt: { fontSize: 9, fontWeight: "900", letterSpacing: 0.8 },
  featMetaRow: { flexDirection: "row", gap: 6, marginBottom: 6, flexWrap: "wrap" },
  featChip: {
    flexDirection: "row", alignItems: "center", gap: 4,
    backgroundColor: COLORS.bg, paddingHorizontal: 7, paddingVertical: 3, borderRadius: 6,
  },
  featChipLbl: { color: COLORS.textMuted, fontSize: 9, fontWeight: "800", letterSpacing: 0.6 },
  featChipVal: { color: COLORS.textPrimary, fontSize: 10, fontWeight: "700", fontVariant: ["tabular-nums"] },
  featSource: { color: COLORS.textMuted, fontSize: 10, marginTop: 4, fontStyle: "italic" },
  featReason: { color: COLORS.textSecondary, fontSize: 11, marginTop: 4 },
  emptyTxt: { color: COLORS.textMuted, fontSize: 12, padding: 16, textAlign: "center" },
  dropList: { paddingHorizontal: 16 },
  dropTxt: { color: "#E07A5F", fontSize: 11, marginBottom: 4, lineHeight: 16 },
  surfList: { paddingHorizontal: 16, marginBottom: 24 },
  surfTxt: { color: COLORS.textPrimary, fontSize: 12, marginBottom: 5, lineHeight: 17 },
  foot: { color: COLORS.textMuted, fontSize: 9, textAlign: "center", paddingHorizontal: 16, marginTop: 8 },
});
