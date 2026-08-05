/**
 * Phase 8b — Alt-Line Magic Chips
 *
 * Horizontally scrollable chip row that surfaces alt-line
 * opportunities (Over/Under alternate thresholds) for a pick.
 * Each chip shows the line + side + model win-probability, with a
 * green edge badge when the model beats the market implied prob.
 *
 * Tapping a chip opens a detail modal with:
 *   • Composite score (0-100)
 *   • Confidence / bucket ROI / stability sub-scores
 *   • Best-price sportsbook + American odds
 *   • Plain-English explanation from the ranker
 *
 * The component fetches on mount from `/api/alt-lines/{pick_id}`;
 * if the pick's market isn't supported (sport gate, retired player,
 * insufficient history) the component renders NOTHING — no empty
 * chrome pollutes the pick card.
 */
import React, { useEffect, useMemo, useState, useCallback } from "react";
import {
  View, Text, StyleSheet, Pressable, ScrollView,
  ActivityIndicator, Modal, Platform,
} from "react-native";
import { COLORS } from "@/src/theme";
import { api } from "@/src/lib/api";

type AltLine = NonNullable<
  Awaited<ReturnType<typeof api.altLines>>["bundle"]
>["alt_lines"][number];

type BundleT = NonNullable<
  Awaited<ReturnType<typeof api.altLines>>["bundle"]
>;

const MAX_CHIPS = 8;
const MIN_COMPOSITE_TO_SHOW = 0.35;

export function AltLineChips({ pickId }: { pickId: string }) {
  const [loading, setLoading] = useState(true);
  const [bundle, setBundle] = useState<BundleT | null>(null);
  const [notSupportedReason, setNotSupportedReason] = useState<string | null>(null);
  const [selected, setSelected] = useState<AltLine | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api
      .altLines(pickId)
      .then((res) => {
        if (cancelled) return;
        if (res.supported === false) {
          setNotSupportedReason(res.reason || "not supported");
          setBundle(null);
        } else {
          setBundle(res.bundle ?? null);
          setNotSupportedReason(null);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setBundle(null);
          setNotSupportedReason("network");
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [pickId]);

  // Filter + sort by composite score (best first), cap at MAX_CHIPS.
  const visible = useMemo<AltLine[]>(() => {
    if (!bundle?.alt_lines?.length) return [];
    return [...bundle.alt_lines]
      .filter((a) => a.composite_score >= MIN_COMPOSITE_TO_SHOW)
      .sort((a, b) => b.composite_score - a.composite_score)
      .slice(0, MAX_CHIPS);
  }, [bundle]);

  const closeModal = useCallback(() => setSelected(null), []);

  // Render nothing when there's nothing meaningful to show.  This
  // includes: still loading (parent already shows its own spinner),
  // unsupported sport, no eligible alt lines.
  if (loading) {
    return (
      <View style={styles.rowLoading} testID="alt-line-chips-loading">
        <ActivityIndicator size="small" color={COLORS.voltBlue} />
        <Text style={styles.loadingText}>Loading alt-lines…</Text>
      </View>
    );
  }
  if (notSupportedReason || !bundle || visible.length === 0) {
    return null;
  }

  return (
    <View style={styles.container} testID="alt-line-chips">
      <View style={styles.header}>
        <Text style={styles.headerTitle}>Alt-Line Magic</Text>
        <Text style={styles.headerSubtitle}>
          {bundle.player}
          {bundle.projected != null ? `  •  proj ${bundle.projected}` : ""}
        </Text>
      </View>
      <ScrollView
        horizontal
        showsHorizontalScrollIndicator={false}
        contentContainerStyle={styles.chipRow}
        testID="alt-line-chips-scroll"
      >
        {visible.map((a, i) => (
          <AltLineChip
            key={`${a.line}-${a.side}-${i}`}
            alt={a}
            onPress={() => setSelected(a)}
          />
        ))}
      </ScrollView>

      <Modal
        visible={selected != null}
        animationType="fade"
        transparent
        onRequestClose={closeModal}
      >
        <Pressable style={styles.modalScrim} onPress={closeModal}>
          <Pressable style={styles.modalCard} onPress={(e) => e.stopPropagation()}>
            {selected && (
              <AltLineDetail
                alt={selected}
                bundle={bundle}
                onClose={closeModal}
              />
            )}
          </Pressable>
        </Pressable>
      </Modal>
    </View>
  );
}

// ─────────────────────────────────────────────
// Chip
// ─────────────────────────────────────────────
function AltLineChip({ alt, onPress }: { alt: AltLine; onPress: () => void }) {
  const isModelOnly = alt.source === "model_projection";
  const edgePct = alt.edge != null ? Math.round(alt.edge * 100) : null;
  const probPct = Math.round(alt.p_model * 100);
  const positiveEdge = edgePct != null && edgePct >= 3;
  const edgeStyle = positiveEdge ? styles.edgePositive : styles.edgeNeutral;

  return (
    <Pressable
      style={[styles.chip, isModelOnly && styles.chipModelOnly]}
      onPress={onPress}
      testID={`alt-chip-${alt.side.toLowerCase()}-${alt.line}`}
    >
      <View style={styles.chipTop}>
        <Text style={styles.chipSide}>{alt.side === "Over" ? "O" : "U"}</Text>
        <Text style={styles.chipLine}>{formatLine(alt.line)}</Text>
      </View>
      <View style={styles.chipBottom}>
        <Text style={styles.chipProb}>{probPct}%</Text>
        {edgePct != null && (
          <View style={[styles.edgeBadge, edgeStyle]}>
            <Text style={styles.edgeText}>
              {edgePct > 0 ? `+${edgePct}` : `${edgePct}`}%
            </Text>
          </View>
        )}
      </View>
      {isModelOnly && (
        <View style={styles.projectionBadge}>
          <Text style={styles.projectionText}>MODEL</Text>
        </View>
      )}
    </Pressable>
  );
}

// ─────────────────────────────────────────────
// Detail modal
// ─────────────────────────────────────────────
function AltLineDetail({
  alt, bundle, onClose,
}: { alt: AltLine; bundle: BundleT; onClose: () => void }) {
  const composite = Math.round(alt.composite_score * 100);
  const confidence = Math.round(alt.confidence * 100);
  const stability = Math.round(alt.stability * 100);
  const bucketRoi =
    alt.bucket_roi != null ? Math.round(alt.bucket_roi * 100) : null;
  const probPct = Math.round(alt.p_model * 100);
  const impliedPct =
    alt.p_implied != null ? Math.round(alt.p_implied * 100) : null;
  const edgePct = alt.edge != null ? Math.round(alt.edge * 100) : null;

  return (
    <>
      <View style={styles.detailHeader}>
        <View style={{ flex: 1 }}>
          <Text style={styles.detailTitle}>
            {alt.side} {formatLine(alt.line)} {bundle.stat}
          </Text>
          <Text style={styles.detailSubtitle}>
            {bundle.player}
            {bundle.opponent ? `  vs  ${bundle.opponent}` : ""}
          </Text>
        </View>
        <Pressable
          onPress={onClose}
          style={styles.closeButton}
          hitSlop={12}
          testID="alt-detail-close"
        >
          <Text style={styles.closeText}>×</Text>
        </Pressable>
      </View>

      <View style={styles.compositeCard}>
        <Text style={styles.compositeLabel}>Composite Score</Text>
        <Text style={styles.compositeValue}>{composite}</Text>
        <Text style={styles.compositeUnit}>/ 100</Text>
      </View>

      <View style={styles.statGrid}>
        <StatCell label="Model %" value={`${probPct}%`} />
        <StatCell
          label="Market %"
          value={impliedPct != null ? `${impliedPct}%` : "—"}
        />
        <StatCell
          label="Edge"
          value={
            edgePct != null ? `${edgePct > 0 ? "+" : ""}${edgePct}%` : "—"
          }
          highlight={edgePct != null && edgePct >= 3}
        />
        <StatCell label="Confidence" value={`${confidence}%`} />
        <StatCell label="Stability" value={`${stability}%`} />
        <StatCell
          label="Bucket ROI"
          value={
            bucketRoi != null ? `${bucketRoi > 0 ? "+" : ""}${bucketRoi}%` : "—"
          }
          highlight={bucketRoi != null && bucketRoi > 5}
        />
      </View>

      {alt.market_odds && alt.market_odds.american != null && (
        <View style={styles.priceCard}>
          <Text style={styles.priceLabel}>Best Price</Text>
          <View style={styles.priceRow}>
            <Text style={styles.priceBook}>
              {formatBook(alt.market_odds.bookmaker)}
            </Text>
            <Text style={styles.priceOdds}>
              {formatAmerican(alt.market_odds.american)}
            </Text>
          </View>
        </View>
      )}

      {alt.source === "model_projection" && (
        <View style={styles.warningCard}>
          <Text style={styles.warningText}>
            ⚠︎ No book has posted this line yet. This is a MODEL projection —
            use it to hunt for a value line on your book.
          </Text>
        </View>
      )}

      <Text style={styles.explanation}>{alt.explanation}</Text>
    </>
  );
}

function StatCell({
  label, value, highlight = false,
}: { label: string; value: string; highlight?: boolean }) {
  return (
    <View style={styles.statCell}>
      <Text style={styles.statLabel}>{label}</Text>
      <Text
        style={[styles.statValue, highlight && styles.statValueHighlight]}
      >
        {value}
      </Text>
    </View>
  );
}

// ─────────────────────────────────────────────
// Formatters
// ─────────────────────────────────────────────
function formatLine(v: number): string {
  return Number.isInteger(v) ? String(v) : v.toFixed(1);
}
function formatAmerican(v: number): string {
  return v > 0 ? `+${v}` : `${v}`;
}
function formatBook(bk?: string): string {
  if (!bk) return "";
  const map: Record<string, string> = {
    draftkings: "DraftKings",
    fanduel: "FanDuel",
    betmgm: "BetMGM",
    caesars: "Caesars",
    pointsbetus: "PointsBet",
    barstool: "Barstool",
  };
  return map[bk] ?? bk;
}

// ─────────────────────────────────────────────
// Styles
// ─────────────────────────────────────────────
const styles = StyleSheet.create({
  container: {
    marginTop: 12,
    paddingTop: 12,
    borderTopWidth: StyleSheet.hairlineWidth,
    borderTopColor: COLORS.borderDefault,
  },
  header: {
    paddingHorizontal: 16,
    marginBottom: 8,
    flexDirection: "row",
    alignItems: "flex-end",
    justifyContent: "space-between",
    gap: 8,
  },
  headerTitle: {
    color: COLORS.goldElite,
    fontSize: 12,
    fontWeight: "700",
    letterSpacing: 0.6,
    textTransform: "uppercase",
  },
  headerSubtitle: {
    color: COLORS.textSecondary,
    fontSize: 11,
    flexShrink: 1,
    textAlign: "right",
  },
  chipRow: {
    paddingHorizontal: 16,
    gap: 8,
    paddingVertical: 4,
  },
  rowLoading: {
    marginTop: 12,
    paddingHorizontal: 16,
    paddingVertical: 10,
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    borderTopWidth: StyleSheet.hairlineWidth,
    borderTopColor: COLORS.borderDefault,
  },
  loadingText: { color: COLORS.textMuted, fontSize: 12 },

  chip: {
    minWidth: 76,
    minHeight: 68,
    borderRadius: 12,
    borderWidth: 1,
    borderColor: COLORS.borderDefault,
    backgroundColor: COLORS.surfaceElevated,
    paddingHorizontal: 12,
    paddingVertical: 8,
    justifyContent: "space-between",
    // ensure 44px touch target
    ...Platform.select({ ios: { shadowOpacity: 0 }, default: {} }),
  },
  chipModelOnly: {
    borderColor: "rgba(255,215,0,0.35)",
    backgroundColor: "rgba(255,215,0,0.05)",
  },
  chipTop: {
    flexDirection: "row",
    alignItems: "baseline",
    gap: 4,
  },
  chipSide: {
    color: COLORS.textSecondary,
    fontSize: 11,
    fontWeight: "700",
    letterSpacing: 0.5,
  },
  chipLine: {
    color: COLORS.textPrimary,
    fontSize: 16,
    fontWeight: "800",
    fontVariant: ["tabular-nums"],
  },
  chipBottom: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    marginTop: 4,
    gap: 6,
  },
  chipProb: {
    color: COLORS.textPrimary,
    fontSize: 13,
    fontWeight: "600",
  },
  edgeBadge: {
    paddingHorizontal: 5,
    paddingVertical: 1,
    borderRadius: 4,
  },
  edgePositive: { backgroundColor: "rgba(50,215,75,0.18)" },
  edgeNeutral: { backgroundColor: "rgba(255,255,255,0.06)" },
  edgeText: {
    color: COLORS.textPrimary,
    fontSize: 10,
    fontWeight: "700",
    fontVariant: ["tabular-nums"],
  },
  projectionBadge: {
    position: "absolute",
    top: -6,
    right: -6,
    backgroundColor: COLORS.goldElite,
    borderRadius: 4,
    paddingHorizontal: 4,
    paddingVertical: 1,
  },
  projectionText: {
    color: "#0A0A0A",
    fontSize: 8,
    fontWeight: "800",
    letterSpacing: 0.4,
  },

  // ── Modal
  modalScrim: {
    flex: 1,
    backgroundColor: "rgba(0,0,0,0.75)",
    justifyContent: "center",
    alignItems: "center",
    padding: 20,
  },
  modalCard: {
    width: "100%",
    maxWidth: 420,
    backgroundColor: COLORS.surface,
    borderRadius: 16,
    borderWidth: 1,
    borderColor: COLORS.borderDefault,
    padding: 20,
  },
  detailHeader: {
    flexDirection: "row",
    alignItems: "flex-start",
    marginBottom: 16,
  },
  detailTitle: {
    color: COLORS.textPrimary,
    fontSize: 18,
    fontWeight: "800",
    marginBottom: 2,
  },
  detailSubtitle: {
    color: COLORS.textSecondary,
    fontSize: 12,
  },
  closeButton: {
    width: 32,
    height: 32,
    borderRadius: 16,
    alignItems: "center",
    justifyContent: "center",
    backgroundColor: COLORS.surfaceElevated,
  },
  closeText: {
    color: COLORS.textPrimary,
    fontSize: 22,
    lineHeight: 22,
    fontWeight: "600",
  },
  compositeCard: {
    alignItems: "center",
    paddingVertical: 12,
    backgroundColor: COLORS.surfaceElevated,
    borderRadius: 12,
    marginBottom: 12,
  },
  compositeLabel: {
    color: COLORS.textMuted,
    fontSize: 10,
    letterSpacing: 0.6,
    textTransform: "uppercase",
    marginBottom: 4,
  },
  compositeValue: {
    color: COLORS.goldElite,
    fontSize: 42,
    fontWeight: "900",
    fontVariant: ["tabular-nums"],
    lineHeight: 46,
  },
  compositeUnit: {
    color: COLORS.textMuted,
    fontSize: 11,
    marginTop: 2,
  },
  statGrid: {
    flexDirection: "row",
    flexWrap: "wrap",
    marginBottom: 12,
  },
  statCell: {
    width: "33.333%",
    paddingVertical: 8,
    alignItems: "center",
  },
  statLabel: {
    color: COLORS.textMuted,
    fontSize: 10,
    letterSpacing: 0.4,
    textTransform: "uppercase",
    marginBottom: 4,
  },
  statValue: {
    color: COLORS.textPrimary,
    fontSize: 15,
    fontWeight: "700",
    fontVariant: ["tabular-nums"],
  },
  statValueHighlight: {
    color: COLORS.neonGreen,
  },
  priceCard: {
    backgroundColor: COLORS.surfaceElevated,
    borderRadius: 10,
    padding: 12,
    marginBottom: 12,
  },
  priceLabel: {
    color: COLORS.textMuted,
    fontSize: 10,
    letterSpacing: 0.4,
    textTransform: "uppercase",
    marginBottom: 4,
  },
  priceRow: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
  },
  priceBook: {
    color: COLORS.textPrimary,
    fontSize: 14,
    fontWeight: "600",
  },
  priceOdds: {
    color: COLORS.voltBlue,
    fontSize: 18,
    fontWeight: "800",
    fontVariant: ["tabular-nums"],
  },
  warningCard: {
    backgroundColor: "rgba(255,215,0,0.08)",
    borderColor: "rgba(255,215,0,0.30)",
    borderWidth: 1,
    borderRadius: 10,
    padding: 10,
    marginBottom: 12,
  },
  warningText: {
    color: COLORS.goldElite,
    fontSize: 11,
    lineHeight: 15,
  },
  explanation: {
    color: COLORS.textSecondary,
    fontSize: 12,
    lineHeight: 17,
  },
});

export default AltLineChips;
