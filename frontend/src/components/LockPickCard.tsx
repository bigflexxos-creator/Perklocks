import React, { useState, useCallback, useEffect } from "react";
import { View, Text, StyleSheet, Pressable, Platform, LayoutAnimation, UIManager, ActivityIndicator, Modal, TextInput } from "react-native";
import { LinearGradient } from "expo-linear-gradient";
import { useRouter } from "expo-router";
import { COLORS, GRADE_COLORS, getLockTierVisual, RADIUS, getSportColor, CONFIDENCE_GRADIENT } from "@/src/theme";
import { PlayerIdentity } from "@/src/components/PlayerIdentity";
import { Pick, PickRationale, api } from "@/src/lib/api";
import { formatGameTime } from "@/src/lib/formatGameTime";
import { useMLBLive } from "@/src/contexts/MLBLiveContext";
import { getDisplayLock } from "@/src/lib/lockScore";
import { PickEventRow } from "@/src/components/PickEventRow";
import { useBetSlip } from "@/src/contexts/BetSlipContext";
import { MatchupGradeBadge } from "@/src/components/MatchupGradeBadge";
import { AltLineChips } from "@/src/components/AltLineChips";

// Local alias so TrackBetButton props type-check without pulling
// the full Pick type through the closure.
type LockPick = Pick;

function LockPickCardImpl({ pick, featured = false }: { pick: Pick; featured?: boolean }) {
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
  // ── Premium UI 2.0 tier visual (VISUAL ONLY — never touches
  // published_lock_score / grade / probability / edge / eligibility) ──
  const tierVisual = getLockTierVisual(displayLock);
  // Only pick.lock_score == 100 receives the full APEX hero treatment.
  // A 99 stays firmly below APEX visually per PREMIUM_UI_2.0 spec.
  const isApexHero = tierVisual.key === "APEX";
  // Player-prop heuristic — presence of a player token in the
  // canonical selection block OR a market that plainly targets a
  // named player.  Falls back safely when unresolved (isPlayerProp=false
  // → team-logo card treatment instead of forcing a player slot).
  const _selPlayer = (pick as any).selection_v2?.selection?.player as string | null | undefined;
  const _isPlayerProp = Boolean(
    _selPlayer || (pick as any).elite_player_name || (pick as any).player_name
  );
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
        {
          // Locks-Mockup 2026-08-22 correction (+brightness lift): keep
          // card interior TRUE BLACK-ish but lift so labels/logos pop.
          backgroundColor: featured ? "#0B0E14" : "#12172A",
          borderColor: featured
            ? "rgba(255,215,0,0.85)"
            : (getSportColor(pick.sport).border || tierVisual.borderColor),
          borderWidth: featured ? 1.75 : Math.max(tierVisual.borderWidth, 1),
          shadowColor: featured
            ? "#FFD700"
            : (getSportColor(pick.sport).glow || tierVisual.glowColor),
          shadowOpacity: featured ? 0.55 : Math.max(tierVisual.glowOpacity * 0.7, 0.22),
          shadowRadius: featured ? 22 : Math.max(tierVisual.glowRadius, 10),
          shadowOffset: { width: 0, height: featured ? 8 : 4 },
        },
        isApexHero && styles.cardApexElevation,
        featured && styles.cardFeatured,
        pressed && { opacity: 0.9, transform: [{ scale: 0.985 }] },
      ]}
    >
      {/* Featured atmosphere (mockup §4/§5): VERY DARK ambient depth —
          a low-opacity radial-like gradient that reads as stadium
          lighting behind the content without tinting the surface.
          Black-first stack; only a single thin sport-colored edge
          illumination at the top so the card feels "lit" without
          coloring the interior. */}
      {featured && (
        <View pointerEvents="none" style={styles.featuredAtmosphere}>
          {/* Base — lifted black wash (was 55/85/95 — too dark) so
              logos and secondary text stay readable while ambient
              depth remains. */}
          <LinearGradient
            colors={["rgba(0,0,0,0.30)", "rgba(0,0,0,0.55)", "rgba(0,0,0,0.75)"]}
            start={{ x: 0.5, y: 0 }}
            end={{ x: 0.5, y: 1 }}
            style={StyleSheet.absoluteFill}
          />
          {/* Featured hero card always uses GOLD top-edge illumination
              regardless of sport — matches the mockup's "premium gold
              featured" treatment. Sport identity is still delivered
              via the sport tag chip + PEAK/SIGNAL colors. */}
          <LinearGradient
            colors={[
              "rgba(255,215,0,0.28)",
              "rgba(0,0,0,0)",
            ]}
            start={{ x: 0.5, y: 0 }}
            end={{ x: 0.5, y: 0.45 }}
            style={StyleSheet.absoluteFill}
          />
        </View>
      )}

      {/* Subtle top-edge gloss highlight — premium depth cue. */}
      <View
        pointerEvents="none"
        style={[
          styles.topGloss,
          {
            backgroundColor: featured
              ? "rgba(255,215,0,0.10)"
              : "rgba(255,255,255,0.05)",
          },
          (isApexHero || featured) && styles.topGlossApex,
        ]}
      />

      <View style={styles.header}>
        <View style={{ flex: 1 }}>
          <View style={styles.tagRow}>
            <View
              style={[
                styles.tag,
                {
                  backgroundColor: getSportColor(pick.sport).soft,
                  borderWidth: 1,
                  borderColor: getSportColor(pick.sport).border,
                },
              ]}
            >
              <Text
                style={[
                  styles.tagText,
                  { color: getSportColor(pick.sport).accent },
                ]}
              >
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
          {/* 🔥 STEAM MOVE badge (2026-07-27) — the steam detector flags
              this pick when the median implied prob moved ≥ threshold_pp
              in the rolling window. "toward" = sharps moving into OUR
              side (bullish), "away" = market moving against our side
              (fade signal). Rendered only for pending picks close to
              kickoff to keep the UI clean. */}
          {(pick as any).steam && (pick as any).steam.magnitude_pp >= 1.5 && (
            <View style={[
              styles.steamBadge,
              (pick as any).steam.direction === "toward" ? styles.steamBadgeToward : styles.steamBadgeAway,
            ]}>
              <Text style={styles.steamIcon}>{(pick as any).steam.direction === "toward" ? "🔥" : "🧊"}</Text>
              <Text style={[
                styles.steamText,
                { color: (pick as any).steam.direction === "toward" ? "#FCA5A5" : "#93C5FD" },
              ]}>
                {(pick as any).steam.direction === "toward" ? "STEAM" : "FADE"} · {(pick as any).steam.magnitude_pp.toFixed(1)}pp
              </Text>
            </View>
          )}
          {/* 🎯 UPSET pick (2026-07-27) — tennis math engine flipped the
              board pick from the book favorite to the dog because
              surface Elo + Sackmann form disagreed with the price. */}
          {(pick as any).is_upset_pick && (
            <View style={styles.upsetBadge}>
              <Text style={styles.upsetIcon}>🎯</Text>
              <Text style={styles.upsetText}>MODEL UPSET</Text>
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
          {_isPlayerProp ? (
            <View style={styles.playerTitleRow}>
              <PlayerIdentity
                pick={pick}
                size={32}
                variant="circle"
                ringColor={tierVisual.borderColor}
                isPlayerProp={true}
              />
              <Text style={[styles.market, styles.playerTitleText]} numberOfLines={2}>
                {pick.market}
              </Text>
            </View>
          ) : (
            <Text style={styles.market} numberOfLines={2}>{pick.market}</Text>
          )}
          {/* ── Player Prop Intelligence archetype badge (v2 engine) ── */}
          {(() => {
            const arch = (pick as any).archetype as string | undefined;
            const archDisplay = (pick as any).archetype_display as string | undefined;
            const marketFit = (pick as any).market_fit as number | undefined;
            if (!arch || !archDisplay) return null;
            const chipStyle =
              arch === "goal_scorer"     ? styles.archChipScorer :
              arch === "creator"         ? styles.archChipCreator :
              arch === "dual_threat"     ? styles.archChipDual :
              arch === "playmaker"       ? styles.archChipPlaymaker :
              styles.archChipDefault;
            const chipIcon =
              arch === "goal_scorer"     ? "⚡" :
              arch === "creator"         ? "🎯" :
              arch === "dual_threat"     ? "🔥" :
              arch === "playmaker"       ? "🔑" :
              "🏷";
            return (
              <View style={[styles.archChip, chipStyle]} testID="archetype-chip">
                <Text style={styles.archChipIcon}>{chipIcon}</Text>
                <Text style={styles.archChipText}>
                  {archDisplay.toUpperCase()}
                  {typeof marketFit === "number" && ` · FIT ${marketFit}%`}
                </Text>
              </View>
            );
          })()}
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
          {/* ── H2H compact chip (2026-02) ─────────────────────────
              Always visible when the backend attached `h2h_summary`.
              Light theme per user request "make it look good, no dark
              colors". Tapping the card exposes the full H2H block on
              the deep-dive screen. */}
          {!!(pick as any).h2h_summary && (
            <View style={styles.h2hChip} testID="h2h-chip">
              <Text style={styles.h2hChipIcon}>⚔️</Text>
              <Text style={styles.h2hChipText} numberOfLines={1}>
                {(pick as any).h2h_summary}
              </Text>
            </View>
          )}
          {/* ── Player-vs-Opponent Matchup Intelligence badge (2026-07-28) ──
              SLICE 1.6 (2026-09-02) — Now feeds `preloaded` directly from
              the Lightweight Board DTO (`pick.matchup_grade` +
              `pick.matchup_score` + related whitelisted fields) so 100+
              per-card `GET /api/picks/{id}/matchup` fetches are avoided
              on a full home slate. The badge still falls back to a
              lazy fetch when the pick payload lacks a grade. */}
          {!!pick.id && (
            <MatchupGradeBadge
              pickId={pick.id}
              preloaded={
                (pick as any).matchup_grade
                  ? {
                      supported: true,
                      matchup_grade: (pick as any).matchup_grade,
                      sample_confidence: (pick as any).matchup_sample_confidence || "medium",
                      opponent_team:
                        pick.sport === "MLB"
                          ? (pick as any).away_team_name ?? null
                          : null,
                      threshold_hit_rate: (pick as any).matchup_hit_rate ?? null,
                      sample_size: (pick as any).matchup_sample_size ?? 0,
                      player_name:
                        (pick as any).player_name ||
                        (pick as any).elite_player_name ||
                        pick.selection,
                    } as any
                  : null
              }
            />
          )}
        </View>
      </View>

      {/* Lock v3 — Stacked badge hero row: Bet Quality / Expected Win / Edge.
          Locks-Mockup 2026-08-22: distinct visual treatment per metric —
          LOCK (metallic gold), WIN (cool neutral), EDGE (neon green when
          the value is positive; red when negative). Matches mockup §6. */}
      {/* Phase 17 defect B closure (2026-09-02) — LOCK badge color
          MUST derive from the canonical tier visual, not hard-code
          gold.  Live evidence showed Lock 92 rendering gold because
          `color={COLORS.goldElite} variant="gold"` fired
          unconditionally.  Now: 85-89/90-92/93-95/96-98 use their
          tier accent, 99 PEAK uses Perklocks Purple, 100 APEX
          remains the only gold path. */}
      <View style={styles.heroBadgeRow}>
        <HeroBadge
          icon="🔒"
          value={`${Math.round(displayLock)}`}
          label="LOCK"
          sub="BET QUALITY"
          color={tierVisual.accent}
          variant={
            tierVisual.key === "APEX"        ? "gold"
            : tierVisual.key === "PEAK"      ? "purple"
            : tierVisual.key === "RARE"      ? "green"
            : tierVisual.key === "STRONG"    ? "green"
            : tierVisual.key === "ELITE"     ? "neutral"
            : /* STANDARD */                  "neutral"
          }
        />
        <HeroBadge
          icon="📊"
          value={`${pick.win_probability}%`}
          label="WIN"
          sub="EXPECTED"
          color={COLORS.textPrimary}
          variant="neutral"
        />
        <HeroBadge
          icon="⚡"
          value={
            typeof pick.edge_percent === "number" && !isNaN(pick.edge_percent)
              ? `${pick.edge_percent > 0 ? "+" : ""}${pick.edge_percent}%`
              : "—"
          }
          label="EDGE"
          sub={
            typeof pick.edge_percent === "number" && !isNaN(pick.edge_percent)
              ? "VALUE"
              : "UNAVAILABLE"
          }
          color={edgeColor}
          variant={edgeColor === COLORS.neonGreen ? "green" : "red"}
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

      {/* Confidence bar — Locks-Mockup 2026-08-22 §8: green → lime →
          gold → orange → red gradient with a bright indicator at the
          current lock position. Same underlying value (displayLock) —
          purely presentational upgrade. */}
      <View style={styles.progressTrack}>
        <LinearGradient
          colors={CONFIDENCE_GRADIENT}
          start={{ x: 0, y: 0.5 }}
          end={{ x: 1, y: 0.5 }}
          style={StyleSheet.absoluteFill}
        />
        <View
          style={[
            styles.progressIndicator,
            {
              left: `${Math.min(98, Math.max(1, displayLock))}%`,
              shadowColor: COLORS.goldGlow,
            },
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
                    {(() => {
                      // ── Block 4 μ-closure — WHY-THIS-PICK PRIORITY ──
                      // Reorder + suppress bullets so market-specific
                      // evidence surfaces first and generic filler
                      // sinks to last:
                      //   1) market-specific evidence
                      //   2) H2H sample-supported
                      //   3) matchup / recent-form / splits
                      //   4) sport-specific rationale
                      //   5) generic key_insights (LAST — suppressed
                      //      entirely when richer bullets exist)
                      const raw = ((pick as any).why_this_pick as string[]) || [];
                      const isGenericFiller = (s: string): boolean => {
                        const t = s.toLowerCase();
                        return (
                          /^\s*model\s+\d/i.test(t) ||
                          t.startsWith("pick rationale (auto)") ||
                          t.startsWith("strong model probability") ||
                          /^\s*confidence[:\s]/i.test(t) ||
                          /^\s*edge[:\s]/i.test(t) ||
                          t === "playing at home" ||
                          t === "home team" ||
                          /^\s*win probability\s*\d/i.test(t)
                        );
                      };
                      const isH2H = (s: string): boolean =>
                        /\b(h2h|head[-\s]?to[-\s]?head|record vs|record against|meetings?|vs\.?\s+opp)\b/i.test(s);
                      const isMatchup = (s: string): boolean =>
                        /\b(l5|l10|last\s+\d|form|split|vs\.?\s+lhp|vs\.?\s+rhp|home\/away|xg|xg\/90|hard\s*hit|barrel|ip\/start|k\/9|whip|era)\b/i.test(s);
                      const isMarketSpecific = (s: string): boolean =>
                        /\b(over|under|line|projected|projection|expected outs|expected strikeouts|season average|career avg|median)\b/i.test(s);
                      const priority = (s: string): number => {
                        if (isGenericFiller(s)) return 4;   // last
                        if (isMarketSpecific(s)) return 0;
                        if (isH2H(s)) return 1;
                        if (isMatchup(s)) return 2;
                        return 3;                            // sport-specific / other
                      };
                      const scored = raw.map((s, i) => ({ s, i, p: priority(s) }));
                      // Suppress generic filler entirely when there is
                      // at least one richer bullet available.
                      const hasRicher = scored.some(x => x.p < 4);
                      const filtered = hasRicher
                        ? scored.filter(x => x.p < 4)
                        : scored;
                      // Stable sort by priority then original index.
                      filtered.sort((a, b) => a.p - b.p || a.i - b.i);
                      return filtered.slice(0, 8).map((x, i) => (
                        <Text key={`why-${i}`} style={styles.whyMatchupBullet}>
                          • {x.s}
                        </Text>
                      ));
                    })()}
                  </View>
                )}

              {/* ── H2H inline block inside "Why this pick" ─────────
                  Shows the compact H2H summary and top splits without
                  requiring a tap through to the deep-dive. Rendered
                  as a light-theme pill so it stays legible on both
                  dark and light card variants. */}
              {(!!(pick as any).h2h_summary || !!(pick as any).h2h_compact) && (
                <View style={styles.h2hWhyBlock} testID="h2h-why-block">
                  <Text style={styles.h2hWhyLabel}>⚔ HEAD-TO-HEAD</Text>
                  {!!(pick as any).h2h_summary && (
                    <Text style={styles.h2hWhySummary}>
                      {(pick as any).h2h_summary}
                    </Text>
                  )}
                  {!!(pick as any).h2h_compact?.player_display && (
                    <Text style={styles.h2hWhyLine}>
                      👤 {(pick as any).h2h_compact.player_display}
                    </Text>
                  )}
                  {!!(pick as any).h2h_compact?.record && (
                    <Text style={styles.h2hWhyLine}>
                      📊 Team record: {(pick as any).h2h_compact.record}
                      {(pick as any).h2h_compact.meetings
                        ? ` over last ${(pick as any).h2h_compact.meetings} meeting${(pick as any).h2h_compact.meetings === 1 ? "" : "s"}`
                        : ""}
                    </Text>
                  )}
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

              {/* ── μ-closure: Summary suppression ────────────────────
                  `rationale.summary` may only render when it carries
                  genuinely useful sporting/matchup info OR when no
                  richer evidence exists. Generic model / edge / win-
                  probability restatements are suppressed here because
                  those numbers already live in the LOCK / WIN / EDGE
                  panel and would duplicate.  Quality > bullet count. */}
              {(() => {
                const summary = rationale!.summary;
                if (!summary) return null;
                const s = String(summary).toLowerCase();
                // Generic-filler patterns: probability / model / edge /
                // "playing at home" / "X% model win prob, +Ypp over book"
                const isGenericSummary =
                  /\bmodel\s+\d{1,3}\s*%/i.test(s) ||
                  /\bmodel\s+win\s*prob/i.test(s) ||
                  /\bwin\s*probability\s*\d/i.test(s) ||
                  /\+\d+(?:\.\d+)?\s*pp\s+over\s+book/i.test(s) ||
                  /\bconfidence\b/i.test(s) && /\d/.test(s) ||
                  /^\s*edge[:\s]/i.test(s) ||
                  s === "playing at home" ||
                  s.includes("model win probability") ||
                  /^\s*model\s+\d/i.test(s) ||
                  /^total\s+runs\s*:/i.test(s) ||
                  /^total\s+goals\s*:/i.test(s) ||
                  /pick rationale \(auto\)/i.test(s);
                // Richer evidence detection — mirrors the same heuristics
                // used by the why_this_pick priority chain above plus
                // matchup / splits / pitcher_quality / recent_form /
                // multipliers / evidence / h2h presence.
                const rawWhy = ((pick as any).why_this_pick as string[]) || [];
                const richBulletCount = rawWhy.filter((b: string) => {
                  const t = b.toLowerCase();
                  const isFiller =
                    /^\s*model\s+\d/i.test(t) ||
                    t.startsWith("pick rationale (auto)") ||
                    t.startsWith("strong model probability") ||
                    /^\s*confidence[:\s]/i.test(t) ||
                    /^\s*edge[:\s]/i.test(t) ||
                    t === "playing at home" ||
                    t === "home team" ||
                    /^\s*win probability\s*\d/i.test(t);
                  return !isFiller;
                }).length;
                const hasRicher =
                  richBulletCount > 0 ||
                  !!rationale!.matchup?.pitcher ||
                  !!rationale!.matchup?.ballpark ||
                  !!rationale!.splits ||
                  !!rationale!.pitcher_quality ||
                  !!rationale!.recent_form ||
                  !!rationale!.multipliers ||
                  (rationale!.evidence?.length ?? 0) > 0 ||
                  !!(pick as any).h2h_summary ||
                  !!(pick as any).h2h_compact;
                // Render only when: (a) not generic OR (b) generic but
                // it's the ONLY honest rationale we have.
                if (isGenericSummary && hasRicher) return null;
                return <Text style={styles.whySummary}>{String(summary)}</Text>;
              })()}

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
                                <Text style={[styles.whyPqChipLabel, { fontSize: 10, opacity: 0.9 }]}>
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
                  {(() => {
                    // Show more evidence bullets when the pick came from
                    // the Player Prop Intelligence v2 engine (which
                    // emits archetype / matchup / form context together).
                    const isV2 =
                      (rationale as any).engine === "player_prop_intelligence_v2" ||
                      (pick as any).source === "player_prop_intelligence_v2";
                    const maxLines = isV2 ? 8 : 4;
                    return rationale!.evidence!.slice(0, maxLines).map((e, i) => (
                      <Text key={`ev-${i}`} style={styles.whyBullet}>
                        {e}
                      </Text>
                    ));
                  })()}
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

              {/* Phase 8b — Alt-Line Magic Chips.  Renders only when
                  the pick is in a supported sport/market (NFL / MLB
                  / NBA / TENNIS) AND at least one alt-line candidate
                  clears the composite-score threshold.  Otherwise
                  renders nothing so the "Why" section stays clean. */}
              <AltLineChips pickId={pick.id} />
            </View>
          )}
        </View>
      )}

      {/* ── Card action row (2026-07-21) ───────────────────────────
          Two toggleable actions:
            • TRACK BET   — logs the pick as a personal bet with a
                            stake picker modal. Second tap opens an
                            "Untrack?" confirmation.
            • + PARLAY    — adds the pick to the Bet Slip (in-memory
                            multi-pick parlay builder). Second tap
                            removes it. Slip is accessible via the
                            floating pill or the Parlay tab.
          Both buttons are opt-in and independent — user can track
          and/or slip the same pick. */}
      <View style={styles.actionRow}>
        <TrackBetButton pick={pick} />
        <ParlaySlipButton pick={pick} />
      </View>
    </Pressable>
  );
}

function ParlaySlipButton({ pick }: { pick: LockPick }) {
  const { has, addPick, removePick, count } = useBetSlip();
  const inSlip = has(pick.id);

  const onTap = useCallback((e: any) => {
    e?.stopPropagation?.();
    if (inSlip) {
      removePick(pick.id);
    } else {
      const res = addPick(pick);
      if (!res.ok && Platform.OS === "web") {
        // Silent — the badge doesn't update but the reason (e.g.
        // "Slip is full") will show if user taps again. Native
        // Alert is intentionally omitted here since Alerts on web
        // are unreliable and interrupt flow.
      }
    }
  }, [inSlip, pick, addPick, removePick]);

  return (
    <Pressable
      onPress={onTap}
      hitSlop={8}
      style={({ pressed }) => [
        styles.slipBtn,
        inSlip && styles.slipBtnDone,
        pressed && { opacity: 0.7 },
      ]}
    >
      <Text style={styles.slipBtnIcon}>{inSlip ? "✓" : "+"}</Text>
      <Text style={[styles.slipBtnText, inSlip && styles.slipBtnTextDone]}>
        {inSlip ? `IN SLIP (${count})` : "PARLAY"}
      </Text>
    </Pressable>
  );
}

function TrackBetButton({ pick }: { pick: LockPick }) {
  // ── Toggle state (2026-07-21) ──────────────────────────────────────
  // Two states: NOT_TRACKED → tap opens stake picker → TRACKED → tap
  // opens untrack confirmation → NOT_TRACKED again. `trackedBetId`
  // stores the user_bet.id returned by /user/bets/track so we can
  // DELETE /user/bets/{id} on untap without a lookup.
  const [trackedBetId, setTrackedBetId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [feedback, setFeedback] = useState<string | null>(null);
  const [customStake, setCustomStake] = useState("1");
  const [stake, setStake] = useState<number | null>(null);

  // On mount: check the per-session cache to hydrate the tracked state
  // without hammering /user/bets on every card. First card triggers the
  // fetch; the rest read from cache.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const cached = trackedCache.get();
      if (cached) {
        const match = cached.find(
          (b) => b.pick_id === pick.id && b.status === "pending",
        );
        if (!cancelled && match) {
          setTrackedBetId(match.id);
          setStake(match.stake_units);
        }
        return;
      }
      try {
        const res = await api.listMyBets({ status: "pending", limit: 500 });
        trackedCache.set(res.bets as any);
        const match = res.bets.find(
          (b: any) => b.pick_id === pick.id && b.status === "pending",
        );
        if (!cancelled && match) {
          setTrackedBetId(match.id);
          setStake(match.stake_units);
        }
      } catch { /* silent — non-critical */ }
    })();
    return () => { cancelled = true; };
  }, [pick.id]);

  const openAction = useCallback((e: any) => {
    e?.stopPropagation?.();
    if (busy) return;
    if (trackedBetId) setConfirmOpen(true);
    else setPickerOpen(true);
  }, [busy, trackedBetId]);

  const submitTrack = useCallback(async (stake_units: number) => {
    setPickerOpen(false);
    setBusy(true);
    try {
      const bet = await api.trackBet({
        pick_id: pick.id,
        bet_type: "straight",
        stake_units,
      });
      setTrackedBetId(bet.id);
      setStake(stake_units);
      trackedCache.add({ id: bet.id, pick_id: pick.id, status: "pending", stake_units } as any);
      setFeedback(`✓ ${stake_units}u tracked`);
      setTimeout(() => setFeedback(null), 3000);
    } catch (err: any) {
      setFeedback(err?.message ?? "Track failed");
      setTimeout(() => setFeedback(null), 3500);
    } finally {
      setBusy(false);
    }
  }, [pick.id]);

  const submitUntrack = useCallback(async () => {
    if (!trackedBetId) return;
    setConfirmOpen(false);
    setBusy(true);
    const oldId = trackedBetId;
    try {
      await api.deleteMyBet(oldId);
      setTrackedBetId(null);
      setStake(null);
      trackedCache.remove(oldId);
      setFeedback("Untracked");
      setTimeout(() => setFeedback(null), 2000);
    } catch (err: any) {
      setFeedback(err?.message ?? "Untrack failed");
      setTimeout(() => setFeedback(null), 3000);
    } finally {
      setBusy(false);
    }
  }, [trackedBetId]);

  const oddsStr = pick.book_odds >= 0 ? `+${pick.book_odds}` : String(pick.book_odds);

  return (
    <>
      <Pressable
        onPress={openAction}
        hitSlop={8}
        style={({ pressed }) => [
          styles.trackBtn,
          trackedBetId && styles.trackBtnDone,
          pressed && { opacity: 0.7 },
        ]}
      >
        {busy ? (
          <ActivityIndicator size="small" color={COLORS.neonGreen} />
        ) : feedback ? (
          <Text style={[styles.trackBtnText, !!trackedBetId && styles.trackBtnTextDone]}>
            {feedback}
          </Text>
        ) : (
          <>
            <Text style={styles.trackBtnIcon}>{trackedBetId ? "✓" : "🎯"}</Text>
            <Text style={[styles.trackBtnText, !!trackedBetId && styles.trackBtnTextDone]}>
              {trackedBetId ? `TRACKED ${stake ?? ""}u` : "TRACK"}
            </Text>
          </>
        )}
      </Pressable>

      {/* ── Stake picker modal (SLICE 1.6 — lazy mount) ───────────
          Only mount the Modal component tree when actually needed.
          Previously every card row mounted 2 idle Modal instances,
          costing ~200 native-view slots on a 100-card board. */}
      {pickerOpen && (
        <Modal
          visible={pickerOpen}
          transparent
          animationType="fade"
          onRequestClose={() => setPickerOpen(false)}
        >
        <Pressable style={styles.modalBackdrop} onPress={() => setPickerOpen(false)}>
          <Pressable style={styles.modalSheet} onPress={(e) => e.stopPropagation()}>
            <Text style={styles.modalTitle}>Track This Bet</Text>
            <Text style={styles.modalSub} numberOfLines={2}>
              {pick.selection}
            </Text>
            <Text style={styles.modalMeta}>
              {pick.sport} · {oddsStr}
            </Text>

            <Text style={styles.modalLabel}>Choose your stake</Text>
            <View style={styles.stakeGrid}>
              {[0.25, 0.5, 1, 1.5, 2, 3].map((s) => (
                <Pressable
                  key={s}
                  onPress={() => submitTrack(s)}
                  style={({ pressed }) => [
                    styles.stakeChip,
                    pressed && { opacity: 0.7 },
                  ]}
                >
                  <Text style={styles.stakeChipText}>{s}u</Text>
                </Pressable>
              ))}
            </View>

            <View style={styles.customStakeRow}>
              <Text style={styles.customStakeLabel}>Custom</Text>
              <TextInput
                value={customStake}
                onChangeText={setCustomStake}
                keyboardType="decimal-pad"
                placeholder="1.0"
                placeholderTextColor={COLORS.textMuted}
                style={styles.customStakeInput}
                onSubmitEditing={() => {
                  const n = parseFloat(customStake);
                  if (Number.isFinite(n) && n > 0 && n <= 100) submitTrack(n);
                }}
                returnKeyType="done"
              />
              <Text style={styles.customStakeLabel}>u</Text>
              <Pressable
                style={styles.confirmBtn}
                onPress={() => {
                  const n = parseFloat(customStake);
                  if (Number.isFinite(n) && n > 0 && n <= 100) submitTrack(n);
                }}
              >
                <Text style={styles.confirmBtnText}>Log</Text>
              </Pressable>
            </View>

            <Pressable style={styles.cancelBtn} onPress={() => setPickerOpen(false)}>
              <Text style={styles.cancelBtnText}>Cancel</Text>
            </Pressable>
          </Pressable>
        </Pressable>
        </Modal>
      )}

      {/* ── Untrack confirmation modal (SLICE 1.6 — lazy mount) ─── */}
      {confirmOpen && (
        <Modal
          visible={confirmOpen}
          transparent
          animationType="fade"
          onRequestClose={() => setConfirmOpen(false)}
        >
          <Pressable style={styles.modalBackdrop} onPress={() => setConfirmOpen(false)}>
            <Pressable style={styles.modalSheet} onPress={(e) => e.stopPropagation()}>
              <Text style={styles.modalTitle}>Untrack Bet?</Text>
              <Text style={styles.modalSub} numberOfLines={2}>
                {pick.selection}
              </Text>
              <Text style={styles.modalMeta}>
                Currently at {stake ?? "?"}u — remove from My Bets?
              </Text>
              <View style={styles.confirmRow}>
                <Pressable style={styles.cancelBtn} onPress={() => setConfirmOpen(false)}>
                  <Text style={styles.cancelBtnText}>Keep</Text>
                </Pressable>
                <Pressable style={styles.destructiveBtn} onPress={submitUntrack}>
                  <Text style={styles.destructiveBtnText}>Untrack</Text>
                </Pressable>
              </View>
            </Pressable>
          </Pressable>
        </Modal>
      )}
    </>
  );
}

// ── Per-session cache of user's pending bets (2026-07-21) ────────────
// 50 cards on screen used to fire 50 GET /user/bets requests. Cache is
// populated by the FIRST mounted card; the rest read synchronously.
// Track/untrack ops update the cache in place so state stays consistent
// without a re-fetch.
const trackedCache = (() => {
  let bets: { id: string; pick_id: string; status: string; stake_units: number }[] | null = null;
  return {
    get: () => bets,
    set: (list: any) => { bets = list; },
    add: (bet: any) => { if (bets) bets.push(bet); else bets = [bet]; },
    remove: (id: string) => { if (bets) bets = bets.filter((b) => b.id !== id); },
    clear: () => { bets = null; },
  };
})();

function HeroBadge({
  icon, value, label, sub, color, variant = "neutral",
}: {
  icon: string;
  value: string;
  label: string;
  sub: string;
  color: string;
  variant?: "gold" | "green" | "red" | "neutral" | "purple";
}) {
  // Phase 17 defect B closure — added `purple` variant for 99 PEAK.
  // Gold remains RESERVED for APEX (100).  Neutral covers ELITE
  // Setup (90-92) + STANDARD (85-89) with cool premium treatment.
  const variantStyle =
    variant === "gold"   ? styles.heroBadgeGold
    : variant === "purple" ? styles.heroBadgePurple
    : variant === "green"  ? styles.heroBadgeGreen
    : variant === "red"    ? styles.heroBadgeRed
    : styles.heroBadgeNeutral;

  const glowColor =
    variant === "gold"   ? COLORS.goldGlow
    : variant === "purple" ? COLORS.perklocksPurple
    : variant === "green"  ? COLORS.neonGreen
    : variant === "red"    ? COLORS.electricBlaze
    : "#000000";

  return (
    <View
      style={[
        styles.heroBadge,
        variantStyle,
        {
          shadowColor: glowColor,
          shadowOpacity: variant === "neutral" ? 0.28 : 0.45,
          shadowRadius: variant === "neutral" ? 6 : 10,
          shadowOffset: { width: 0, height: 3 },
        },
      ]}
    >
      <View pointerEvents="none" style={styles.heroBadgeGloss} />
      <Text style={styles.heroIcon}>{icon}</Text>
      <Text style={[styles.heroValue, { color }]} numberOfLines={1}>{value}</Text>
      <Text style={[
        styles.heroLabel,
        variant === "gold"   && { color: COLORS.goldRich },
        variant === "purple" && { color: COLORS.perklocksPurpleRich },
      ]}>{label}</Text>
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
function arePropsEqual(prev: { pick: Pick; featured?: boolean }, next: { pick: Pick; featured?: boolean }): boolean {
  const a = prev.pick;
  const b = next.pick;
  // Featured flag drives border/glow/atmosphere — force re-render on flip.
  if ((prev.featured || false) !== (next.featured || false)) return false;
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
    // Base card — the surface/border/shadow are OVERRIDDEN per-pick by
    // the tier visual resolver so we don't hardcode colors here.
    backgroundColor: COLORS.surface,
    borderRadius: RADIUS.xl,
    padding: 18,
    borderWidth: 1,
    borderColor: COLORS.borderDefault,
    marginBottom: 14,
    // Slight elevation so the card feels lifted from the background,
    // even for STANDARD tier where the tier visual glow is minimal.
    elevation: 4,
    overflow: "hidden",
  },
  // Locks-Mockup 2026-08-22 §4: featured hero card gets thicker padding,
  // deeper shadow, and stronger elevation so it visibly dominates.
  cardFeatured: {
    padding: 20,
    marginBottom: 18,
    elevation: 12,
  },
  featuredAtmosphere: {
    ...StyleSheet.absoluteFillObject,
    borderRadius: RADIUS.xl,
    overflow: "hidden",
  },
  cardApex: {
    // Retained for legacy back-compat; APEX picks now drive their
    // border/glow from the tier visual resolver, but this class is
    // still applied by external screens (e.g. Rollover) that predate
    // Premium UI 2.0.  Kept intentionally subtle here.
    borderColor: "rgba(255,210,74,0.75)",
    borderWidth: 1.75,
  },
  cardApexElevation: {
    // Extra shadow depth reserved exclusively for pick.lock_score==100.
    shadowOffset: { width: 0, height: 8 },
    elevation: 12,
  },
  // ── Premium UI 2.0 surfaces ───────────────────────────────────────
  topGloss: {
    // Brighter, taller top-edge highlight — creates a visible glass
    // dimension on OLED without overpowering betting information.
    position: "absolute",
    top: 0, left: 0, right: 0,
    height: 56,
    // Slight rounded top edge inherited from card.borderRadius.
    borderTopLeftRadius: RADIUS.xl,
    borderTopRightRadius: RADIUS.xl,
  },
  topGlossApex: {
    // A stronger warmer highlight only for APEX.
    height: 78,
  },
  identitySlot: {
    position: "absolute",
    top: 12,
    right: 12,
    // On top of the top-gloss and the tag row; still non-interactive
    // (pointerEvents="none" on the slot) so it doesn't steal presses.
    zIndex: 5,
  },
  tierBadge: {
    position: "absolute",
    top: 12,
    left: 12,
    flexDirection: "row",
    alignItems: "center",
    gap: 4,
    paddingHorizontal: 8,
    paddingVertical: 3,
    borderRadius: RADIUS.pill,
    borderWidth: 1,
    zIndex: 6,
  },
  tierBadgeIcon: {
    fontSize: 11,
    lineHeight: 12,
  },
  tierBadgeText: {
    fontSize: 9.5,
    fontWeight: "900",
    letterSpacing: 1.0,
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
    flex: 1, paddingVertical: 10,
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
  // ── Stake picker modal ─────────────────────────────────────────────
  modalBackdrop: {
    flex: 1, backgroundColor: "rgba(0,0,0,0.75)",
    alignItems: "center", justifyContent: "center", padding: 24,
  },
  modalSheet: {
    width: "100%", maxWidth: 400,
    backgroundColor: "#111", borderRadius: 16,
    borderWidth: 1, borderColor: COLORS.borderDefault,
    padding: 20,
  },
  modalTitle: {
    color: COLORS.textPrimary, fontSize: 18, fontWeight: "900",
    letterSpacing: 0.3,
  },
  modalSub: {
    color: COLORS.textPrimary, fontSize: 14, fontWeight: "700",
    marginTop: 8,
  },
  modalMeta: { color: COLORS.textMuted, fontSize: 12, marginTop: 2 },
  modalLabel: {
    color: COLORS.textMuted, fontSize: 11, fontWeight: "800",
    letterSpacing: 1.2, marginTop: 20, marginBottom: 10,
  },
  stakeGrid: {
    flexDirection: "row", flexWrap: "wrap", gap: 8,
  },
  stakeChip: {
    flexBasis: "31%", flexGrow: 1,
    paddingVertical: 14, borderRadius: 10, alignItems: "center",
    backgroundColor: COLORS.neonGreen + "15",
    borderWidth: 1, borderColor: COLORS.neonGreen + "55",
  },
  stakeChipText: {
    color: COLORS.neonGreen, fontSize: 16, fontWeight: "900",
  },
  customStakeRow: {
    flexDirection: "row", alignItems: "center", gap: 8, marginTop: 16,
  },
  customStakeLabel: {
    color: COLORS.textMuted, fontSize: 12, fontWeight: "700",
  },
  customStakeInput: {
    flex: 1, height: 40, paddingHorizontal: 12,
    borderRadius: 8, borderWidth: 1, borderColor: COLORS.borderDefault,
    color: COLORS.textPrimary, fontSize: 14,
    backgroundColor: "rgba(255,255,255,0.04)",
  },
  confirmBtn: {
    paddingHorizontal: 14, paddingVertical: 10, borderRadius: 8,
    backgroundColor: COLORS.neonGreen,
  },
  confirmBtnText: {
    color: "#000", fontSize: 12, fontWeight: "900", letterSpacing: 1,
  },
  cancelBtn: {
    marginTop: 16, paddingVertical: 12, alignItems: "center",
  },
  cancelBtnText: {
    color: COLORS.textMuted, fontSize: 13, fontWeight: "700",
  },
  // ── Two-button action row + Parlay-slip toggle ─────────────────────
  actionRow: {
    flexDirection: "row", gap: 8,
    marginTop: 8, marginHorizontal: 12,
  },
  slipBtn: {
    flex: 1, paddingVertical: 10, borderRadius: 8,
    flexDirection: "row", alignItems: "center", justifyContent: "center", gap: 6,
    backgroundColor: COLORS.goldElite + "18",
    borderWidth: 1, borderColor: COLORS.goldElite + "55",
  },
  slipBtnDone: {
    backgroundColor: COLORS.goldElite + "33",
    borderColor: COLORS.goldElite,
  },
  slipBtnIcon: { color: COLORS.goldElite, fontSize: 14, fontWeight: "900" },
  slipBtnText: {
    color: COLORS.goldElite, fontSize: 12, fontWeight: "900",
    letterSpacing: 1.3,
  },
  slipBtnTextDone: { color: COLORS.goldElite },
  // Confirmation modal row for the untrack flow
  confirmRow: {
    flexDirection: "row", gap: 12, marginTop: 16, alignItems: "center",
  },
  destructiveBtn: {
    flex: 1, paddingVertical: 12, borderRadius: 8, alignItems: "center",
    backgroundColor: COLORS.electricBlaze,
  },
  destructiveBtnText: {
    color: "#000", fontSize: 13, fontWeight: "900", letterSpacing: 1.1,
  },
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
  // ── 🔥 STEAM / 🧊 FADE badge (2026-07-27) ──
  steamBadge: {
    alignSelf: "flex-start",
    flexDirection: "row",
    alignItems: "center",
    gap: 4,
    paddingHorizontal: 8,
    paddingVertical: 3,
    borderRadius: 6,
    borderWidth: 1,
    marginTop: 4,
  },
  steamBadgeToward: {
    borderColor: "rgba(252, 165, 165, 0.55)",
    backgroundColor: "rgba(252, 165, 165, 0.15)",
  },
  steamBadgeAway: {
    borderColor: "rgba(147, 197, 253, 0.55)",
    backgroundColor: "rgba(147, 197, 253, 0.15)",
  },
  steamIcon: { fontSize: 10 },
  steamText: { fontSize: 9, fontWeight: "900", letterSpacing: 0.9 },
  // ── 🎯 UPSET pick badge (2026-07-27) ──
  upsetBadge: {
    alignSelf: "flex-start",
    flexDirection: "row",
    alignItems: "center",
    gap: 4,
    paddingHorizontal: 8,
    paddingVertical: 3,
    borderRadius: 6,
    borderWidth: 1,
    borderColor: "rgba(255, 215, 0, 0.55)",
    backgroundColor: "rgba(255, 215, 0, 0.12)",
    marginTop: 4,
  },
  upsetIcon: { fontSize: 10 },
  upsetText: {
    color: "#FCD34D", fontSize: 9, fontWeight: "900", letterSpacing: 0.9,
  },
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
  // ── Player Prop Intelligence archetype chip ──
  archChip: {
    alignSelf: "flex-start",
    flexDirection: "row",
    alignItems: "center",
    gap: 4,
    paddingHorizontal: 8,
    paddingVertical: 4,
    borderRadius: 6,
    borderWidth: 1,
    marginTop: 6,
    marginBottom: 2,
  },
  archChipScorer:    { borderColor: "rgba(252,165,165,0.55)", backgroundColor: "rgba(252,165,165,0.12)" },
  archChipCreator:   { borderColor: "rgba(147,197,253,0.55)", backgroundColor: "rgba(147,197,253,0.12)" },
  archChipDual:      { borderColor: "rgba(216,180,254,0.55)", backgroundColor: "rgba(216,180,254,0.14)" },
  archChipPlaymaker: { borderColor: "rgba(134,239,172,0.55)", backgroundColor: "rgba(134,239,172,0.12)" },
  archChipDefault:   { borderColor: "rgba(180,180,180,0.55)", backgroundColor: "rgba(180,180,180,0.12)" },
  archChipIcon:  { fontSize: 11 },
  archChipText:  { color: COLORS.textPrimary, fontSize: 9.5, fontWeight: "900", letterSpacing: 0.8 },
  // ── H2H compact chip (2026-02) ─────────────────────────────
  // Light, high-contrast chip so it's ALWAYS legible on the card.
  // Per user spec: "make it look good, ensure you can see visible
  // no dark colors". Amber-tinted background, near-black text.
  h2hChip: {
    flexDirection: "row",
    alignItems: "center",
    alignSelf: "flex-start",
    marginTop: 8,
    paddingHorizontal: 10,
    paddingVertical: 5,
    borderRadius: 6,
    borderWidth: 1,
    borderColor: "rgba(251,191,36,0.75)",   // amber-400 border
    backgroundColor: "rgba(254,243,199,0.98)", // amber-100 fill
    gap: 6,
  },
  h2hChipIcon: { fontSize: 12 },
  h2hChipText: {
    color: "#78350F",                       // amber-900 for high contrast
    fontSize: 11,
    fontWeight: "800",
    letterSpacing: 0.2,
  },
  // H2H block inside "Why this pick" — light amber surface so the H2H
  // section is always highly legible inside the expanded panel.
  h2hWhyBlock: {
    marginTop: 8,
    borderRadius: 10,
    borderWidth: 1,
    borderColor: "#F59E0B",
    backgroundColor: "#FEF3C7",
    padding: 10,
    gap: 4,
  },
  h2hWhyLabel: {
    color: "#78350F",
    fontSize: 10,
    fontWeight: "900",
    letterSpacing: 1.3,
    marginBottom: 2,
  },
  h2hWhySummary: {
    color: "#1F2937",
    fontSize: 13,
    fontWeight: "800",
    lineHeight: 18,
  },
  h2hWhyLine: {
    color: "#374151",
    fontSize: 12,
    fontWeight: "600",
    lineHeight: 17,
  },
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
  // Inline player-identity row for player-prop cards ONLY.  Game markets
  // continue to rely on the existing PickEventRow (both team logos) and
  // MUST NOT render this row — enforced by the ``_isPlayerProp`` guard.
  playerTitleRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
    marginTop: 2,
  },
  playerTitleText: {
    // Absorb remaining horizontal space so long market strings wrap
    // cleanly beside the small (32dp) avatar without pushing width.
    flex: 1,
  },
  heroBadge: {
    flex: 1,
    paddingVertical: 12,
    paddingHorizontal: 8,
    borderRadius: 12,
    borderWidth: 1.4,
    alignItems: "center",
    justifyContent: "center",
    overflow: "hidden",
    elevation: 3,
  },
  // Brightness lift 2026-08-22: interiors nudged from rgba(0,0,0,0.55)
  // to rgba(20,25,40,0.55) so metric boxes read as elevated glass
  // rather than pitch-black cutouts — while still contrasting against
  // the sport-tinted card behind them.
  heroBadgeGold: {
    backgroundColor: "rgba(24,20,8,0.85)",
    borderColor: "rgba(255,215,0,0.90)",
  },
  heroBadgePurple: {
    // Phase 17 defect B closure — 99 PEAK badge; deep purple bg +
    // Perklocks Purple border.  Visually distinct from gold APEX.
    backgroundColor: "rgba(20,16,32,0.85)",
    borderColor: "rgba(185,140,255,0.90)",
  },
  heroBadgeGreen: {
    backgroundColor: "rgba(10,24,16,0.85)",
    borderColor: "rgba(77,230,138,0.85)",
  },
  heroBadgeRed: {
    backgroundColor: "rgba(24,10,10,0.85)",
    borderColor: "rgba(255,95,92,0.75)",
  },
  heroBadgeNeutral: {
    backgroundColor: "rgba(18,22,34,0.85)",
    borderColor: "rgba(255,255,255,0.30)",
  },
  heroBadgeGloss: {
    // Subtle inner top-highlight — glass panel dimensionality.
    position: "absolute",
    top: 0, left: 0, right: 0,
    height: 18,
    backgroundColor: "rgba(255,255,255,0.09)",
    borderTopLeftRadius: 12,
    borderTopRightRadius: 12,
  },
  heroIcon: { fontSize: 14, marginBottom: 2 },
  heroValue: { fontSize: 24, fontWeight: "900", letterSpacing: -0.8, marginTop: 2 },
  heroLabel: {
    color: COLORS.textPrimary,
    fontSize: 9.5,
    fontWeight: "900",
    letterSpacing: 1.5,
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
    height: 6,
    backgroundColor: "rgba(255,255,255,0.06)",
    borderRadius: 3,
    overflow: "hidden",
    position: "relative",
  },
  // Legacy — kept for backwards compat but no longer rendered.
  progressFill: { height: "100%", borderRadius: 3 },
  // Locks-Mockup 2026-08-22 §8: bright indicator dot on the gradient
  // bar showing the current lock position. Marker is 12px wide with a
  // luminous outline; glows in gold to draw the eye.
  progressIndicator: {
    position: "absolute",
    top: -3,
    width: 12,
    height: 12,
    borderRadius: 6,
    marginLeft: -6,
    backgroundColor: "#FFFFFF",
    borderWidth: 1.5,
    borderColor: COLORS.goldElite,
    shadowOpacity: 0.9,
    shadowRadius: 6,
    shadowOffset: { width: 0, height: 0 },
    elevation: 4,
  },
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
    // 2026-07-22 user report: "you can barely see anything" in SPLITS.
    // Bumped from textMuted (#71717A) → textSecondary (#A1A1AA) so the
    // "Batter avg" / "Pitcher BAA" row labels read against the dark card.
    color: COLORS.textSecondary,
    fontSize: 11,
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
    // Was textMuted → bump to lighter gray so "vs LHP / vs RHP" labels
    // are readable next to their number cells.
    color: "#B4B4BC",
    fontSize: 10,
    fontWeight: "800",
    letterSpacing: 0.6,
  },
  whySplitCell: {
    color: COLORS.textPrimary,
    fontSize: 13,
    fontWeight: "800",
    fontVariant: ["tabular-nums"],
    paddingHorizontal: 5,
    paddingVertical: 2,
    borderRadius: 3,
    minWidth: 44,
    textAlign: "center",
  },
  whySplitCellActive: {
    color: COLORS.voltBlue,
    backgroundColor: COLORS.voltBlue + "1F",
    fontWeight: "900",
  },
  whySplitHint: {
    // Was textMuted italic — hard to read. Ramp up brightness + drop
    // italic so the actionable "this matchup" summary stays legible.
    color: COLORS.textSecondary,
    fontSize: 11,
    fontStyle: "normal",
    marginTop: 4,
    lineHeight: 15,
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
    // 2026-07-22 user report: L5/L10/L20 recent-form labels + secondary
    // meta text ("5 GS · 5.60 ERA") were unreadable. Bumped from
    // textMuted (#71717A) → brighter grey so both the window tag and
    // the per-window GS/ERA meta text stay legible.
    color: "#C4C4C8",
    fontSize: 10,
    fontWeight: "900",
    letterSpacing: 0.5,
  },
  whyPqChipVal: {
    fontSize: 13,
    fontWeight: "900",
    fontVariant: ["tabular-nums"],
    marginTop: 1,
    color: COLORS.textPrimary,
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
    // 2026-07-22 user report: section labels + circled rows barely
    // visible. Nudge from textMuted → brighter grey so headers register
    // clearly against the dark card without stealing focus from data.
    color: "#B4B4BC",
    fontSize: 10,
    fontWeight: "900",
    letterSpacing: 1.3,
    marginBottom: 3,
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
