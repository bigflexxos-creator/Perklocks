import React, { useCallback, useEffect, useRef, useState } from "react";
import {
  View, Text, StyleSheet, ScrollView, ActivityIndicator, Pressable,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { Ionicons } from "@expo/vector-icons";
import { useLocalSearchParams, useRouter } from "expo-router";
import { COLORS, GRADE_COLORS } from "@/src/theme";
import { api, Pick } from "@/src/lib/api";
import { useBetSlip, MAX_SLIP_SIZE } from "@/src/contexts/BetSlipContext";
import { formatGameTime } from "@/src/lib/formatGameTime";
import { SurvivabilityPanel } from "@/src/components/SurvivabilityPanel";
import { LockV2Panel } from "@/src/components/LockV2Panel";
import { MarketRankPanel } from "@/src/components/MarketRankPanel";
import { ScorerBundlesPanel } from "@/src/components/ScorerBundlesPanel";
import { SimulatorPanel } from "@/src/components/SimulatorPanel";
import { getDisplayLockRounded } from "@/src/lib/lockScore";
import { buildSlipText, shareSlip, saveSlipImage, copySlipText } from "@/src/lib/shareBetSlip";

export default function PickDetail() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const router = useRouter();
  const slip = useBetSlip();
  const [pick, setPick] = useState<Pick | null>(null);
  const [loading, setLoading] = useState(true);
  const [aiLoading, setAiLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const shareCardRef = useRef<View>(null);
  const [shareBusy, setShareBusy] = useState(false);

  const buildPickPayload = useCallback((p: Pick) => ({
    single: {
      sport: p.sport,
      league: (p as any).league,
      event: p.event,
      market: p.market,
      selection: p.market,
      book_odds: p.book_odds,
      bookmaker: (p as any).bookmaker || (p as any).book,
      confidence: getDisplayLockRounded(p),
    },
    generated_at: new Date().toISOString(),
  }), []);

  const onShare = useCallback(async () => {
    if (!pick || shareBusy) return;
    setShareBusy(true);
    try {
      await shareSlip(shareCardRef, buildSlipText(buildPickPayload(pick)));
    } finally { setShareBusy(false); }
  }, [pick, shareBusy, buildPickPayload]);

  const onCopy = useCallback(async () => {
    if (!pick) return;
    await copySlipText(buildSlipText(buildPickPayload(pick)));
  }, [pick, buildPickPayload]);

  const onSaveImg = useCallback(async () => {
    if (!pick || shareBusy) return;
    setShareBusy(true);
    try { await saveSlipImage(shareCardRef); } finally { setShareBusy(false); }
  }, [pick, shareBusy]);

  useEffect(() => {
    if (!id) return;
    let cancelled = false;
    (async () => {
      try {
        const p = await api.pickDetail(id);
        if (cancelled) return;
        setPick(p);
        setLoading(false);
        // If the pick hasn't been AI-enriched yet, kick off the upgrade.
        if ((p as any).ai_pending) {
          setAiLoading(true);
          try {
            const ai = await api.pickAiExplain(id);
            if (!cancelled && ai?.explanation) {
              setPick((prev) => prev ? { ...prev, explanation: ai.explanation } : prev);
            }
          } catch {
            // Keep the fallback template that was returned with the pick.
          } finally {
            if (!cancelled) setAiLoading(false);
          }
        }
      } catch (e: any) {
        if (!cancelled) {
          setError(e?.message || "Failed to load pick");
          setLoading(false);
        }
      }
    })();
    return () => { cancelled = true; };
  }, [id]);

  // Detail screen always renders as "PICK BREAKDOWN" — Bet Killer was
  // deprecated and replaced by the Under-of-the-Day tab; every pick that
  // reaches the UI is a recommended pick post NO_BET filtering.
  const gradeColor = pick ? GRADE_COLORS[pick.grade] : COLORS.textMuted;

  return (
    <SafeAreaView
      style={styles.safe}
      edges={["top", "bottom"]}
    >
      <View style={styles.headerBar}>
        <Pressable
          testID="back-button"
          onPress={() => router.back()}
          hitSlop={12}
          style={styles.backBtn}
        >
          <Ionicons name="chevron-back" size={22} color={COLORS.textPrimary} />
        </Pressable>
        <Text style={styles.headerTitle}>
          PICK BREAKDOWN
        </Text>
        <Pressable
          testID="add-to-slip-button"
          onPress={() => {
            if (!pick) return;
            if (slip.has(pick.id)) {
              slip.removePick(pick.id);
            } else {
              const res = slip.addPick(pick);
              if (!res.ok) {
                console.warn(res.reason);
              }
            }
          }}
          hitSlop={12}
          style={[styles.slipBtn, pick && slip.has(pick.id) && styles.slipBtnActive]}
        >
          <Ionicons
            name={pick && slip.has(pick.id) ? "checkmark" : "add"}
            size={20}
            color={pick && slip.has(pick.id) ? COLORS.bg : COLORS.textPrimary}
          />
          {slip.count > 0 && (
            <View style={styles.slipBadge}>
              <Text style={styles.slipBadgeText}>{slip.count}</Text>
            </View>
          )}
        </Pressable>
      </View>

      <ScrollView contentContainerStyle={styles.content} showsVerticalScrollIndicator={false}>
        {loading ? (
          <View style={styles.center}>
            <ActivityIndicator color={COLORS.voltBlue} />
          </View>
        ) : error ? (
          <Text style={styles.error}>{error}</Text>
        ) : pick ? (
          <>
            <View ref={shareCardRef} collapsable={false} style={styles.shareCardWrap}>
              <View style={styles.tagRow}>
                <View style={[styles.tag, { backgroundColor: `${gradeColor}20`, borderColor: gradeColor }]}>
                  <Text style={[styles.tagText, { color: gradeColor }]}>{pick.grade.toUpperCase()}</Text>
                </View>
                <Text style={styles.metaText}>
                  {pick.sport} · {pick.league}
                </Text>
              </View>

              <Text style={styles.event}>{pick.event}</Text>
              {pick.event_time && (
                <Text style={styles.eventTime}>{formatGameTime(pick.event_time)}</Text>
              )}
              <Text style={styles.market}>{pick.market}</Text>

              <View style={[styles.scoreWrap, { borderColor: gradeColor }]}>
                <Text style={[styles.scoreBig, { color: gradeColor }]} testID="pick-detail-lock-score">
                  {getDisplayLockRounded(pick)}
                </Text>
                <Text style={styles.scoreSub}>🔒 LOCK SCORE · BET QUALITY</Text>
                <Text style={styles.scoreNote}>
                  A 0-99 quality score blending edge, alignment, ROI, data quality, volatility &amp; CLV.
                </Text>

                {/* Lock v3 — Expected Win and Edge displayed alongside */}
                <View style={styles.scoreSplitRow}>
                  <View style={styles.scoreSplitCell}>
                    <Text style={styles.scoreSplitIcon}>📊</Text>
                    <Text style={[styles.scoreSplitValue, { color: COLORS.textPrimary }]}>
                      {pick.win_probability}%
                    </Text>
                    <Text style={styles.scoreSplitLabel}>EXPECTED WIN</Text>
                  </View>
                  <View style={styles.scoreSplitDivider} />
                  <View style={styles.scoreSplitCell}>
                    <Text style={styles.scoreSplitIcon}>⚡</Text>
                    <Text
                      style={[
                        styles.scoreSplitValue,
                        { color: pick.edge_percent > 0 ? COLORS.neonGreen : COLORS.electricBlaze },
                      ]}
                    >
                      {pick.edge_percent > 0 ? "+" : ""}
                      {pick.edge_percent}%
                    </Text>
                    <Text style={styles.scoreSplitLabel}>EDGE VS BOOK</Text>
                  </View>
                </View>

                <Text style={styles.confidence}>Confidence: {pick.confidence}</Text>
              </View>

              <View style={styles.bento}>
                <BentoCell label="BOOK IMPLIED" value={`${pick.implied_probability}%`} muted />
                <BentoCell
                  label="BOOK ODDS"
                  value={pick.book_odds > 0 ? `+${pick.book_odds}` : `${pick.book_odds}`}
                />
              </View>
            </View>

            {/* ── Share-to-Gambly action row ────────────────────────── */}
            <View style={styles.shareRow}>
              <Pressable
                testID="pick-copy"
                onPress={onCopy}
                style={({ pressed }) => [
                  styles.shareBtn, { borderColor: COLORS.textMuted }, pressed && { opacity: 0.6 },
                ]}
              >
                <Ionicons name="copy-outline" size={14} color={COLORS.textPrimary} />
                <Text style={styles.shareTxt}>COPY</Text>
              </Pressable>
              <Pressable
                testID="pick-save-image"
                onPress={onSaveImg}
                disabled={shareBusy}
                style={({ pressed }) => [
                  styles.shareBtn, { borderColor: COLORS.voltBlue }, (pressed || shareBusy) && { opacity: 0.6 },
                ]}
              >
                <Ionicons name="download-outline" size={14} color={COLORS.voltBlue} />
                <Text style={[styles.shareTxt, { color: COLORS.voltBlue }]}>SAVE IMG</Text>
              </Pressable>
              <Pressable
                testID="pick-share"
                onPress={onShare}
                disabled={shareBusy}
                style={({ pressed }) => [
                  styles.shareBtnPrimary, (pressed || shareBusy) && { opacity: 0.65 },
                ]}
              >
                <Ionicons name="share-social-outline" size={14} color={COLORS.bg} />
                <Text style={[styles.shareTxt, { color: COLORS.bg }]}>SHARE TO GAMBLY</Text>
              </Pressable>
            </View>

            <Text style={styles.sectionLabel}>
              WHY THIS PICK
            </Text>
            <View style={styles.explainCard}>
              {pick.explanation ? (
                <Text style={styles.explainText}>{pick.explanation}</Text>
              ) : (
                <View style={styles.aiLoading}>
                  <ActivityIndicator size="small" color={COLORS.voltBlue} />
                  <View style={{ flex: 1 }}>
                    <Text style={styles.aiLoadingTitle}>Claude Sonnet 4.5 is analyzing this pick…</Text>
                    <Text style={styles.aiLoadingSub}>AI breakdown usually takes 5–15 seconds. Keep this screen open.</Text>
                  </View>
                </View>
              )}
            </View>

            <Text style={styles.sectionLabel}>FACTOR BREAKDOWN</Text>
            <View style={styles.factorsCard}>
              {/* Defensive: alt-line picks (e.g. tennis Tommy Paul +0.5 Games)
                  occasionally arrive without a populated `factors` map —
                  Object.entries(undefined) was crashing the whole pick
                  detail screen with "Cannot convert undefined value to
                  object". Coalesce to {} so the section just renders
                  empty rather than tanking the route. */}
              {Object.entries(pick.factors || {}).map(([k, v]) => (
                <View key={k} style={styles.factorRow}>
                  <Text style={styles.factorName}>{k}</Text>
                  <View style={styles.factorBarTrack}>
                    <View style={[
                      styles.factorBarFill,
                      { width: `${v}%`, backgroundColor: gradeColor },
                    ]} />
                  </View>
                  <Text style={[styles.factorValue, { color: gradeColor }]}>{Math.round(Number(v) || 0)}</Text>
                </View>
              ))}
              {(!pick.factors || Object.keys(pick.factors).length === 0) && (
                <Text style={[styles.factorName, { textAlign: "center", paddingVertical: 8 }]}>
                  Factor breakdown unavailable for this pick.
                </Text>
              )}
            </View>

            {/* Survivability Engine — only renders for MLB hit props */}
            <SurvivabilityPanel pick={pick} />

            {/* Monte Carlo Simulator — Phase A, MLB only. Shows 10k-run win
                probability with 95% CI and disagreement signal vs the
                blended model, plus any lock_score lift applied. */}
            <SimulatorPanel pick={pick} />

            {/* Lock Engine V2 — Deep Thinking (Counter + Survival + Sim) — shadow mode */}
            <LockV2Panel pick={pick} />

            {/* Market Competition Engine — rank parallel markets in this event */}
            <MarketRankPanel pick={pick} />

            {/* Scorer Bundles — synthesized 2+ Goals / Hat-Trick / Goal+Assist
                Only renders for Soccer Anytime Goal Scorer picks. */}
            <ScorerBundlesPanel pick={pick} />

            {/* Batter-vs-Pitcher card — MLB hit/total bases/HR props only.
                Populated by /app/backend/mlb_bvp.py at pick-generation time
                (career splits from the free MLB Stats API). Renders nothing
                when there's no historical BvP sample for this matchup. */}
            {pick.bvp_history && (pick.bvp_history as any).ab > 0 && (
              <View style={[styles.insightsCard, { marginTop: 10, borderColor: COLORS.voltBlue + "55" }]}>
                <Text style={[styles.sectionLabel, { marginBottom: 6 }]}>
                  BATTER vs PITCHER
                </Text>
                <Text style={{ color: COLORS.textSecondary, fontSize: 12, lineHeight: 18 }}>
                  <Text style={{ color: COLORS.textPrimary, fontWeight: "800" }}>
                    {(pick.bvp_history as any).batter_name}
                  </Text>
                  {"  vs  "}
                  <Text style={{ color: COLORS.textPrimary, fontWeight: "800" }}>
                    {(pick.bvp_history as any).pitcher_name}
                  </Text>
                </Text>
                <View style={{ flexDirection: "row", marginTop: 8, gap: 14, flexWrap: "wrap" }}>
                  <Text style={{ color: COLORS.voltBlue, fontSize: 13, fontWeight: "800" }}>
                    {(pick.bvp_history as any).h}-for-{(pick.bvp_history as any).ab}{" "}
                    <Text style={{ color: COLORS.textSecondary, fontWeight: "500" }}>
                      ({((pick.bvp_history as any).avg || 0).toFixed(3)})
                    </Text>
                  </Text>
                  {(pick.bvp_history as any).hr > 0 && (
                    <Text style={{ color: COLORS.neonGreen, fontSize: 13, fontWeight: "700" }}>
                      {(pick.bvp_history as any).hr} HR
                    </Text>
                  )}
                  {(pick.bvp_history as any).so > 0 && (
                    <Text style={{ color: COLORS.textMuted, fontSize: 12 }}>
                      {(pick.bvp_history as any).so} SO
                    </Text>
                  )}
                  {(pick.bvp_history as any).bb > 0 && (
                    <Text style={{ color: COLORS.textMuted, fontSize: 12 }}>
                      {(pick.bvp_history as any).bb} BB
                    </Text>
                  )}
                </View>
                {pick.bvp_lock_adjustment != null && pick.bvp_lock_adjustment !== 0 && (
                  <Text style={{
                    color: pick.bvp_lock_adjustment > 0 ? COLORS.neonGreen : COLORS.electricBlaze,
                    fontSize: 11, fontWeight: "700", marginTop: 6, letterSpacing: 0.5,
                  }}>
                    {pick.bvp_lock_adjustment > 0 ? "+" : ""}
                    {pick.bvp_lock_adjustment} Lock Score adjustment from BvP
                  </Text>
                )}
              </View>
            )}

            <Text style={styles.sectionLabel}>WHY THIS PICK</Text>
            <View style={styles.insightsCard}>
              {(pick.top_reasons && pick.top_reasons.length > 0
                ? pick.top_reasons
                : (pick.key_insights || []).slice(0, 3)
              ).map((reason, idx) => (
                <View key={`reason-${idx}`} style={styles.reasonRow}>
                  <View style={[styles.reasonBadge, { backgroundColor: gradeColor + "22", borderColor: gradeColor }]}>
                    <Text style={[styles.reasonBadgeTxt, { color: gradeColor }]}>{idx + 1}</Text>
                  </View>
                  <Text style={styles.reasonText}>{reason}</Text>
                </View>
              ))}
            </View>

            {(pick.confidence_score != null || pick.edge_score != null || pick.risk_score != null) && (
              <View style={styles.scoresRow}>
                {pick.confidence_score != null && (
                  <ScoreChip label="Confidence" value={pick.confidence_score} color={COLORS.neonGreen} />
                )}
                {pick.edge_score != null && (
                  <ScoreChip label="Edge" value={pick.edge_score} color={COLORS.voltBlue} />
                )}
                {pick.risk_score != null && (
                  <ScoreChip label="Risk" value={pick.risk_score} color={COLORS.electricBlaze} inverse />
                )}
              </View>
            )}

            <Text style={styles.sectionLabel}>KEY INSIGHTS</Text>
            <View style={styles.insightsCard}>
              {pick.key_insights.map((i, idx) => (
                <View key={idx} style={styles.bullet}>
                  <View style={[styles.bulletDot, { backgroundColor: gradeColor }]} />
                  <Text style={styles.bulletText}>{i}</Text>
                </View>
              ))}
            </View>

            <View style={styles.sportsbookSection}>
              <Text style={styles.sectionLabel}>SHARE THIS PICK</Text>
              <Text style={styles.sportsbookHelper}>
                Use the share buttons at the top of this card to send the slip to Gambly or any installed app.
              </Text>
            </View>

            <Text style={styles.disclaimer}>
              Probabilistic forecast — no bet is guaranteed. Always bet responsibly.
            </Text>
          </>
        ) : null}
      </ScrollView>
    </SafeAreaView>
  );
}

function BentoCell({ label, value, color = COLORS.textPrimary, muted }: {
  label: string; value: string; color?: string; muted?: boolean;
}) {
  return (
    <View style={[styles.bentoCell, muted && { backgroundColor: "transparent" }]}>
      <Text style={styles.bentoLabel}>{label}</Text>
      <Text style={[styles.bentoValue, { color }]}>{value}</Text>
    </View>
  );
}

function ScoreChip({ label, value, color, inverse }: {
  label: string; value: number; color: string; inverse?: boolean;
}) {
  // For Risk, the user-facing "good" direction is LOW, so we tint by inverted value.
  const shown = inverse ? 100 - value : value;
  const tint = shown >= 70 ? color
             : shown >= 40 ? COLORS.voltBlue
             : COLORS.textMuted;
  return (
    <View style={[styles.scoreChip, { borderColor: tint + "55" }]}>
      <Text style={styles.scoreChipLabel}>{label.toUpperCase()}</Text>
      <Text style={[styles.scoreChipValue, { color: tint }]}>{Math.round(value)}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: COLORS.bg },
  headerBar: {
    flexDirection: "row", alignItems: "center", justifyContent: "space-between",
    paddingHorizontal: 16, paddingBottom: 10,
  },
  backBtn: {
    width: 36, height: 36, borderRadius: 18,
    backgroundColor: COLORS.surface, alignItems: "center", justifyContent: "center",
    borderWidth: 1, borderColor: COLORS.borderDefault,
  },
  headerTitle: { color: COLORS.textPrimary, fontWeight: "900", letterSpacing: 2, fontSize: 13 },
  content: { paddingHorizontal: 20, paddingBottom: 30 },
  center: { paddingVertical: 80, alignItems: "center" },
  error: { color: COLORS.electricBlaze, textAlign: "center", marginTop: 40 },

  tagRow: { flexDirection: "row", alignItems: "center", gap: 10, marginTop: 8, marginBottom: 10 },
  tag: { paddingHorizontal: 10, paddingVertical: 5, borderRadius: 6, borderWidth: 1 },
  tagText: { fontSize: 10, fontWeight: "900", letterSpacing: 1.4 },
  metaText: { color: COLORS.textMuted, fontSize: 11, fontWeight: "700", letterSpacing: 0.8 },

  event: { color: COLORS.textSecondary, fontSize: 14, fontWeight: "600" },
  eventTime: { color: COLORS.voltBlue, fontSize: 12, fontWeight: "800", letterSpacing: 0.3, marginTop: 4 },
  gameTime: { color: COLORS.voltBlue, fontSize: 12, fontWeight: "700", letterSpacing: 0.3, marginTop: 4 },
  market: { color: COLORS.textPrimary, fontSize: 24, fontWeight: "900", letterSpacing: -0.5, marginTop: 6 },

  scoreWrap: {
    alignItems: "center", padding: 24, marginTop: 18, marginBottom: 20,
    borderRadius: 20, borderWidth: 1, backgroundColor: COLORS.surface,
  },
  scoreBig: { fontSize: 78, fontWeight: "900", letterSpacing: -3, lineHeight: 84 },
  scoreSub: { color: COLORS.textMuted, fontSize: 10, fontWeight: "800", letterSpacing: 2 },
  scoreNote: {
    color: COLORS.textMuted,
    fontSize: 10,
    fontWeight: "500",
    textAlign: "center",
    marginTop: 8,
    paddingHorizontal: 8,
    lineHeight: 14,
  },
  scoreSplitRow: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    marginTop: 18,
    paddingTop: 16,
    borderTopWidth: 1,
    borderTopColor: COLORS.borderDefault,
    width: "100%",
  },
  scoreSplitCell: { flex: 1, alignItems: "center" },
  scoreSplitDivider: { width: 1, height: 44, backgroundColor: COLORS.borderDefault },
  scoreSplitIcon: { fontSize: 16, marginBottom: 4 },
  scoreSplitValue: { fontSize: 26, fontWeight: "900", letterSpacing: -0.6 },
  scoreSplitLabel: {
    color: COLORS.textMuted,
    fontSize: 9,
    fontWeight: "800",
    letterSpacing: 1.3,
    marginTop: 4,
  },
  confidence: { color: COLORS.textSecondary, fontSize: 12, fontWeight: "700", marginTop: 14, letterSpacing: 0.5 },

  bento: { flexDirection: "row", flexWrap: "wrap", marginHorizontal: -6, marginBottom: 8 },
  bentoCell: {
    width: "50%", padding: 6,
  },
  bentoLabel: { color: COLORS.textMuted, fontSize: 9, fontWeight: "800", letterSpacing: 1.3 },
  bentoValue: { fontSize: 24, fontWeight: "900", marginTop: 6, letterSpacing: -0.5 },

  sectionLabel: { color: COLORS.textMuted, fontSize: 10, fontWeight: "800", letterSpacing: 1.6, marginTop: 22, marginBottom: 10 },
  explainCard: {
    backgroundColor: COLORS.surface, padding: 18, borderRadius: 14,
    borderWidth: 1, borderColor: COLORS.borderDefault,
  },
  explainText: { color: COLORS.textPrimary, fontSize: 13, lineHeight: 21 },
  aiUpgradeRow: { flexDirection: "row", alignItems: "center", gap: 10, marginTop: 14, paddingTop: 12, borderTopWidth: 1, borderTopColor: COLORS.borderDefault },
  aiUpgradeText: { color: COLORS.textMuted, fontSize: 11, fontWeight: "600" },
  aiLoading: { flexDirection: "row", alignItems: "center", gap: 12, paddingVertical: 4 },
  aiLoadingTitle: { color: COLORS.textPrimary, fontSize: 13, fontWeight: "700", marginBottom: 4 },
  aiLoadingSub: { color: COLORS.textMuted, fontSize: 11, lineHeight: 16 },

  factorsCard: {
    backgroundColor: COLORS.surface, padding: 16, borderRadius: 14,
    borderWidth: 1, borderColor: COLORS.borderDefault,
  },
  factorRow: { flexDirection: "row", alignItems: "center", marginVertical: 6, gap: 10 },
  factorName: { flex: 1.5, color: COLORS.textSecondary, fontSize: 12, fontWeight: "600" },
  factorBarTrack: { flex: 2, height: 6, backgroundColor: "rgba(255,255,255,0.06)", borderRadius: 3, overflow: "hidden" },
  factorBarFill: { height: "100%" },
  factorValue: { width: 32, textAlign: "right", fontWeight: "900", fontSize: 12, letterSpacing: -0.3 },

  insightsCard: {
    backgroundColor: COLORS.surface, padding: 16, borderRadius: 14,
    borderWidth: 1, borderColor: COLORS.borderDefault,
  },
  bullet: { flexDirection: "row", alignItems: "flex-start", marginVertical: 6 },
  bulletDot: { width: 6, height: 6, borderRadius: 3, marginTop: 7, marginRight: 10 },
  bulletText: { flex: 1, color: COLORS.textPrimary, fontSize: 13, lineHeight: 20 },

  reasonRow: { flexDirection: "row", alignItems: "flex-start", marginVertical: 8 },
  reasonBadge: {
    width: 24, height: 24, borderRadius: 12, marginRight: 12,
    alignItems: "center", justifyContent: "center", borderWidth: 1.5,
  },
  reasonBadgeTxt: { fontSize: 12, fontWeight: "900" },
  reasonText: { flex: 1, color: COLORS.textPrimary, fontSize: 14, lineHeight: 20, fontWeight: "500" },

  scoresRow: {
    flexDirection: "row", gap: 8, marginTop: 12, marginBottom: 8,
  },
  scoreChip: {
    flex: 1, paddingVertical: 10, paddingHorizontal: 6,
    backgroundColor: COLORS.surface, borderRadius: 10, borderWidth: 1,
    borderColor: COLORS.borderDefault, alignItems: "center",
  },
  scoreChipLabel: { color: COLORS.textMuted, fontSize: 9, fontWeight: "800", letterSpacing: 1.1 },
  scoreChipValue: { fontSize: 20, fontWeight: "900", marginTop: 4, letterSpacing: -0.3 },

  sportsbookSection: {
    marginTop: 22, padding: 16, borderRadius: 14,
    backgroundColor: COLORS.surface,
    borderWidth: 1, borderColor: COLORS.borderDefault,
  },
  slipBtn: {
    width: 32, height: 32, borderRadius: 16, alignItems: "center", justifyContent: "center",
    borderWidth: 1.5, borderColor: COLORS.textPrimary, backgroundColor: "transparent",
  },
  slipBtnActive: { backgroundColor: COLORS.neonGreen, borderColor: COLORS.neonGreen },
  slipBadge: {
    position: "absolute", top: -4, right: -4, minWidth: 16, height: 16, borderRadius: 8,
    backgroundColor: COLORS.electricBlaze, alignItems: "center", justifyContent: "center",
    paddingHorizontal: 4,
  },
  slipBadgeText: { color: COLORS.textPrimary, fontSize: 9, fontWeight: "900" },
  sportsbookHelper: {
    color: COLORS.textSecondary, fontSize: 11, lineHeight: 16, marginBottom: 12,
  },
  sportsbookGrid: { flexDirection: "row", gap: 8, flexWrap: "wrap" },
  sportsbookBtn: {
    flex: 1, minWidth: 90, flexDirection: "row", alignItems: "center", justifyContent: "center",
    gap: 6, paddingVertical: 12, paddingHorizontal: 10,
    backgroundColor: COLORS.bg, borderRadius: 10,
    borderWidth: 1, borderColor: COLORS.voltBlue,
  },
  sportsbookText: { color: COLORS.textPrimary, fontWeight: "900", fontSize: 12, letterSpacing: 0.5 },

  disclaimer: {
    color: COLORS.textMuted, fontSize: 11, lineHeight: 16,
    marginTop: 22, textAlign: "center",
  },
  // ── Share-to-Gambly ─────────────────────────────────────────────
  shareCardWrap: { paddingTop: 4 },
  shareRow: {
    flexDirection: "row", alignItems: "center", gap: 8,
    marginTop: 14, marginBottom: 4, flexWrap: "wrap",
  },
  shareBtn: {
    flexDirection: "row", alignItems: "center", gap: 6,
    paddingHorizontal: 12, height: 36, borderRadius: 18, borderWidth: 1.5,
  },
  shareBtnPrimary: {
    flex: 1, minWidth: 150,
    flexDirection: "row", alignItems: "center", justifyContent: "center", gap: 6,
    paddingHorizontal: 14, height: 36, borderRadius: 18,
    backgroundColor: COLORS.goldElite,
  },
  shareTxt: { color: COLORS.textPrimary, fontSize: 11, fontWeight: "900", letterSpacing: 1.2 },
});
