import React, { useState, useCallback } from "react";
import { View, Text, StyleSheet, Pressable, Platform, LayoutAnimation, UIManager, Alert, ActivityIndicator } from "react-native";
import { useRouter } from "expo-router";
import { COLORS, GRADE_COLORS } from "@/src/theme";
import { Pick, PickRationale, api } from "@/src/lib/api";
import { formatGameTime } from "@/src/lib/formatGameTime";
import { useMLBLive } from "@/src/contexts/MLBLiveContext";
import { getDisplayLock } from "@/src/lib/lockScore";
import { PickEventRow } from "@/src/components/PickEventRow";

// Local alias so TrackBetButton props type-check without pulling
// the full Pick type through the closure.
type LockPick = Pick;

function LockPickCardImpl({ pick }: { pick: Pick }) {
  const router = useRouter();
  const [whyOpen, setWhyOpen] = useState(false);
  // The lite payload trims `pick_rationale` to summary + lean + top
  // evidence bullet. The deep blocks (matchup, splits, pitcher_quality,
  // multipliers) live on the detail endpoint and are lazy-fetched on
  // first expand below. Cached in component state so a re-expand
  // doesn't re-fetch.
  const [deepRationale, setDeepRationale] = useState<PickRationale | null>(null);
  const [deepLoading, setDeepLoading] = useState(false);
  const slimRationale: PickRationale | undefined = (pick as any).pick_rationale;
  const isSlim = !!(slimRationale && (slimRationale as any)._slim);
  const rationale: PickRationale | undefined = deepRationale || slimRationale;
  const toggleWhy = useCallback(() => {
    if (Platform.OS === "android" && UIManager.setLayoutAnimationEnabledExperimental) {
      UIManager.setLayoutAnimationEnabledExperimental(true);
    }
    // Subtle expand/collapse — skip on web where layout anim can flicker.
    if (Platform.OS !== "web") {
      LayoutAnimation.configureNext(LayoutAnimation.Presets.easeInEaseOut);
    }
    setWhyOpen((v) => {
      const next = !v;
      // First-time expand on a slim pick → fetch the full doc to get
      // the deep rationale. Subsequent expands hit cached state.
      if (next && isSlim && !deepRationale && !deepLoading) {
        setDeepLoading(true);
        api.pickDetail(pick.id)
          .then((full) => {
            const r = (full as any).pick_rationale;
            if (r && typeof r === "object") {
              setDeepRationale(r);
            }
          })
          .catch(() => {
            // Network failure is non-fatal — the slim payload still
            // renders the summary + lean + top evidence bullet.
          })
          .finally(() => setDeepLoading(false));
      }
      return next;
    });
  }, [isSlim, deepRationale, deepLoading, pick.id]);
  const hasRationale =
    !!rationale &&
    ((rationale.evidence?.length ?? 0) > 0 ||
      (rationale.concerns?.length ?? 0) > 0 ||
      !!rationale.summary);
  const gradeColor = GRADE_COLORS[pick.grade] || COLORS.textMuted;
  // Edge color — green when EITHER our model edge is positive OR the
  // Monte Carlo simulator endorses the pick at ≥75% (chalk picks like
  // Bieber Over 2.5 K's at -650 carry negative model edge by construction
  // but the underlying win probability is genuine — backtest shows sim ≥
  // 75% is profitable at +6% ROI, sim ≥ 85% at +14% ROI. Letting the sim
  // veto the negative-edge red is honest signalling, not cosmetic).
  // Audit fields preserved so the pick detail page can still explain WHY
  // the edge looks positive when the model number is negative.
  const simWP =
    typeof pick.sim_win_probability === "number" ? pick.sim_win_probability : 0;
  const simEndorsed = simWP >= 75;
  const edgeColor =
    pick.edge_percent > 0 || simEndorsed
      ? COLORS.neonGreen
      : COLORS.electricBlaze;
  // Live MLB score lookup — null for non-MLB picks (zero cost when missing).
  // Pass event_time so a multi-game series doesn't show yesterday's FINAL
  // on tomorrow's matching matchup card.
  const live = useMLBLive(
    pick.sport === "MLB" ? pick.event : null,
    pick.sport === "MLB" ? (pick.event_time as string | null) : null,
  );
  const showLiveBadge = !!live && (live.is_live || live.is_final);

  // Lock V2 (Deep Thinking) shadow scores.
  //
  // CRITICAL: V2 is in SHADOW MODE — its data must not contradict V1 on
  // the home card. Showing a gold APEX border + "RARE LOCK" chip on a
  // pick whose V1 grade is "Pass" is the inconsistency the user flagged.
  // So we only allow V2 UI elements to surface when they AGREE with V1:
  // V2 chip + APEX border only render when the V1 lock_score is already
  // in the Strong Lock band (95+). Otherwise treat V2 as silent data
  // available solely in the pick detail "Deep Thinking" panel.
  // ── Single source of truth: see /src/lib/lockScore.ts ──
  // Backend now also canonicalizes lock_score = max(v1, v2) at READ time
  // (server.py `_canonicalize_lock_score`), so `pick.lock_score` from the
  // wire is already the right number. We keep `getDisplayLock` as defense
  // in depth in case any pick slips through with v2 > v1.
  const displayLock = getDisplayLock(pick);
  // Anything that referenced pick.lock_score for visual logic now uses
  // displayLock so the badge / progress bar / strong-lock gates all
  // match the headline number.
  const v1IsStrong = displayLock >= 95;
  const v2Tier = v1IsStrong ? pick.tier_v2 : undefined;
  const isApex = v1IsStrong && !!pick.is_apex;
  const v2Lock = pick.lock_score_v2 ?? null;
  const tierColor =
    v2Tier === "Apex Lock"    ? "#FFD700"
  : v2Tier === "Rare Lock"    ? COLORS.neonGreen
  : v2Tier === "Strong Lock"  ? COLORS.voltBlue
  : v2Tier === "Elite Setup"  ? COLORS.textSecondary
  : COLORS.textMuted;
  // "Almost Apex" hint suppressed for non-Strong-Lock picks (same reason).
  const nearMiss = v1IsStrong
    && !isApex
    && v2Lock != null
    && v2Lock >= 97
    && Array.isArray(pick.apex_blockers)
    && pick.apex_blockers.length > 0;
  const firstBlocker = nearMiss ? pick.apex_blockers![0] : null;

  return (
    <Pressable
      testID={`pick-card-${pick.id}`}
      onPress={() => router.push(`/pick/${pick.id}`)}
      style={({ pressed }) => [
        styles.card,
        isApex && styles.cardApex,
        pressed && { opacity: 0.85, transform: [{ scale: 0.98 }] },
      ]}
    >
      <View style={styles.header}>
        <View style={{ flex: 1 }}>
          <View style={styles.tagRow}>
            <View style={styles.tag}>
              <Text style={styles.tagText}>
                {pick.sport.toUpperCase()}
              </Text>
            </View>
            {pick.elite_player && (
              <View style={styles.eliteTag}>
                <Text style={styles.eliteTagText}>⭐ ELITE</Text>
              </View>
            )}
            {(pick as any).is_extra && (
              <View style={styles.extraTag}>
                <Text style={styles.extraTagText}>EXT</Text>
              </View>
            )}
            {(pick as any).is_model_only && (
              <View style={styles.modelTag}>
                <Text style={styles.modelTagText}>MODEL</Text>
              </View>
            )}
            {(pick as any).chalk_trap && (
              <View style={styles.chalkTrapTag}>
                <Text style={styles.chalkTrapTagText}>⚠️ TRAP</Text>
              </View>
            )}
            {(pick as any).chalk_verified && (
              <View style={styles.chalkVerifiedTag}>
                <Text style={styles.chalkVerifiedTagText}>✓ CHALK+</Text>
              </View>
            )}
            <Text style={styles.league} numberOfLines={1}>{pick.league}</Text>
            <View style={[styles.gradePill, { borderColor: gradeColor, backgroundColor: gradeColor + "18" }]}>
              <Text style={[styles.gradePillText, { color: gradeColor }]} numberOfLines={1}>
                {pick.grade.toUpperCase()}
              </Text>
            </View>
            {/* Lock V2 tier chip — shadow engine verdict at a glance */}
            {v2Tier && (
              <View style={[styles.v2Chip, { borderColor: tierColor + "66", backgroundColor: tierColor + "12" }]}>
                {isApex && <Text style={styles.apexIcon}>⚡</Text>}
                <Text style={[styles.v2ChipText, { color: tierColor }]} numberOfLines={1}>
                  {isApex ? "APEX" : v2Tier === "Rare Lock" ? "RARE"
                          : v2Tier === "Strong Lock" ? "STRONG"
                          : v2Tier === "Elite Setup" ? "ELITE" : v2Tier.toUpperCase()}
                </Text>
              </View>
            )}
          </View>
          <PickEventRow pick={pick} size="card" showInjuryChip={true} />
          {pick.event_time && (
            <Text style={styles.gameTime}>{formatGameTime(pick.event_time)}</Text>
          )}
          {showLiveBadge && live && (
            <View style={[styles.liveBadge, live.is_final ? styles.liveBadgeFinal : styles.liveBadgeLive]}>
              <View style={[styles.liveDot, { backgroundColor: live.is_final ? COLORS.textMuted : COLORS.neonGreen }]} />
              <Text style={[styles.liveBadgeText, { color: live.is_final ? COLORS.textMuted : COLORS.neonGreen }]}>
                {live.is_final ? "FINAL" : "LIVE"} · {live.away_score ?? 0}-{live.home_score ?? 0}
              </Text>
            </View>
          )}
          {/* PINNED 95+ peak badge — once a pick crosses 95 lock_score on
              any refresh cycle, it stays on the board across subsequent
              refreshes so users who saw a 99-lock pick can always find it.
              Shows the peak that earned the pin so users know whether the
              current line is still the same as when it was added to slip. */}
          {(pick as any).pinned && (pick as any).lock_score_peak != null && (
            <View style={styles.pinBadge}>
              <Text style={styles.pinBadgeText}>
                📌 PEAK {Math.round((pick as any).lock_score_peak)}
              </Text>
            </View>
          )}
          {/* Player form streak — 🔥 for hot, ❄️ for cold. Soccer
              goalscorer markets now read from REAL Understat match
              data (streak_source: "understat" / "elite_anchor") and
              show a "FORM" suffix instead of W/L. Other sports keep
              the legacy pick-history W/L semantics. */}
          {pick.player_form &&
           pick.player_form.n_picks >= 3 &&
           Math.abs(pick.player_form.current_streak ?? 0) >= 2 && (
            <View style={[styles.streakBadge, (pick.player_form.current_streak ?? 0) > 0 ? styles.streakBadgeHot : styles.streakBadgeCold]}>
              <Text style={styles.streakIcon}>{(pick.player_form.current_streak ?? 0) > 0 ? "🔥" : "❄️"}</Text>
              <Text style={[styles.streakText, { color: (pick.player_form.current_streak ?? 0) > 0 ? "#FCA5A5" : "#93C5FD" }]}>
                {(pick.player_form.current_streak ?? 0) > 0 ? "HOT" : "COLD"} · {Math.abs(pick.player_form.current_streak ?? 0)}
                {(["understat", "elite_anchor"].includes(((pick.player_form as any).streak_source) || ""))
                  ? " FORM"
                  : ((pick.player_form.current_streak ?? 0) > 0 ? "W" : "L")}
              </Text>
            </View>
          )}
          {/* SIM EDGE chip — appears when the Monte Carlo simulator returns
              ≥85% win probability AND it's at least 5pp stronger than the
              blended model. Signals a high-confidence "the math really
              loves this" finding. */}
          {typeof pick.sim_win_probability === "number" &&
           pick.sim_win_probability >= 85 &&
           (pick.sim_disagreement_with_model ?? 0) >= 5 && (
            <View style={styles.simEdgeBadge}>
              <Text style={styles.simEdgeIcon}>🎲</Text>
              <Text style={styles.simEdgeText}>SIM EDGE · {pick.sim_win_probability.toFixed(0)}%</Text>
            </View>
          )}
          {/* SIGNAL chip — PerksLocks Signal Engine 0-100 score. Only
              renders when the score meaningfully deviates from the
              neutral 50 (±8) so average picks don't get badge noise.
              Full component breakdown lives in the detail screen.
              2026-07-19 — Prefer `signal_score_raw` (absolute quality)
              over `signal_score` (per-sport percentile rank). Rank
              masked real edges; raw reflects the pick's actual
              evidence strength on the same scale as the Pick
              Breakdown detail panel. */}
          {(() => {
            const sig = (typeof pick.signal_score_raw === "number")
              ? pick.signal_score_raw
              : pick.signal_score;
            if (typeof sig !== "number") return null;
            if (Math.abs(sig - 50) < 8) return null;
            return (
              <View
                style={[
                  styles.signalBadge,
                  sig >= 50 ? styles.signalBadgePos : styles.signalBadgeNeg,
                ]}
                testID="signal-score-chip"
              >
                <Text style={styles.signalIcon}>📡</Text>
                <Text
                  style={[
                    styles.signalText,
                    { color: sig >= 65 ? "#86EFAC"
                           : sig >= 50 ? "#93C5FD" : "#FCA5A5" },
                  ]}
                >
                  SIGNAL {Math.round(sig)}/100
                </Text>
              </View>
            );
          })()}
          {/* xG FORM chip — Understat-derived season form for soccer
              goalscorer markets. Renders only HOT or COLD (we hide
              NEUTRAL because every average player would otherwise
              show a meaningless badge). The +6pp / −6pp lift is the
              probability adjustment the engine applies to this leg. */}
          {(pick as any).understat_form &&
           ((pick as any).understat_form.label === "HOT" ||
            (pick as any).understat_form.label === "COLD") && (
            <View
              style={[
                styles.xgFormBadge,
                (pick as any).understat_form.label === "HOT"
                  ? styles.xgFormBadgeHot
                  : styles.xgFormBadgeCold,
              ]}
              testID="xg-form-chip"
            >
              <Text style={styles.xgFormIcon}>
                {(pick as any).understat_form.label === "HOT" ? "🔥" : "❄️"}
              </Text>
              <Text
                style={[
                  styles.xgFormText,
                  {
                    color:
                      (pick as any).understat_form.label === "HOT"
                        ? "#FCA5A5"
                        : "#93C5FD",
                  },
                ]}
              >
                xG FORM · {(pick as any).understat_form.label}
                {typeof (pick as any).understat_form.lift_pp === "number" &&
                  ` · ${(pick as any).understat_form.lift_pp >= 0 ? "+" : ""}${(pick as any).understat_form.lift_pp.toFixed(0)}pp`}
              </Text>
            </View>
          )}
          <Text style={styles.market} numberOfLines={2}>{pick.market}</Text>
          {(pick as any).model_line === true && (
            <Text style={styles.modelLineText} numberOfLines={1}>
              📐 Model line — synthesized from market O/U
            </Text>
          )}
          {nearMiss && firstBlocker && (
            <Text style={styles.nearMissText} numberOfLines={1}>
              ⚡ Almost Apex — blocked by {firstBlocker}
            </Text>
          )}
        </View>
      </View>

      {/* Lock v3 — Stacked badge hero row: Bet Quality / Expected Win / Edge */}
      <View style={styles.heroBadgeRow}>
        <HeroBadge
          icon="🔒"
          value={`${Math.round(displayLock)}`}
          label="LOCK"
          sub="BET QUALITY"
          color={gradeColor}
        />
        <HeroBadge
          icon="📊"
          value={`${pick.win_probability}%`}
          label="WIN"
          sub="EXPECTED"
          color={COLORS.textPrimary}
        />
        <HeroBadge
          icon="⚡"
          value={`${pick.edge_percent > 0 ? "+" : ""}${pick.edge_percent}%`}
          label="EDGE"
          sub="VALUE"
          color={edgeColor}
        />
      </View>

      <View style={styles.secondaryRow}>
        <Metric label="IMPLIED" value={`${pick.implied_probability}%`} color={COLORS.textSecondary} />
        <View style={styles.secondaryDivider} />
        <Metric
          label="ODDS"
          value={pick.book_odds > 0 ? `+${pick.book_odds}` : `${pick.book_odds}`}
          color={COLORS.textPrimary}
        />
      </View>

      <View style={styles.progressTrack}>
        <View
          style={[
            styles.progressFill,
            { width: `${Math.min(100, displayLock)}%`, backgroundColor: gradeColor },
          ]}
        />
      </View>
      <View style={styles.footer}>
        <Text style={styles.lockNote}>Lock = Bet Quality · Win = Expected Hit Rate</Text>
        <Text style={styles.confidence}>{pick.confidence}</Text>
      </View>

      {hasRationale && (
        <View style={styles.whyWrap}>
          <Pressable
            testID={`why-toggle-${pick.id}`}
            onPress={toggleWhy}
            hitSlop={6}
            style={({ pressed }) => [
              styles.whyToggleRow,
              pressed && { opacity: 0.7 },
            ]}
          >
            <Text style={styles.whyIcon}>💡</Text>
            <Text style={styles.whyToggleLabel}>Why this pick?</Text>
            {rationale!.confidence_score != null && (
              <View style={styles.whyConfChip}>
                <Text style={styles.whyConfChipText}>
                  {Math.round(rationale!.confidence_score)}/100
                </Text>
              </View>
            )}
            {/* LEAN chip — only show when the model lean AGREES with
                the pick direction. On an "Over 0.5 Hits" line with
                LEAN UNDER, the chip looked contradictory on a 99-Lock
                pick; the agreeing case (LEAN OVER on Over) reinforces
                user confidence. Disagreement still gets surfaced
                inside the expanded panel via the summary line. */}
            {(() => {
              const lean = rationale!.lean;
              if (lean !== "OVER" && lean !== "UNDER") return null;
              const mkt = (pick.market || "").toLowerCase();
              const isOverPick = /\bover\b/.test(mkt);
              const isUnderPick = /\bunder\b/.test(mkt);
              const agrees =
                (lean === "OVER" && isOverPick) ||
                (lean === "UNDER" && isUnderPick);
              if (!agrees) return null;
              return (
                <View
                  style={[
                    styles.whyLeanChip,
                    lean === "OVER" ? styles.whyLeanChipOver : styles.whyLeanChipUnder,
                  ]}
                >
                  <Text
                    style={[
                      styles.whyLeanChipText,
                      { color: lean === "OVER" ? "#86EFAC" : "#FCA5A5" },
                    ]}
                  >
                    LEAN {lean}
                  </Text>
                </View>
              );
            })()}
            <Text style={styles.whyChevron}>{whyOpen ? "▴" : "▾"}</Text>
          </Pressable>

          {whyOpen && (
            <View style={styles.whyBody} testID={`why-body-${pick.id}`}>
              {/* ── Soccer Goalscorer Matchup Engine v3 — chip row + bullets.
                   Populated by /app/backend/goalscorer_matchup.py whenever
                   the pick is a soccer anytime/first/last goal scorer or
                   to-score-or-assist market. Surfaces the user-spec'd
                   explainability: matchup grade, starter prob, expected
                   minutes, role, penalty taker, xG form, and the
                   bullet-point "why_this_pick" reasoning. */}
              {(pick as any).matchup_score != null && (
                <View style={styles.whyMatchupRow}>
                  <Text
                    style={[
                      styles.whyMatchupChip,
                      styles.matchupGradeChip,
                      gradeChipStyle(((pick as any).matchup_grade) || "C"),
                    ]}
                  >
                    Matchup {(pick as any).matchup_grade || "C"} · {Number((pick as any).matchup_score).toFixed(0)}
                  </Text>
                  {(pick as any).starter_probability != null && (
                    <Text style={styles.whyMatchupChip}>
                      👤 {Math.round(Number((pick as any).starter_probability) * 100)}% start
                    </Text>
                  )}
                  {(pick as any).expected_minutes != null && (
                    <Text style={styles.whyMatchupChip}>
                      ⏱ {Math.round(Number((pick as any).expected_minutes))}′ exp
                    </Text>
                  )}
                  {!!(pick as any).role && (
                    <Text style={styles.whyMatchupChip}>{String((pick as any).role)}</Text>
                  )}
                  {(pick as any).penalty_taker === true && (
                    <Text style={styles.whyMatchupChip}>⚽ PK taker</Text>
                  )}
                  {(pick as any).xG_form != null && Number((pick as any).xG_form) > 0 && (
                    <Text style={styles.whyMatchupChip}>
                      xG/90 {Number((pick as any).xG_form).toFixed(2)}
                    </Text>
                  )}
                </View>
              )}

              {Array.isArray((pick as any).why_this_pick) &&
                ((pick as any).why_this_pick as string[]).length > 0 && (
                  <View style={styles.whyMatchupBullets}>
                    {((pick as any).why_this_pick as string[])
                      .slice(0, 6)
                      .map((b, i) => (
                        <Text key={`why-${i}`} style={styles.whyMatchupBullet}>
                          • {b}
                        </Text>
                      ))}
                  </View>
                )}

              {Array.isArray((pick as any).why_not_this_pick) &&
                ((pick as any).why_not_this_pick as string[]).length > 0 && (
                  <View style={styles.whyMatchupBullets}>
                    {((pick as any).why_not_this_pick as string[])
                      .slice(0, 4)
                      .map((b, i) => (
                        <Text
                          key={`whynot-${i}`}
                          style={[styles.whyMatchupBullet, styles.whyMatchupBulletNegative]}
                        >
                          ⚠️ {b}
                        </Text>
                      ))}
                  </View>
                )}

              {!!rationale!.summary && (
                <Text style={styles.whySummary}>{rationale!.summary}</Text>
              )}

              {rationale!.matchup && (rationale!.matchup.pitcher ||
                rationale!.matchup.ballpark) && (
                <View style={styles.whyMatchupRow}>
                  {!!rationale!.matchup.pitcher && (
                    <Text style={styles.whyMatchupChip}>
                      vs {rationale!.matchup.pitcher}
                      {rationale!.matchup.pitcher_hand
                        ? ` (${rationale!.matchup.pitcher_hand}HP)`
                        : ""}
                    </Text>
                  )}
                  {!!rationale!.matchup.ballpark && (
                    <Text style={styles.whyMatchupChip}>
                      🏟 {rationale!.matchup.ballpark}
                    </Text>
                  )}
                  {rationale!.matchup.batting_order != null && (
                    <Text style={styles.whyMatchupChip}>
                      #{rationale!.matchup.batting_order} in order
                    </Text>
                  )}
                </View>
              )}

              {/* ── MLB Splits table — vs LHP/RHP for the batter,
                   vs LHB/RHB for the pitcher. Side that matters in this
                   matchup is bolded based on the opposing hand. */}
              {rationale!.splits && (
                (() => {
                  const sp = rationale!.splits!;
                  const m = rationale!.matchup || {};
                  const pH = (m.pitcher_hand || "").toUpperCase().charAt(0);
                  const bH = (m.batter_hand || "").toUpperCase().charAt(0);
                  // The batter sees the pitcher's hand → that's the split
                  // that matters for the batter. Vice versa for the pitcher.
                  const batterVsActiveHand = pH === "L"
                    ? sp.batter_vs_lhp_avg
                    : pH === "R"
                      ? sp.batter_vs_rhp_avg
                      : null;
                  const pitcherVsActiveHand = bH === "L"
                    ? sp.pitcher_vs_lhb_avg
                    : bH === "R"
                      ? sp.pitcher_vs_rhb_avg
                      : bH === "S"
                        // Switch-hitter takes the opposite-side split
                        ? (pH === "L" ? sp.pitcher_vs_rhb_avg : sp.pitcher_vs_lhb_avg)
                        : null;
                  const hasAny =
                    sp.batter_vs_lhp_avg != null ||
                    sp.batter_vs_rhp_avg != null ||
                    sp.pitcher_vs_lhb_avg != null ||
                    sp.pitcher_vs_rhb_avg != null;
                  if (!hasAny) return null;
                  const cell = (v: number | null | undefined, isActive: boolean) => (
                    <Text
                      style={[
                        styles.whySplitCell,
                        isActive && styles.whySplitCellActive,
                      ]}
                    >
                      {v == null ? "—" : v.toFixed(3).replace(/^0/, "")}
                    </Text>
                  );
                  return (
                    <View style={styles.whySection}>
                      <Text style={styles.whySectionLabel}>SPLITS · vs HAND</Text>
                      <View style={styles.whySplitRow}>
                        <Text style={styles.whySplitLabel}>Batter avg</Text>
                        <View style={styles.whySplitCellsRow}>
                          <Text style={styles.whySplitColTag}>vs LHP</Text>
                          {cell(sp.batter_vs_lhp_avg, pH === "L")}
                          <Text style={styles.whySplitColTag}>vs RHP</Text>
                          {cell(sp.batter_vs_rhp_avg, pH === "R")}
                        </View>
                      </View>
                      <View style={styles.whySplitRow}>
                        <Text style={styles.whySplitLabel}>Pitcher BAA</Text>
                        <View style={styles.whySplitCellsRow}>
                          <Text style={styles.whySplitColTag}>vs LHB</Text>
                          {cell(sp.pitcher_vs_lhb_avg, bH === "L" || (bH === "S" && pH === "R"))}
                          <Text style={styles.whySplitColTag}>vs RHB</Text>
                          {cell(sp.pitcher_vs_rhb_avg, bH === "R" || (bH === "S" && pH === "L"))}
                        </View>
                      </View>
                      {batterVsActiveHand != null && pitcherVsActiveHand != null && (
                        <Text style={styles.whySplitHint}>
                          This matchup: batter hits{" "}
                          <Text style={styles.whySplitHintBold}>
                            {batterVsActiveHand.toFixed(3).replace(/^0/, "")}
                          </Text>{" "}
                          vs {pH}HP · pitcher allows{" "}
                          <Text style={styles.whySplitHintBold}>
                            {pitcherVsActiveHand.toFixed(3).replace(/^0/, "")}
                          </Text>{" "}
                          to {bH}HB
                        </Text>
                      )}
                    </View>
                  );
                })()
              )}

              {/* ── Pitcher quality (ERA / WHIP / K/9 / BB/9 / H/9) */}
              {rationale!.pitcher_quality && (
                (() => {
                  const pq = rationale!.pitcher_quality!;
                  const chips: { label: string; v: number | null | undefined; fmt: (x: number) => string; ok: (x: number) => boolean }[] = [
                    { label: "ERA",  v: pq.era,      fmt: (x) => x.toFixed(2), ok: (x) => x < 3.50 },
                    { label: "WHIP", v: pq.whip,     fmt: (x) => x.toFixed(2), ok: (x) => x < 1.20 },
                    { label: "K/9",  v: pq.k_per_9,  fmt: (x) => x.toFixed(1), ok: (x) => x > 9.0 },
                    { label: "BB/9", v: pq.bb_per_9, fmt: (x) => x.toFixed(1), ok: (x) => x < 3.0 },
                    { label: "H/9",  v: pq.h_per_9,  fmt: (x) => x.toFixed(1), ok: (x) => x < 8.0 },
                  ].filter((c) => c.v != null && !isNaN(c.v as number));
                  if (chips.length === 0) return null;
                  return (
                    <View style={styles.whySection}>
                      <Text style={styles.whySectionLabel}>PITCHER QUALITY</Text>
                      <View style={styles.whyPqRow}>
                        {chips.map((c) => (
                          <View
                            key={c.label}
                            style={[
                              styles.whyPqChip,
                              c.ok(c.v as number)
                                ? styles.whyPqChipGood
                                : styles.whyPqChipBad,
                            ]}
                          >
                            <Text style={styles.whyPqChipLabel}>{c.label}</Text>
                            <Text
                              style={[
                                styles.whyPqChipVal,
                                {
                                  color: c.ok(c.v as number) ? "#FCA5A5" : "#86EFAC",
                                  // Good pitcher = bad for the batter (red).
                                  // Bad pitcher = good for the batter (green).
                                },
                              ]}
                            >
                              {c.fmt(c.v as number)}
                            </Text>
                          </View>
                        ))}
                      </View>
                    </View>
                  );
                })()
              )}

              {/* ── Batter-vs-Pitcher career splits (populated by
                   /app/backend/mlb_bvp.py). Only renders when the batter
                   has actually faced the opposing starter previously and
                   the sample is meaningful (≥3 PA). This is the "we
                   missing data not showing batter vs pitcher" fix — the
                   BvP was in the pick doc but was hidden in the detail
                   view; now surfaces on the card itself. */}
              {(() => {
                const bvp = (pick as any).bvp_history as
                  | {
                      ab?: number;
                      h?: number;
                      hr?: number;
                      so?: number;
                      bb?: number;
                      avg?: number;
                      batter_name?: string;
                      pitcher_name?: string;
                    }
                  | undefined;
                if (!bvp || !bvp.ab || bvp.ab < 3) return null;
                const bvpAdj = (pick as any).bvp_lock_adjustment as
                  | number
                  | undefined;
                const hot = (bvp.avg ?? 0) >= 0.300;
                const cold = (bvp.avg ?? 0) < 0.200 && (bvp.ab ?? 0) >= 6;
                return (
                  <View style={styles.whySection}>
                    <Text style={styles.whySectionLabel}>
                      BATTER vs PITCHER
                    </Text>
                    <View style={styles.whyPqRow}>
                      <View
                        style={[
                          styles.whyPqChip,
                          hot && styles.whyPqChipGood,
                          cold && styles.whyPqChipBad,
                        ]}
                      >
                        <Text style={styles.whyPqChipLabel}>H/AB</Text>
                        <Text
                          style={[
                            styles.whyPqChipVal,
                            hot && { color: "#86EFAC" },
                            cold && { color: "#FCA5A5" },
                          ]}
                        >
                          {bvp.h}/{bvp.ab}
                        </Text>
                        <Text
                          style={[
                            styles.whyPqChipLabel,
                            { fontSize: 9, opacity: 0.7 },
                          ]}
                        >
                          {(bvp.avg ?? 0).toFixed(3).replace(/^0/, "")} avg
                        </Text>
                      </View>
                      {(bvp.hr ?? 0) > 0 && (
                        <View style={styles.whyPqChip}>
                          <Text style={styles.whyPqChipLabel}>HR</Text>
                          <Text
                            style={[
                              styles.whyPqChipVal,
                              { color: "#86EFAC" },
                            ]}
                          >
                            {bvp.hr}
                          </Text>
                        </View>
                      )}
                      {(bvp.so ?? 0) > 0 && (
                        <View style={styles.whyPqChip}>
                          <Text style={styles.whyPqChipLabel}>K</Text>
                          <Text
                            style={[
                              styles.whyPqChipVal,
                              { color: "#FCA5A5" },
                            ]}
                          >
                            {bvp.so}
                          </Text>
                        </View>
                      )}
                      {(bvp.bb ?? 0) > 0 && (
                        <View style={styles.whyPqChip}>
                          <Text style={styles.whyPqChipLabel}>BB</Text>
                          <Text style={styles.whyPqChipVal}>{bvp.bb}</Text>
                        </View>
                      )}
                    </View>
                    {typeof bvpAdj === "number" && bvpAdj !== 0 && (
                      <Text
                        style={[
                          styles.whySplitHint,
                          {
                            color:
                              bvpAdj > 0 ? "#86EFAC" : "#FCA5A5",
                          },
                        ]}
                      >
                        {bvpAdj > 0 ? "+" : ""}
                        {bvpAdj} Lock Score from BvP history
                      </Text>
                    )}
                  </View>
                );
              })()}

              {/* ── Recent Form rolling windows (L5 / L10 / L20) ────
                   Real player game-log data. Format depends on engine:
                   • MLB HITTERS → "5/5" (games with ≥1 hit / games)
                   • MLB PITCHERS → "5.4 K/GS" (K's per start)
                   Same data structure, different display for readability. */}
              {rationale!.recent_form && (
                (() => {
                  const rf = rationale!.recent_form as Record<string, unknown>;
                  const engine = String(rf.engine || "");
                  const isPitcher = engine === "mlb_pitcher_intel";
                  const windows: {
                    label: string;
                    avg: number | null;
                    gwh: number | null;
                    gp: number | null;
                    era: number | null;
                  }[] = [
                    {
                      label: "L5",
                      avg: (rf.last5_avg as number) ?? null,
                      gwh: (rf.last5_games_with_hit as number) ?? null,
                      gp: (rf.last5_games_played as number) ?? null,
                      era: (rf.last5_era as number) ?? null,
                    },
                    {
                      label: "L10",
                      avg: (rf.last10_avg as number) ?? null,
                      gwh: (rf.last10_games_with_hit as number) ?? null,
                      gp: (rf.last10_games_played as number) ?? null,
                      era: (rf.last10_era as number) ?? null,
                    },
                    {
                      label: "L20",
                      avg: (rf.last20_avg as number) ?? null,
                      gwh: (rf.last20_games_with_hit as number) ?? null,
                      gp: (rf.last20_games_played as number) ?? null,
                      era: (rf.last20_era as number) ?? null,
                    },
                  ].filter((w) => w.avg != null && w.gp != null && (w.gp as number) > 0);
                  if (windows.length === 0) return null;
                  const sectionLabel = isPitcher
                    ? "RECENT FORM · K PER START"
                    : "RECENT FORM · HITS PER GAME";
                  return (
                    <View style={styles.whySection}>
                      <Text style={styles.whySectionLabel}>{sectionLabel}</Text>
                      <View style={styles.whyPqRow}>
                        {windows.map((w) => {
                          if (isPitcher) {
                            // Pitcher chip — main number is avg K/start,
                            // color code by absolute K rate (hot ≥ 8, cold < 5).
                            const isHot = (w.avg as number) >= 8.0;
                            const isCold = (w.avg as number) < 5.0;
                            return (
                              <View
                                key={w.label}
                                style={[
                                  styles.whyPqChip,
                                  isHot && styles.whyPqChipBad,
                                  isCold && styles.whyPqChipGood,
                                ]}
                              >
                                <Text style={styles.whyPqChipLabel}>{w.label}</Text>
                                <Text
                                  style={[
                                    styles.whyPqChipVal,
                                    isHot && { color: "#86EFAC" },
                                    isCold && { color: "#FCA5A5" },
                                  ]}
                                >
                                  {(w.avg as number).toFixed(1)}
                                </Text>
                                <Text style={[styles.whyPqChipLabel, { fontSize: 9, opacity: 0.7 }]}>
                                  {w.gp} GS · {w.era ? `${(w.era as number).toFixed(2)} ERA` : "—"}
                                </Text>
                              </View>
                            );
                          }
                          // Hitter chip — games-with-hit / games-played.
                          const gp = (w.gp as number) || 1;
                          const gwh = (w.gwh as number) || 0;
                          const gwh_rate = gwh / gp;
                          const isHot = gwh_rate >= 0.75;
                          const isCold = gwh_rate < 0.50;
                          return (
                            <View
                              key={w.label}
                              style={[
                                styles.whyPqChip,
                                isHot && styles.whyPqChipBad,
                                isCold && styles.whyPqChipGood,
                              ]}
                            >
                              <Text style={styles.whyPqChipLabel}>{w.label}</Text>
                              <Text
                                style={[
                                  styles.whyPqChipVal,
                                  isHot && { color: "#86EFAC" },
                                  isCold && { color: "#FCA5A5" },
                                ]}
                              >
                                {gwh}/{gp}
                              </Text>
                              <Text style={[styles.whyPqChipLabel, { fontSize: 9, opacity: 0.7 }]}>
                                {(w.avg as number).toFixed(3).replace(/^0/, "")} avg
                              </Text>
                            </View>
                          );
                        })}
                      </View>
                    </View>
                  );
                })()
              )}

              {/* ── Engine multipliers (platoon / pitcher / park / form / home-away) */}
              {rationale!.multipliers && (
                (() => {
                  const ms = rationale!.multipliers!;
                  const order: { key: string; label: string }[] = [
                    { key: "platoon",        label: "Platoon" },
                    { key: "pitcher_quality", label: "Pitcher" },
                    { key: "park",           label: "Park" },
                    { key: "recent_form",    label: "Form" },
                    { key: "home_away",      label: "Home/Away" },
                  ];
                  const rows = order
                    .map((r) => ({ ...r, v: ms[r.key] }))
                    .filter((r) => typeof r.v === "number" && !isNaN(r.v));
                  if (rows.length === 0) return null;
                  return (
                    <View style={styles.whySection}>
                      <Text style={styles.whySectionLabel}>MODEL ADJUSTMENTS</Text>
                      <View style={styles.whyMultRow}>
                        {rows.map((r) => {
                          const pct = ((r.v as number) - 1) * 100;
                          const isUp = pct > 0.5;
                          const isDown = pct < -0.5;
                          return (
                            <View key={r.key} style={styles.whyMultChip}>
                              <Text style={styles.whyMultLabel}>{r.label}</Text>
                              <Text
                                style={[
                                  styles.whyMultVal,
                                  isUp && { color: COLORS.neonGreen },
                                  isDown && { color: "#FCA5A5" },
                                ]}
                              >
                                {pct >= 0 ? "+" : ""}
                                {pct.toFixed(1)}%
                              </Text>
                            </View>
                          );
                        })}
                      </View>
                    </View>
                  );
                })()
              )}

              {(rationale!.evidence?.length ?? 0) > 0 && (
                <View style={styles.whySection}>
                  <Text style={styles.whySectionLabel}>WHY WE LIKE IT</Text>
                  {rationale!.evidence!.slice(0, 4).map((e, i) => (
                    <Text key={`ev-${i}`} style={styles.whyBullet}>
                      {e}
                    </Text>
                  ))}
                </View>
              )}

              {(rationale!.concerns?.length ?? 0) > 0 && (
                <View style={styles.whySection}>
                  <Text style={styles.whySectionLabel}>WATCH-OUTS</Text>
                  {rationale!.concerns!.slice(0, 3).map((c, i) => (
                    <Text key={`cn-${i}`} style={styles.whyBulletConcern}>
                      {c}
                    </Text>
                  ))}
                </View>
              )}

              {rationale!.final_hit_prob_pct != null && (
                <Text style={styles.whyMetaLine}>
                  Engine prob: {rationale!.final_hit_prob_pct.toFixed(1)}%
                  {rationale!.edge_pct_points != null && (
                    <Text style={{ color: COLORS.textMuted }}>
                      {"  ·  Edge vs market: "}
                      <Text
                        style={{
                          color:
                            rationale!.edge_pct_points > 0
                              ? COLORS.neonGreen
                              : COLORS.electricBlaze,
                          fontWeight: "800",
                        }}
                      >
                        {rationale!.edge_pct_points > 0 ? "+" : ""}
                        {rationale!.edge_pct_points.toFixed(1)}pp
                      </Text>
                    </Text>
                  )}
                </Text>
              )}

              {rationale!.espn_rank != null && (
                <Text style={styles.whyMetaLine}>
                  ESPN scorer rank ·{" "}
                  <Text style={{ color: COLORS.voltBlue, fontWeight: "800" }}>
                    #{rationale!.espn_rank}
                  </Text>
                </Text>
              )}

              <Text style={styles.whySource}>
                Source: {rationale!.data_source || "model"}
                {rationale!.engine ? ` · ${rationale!.engine}` : ""}
              </Text>
            </View>
          )}
        </View>
      )}

      {/* ── Track Bet button (2026-07-21) ────────────────────────────
          Logs the pick as a user_bet at the chosen stake. Server
          scopes everything to user_id, so bets stay private.
          Auto-settles when the pick's status flips to won/lost/push. */}
      <TrackBetButton pick={pick} />
    </Pressable>
  );
}

function TrackBetButton({ pick }: { pick: LockPick }) {
  const [tracked, setTracked] = useState(false);
  const [busy, setBusy] = useState(false);

  const onTrack = useCallback((e: any) => {
    e?.stopPropagation?.();
    if (busy || tracked) return;
    // Prompt user for stake — small options list, easy one-tap
    Alert.alert(
      "Track This Bet",
      `Log ${pick.selection} at ${pick.book_odds >= 0 ? "+" : ""}${pick.book_odds} as a personal bet?`,
      [
        { text: "Cancel", style: "cancel" },
        { text: "0.5u", onPress: () => submit(0.5) },
        { text: "1u",   onPress: () => submit(1.0) },
        { text: "2u",   onPress: () => submit(2.0) },
      ],
      { cancelable: true },
    );
  }, [pick, busy, tracked]);

  const submit = async (stake_units: number) => {
    setBusy(true);
    try {
      await api.trackBet({
        pick_id: pick.id,
        bet_type: "straight",
        stake_units,
      });
      setTracked(true);
      Alert.alert("✓ Bet Tracked", `${stake_units}u on ${pick.selection}. View it in My Bets.`);
    } catch (err: any) {
      Alert.alert("Could not track bet", err?.message ?? "Try again in a moment.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Pressable
      onPress={onTrack}
      hitSlop={8}
      style={({ pressed }) => [
        styles.trackBtn,
        tracked && styles.trackBtnDone,
        pressed && { opacity: 0.7 },
      ]}
    >
      {busy ? (
        <ActivityIndicator size="small" color={COLORS.neonGreen} />
      ) : (
        <>
          <Text style={styles.trackBtnIcon}>{tracked ? "✓" : "🎯"}</Text>
          <Text style={[styles.trackBtnText, tracked && styles.trackBtnTextDone]}>
            {tracked ? "TRACKED" : "TRACK BET"}
          </Text>
        </>
      )}
    </Pressable>
  );
}

function HeroBadge({
  icon, value, label, sub, color,
}: { icon: string; value: string; label: string; sub: string; color: string }) {
  return (
    <View style={[styles.heroBadge, { borderColor: color + "55", backgroundColor: color + "10" }]}>
      <Text style={styles.heroIcon}>{icon}</Text>
      <Text style={[styles.heroValue, { color }]} numberOfLines={1}>{value}</Text>
      <Text style={styles.heroLabel}>{label}</Text>
      <Text style={styles.heroSub}>{sub}</Text>
    </View>
  );
}

function Metric({ label, value, color }: { label: string; value: string; color: string }) {
  return (
    <View style={styles.metric}>
      <Text style={styles.metricLabel}>{label}</Text>
      <Text style={[styles.metricValue, { color }]} numberOfLines={1}>{value}</Text>
    </View>
  );
}

// ── React.memo wrapper ─────────────────────────────────────────────
// 248 cards on the home feed → without memoization, every parent state
// change (filter toggle, sport switch, periodic refetch) re-renders
// EVERY card. Profile showed ~50 ms wasted per render pass.
//
// Custom equality: compare only the fields the card actually paints.
// We deliberately ignore deep arrays like `pick_rationale.evidence`
// (they're stable references after `setPicks(fresh)`, and the deep
// rationale is fetched lazily into local state — so it's invisible to
// the parent's pick prop).
function arePropsEqual(prev: { pick: Pick }, next: { pick: Pick }): boolean {
  const a = prev.pick;
  const b = next.pick;
  if (a === b) return true;                  // referential — fast path
  if (a.id !== b.id) return false;
  // Numeric fields the card paints
  if (a.lock_score !== b.lock_score) return false;
  if (a.edge_percent !== b.edge_percent) return false;
  if (a.win_probability !== b.win_probability) return false;
  if (a.implied_probability !== b.implied_probability) return false;
  if (a.american_odds !== b.american_odds) return false;
  if (a.lock_score_v2 !== b.lock_score_v2) return false;
  if (a.lock_score_peak !== b.lock_score_peak) return false;
  if (a.signal_score !== b.signal_score) return false;
  // String / enum fields
  if (a.grade !== b.grade) return false;
  if (a.market !== b.market) return false;
  if (a.event !== b.event) return false;
  if (a.event_time !== b.event_time) return false;
  if (a.league !== b.league) return false;
  if (a.tier_v2 !== b.tier_v2) return false;
  // Boolean flags
  if (a.is_apex !== b.is_apex) return false;
  if (a.elite_player !== b.elite_player) return false;
  if (a.pinned !== b.pinned) return false;
  if ((a as any).is_extra !== (b as any).is_extra) return false;
  if ((a as any).is_model_only !== (b as any).is_model_only) return false;
  // pick_rationale: identity check is enough — backend serializer
  // re-emits the dict on every refresh, so a NEW object means new
  // content. We don't need a deep compare.
  if ((a as any).pick_rationale !== (b as any).pick_rationale) return false;
  return true;
}

export const LockPickCard = React.memo(LockPickCardImpl, arePropsEqual);

// ── Matchup-grade chip color helper ─────────────────────────────────
// Maps the goalscorer matchup engine's A+..F grade to a styles row so
// the chip lights up green/yellow/red. Returns an empty object when
// the grade is unknown so React Native ignores it cleanly.
function gradeChipStyle(grade: string) {
  const g = (grade || "").toUpperCase();
  if (g === "A+" || g === "A") return styles.matchupGradeA;
  if (g === "B+" || g === "B") return styles.matchupGradeB;
  if (g === "C+" || g === "C") return styles.matchupGradeC;
  if (g === "D") return styles.matchupGradeD;
  if (g === "F") return styles.matchupGradeF;
  return {};
}

const styles = StyleSheet.create({
  card: {
    backgroundColor: COLORS.surface,
    borderRadius: 16,
    padding: 18,
    borderWidth: 1,
    borderColor: COLORS.borderDefault,
    marginBottom: 14,
  },
  cardApex: {
    borderColor: "#FFD700",
    borderWidth: 1.5,
    shadowColor: "#FFD700",
    shadowOpacity: 0.25,
    shadowOffset: { width: 0, height: 0 },
    shadowRadius: 8,
  },
  v2Chip: {
    flexDirection: "row",
    alignItems: "center",
    gap: 3,
    paddingHorizontal: 6,
    paddingVertical: 2,
    borderRadius: 4,
    borderWidth: 1,
  },
  v2ChipText: {
    fontSize: 8.5,
    fontWeight: "900",
    letterSpacing: 0.9,
  },
  apexIcon: { fontSize: 9 },
  nearMissText: {
    color: "#FFD700",
    fontSize: 10.5,
    fontWeight: "700",
    letterSpacing: 0.3,
    marginTop: 6,
    opacity: 0.85,
  },
  modelLineText: {
    color: COLORS.textMuted,
    fontSize: 10.5,
    fontWeight: "600",
    letterSpacing: 0.2,
    marginTop: 4,
    fontStyle: "italic",
  },
  header: { flexDirection: "row", justifyContent: "space-between" },
  tagRow: { flexDirection: "row", alignItems: "center", gap: 8, marginBottom: 8 },
  tag: {
    paddingHorizontal: 8,
    paddingVertical: 3,
    borderRadius: 4,
    backgroundColor: "rgba(255,255,255,0.08)",
  },
  tagText: { color: COLORS.textPrimary, fontSize: 10, fontWeight: "800", letterSpacing: 1.2 },
  extraTag: {
    // Visually distinct from ELITE/STRONG/grade badges — orange-amber
    // so users instantly recognize "this pick's line came from a
    // TennisExplorer scrape, not a US sportsbook". The amber palette
    // signals "use with caution" without screaming red.
    paddingHorizontal: 7, paddingVertical: 3, borderRadius: 4,
    borderWidth: 1, borderColor: "#FFA94D",
    backgroundColor: "#FFA94D22",
  },
  extraTagText: {
    color: "#FFA94D", fontSize: 10, fontWeight: "900",
    letterSpacing: 1.2,
  },
  // ── MODEL badge — for synthetic picks where no bookmaker line exists.
  // Examples: CSL anytime-goal-scorer picks built from SportDB stats.
  // Purple palette signals "data-driven derivation" — distinct from EXT
  // (line scrape) and ELITE (player tier). Lets the user instantly know
  // there's no real market price to compare edge against.
  modelTag: {
    paddingHorizontal: 7, paddingVertical: 3, borderRadius: 4,
    borderWidth: 1, borderColor: "#A78BFA",
    backgroundColor: "#A78BFA22",
  },
  modelTagText: {
    color: "#A78BFA", fontSize: 10, fontWeight: "900",
    letterSpacing: 1.2,
  },
  // ── CHALK TRAP badge — red warning on picks priced -250 or worse
  // that lack strong data-driven confirmation. User still sees the
  // pick but the app is explicitly warning: "high juice, thin edge —
  // needs ~72%+ hit rate just to break even". Paired with the
  // demoted "Solid Lean" grade so a user glancing at the card
  // instantly clocks it as informational, not a Lock.
  chalkTrapTag: {
    paddingHorizontal: 7, paddingVertical: 3, borderRadius: 4,
    borderWidth: 1, borderColor: "#F87171",
    backgroundColor: "#F8717122",
  },
  chalkTrapTagText: {
    color: "#F87171", fontSize: 10, fontWeight: "900",
    letterSpacing: 1.2,
  },
  // ── CHALK VERIFIED (CHALK+) badge — green stamp on picks priced
  // -250 or worse that CLEARED the kill switch (>=8pp true edge and
  // >=3 aligned data signals). Rare — usually 0-3 per slate — but
  // when they hit, they're the genuine +EV chalk plays.
  chalkVerifiedTag: {
    paddingHorizontal: 7, paddingVertical: 3, borderRadius: 4,
    borderWidth: 1, borderColor: COLORS.neonGreen,
    backgroundColor: COLORS.neonGreen + "22",
  },
  chalkVerifiedTagText: {
    color: COLORS.neonGreen, fontSize: 10, fontWeight: "900",
    letterSpacing: 1.2,
  },
  // ── Track Bet button — bottom of every card (2026-07-21) ──────────
  // Adds a lightweight "log this to my personal bets" action so users
  // can build their own ROI outside of the model's auto-graded slate.
  // Server enforces user_id scope on all reads.
  trackBtn: {
    marginTop: 8, marginHorizontal: 12, paddingVertical: 10,
    borderRadius: 8, alignItems: "center", flexDirection: "row",
    justifyContent: "center", gap: 6,
    backgroundColor: COLORS.neonGreen + "18",
    borderWidth: 1, borderColor: COLORS.neonGreen + "55",
  },
  trackBtnDone: {
    backgroundColor: COLORS.neonGreen + "33",
    borderColor: COLORS.neonGreen,
  },
  trackBtnIcon: { fontSize: 14 },
  trackBtnText: {
    color: COLORS.neonGreen, fontSize: 12, fontWeight: "900",
    letterSpacing: 1.3,
  },
  trackBtnTextDone: { color: COLORS.neonGreen },
  league: { color: COLORS.textMuted, fontSize: 11, fontWeight: "600", flex: 1 },
  event: { color: COLORS.textSecondary, fontSize: 12, marginBottom: 2, fontWeight: "500" },
  gameTime: { color: COLORS.voltBlue, fontSize: 11, fontWeight: "700", letterSpacing: 0.3, marginBottom: 6 },
  liveBadge: {
    flexDirection: "row", alignItems: "center", alignSelf: "flex-start",
    gap: 6, paddingHorizontal: 8, paddingVertical: 3,
    borderRadius: 4, borderWidth: 1, marginBottom: 6,
  },
  liveBadgeLive: {
    backgroundColor: "rgba(0,255,170,0.10)",
    borderColor: "rgba(0,255,170,0.45)",
  },
  liveBadgeFinal: {
    backgroundColor: "rgba(255,255,255,0.04)",
    borderColor: "rgba(255,255,255,0.18)",
  },
  liveDot: { width: 6, height: 6, borderRadius: 3 },
  liveBadgeText: { fontSize: 10, fontWeight: "800", letterSpacing: 0.8, fontVariant: ["tabular-nums"] },
  pinBadge: {
    paddingHorizontal: 8,
    paddingVertical: 2,
    borderRadius: 10,
    borderWidth: 1,
    borderColor: COLORS.neonGreen,
    backgroundColor: COLORS.neonGreen + "1A",
    marginLeft: 6,
  },
  pinBadgeText: {
    color: COLORS.neonGreen,
    fontSize: 10,
    fontWeight: "900",
    letterSpacing: 0.6,
    fontVariant: ["tabular-nums"],
  },
  // SIM EDGE — high-confidence simulator agreement chip
  simEdgeBadge: {
    alignSelf: "flex-start",
    flexDirection: "row",
    alignItems: "center",
    gap: 4,
    paddingHorizontal: 7,
    paddingVertical: 3,
    borderRadius: 5,
    borderWidth: 1,
    borderColor: "rgba(167, 139, 250, 0.55)",
    backgroundColor: "rgba(167, 139, 250, 0.12)",
    marginTop: 4,
  },
  simEdgeIcon: { fontSize: 10 },
  simEdgeText: { color: "#C4B5FD", fontSize: 9, fontWeight: "900", letterSpacing: 0.9 },
  signalBadge: {
    flexDirection: "row",
    alignItems: "center",
    gap: 4,
    alignSelf: "flex-start",
    paddingHorizontal: 8,
    paddingVertical: 3,
    borderRadius: 10,
    borderWidth: 1,
    marginTop: 6,
  },
  signalBadgePos: { backgroundColor: "#052E1622", borderColor: "#22C55E55" },
  signalBadgeNeg: { backgroundColor: "#450A0A22", borderColor: "#EF444455" },
  signalIcon: { fontSize: 10 },
  signalText: { fontSize: 9, fontWeight: "900", letterSpacing: 0.9 },
  // ── Player streak (hot/cold) badge ──
  streakBadge: {
    alignSelf: "flex-start",
    flexDirection: "row",
    alignItems: "center",
    gap: 4,
    paddingHorizontal: 7,
    paddingVertical: 3,
    borderRadius: 5,
    borderWidth: 1,
    marginTop: 4,
  },
  streakBadgeHot: {
    borderColor: "rgba(252, 165, 165, 0.55)",
    backgroundColor: "rgba(252, 165, 165, 0.12)",
  },
  streakBadgeCold: {
    borderColor: "rgba(147, 197, 253, 0.55)",
    backgroundColor: "rgba(147, 197, 253, 0.12)",
  },
  streakIcon: { fontSize: 10 },
  streakText: { fontSize: 9, fontWeight: "900", letterSpacing: 0.9 },
  // ── xG FORM badge (Understat-derived, soccer goalscorer markets) ──
  xgFormBadge: {
    alignSelf: "flex-start",
    flexDirection: "row",
    alignItems: "center",
    gap: 4,
    paddingHorizontal: 7,
    paddingVertical: 3,
    borderRadius: 5,
    borderWidth: 1,
    marginTop: 4,
  },
  xgFormBadgeHot: {
    borderColor: "rgba(252, 165, 165, 0.55)",
    backgroundColor: "rgba(252, 165, 165, 0.10)",
  },
  xgFormBadgeCold: {
    borderColor: "rgba(147, 197, 253, 0.55)",
    backgroundColor: "rgba(147, 197, 253, 0.10)",
  },
  xgFormIcon: { fontSize: 10 },
  xgFormText: { fontSize: 9, fontWeight: "900", letterSpacing: 0.9 },
  market: { color: COLORS.textPrimary, fontSize: 17, fontWeight: "800", letterSpacing: -0.3 },
  metricsRow: { flexDirection: "row", justifyContent: "space-between", marginTop: 18, marginBottom: 12 },
  metric: { flex: 1 },
  metricLabel: { color: COLORS.textMuted, fontSize: 9, fontWeight: "800", letterSpacing: 1.3 },
  metricValue: { fontSize: 16, fontWeight: "900", marginTop: 3, letterSpacing: -0.3 },

  heroBadgeRow: {
    flexDirection: "row",
    gap: 8,
    marginTop: 16,
    marginBottom: 12,
  },
  heroBadge: {
    flex: 1,
    paddingVertical: 12,
    paddingHorizontal: 8,
    borderRadius: 12,
    borderWidth: 1,
    alignItems: "center",
    justifyContent: "center",
  },
  heroIcon: { fontSize: 14, marginBottom: 2 },
  heroValue: { fontSize: 22, fontWeight: "900", letterSpacing: -0.6, marginTop: 2 },
  heroLabel: {
    color: COLORS.textPrimary,
    fontSize: 9,
    fontWeight: "900",
    letterSpacing: 1.4,
    marginTop: 4,
  },
  heroSub: {
    color: COLORS.textMuted,
    fontSize: 8,
    fontWeight: "700",
    letterSpacing: 1.0,
    marginTop: 1,
  },

  secondaryRow: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingVertical: 6,
    paddingHorizontal: 4,
    marginBottom: 10,
  },
  secondaryDivider: {
    width: 1,
    height: 22,
    backgroundColor: COLORS.borderDefault,
  },

  gradePill: {
    paddingHorizontal: 8,
    paddingVertical: 3,
    borderRadius: 4,
    borderWidth: 1,
  },
  gradePillText: {
    fontSize: 9,
    fontWeight: "900",
    letterSpacing: 1.2,
  },
  eliteTag: {
    paddingHorizontal: 7,
    paddingVertical: 2,
    borderRadius: 4,
    backgroundColor: "#FFD70022",
    borderWidth: 1,
    borderColor: "#FFD700",
  },
  eliteTagText: {
    color: "#FFD700",
    fontSize: 9,
    fontWeight: "900",
    letterSpacing: 1.0,
  },
  progressTrack: {
    height: 4,
    backgroundColor: "rgba(255,255,255,0.06)",
    borderRadius: 2,
    overflow: "hidden",
  },
  progressFill: { height: "100%", borderRadius: 2 },
  footer: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center",
    marginTop: 10,
  },
  gradeText: { fontSize: 11, fontWeight: "900", letterSpacing: 1.5 },
  lockNote: {
    color: COLORS.textMuted,
    fontSize: 9,
    fontWeight: "700",
    letterSpacing: 0.4,
    flex: 1,
  },
  confidence: { fontSize: 10, color: COLORS.textMuted, fontWeight: "700", letterSpacing: 0.8 },

  // ── "Why this pick?" rationale expansion ────────────────────────────
  whyWrap: {
    marginTop: 12,
    paddingTop: 10,
    borderTopWidth: 1,
    borderTopColor: COLORS.borderDefault,
  },
  whyToggleRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    paddingVertical: 4,
  },
  whyIcon: { fontSize: 12 },
  whyToggleLabel: {
    color: COLORS.textPrimary,
    fontSize: 12,
    fontWeight: "800",
    letterSpacing: 0.4,
    flex: 1,
  },
  whyConfChip: {
    paddingHorizontal: 7,
    paddingVertical: 2,
    borderRadius: 4,
    borderWidth: 1,
    borderColor: COLORS.voltBlue + "66",
    backgroundColor: COLORS.voltBlue + "12",
  },
  whyConfChipText: {
    color: COLORS.voltBlue,
    fontSize: 9,
    fontWeight: "900",
    letterSpacing: 0.6,
    fontVariant: ["tabular-nums"],
  },
  whyLeanChip: {
    paddingHorizontal: 7,
    paddingVertical: 2,
    borderRadius: 4,
    borderWidth: 1,
  },
  whyLeanChipOver: {
    borderColor: "rgba(134, 239, 172, 0.55)",
    backgroundColor: "rgba(134, 239, 172, 0.12)",
  },
  whyLeanChipUnder: {
    borderColor: "rgba(252, 165, 165, 0.55)",
    backgroundColor: "rgba(252, 165, 165, 0.12)",
  },
  whyLeanChipText: {
    fontSize: 9,
    fontWeight: "900",
    letterSpacing: 0.8,
  },
  whyChevron: {
    color: COLORS.textMuted,
    fontSize: 14,
    fontWeight: "900",
    minWidth: 14,
    textAlign: "center",
  },
  whyBody: {
    marginTop: 10,
    paddingTop: 4,
    gap: 8,
  },
  whySummary: {
    color: COLORS.textSecondary,
    fontSize: 12,
    lineHeight: 17,
    fontStyle: "italic",
  },
  whyMatchupRow: {
    flexDirection: "row",
    flexWrap: "wrap",
    gap: 6,
  },
  whyMatchupChip: {
    color: COLORS.textSecondary,
    fontSize: 10.5,
    fontWeight: "700",
    paddingHorizontal: 7,
    paddingVertical: 3,
    borderRadius: 4,
    backgroundColor: "rgba(255,255,255,0.05)",
    borderWidth: 1,
    borderColor: COLORS.borderDefault,
  },
  // ── Goalscorer Matchup Engine v3 — header chip ──
  matchupGradeChip: {
    fontWeight: "800",
    letterSpacing: 0.4,
  },
  matchupGradeA: {
    color: "#0a0a0a",
    backgroundColor: "rgba(74, 222, 128, 0.92)",
    borderColor: "rgba(74, 222, 128, 0.45)",
  },
  matchupGradeB: {
    color: "#0a0a0a",
    backgroundColor: "rgba(132, 204, 22, 0.85)",
    borderColor: "rgba(132, 204, 22, 0.45)",
  },
  matchupGradeC: {
    color: "#0a0a0a",
    backgroundColor: "rgba(234, 179, 8, 0.85)",
    borderColor: "rgba(234, 179, 8, 0.45)",
  },
  matchupGradeD: {
    color: "#f5f5f5",
    backgroundColor: "rgba(249, 115, 22, 0.78)",
    borderColor: "rgba(249, 115, 22, 0.45)",
  },
  matchupGradeF: {
    color: "#f5f5f5",
    backgroundColor: "rgba(239, 68, 68, 0.80)",
    borderColor: "rgba(239, 68, 68, 0.45)",
  },
  whyMatchupBullets: {
    gap: 3,
    marginTop: 2,
  },
  whyMatchupBullet: {
    color: COLORS.textSecondary,
    fontSize: 11.5,
    lineHeight: 16,
  },
  whyMatchupBulletNegative: {
    color: "#fca5a5",
  },

  // ── MLB Splits table (vs LHP/RHP, vs LHB/RHB) ─────────────────
  whySplitRow: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingVertical: 3,
  },
  whySplitLabel: {
    color: COLORS.textMuted,
    fontSize: 10,
    fontWeight: "800",
    letterSpacing: 0.4,
    minWidth: 78,
  },
  whySplitCellsRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
  },
  whySplitColTag: {
    color: COLORS.textMuted,
    fontSize: 9,
    fontWeight: "700",
    letterSpacing: 0.6,
  },
  whySplitCell: {
    color: COLORS.textSecondary,
    fontSize: 12,
    fontWeight: "700",
    fontVariant: ["tabular-nums"],
    paddingHorizontal: 5,
    paddingVertical: 2,
    borderRadius: 3,
    minWidth: 44,
    textAlign: "center",
  },
  whySplitCellActive: {
    color: COLORS.voltBlue,
    backgroundColor: COLORS.voltBlue + "14",
    fontWeight: "900",
  },
  whySplitHint: {
    color: COLORS.textMuted,
    fontSize: 10.5,
    fontStyle: "italic",
    marginTop: 4,
    lineHeight: 14,
  },
  whySplitHintBold: {
    color: COLORS.voltBlue,
    fontWeight: "900",
    fontStyle: "normal",
    fontVariant: ["tabular-nums"],
  },

  // ── Pitcher Quality chips (ERA / WHIP / K/9 / BB/9 / H/9) ──────
  whyPqRow: {
    flexDirection: "row",
    flexWrap: "wrap",
    gap: 6,
  },
  whyPqChip: {
    paddingHorizontal: 7,
    paddingVertical: 4,
    borderRadius: 4,
    borderWidth: 1,
    minWidth: 52,
    alignItems: "center",
  },
  whyPqChipGood: {
    // Good pitcher → bad for batter → red border
    borderColor: "rgba(252, 165, 165, 0.45)",
    backgroundColor: "rgba(252, 165, 165, 0.08)",
  },
  whyPqChipBad: {
    // Bad pitcher → good for batter → green border
    borderColor: "rgba(134, 239, 172, 0.45)",
    backgroundColor: "rgba(134, 239, 172, 0.08)",
  },
  whyPqChipLabel: {
    color: COLORS.textMuted,
    fontSize: 9,
    fontWeight: "900",
    letterSpacing: 0.5,
  },
  whyPqChipVal: {
    fontSize: 12,
    fontWeight: "900",
    fontVariant: ["tabular-nums"],
    marginTop: 1,
  },

  // ── Engine Multipliers (platoon, pitcher, park, form, home/away) ──
  whyMultRow: {
    flexDirection: "row",
    flexWrap: "wrap",
    gap: 6,
  },
  whyMultChip: {
    paddingHorizontal: 7,
    paddingVertical: 4,
    borderRadius: 4,
    backgroundColor: "rgba(255,255,255,0.04)",
    borderWidth: 1,
    borderColor: COLORS.borderDefault,
    alignItems: "center",
    minWidth: 60,
  },
  whyMultLabel: {
    color: COLORS.textMuted,
    fontSize: 9,
    fontWeight: "900",
    letterSpacing: 0.5,
  },
  whyMultVal: {
    color: COLORS.textSecondary,
    fontSize: 12,
    fontWeight: "900",
    fontVariant: ["tabular-nums"],
    marginTop: 1,
  },
  whySection: { gap: 3 },
  whySectionLabel: {
    color: COLORS.textMuted,
    fontSize: 9,
    fontWeight: "900",
    letterSpacing: 1.3,
    marginBottom: 2,
  },
  whyBullet: {
    color: COLORS.textPrimary,
    fontSize: 12,
    lineHeight: 17,
    fontWeight: "500",
  },
  whyBulletConcern: {
    color: "#FCA5A5",
    fontSize: 12,
    lineHeight: 17,
    fontWeight: "500",
  },
  whyMetaLine: {
    color: COLORS.textSecondary,
    fontSize: 11,
    fontWeight: "700",
    fontVariant: ["tabular-nums"],
  },
  whySource: {
    color: COLORS.textMuted,
    fontSize: 9,
    fontWeight: "700",
    letterSpacing: 0.6,
    marginTop: 2,
  },
});
