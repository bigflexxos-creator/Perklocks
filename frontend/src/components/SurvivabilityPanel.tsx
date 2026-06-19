/**
 * SurvivabilityPanel — clean conditional-hit coverage display for MLB hit props.
 *
 * Mounts under the pick-detail screen for MLB hit props only.
 * Shows three glanceable sections:
 *   • COVERAGE  — Survival Index (0-100) + sample reliability label
 *   • HISTORY   — top conditional hitters w/ streak + score
 *   • WHY       — short explainer
 *
 * Pure-insight: never modifies the primary pick. If the backend reports
 * insufficient sample / not eligible, we render a clean inline message
 * instead of breaking the layout.
 */
import React, { useEffect, useState } from "react";
import { View, Text, StyleSheet, ActivityIndicator } from "react-native";
import { COLORS } from "@/src/theme";
import { api, Pick } from "@/src/lib/api";

type Coverage = Awaited<ReturnType<typeof api.pickCoverage>>;

export function SurvivabilityPanel({ pick }: { pick: Pick }) {
  const eligible = isMLBHitProp(pick);
  const [data, setData] = useState<Coverage | null>(null);
  const [loading, setLoading] = useState(eligible);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!eligible) return;
    let cancelled = false;
    (async () => {
      try {
        const cov = await api.pickCoverage(pick.id);
        if (!cancelled) {
          setData(cov);
          setLoading(false);
        }
      } catch (e: any) {
        if (!cancelled) {
          setError(e?.message || "Coverage unavailable");
          setLoading(false);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [pick.id, eligible]);

  if (!eligible) return null;

  return (
    <View style={styles.wrap}>
      <View style={styles.headerRow}>
        <View style={styles.titleBlock}>
          <Text style={styles.sectionLabel}>SURVIVABILITY ENGINE</Text>
          <Text style={styles.tagline}>
            If {firstName(data?.primary?.name) || "the primary"} misses,
            here&apos;s who historically picks up the slack.
          </Text>
        </View>
        <View style={styles.insightChip}>
          <Text style={styles.insightChipText}>INSIGHT</Text>
        </View>
      </View>

      {loading ? (
        <View style={styles.loadingRow}>
          <ActivityIndicator size="small" color={COLORS.voltBlue} />
          <Text style={styles.loadingText}>Crunching season game logs…</Text>
        </View>
      ) : error ? (
        <View style={styles.emptyCard}>
          <Text style={styles.emptyText}>Coverage unavailable. {error}</Text>
        </View>
      ) : data ? (
        <CoverageBody data={data} />
      ) : null}
    </View>
  );
}

function CoverageBody({ data }: { data: Coverage }) {
  const insufficient = !data.candidates || data.candidates.length === 0;
  const idx = Math.round(data.survival_index || 0);
  const idxColor =
    idx >= 70 ? COLORS.neonGreen : idx >= 40 ? COLORS.voltBlue : COLORS.textMuted;
  const reliabilityColor =
    data.reliability === "High Sample"
      ? COLORS.neonGreen
      : data.reliability === "Medium Sample"
      ? COLORS.voltBlue
      : COLORS.textMuted;

  return (
    <>
      {/* COVERAGE */}
      <View style={styles.subBlock}>
        <Text style={styles.subLabel}>COVERAGE</Text>
        <View style={styles.coverageRow}>
          <View style={[styles.coverageScore, { borderColor: idxColor + "55" }]}>
            <Text style={[styles.coverageValue, { color: idxColor }]}>{idx}</Text>
            <Text style={styles.coverageUnit}>/ 100</Text>
          </View>
          <View style={{ flex: 1 }}>
            <Text style={styles.coverageHeadline}>Survival Index</Text>
            <Text style={styles.coverageSub}>
              Weighted historical hit-coverage when{" "}
              {firstName(data.primary?.name) || "primary"} records 0 hits.
            </Text>
            <View style={styles.metaRow}>
              <View style={[styles.metaPill, { borderColor: reliabilityColor + "55" }]}>
                <View
                  style={[styles.metaDot, { backgroundColor: reliabilityColor }]}
                />
                <Text style={[styles.metaPillText, { color: reliabilityColor }]}>
                  {data.reliability}
                </Text>
              </View>
              {typeof data.primary?.miss_games === "number" && (
                <Text style={styles.metaText}>
                  {data.primary.miss_games} miss games
                </Text>
              )}
              {typeof data.cohort_size === "number" && data.cohort_size > 0 && (
                <Text style={styles.metaText}>
                  {data.cohort_size} hitters scanned
                </Text>
              )}
            </View>
          </View>
        </View>
      </View>

      {/* HISTORY */}
      <View style={styles.subBlock}>
        <Text style={styles.subLabel}>HISTORY</Text>
        {insufficient ? (
          <View style={styles.emptyCard}>
            <Text style={styles.emptyText}>
              {data.note ||
                "Not enough overlap between the primary's miss games and teammates this season."}
            </Text>
          </View>
        ) : (
          <View style={styles.historyList}>
            {data.candidates.slice(0, 5).map((c, idx) => (
              <CandidateRow key={c.id || idx} candidate={c} rank={idx + 1} />
            ))}
          </View>
        )}
      </View>

      {/* WHY */}
      <View style={styles.subBlock}>
        <Text style={styles.subLabel}>WHY</Text>
        <View style={styles.whyCard}>
          <Text style={styles.whyText}>
            We scan season game logs for every teammate, count hits on dates the
            primary went 0-for, and rank by a recency-weighted average
            (last 10 · last 30 · season).
          </Text>
          <Text style={[styles.whyText, { marginTop: 6 }]}>
            Anti-overfit guardrails: minimum 5 miss-game overlap, streak display
            capped at last 10, no &quot;100%&quot; labels.
          </Text>
          <Text style={styles.whyFooter}>
            Insight only — does not replace or modify the primary pick.
          </Text>
        </View>
      </View>
    </>
  );
}

function CandidateRow({
  candidate,
  rank,
}: {
  candidate: Coverage["candidates"][number];
  rank: number;
}) {
  const pct = Math.round((candidate.score || 0) * 100);
  const tint =
    pct >= 70 ? COLORS.neonGreen : pct >= 55 ? COLORS.voltBlue : COLORS.textMuted;
  return (
    <View style={styles.candidateRow}>
      <View style={[styles.rankBadge, { borderColor: tint + "55" }]}>
        <Text style={[styles.rankBadgeText, { color: tint }]}>{rank}</Text>
      </View>
      <View style={{ flex: 1 }}>
        <View style={styles.candidateTopRow}>
          <Text style={styles.candidateName} numberOfLines={1}>
            {candidate.name}
          </Text>
          {candidate.position && (
            <Text style={styles.candidatePos}>{candidate.position}</Text>
          )}
        </View>
        <Text style={styles.candidateLabel}>{candidate.label}</Text>
      </View>
      <View style={styles.candidateStats}>
        <Text style={[styles.candidatePct, { color: tint }]}>{pct}%</Text>
        <Text style={styles.candidateStreak}>{candidate.streak}</Text>
      </View>
    </View>
  );
}

function isMLBHitProp(pick: Pick): boolean {
  if ((pick.sport || "").toUpperCase() !== "MLB") return false;
  const m = (pick.market || "").toLowerCase();
  return m.includes("hits") && !m.includes("strikeout");
}

function firstName(full?: string | null) {
  if (!full) return null;
  return full.split(" ")[0];
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

  subBlock: { marginTop: 16 },
  subLabel: {
    color: COLORS.textMuted,
    fontSize: 9,
    fontWeight: "800",
    letterSpacing: 1.6,
    marginBottom: 10,
  },

  coverageRow: { flexDirection: "row", alignItems: "center", gap: 14 },
  coverageScore: {
    width: 68,
    height: 68,
    borderRadius: 34,
    borderWidth: 1.5,
    alignItems: "center",
    justifyContent: "center",
  },
  coverageValue: { fontSize: 24, fontWeight: "900", letterSpacing: -0.5 },
  coverageUnit: {
    color: COLORS.textMuted,
    fontSize: 8,
    fontWeight: "800",
    letterSpacing: 0.8,
    marginTop: -2,
  },
  coverageHeadline: {
    color: COLORS.textPrimary,
    fontSize: 14,
    fontWeight: "800",
    letterSpacing: -0.2,
  },
  coverageSub: {
    color: COLORS.textSecondary,
    fontSize: 11,
    lineHeight: 16,
    marginTop: 3,
  },
  metaRow: {
    flexDirection: "row",
    alignItems: "center",
    flexWrap: "wrap",
    gap: 8,
    marginTop: 8,
  },
  metaPill: {
    flexDirection: "row",
    alignItems: "center",
    gap: 5,
    paddingHorizontal: 7,
    paddingVertical: 2,
    borderRadius: 4,
    borderWidth: 1,
  },
  metaDot: { width: 5, height: 5, borderRadius: 3 },
  metaPillText: {
    fontSize: 9,
    fontWeight: "900",
    letterSpacing: 0.8,
  },
  metaText: {
    color: COLORS.textMuted,
    fontSize: 10,
    fontWeight: "700",
    letterSpacing: 0.3,
  },

  historyList: { gap: 8 },
  candidateRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
    paddingVertical: 8,
    paddingHorizontal: 10,
    borderRadius: 10,
    borderWidth: 1,
    borderColor: COLORS.borderDefault,
    backgroundColor: "rgba(255,255,255,0.02)",
  },
  rankBadge: {
    width: 22,
    height: 22,
    borderRadius: 11,
    borderWidth: 1.5,
    alignItems: "center",
    justifyContent: "center",
  },
  rankBadgeText: { fontSize: 11, fontWeight: "900" },
  candidateTopRow: { flexDirection: "row", alignItems: "center", gap: 8 },
  candidateName: {
    color: COLORS.textPrimary,
    fontSize: 13,
    fontWeight: "800",
    letterSpacing: -0.2,
  },
  candidatePos: {
    color: COLORS.textMuted,
    fontSize: 9,
    fontWeight: "800",
    letterSpacing: 0.8,
  },
  candidateLabel: {
    color: COLORS.textSecondary,
    fontSize: 11,
    fontWeight: "600",
    marginTop: 2,
  },
  candidateStats: { alignItems: "flex-end" },
  candidatePct: {
    fontSize: 16,
    fontWeight: "900",
    letterSpacing: -0.3,
    fontVariant: ["tabular-nums"],
  },
  candidateStreak: {
    color: COLORS.textMuted,
    fontSize: 10,
    fontWeight: "700",
    fontVariant: ["tabular-nums"],
    marginTop: 1,
  },

  whyCard: {
    backgroundColor: "rgba(255,255,255,0.02)",
    borderRadius: 10,
    padding: 12,
    borderWidth: 1,
    borderColor: COLORS.borderDefault,
  },
  whyText: {
    color: COLORS.textSecondary,
    fontSize: 11,
    lineHeight: 17,
  },
  whyFooter: {
    color: COLORS.textMuted,
    fontSize: 10,
    fontWeight: "700",
    letterSpacing: 0.4,
    marginTop: 10,
  },

  emptyCard: {
    backgroundColor: "rgba(255,255,255,0.02)",
    borderRadius: 10,
    padding: 12,
    borderWidth: 1,
    borderColor: COLORS.borderDefault,
  },
  emptyText: {
    color: COLORS.textMuted,
    fontSize: 11,
    lineHeight: 17,
    fontWeight: "600",
  },
});
