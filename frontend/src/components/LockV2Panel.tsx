/**
 * LockV2Panel — Shadow Lock Engine breakdown.
 *
 * Renders a clean "DEEP THINKING" insight card under FACTOR BREAKDOWN on the
 * pick detail screen. Shows the parallel Counter / Survival / Sim metrics
 * computed by the Lock Engine V2 (shadow mode) without disrupting the
 * production lock_score the user is already familiar with.
 *
 * Hidden gracefully if the pick has no v2 data yet.
 */
import React, { useEffect, useState } from "react";
import { View, Text, StyleSheet, ActivityIndicator } from "react-native";
import { COLORS } from "@/src/theme";
import { api, Pick } from "@/src/lib/api";

type Breakdown = Awaited<ReturnType<typeof api.pickLockBreakdown>>;

const TIER_COLOR: Record<string, string> = {
  "Apex Lock":   COLORS.electricBlaze,
  "Rare Lock":   COLORS.neonGreen,
  "Strong Lock": COLORS.voltBlue,
  "Elite Setup": COLORS.textSecondary,
};

export function LockV2Panel({ pick }: { pick: Pick }) {
  const [data, setData] = useState<Breakdown | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await api.pickLockBreakdown(pick.id);
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
          <Text style={styles.sectionLabel}>DEEP THINKING ENGINE</Text>
          <View style={styles.shadowChip}>
            <Text style={styles.shadowChipText}>SHADOW</Text>
          </View>
        </View>
        <View style={styles.loadingRow}>
          <ActivityIndicator size="small" color={COLORS.voltBlue} />
          <Text style={styles.mutedSmall}>Running counter cases…</Text>
        </View>
      </View>
    );
  }

  if (!data?.shadow || data.shadow.lock_score_v2 == null) {
    return null;
  }

  const s = data.shadow;
  const tierColor = TIER_COLOR[s.tier_v2 || ""] || COLORS.textSecondary;
  const counterColor =
    (s.counter_score ?? 0) >= 60 ? COLORS.electricBlaze
    : (s.counter_score ?? 0) >= 40 ? "#F59E0B"
    : (s.counter_score ?? 0) >= 20 ? COLORS.voltBlue
    : COLORS.neonGreen;

  return (
    <View style={styles.wrap}>
      <View style={styles.headerRow}>
        <View style={{ flex: 1 }}>
          <Text style={styles.sectionLabel}>DEEP THINKING ENGINE</Text>
          <Text style={styles.tagline}>
            Same pick, scored through opposing-case + edge-removal lenses.
          </Text>
        </View>
        <View style={styles.shadowChip}>
          <Text style={styles.shadowChipText}>SHADOW</Text>
        </View>
      </View>

      {/* Headline: Lock V2 + Tier */}
      <View style={styles.headlineRow}>
        <View style={[styles.lockBlock, { borderColor: tierColor + "55" }]}>
          <Text style={[styles.lockValue, { color: tierColor }]}>
            {Number(s.lock_score_v2).toFixed(0)}
          </Text>
          <Text style={styles.lockUnit}>LOCK V2</Text>
        </View>
        <View style={{ flex: 1 }}>
          <Text style={[styles.tierName, { color: tierColor }]}>
            {s.tier_v2}{s.is_apex ? " · ⚡ APEX" : ""}
          </Text>
          <Text style={styles.subHeadline}>
            Old lock {pick.lock_score?.toFixed(0)} →{" "}
            <Text style={{ color: tierColor, fontWeight: "800" }}>
              v2 {Number(s.lock_score_v2).toFixed(0)}
            </Text>
          </Text>
          {!s.is_apex && s.apex_blockers && s.apex_blockers.length > 0 && (
            <Text style={styles.blockerText} numberOfLines={2}>
              Apex blocked by: {s.apex_blockers.slice(0, 2).join(" · ")}
            </Text>
          )}
        </View>
      </View>

      {/* 4 metric rows */}
      <View style={styles.metricGrid}>
        <Metric label="Evidence"  value={s.evidence_score}  hi={75} kind="up" />
        <Metric label="Conviction" value={s.conviction_score} hi={75} kind="up" />
        <Metric label="Counter"    value={s.counter_score}   hi={30} kind="down"
                colorOverride={counterColor} />
        <Metric label="Survival"   value={s.survival_score}  hi={70} kind="up" />
        <Metric label="Sim Pass"   value={s.simulation_pass} hi={80} kind="up" suffix="%" />
        <Metric label="Agreement"  value={s.agreement_score} hi={70} kind="up" />
      </View>

      {/* Why NOT this pick */}
      {s.v2_reasons?.counter && s.v2_reasons.counter.length > 0 && (
        <View style={styles.reasonBlock}>
          <Text style={styles.reasonHeader}>WHY NOT THIS PICK</Text>
          {s.v2_reasons.counter.slice(0, 3).map(([sign, kind, detail], i) => (
            <ReasonLine key={i} sign={sign as any} kind={kind} detail={detail} />
          ))}
        </View>
      )}

      {/* Edge-removal survivability */}
      {s.v2_reasons?.survival && s.v2_reasons.survival.length > 0 && (
        <View style={styles.reasonBlock}>
          <Text style={styles.reasonHeader}>SURVIVES WITHOUT…</Text>
          {s.v2_reasons.survival.slice(0, 5).map(([sign, kind, detail], i) => (
            <ReasonLine key={i} sign={sign as any} kind={kind} detail={detail} />
          ))}
        </View>
      )}
    </View>
  );
}

function Metric({
  label, value, hi, kind, suffix = "", colorOverride,
}: {
  label: string;
  value?: number;
  hi: number;
  kind: "up" | "down";
  suffix?: string;
  colorOverride?: string;
}) {
  const v = value ?? 0;
  const good = kind === "up" ? v >= hi : v <= hi;
  const color = colorOverride
    ? colorOverride
    : good ? COLORS.neonGreen : COLORS.textMuted;
  return (
    <View style={styles.metricCell}>
      <Text style={styles.metricLabel}>{label}</Text>
      <Text style={[styles.metricValue, { color }]}>
        {Math.round(v)}{suffix}
      </Text>
    </View>
  );
}

function ReasonLine({
  sign, kind, detail,
}: { sign: "ok" | "warn" | string; kind: string; detail: string }) {
  const isOk = sign === "ok";
  const color = isOk ? COLORS.neonGreen : "#F59E0B";
  const mark = isOk ? "✓" : "⚠";
  return (
    <View style={styles.reasonRow}>
      <Text style={[styles.reasonMark, { color }]}>{mark}</Text>
      <View style={{ flex: 1 }}>
        <Text style={styles.reasonKind} numberOfLines={1}>{kind}</Text>
        <Text style={styles.reasonDetail} numberOfLines={2}>{detail}</Text>
      </View>
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
    borderColor: COLORS.textMuted + "55",
    backgroundColor: COLORS.textMuted + "12",
  },
  shadowChipText: {
    color: COLORS.textMuted,
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
  mutedSmall: { color: COLORS.textMuted, fontSize: 11, fontWeight: "600" },

  headlineRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 14,
    marginTop: 16,
  },
  lockBlock: {
    width: 72,
    height: 72,
    borderRadius: 36,
    borderWidth: 1.5,
    alignItems: "center",
    justifyContent: "center",
  },
  lockValue: {
    fontSize: 26,
    fontWeight: "900",
    letterSpacing: -0.5,
    fontVariant: ["tabular-nums"],
  },
  lockUnit: {
    color: COLORS.textMuted,
    fontSize: 8,
    fontWeight: "800",
    letterSpacing: 0.8,
    marginTop: -2,
  },
  tierName: {
    fontSize: 14,
    fontWeight: "900",
    letterSpacing: -0.2,
  },
  subHeadline: {
    color: COLORS.textSecondary,
    fontSize: 12,
    marginTop: 3,
  },
  blockerText: {
    color: COLORS.textMuted,
    fontSize: 10,
    fontWeight: "600",
    marginTop: 5,
    lineHeight: 14,
  },

  metricGrid: {
    flexDirection: "row",
    flexWrap: "wrap",
    marginTop: 16,
    gap: 8,
  },
  metricCell: {
    flexBasis: "30%",
    flexGrow: 1,
    backgroundColor: "rgba(255,255,255,0.02)",
    borderRadius: 8,
    borderWidth: 1,
    borderColor: COLORS.borderDefault,
    paddingVertical: 8,
    paddingHorizontal: 10,
  },
  metricLabel: {
    color: COLORS.textMuted,
    fontSize: 9,
    fontWeight: "800",
    letterSpacing: 1.0,
    marginBottom: 3,
  },
  metricValue: {
    fontSize: 18,
    fontWeight: "900",
    letterSpacing: -0.3,
    fontVariant: ["tabular-nums"],
  },

  reasonBlock: {
    marginTop: 16,
    backgroundColor: "rgba(255,255,255,0.02)",
    borderRadius: 10,
    padding: 12,
    borderWidth: 1,
    borderColor: COLORS.borderDefault,
    gap: 10,
  },
  reasonHeader: {
    color: COLORS.textMuted,
    fontSize: 9,
    fontWeight: "800",
    letterSpacing: 1.6,
    marginBottom: 2,
  },
  reasonRow: {
    flexDirection: "row",
    alignItems: "flex-start",
    gap: 10,
  },
  reasonMark: {
    fontSize: 14,
    fontWeight: "900",
    width: 14,
    textAlign: "center",
    marginTop: 1,
  },
  reasonKind: {
    color: COLORS.textPrimary,
    fontSize: 12,
    fontWeight: "800",
    letterSpacing: -0.1,
  },
  reasonDetail: {
    color: COLORS.textSecondary,
    fontSize: 11,
    lineHeight: 15,
    marginTop: 1,
  },
});
