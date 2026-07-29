/**
 * MatchupGradeBadge (2026-07-28)
 *
 * A small self-contained badge that lazily fetches
 *   GET /api/picks/{id}/matchup
 * and renders a letter grade + confidence chip when the pick supports
 * matchup intelligence (i.e. is a player-prop, not a team/moneyline
 * pick) AND we have historical rows for the (player, opponent) pair.
 *
 * Design goals:
 *   • Never blocks card render — appears when data resolves.
 *   • Silently hides when unsupported / no data / cold cache.
 *   • Tapping opens a compact modal showing the split summary.
 *
 * Colour convention matches other badges in the app:
 *   A+ / A  → neonGreen
 *   B       → cyan
 *   C       → warm amber
 *   D / F   → muted (badge suppressed for F unless sample is high)
 */
import React, { useEffect, useState, useCallback } from "react";
import {
  View,
  Text,
  Pressable,
  StyleSheet,
  Modal,
  ScrollView,
} from "react-native";
import { COLORS } from "@/src/theme";
import { api } from "@/src/lib/api";

type MatchupPayload = Awaited<ReturnType<typeof api.picks.pickMatchup>>;

const GRADE_COLOR: Record<string, string> = {
  "A+": "#00E68A",
  "A":  "#00D97E",
  "B":  "#4DBEFF",
  "C":  "#FFB84D",
  "D":  "#FF8C4D",
  "F":  "#8A8FA3",
};

function fmtPct(v?: number | null): string {
  if (v == null || Number.isNaN(v)) return "—";
  return `${Math.round(v * 100)}%`;
}

function fmtOpp(opp?: string | null): string {
  if (!opp) return "opponent";
  // Compact team names for the badge (long → 3-letter city).
  const parts = opp.split(/\s+/);
  if (parts.length >= 2) return parts.slice(0, 2).join(" ");
  return opp;
}

interface Props {
  pickId: string;
  /** If true, badge is compact (icon + grade only). Default: false. */
  compact?: boolean;
}

export function MatchupGradeBadge({ pickId, compact = false }: Props) {
  const [data, setData] = useState<MatchupPayload | null>(null);
  const [error, setError] = useState<boolean>(false);
  const [open, setOpen] = useState<boolean>(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await api.picks.pickMatchup(pickId);
        if (!cancelled) setData(res);
      } catch {
        if (!cancelled) setError(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [pickId]);

  const handleOpen = useCallback(() => setOpen(true), []);
  const handleClose = useCallback(() => setOpen(false), []);

  if (error || !data) return null;
  if (!data.supported) return null;
  // Hide when we have no historical rows at all — no signal to show.
  const confidence = data.sample_confidence || "none";
  const grade = (data.matchup_grade || "").toUpperCase();
  if (confidence === "none" || !grade || grade === "F") return null;

  const color = GRADE_COLOR[grade] ?? COLORS.textMuted;
  const opp = fmtOpp(data.opponent_team ?? undefined);
  const hitRate = fmtPct(data.threshold_hit_rate ?? 0);
  const sample = data.sample_size ?? 0;

  return (
    <>
      <Pressable
        onPress={handleOpen}
        style={[styles.badge, { borderColor: color }]}
        testID="matchup-grade-badge"
        accessibilityRole="button"
        accessibilityLabel={`Matchup grade ${grade} versus ${opp}`}
      >
        <Text style={[styles.gradeText, { color }]}>{grade}</Text>
        <Text style={styles.vsText}>
          vs {opp}
        </Text>
        {!compact && (
          <Text style={[styles.hitRateText, { color }]}>
            {hitRate}
          </Text>
        )}
      </Pressable>

      <Modal
        visible={open}
        transparent
        animationType="fade"
        onRequestClose={handleClose}
      >
        <Pressable style={styles.modalOverlay} onPress={handleClose}>
          <Pressable style={styles.modalCard} onPress={() => { /* eat */ }}>
            <View style={styles.modalHeader}>
              <Text style={styles.modalTitle}>
                Matchup Intelligence
              </Text>
              <Text
                style={[
                  styles.modalGrade,
                  { color, borderColor: color },
                ]}
              >
                {grade}
              </Text>
            </View>
            <Text style={styles.modalSub}>
              {data.player_name}
              {opp ? ` vs ${opp}` : ""}
              {data.stat ? ` · ${data.stat.replace(/_/g, " ")}` : ""}
              {data.threshold != null ? ` · ${data.threshold}` : ""}
            </Text>

            <ScrollView style={styles.modalBody}>
              <MatchupRow
                label="Hit rate (threshold)"
                value={fmtPct(data.threshold_hit_rate)}
              />
              <MatchupRow
                label="Sample size"
                value={`${sample} games (${confidence})`}
              />
              {typeof data.avg_stat_output === "number" &&
                data.avg_stat_output > 0 && (
                  <MatchupRow
                    label="Average output"
                    value={data.avg_stat_output.toFixed(2)}
                  />
                )}
              {typeof data.consistency_score === "number" &&
                data.consistency_score > 0 && (
                  <MatchupRow
                    label="Consistency"
                    value={fmtPct(data.consistency_score)}
                  />
                )}

              {/* Career vs opponent slice */}
              {data.career_vs_opponent &&
                data.career_vs_opponent.games > 0 && (
                  <View style={styles.sliceCard}>
                    <Text style={styles.sliceTitle}>
                      Career vs {opp}
                    </Text>
                    <Text style={styles.sliceLine}>
                      {data.career_vs_opponent.games} games · avg{" "}
                      {data.career_vs_opponent.avg.toFixed(2)}
                      {data.threshold != null &&
                        ` · ${fmtPct(data.career_vs_opponent.hit_rate)} over ${data.threshold}`}
                    </Text>
                  </View>
                )}

              {/* Recent form */}
              {data.overall_last_10 && data.overall_last_10.games > 0 && (
                <View style={styles.sliceCard}>
                  <Text style={styles.sliceTitle}>Last 10</Text>
                  <Text style={styles.sliceLine}>
                    {data.overall_last_10.games} games · avg{" "}
                    {data.overall_last_10.avg.toFixed(2)}
                    {data.threshold != null &&
                      ` · ${fmtPct(data.overall_last_10.hit_rate)} over ${data.threshold}`}
                  </Text>
                </View>
              )}

              {/* NFL stat_lines multi-threshold table */}
              {data.stat_lines && data.stat_lines[data.stat || ""] && (
                <View style={styles.sliceCard}>
                  <Text style={styles.sliceTitle}>
                    Threshold breakdown
                  </Text>
                  {Object.values(
                    data.stat_lines[data.stat || ""].thresholds,
                  ).map((t) => (
                    <Text
                      key={`thr-${t.threshold}`}
                      style={styles.sliceLine}
                    >
                      {t.threshold}+: {t.hits}/{t.games} ({fmtPct(t.hit_rate)})
                    </Text>
                  ))}
                </View>
              )}

              {/* NFL last meeting */}
              {data.last_meeting && (
                <View style={styles.sliceCard}>
                  <Text style={styles.sliceTitle}>Last meeting</Text>
                  <Text style={styles.sliceLine}>
                    {data.last_meeting.season}
                    {data.last_meeting.week
                      ? ` · Week ${data.last_meeting.week}`
                      : ""}
                    {data.last_meeting.passing_yards != null &&
                      ` · ${data.last_meeting.passing_yards} pass yds`}
                    {data.last_meeting.passing_tds != null &&
                      ` · ${data.last_meeting.passing_tds} TD`}
                    {data.last_meeting.rushing_yards != null &&
                      ` · ${data.last_meeting.rushing_yards} rush yds`}
                    {data.last_meeting.receiving_yards != null &&
                      ` · ${data.last_meeting.receiving_yards} rec yds`}
                    {data.last_meeting.receptions != null &&
                      ` · ${data.last_meeting.receptions} rec`}
                  </Text>
                </View>
              )}

              {data.data_sources_used?.length ? (
                <Text style={styles.sourceLine}>
                  sources: {data.data_sources_used.join(", ")}
                </Text>
              ) : null}
            </ScrollView>

            <Pressable style={styles.modalCloseBtn} onPress={handleClose}>
              <Text style={styles.modalCloseText}>Close</Text>
            </Pressable>
          </Pressable>
        </Pressable>
      </Modal>
    </>
  );
}

function MatchupRow({ label, value }: { label: string; value: string }) {
  return (
    <View style={styles.row}>
      <Text style={styles.rowLabel}>{label}</Text>
      <Text style={styles.rowValue}>{value}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  badge: {
    flexDirection: "row",
    alignItems: "center",
    alignSelf: "flex-start",
    paddingHorizontal: 8,
    paddingVertical: 3,
    borderRadius: 999,
    borderWidth: 1,
    marginTop: 6,
    backgroundColor: "rgba(255,255,255,0.04)",
    gap: 6,
  },
  gradeText: {
    fontWeight: "800",
    fontSize: 12,
    letterSpacing: 0.3,
  },
  vsText: {
    color: COLORS.textMuted,
    fontSize: 11,
    fontWeight: "600",
  },
  hitRateText: {
    fontSize: 11,
    fontWeight: "700",
    marginLeft: 2,
  },
  modalOverlay: {
    flex: 1,
    backgroundColor: "rgba(0,0,0,0.6)",
    justifyContent: "center",
    alignItems: "center",
    paddingHorizontal: 20,
  },
  modalCard: {
    backgroundColor: COLORS.bg,
    borderRadius: 14,
    padding: 18,
    width: "100%",
    maxWidth: 380,
    maxHeight: "80%",
    borderWidth: 1,
    borderColor: "rgba(255,255,255,0.08)",
  },
  modalHeader: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center",
  },
  modalTitle: {
    color: COLORS.text,
    fontSize: 16,
    fontWeight: "800",
  },
  modalGrade: {
    fontSize: 18,
    fontWeight: "900",
    borderWidth: 1.5,
    borderRadius: 999,
    paddingHorizontal: 10,
    paddingVertical: 2,
  },
  modalSub: {
    color: COLORS.textMuted,
    fontSize: 12,
    marginTop: 4,
  },
  modalBody: {
    marginTop: 12,
  },
  row: {
    flexDirection: "row",
    justifyContent: "space-between",
    marginBottom: 6,
  },
  rowLabel: {
    color: COLORS.textMuted,
    fontSize: 13,
    fontWeight: "500",
  },
  rowValue: {
    color: COLORS.text,
    fontSize: 13,
    fontWeight: "700",
  },
  sliceCard: {
    marginTop: 10,
    padding: 8,
    borderRadius: 8,
    borderWidth: 1,
    borderColor: "rgba(255,255,255,0.08)",
    backgroundColor: "rgba(255,255,255,0.03)",
  },
  sliceTitle: {
    color: COLORS.text,
    fontWeight: "700",
    fontSize: 12,
    marginBottom: 4,
  },
  sliceLine: {
    color: COLORS.textMuted,
    fontSize: 12,
    lineHeight: 18,
  },
  sourceLine: {
    color: COLORS.textMuted,
    fontSize: 10,
    marginTop: 12,
    fontStyle: "italic",
  },
  modalCloseBtn: {
    marginTop: 14,
    alignSelf: "center",
    paddingHorizontal: 20,
    paddingVertical: 8,
    borderRadius: 999,
    backgroundColor: COLORS.neonGreen,
  },
  modalCloseText: {
    color: "#000",
    fontWeight: "800",
    fontSize: 13,
  },
});

export default MatchupGradeBadge;
