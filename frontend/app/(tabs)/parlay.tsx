import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  View, Text, StyleSheet, ScrollView, ActivityIndicator,
  RefreshControl, Pressable, Alert,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { Ionicons } from "@expo/vector-icons";
import { useRouter } from "expo-router";
import { COLORS, GRADE_COLORS } from "@/src/theme";
import { api, LineType, ParlayCard } from "@/src/lib/api";
import { LineTypeToggle } from "@/src/components/LineTypeToggle";
import { SportFilterBar } from "@/src/components/SportFilterBar";
import { PremiumHeader } from "@/src/components/PremiumHeader";
import { useParlayPreferences } from "@/src/lib/useParlayPreferences";
import {
  SportsbookId,
  formatBetSlip, copyBetSlip,
} from "@/src/lib/sportsbookLinks";
import { formatGameTime } from "@/src/lib/formatGameTime";
import { useFocusRefetch } from "@/src/lib/useFocusRefetch";
import { swrCacheRead, swrCacheWrite } from "@/src/lib/useSWR";
import { getDisplayLockRounded } from "@/src/lib/lockScore";
import { buildSlipText as buildGamblySlipText, shareSlip, saveSlipImage } from "@/src/lib/shareBetSlip";
import { SkeletonList } from "@/src/components/Skeleton";
import { EmptyState } from "@/src/components/EmptyState";

// ─── Card label → accent colour mapping ───────────────────────────────
const CARD_ACCENTS: Record<ParlayCard["label"], string> = {
  SAFE: COLORS.neonGreen,
  BALANCED: COLORS.goldElite,
  AGGRESSIVE: COLORS.electricBlaze,
};
const CARD_ICONS: Record<ParlayCard["label"], any> = {
  SAFE: "shield-checkmark",
  BALANCED: "diamond",
  AGGRESSIVE: "flame",
};
const CARD_TAGLINES: Record<ParlayCard["label"], string> = {
  SAFE: "Highest survival probability",
  BALANCED: "Best risk-reward balance",
  AGGRESSIVE: "Bigger payouts, more variance",
};

// μ-closure P3 — Parlay SWR key builder. Warm revisits with the SAME
// input tuple show the previous parlay set instantly; only a change
// to inputs (mode/legs/sport/etc.) that has never been resolved
// triggers the skeleton state.
type ParlaySnapshot = { parlays: ParlayCard[]; reason: string };
const _parlayKey = (
  n: number, m: string, s: string, lt: string,
  incl: string[], excl: string[], f: any, r: number,
  locked: string[], sMode: string, wHours: number, nonce: number,
  advSub?: string,
) =>
  `parlay|${n}|${m}|${s}|${lt}|${incl.join(",")}|${excl.join(",")}|`
  + `${JSON.stringify(f || {})}|${r}|${locked.join(",")}|`
  + `${sMode}|${wHours}|${nonce}|${advSub || ""}`;

// Health grade → colour
const GRADE_TINT: Record<string, string> = {
  A: COLORS.neonGreen,
  B: COLORS.goldElite,
  C: COLORS.voltBlue,
  D: COLORS.electricBlaze,
  F: "#FF3B5C",
};

const isCustomWithIncludes = (
  mode: "auto"|"custom"|"single",
  included: string[],
) => mode === "custom" && included.length > 0;

export default function ParlayScreen() {
  const router = useRouter();
  const { prefs, updatePrefs, hydrated } = useParlayPreferences();
  const [parlays, setParlays] = useState<ParlayCard[]>([]);
  const [reason, setReason] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Per-card refresh cursor: ephemeral — NOT persisted (resets every launch)
  const [rank, setRank] = useState(1);
  // Refresh nonce — bumped on every user-triggered REGENERATE so the
  // optimizer reshuffles seeds and emits *different* parlays for the
  // same underlying pick pool. User spec: "the app should try to build
  // a better parlay with every refresh".
  const [refreshNonce, setRefreshNonce] = useState(0);
  // Pinned leg IDs survive refresh — ephemeral session-only state
  const [lockedIds, setLockedIds] = useState<string[]>([]);

  // Convenience accessors
  const {
    mode, legs, sport, lineType, excludedSports, includedSports,
    sportMode, windowHours, filters, preferredBook,
  } = prefs;

  // 2026-08-27 TAB-ISOLATION P3: async race-protection token.  When
  // the user rapid-taps modes/sports (e.g. auto → custom → sport CFB
  // → sport NFL), an earlier /api/parlay response may arrive AFTER a
  // newer one — this token discards their `setParlays`/`setReason`
  // calls so the visible screen only reflects the current selection.
  const _reqTokenRef = useRef(0);

  const load = useCallback(
    async (n: number, m: "standard" | "high_risk" | "today_window" | "advanced", s: string, lt: LineType,
           incl: string[], excl: string[], f: any, r: number, locked: string[],
           sMode: "auto"|"custom"|"single", wHours: number, nonce: number,
           advSub?: "safer" | "ev", silent: boolean = false) => {
      const myToken = _reqTokenRef.current + 1;
      _reqTokenRef.current = myToken;
      try {
        setError(null);
        const res = await api.parlay(
          n, m, s, lt, incl, f, r, locked, sMode, wHours, excl, nonce, advSub,
        );
        // Race guard: if a newer request superseded this one, discard.
        if (myToken !== _reqTokenRef.current) return;
        const parlays = res.parlays || [];
        const reason = res.reason || "";
        setParlays(parlays);
        setReason(reason);
        // Seed the SWR cache for warm revisits.
        swrCacheWrite<ParlaySnapshot>(
          _parlayKey(n, m, s, lt, incl, excl, f, r, locked, sMode, wHours, nonce, advSub),
          { parlays, reason },
        );
      } catch (e: any) {
        if (myToken !== _reqTokenRef.current) return;
        console.warn("parlay load", e);
        if (!silent) setError(String(e?.message || "Couldn't build parlays."));
      } finally {
        if (myToken !== _reqTokenRef.current) return;
        if (!silent) setLoading(false);
        setRefreshing(false);
      }
    },
    [],
  );

  // Wait until preferences are hydrated from AsyncStorage before first fetch.
  const advancedSub = prefs.advancedSub || "ev";

  useEffect(() => {
    if (!hydrated) return;
    const key = _parlayKey(legs, mode, sport, lineType, includedSports, excludedSports,
                           filters, rank, lockedIds, sportMode, windowHours, refreshNonce,
                           mode === "advanced" ? advancedSub : undefined);
    const cached = swrCacheRead<ParlaySnapshot>(key);
    if (cached) {
      // Warm — paint instantly, silent background revalidation.
      setParlays(cached.parlays);
      setReason(cached.reason);
      setLoading(false);
      load(legs, mode, sport, lineType, includedSports, excludedSports,
           filters, rank, lockedIds, sportMode, windowHours, refreshNonce,
           mode === "advanced" ? advancedSub : undefined, true);
    } else {
      // 2026-08-27 TAB-ISOLATION P0: clear previous-selection parlays
      // BEFORE the skeleton paints so the user never sees a stale
      // parlay slate (e.g. CFB parlays) briefly under a freshly
      // selected sport (e.g. NFL).
      setParlays([]);
      setReason("");
      setLoading(true);
      load(legs, mode, sport, lineType, includedSports, excludedSports,
           filters, rank, lockedIds, sportMode, windowHours, refreshNonce,
           mode === "advanced" ? advancedSub : undefined, false);
    }
  }, [hydrated, legs, mode, sport, lineType, includedSports, excludedSports,
      filters, rank, lockedIds, sportMode, windowHours, refreshNonce, load, advancedSub]);

  // Smart refetch on focus — silent (SWR) so warm revisits never flash.
  useFocusRefetch(
    () => {
      if (!hydrated) return;
      load(legs, mode, sport, lineType, includedSports, excludedSports,
           filters, rank, lockedIds, sportMode, windowHours, refreshNonce,
           mode === "advanced" ? advancedSub : undefined, true);
    },
    [hydrated, legs, mode, sport, lineType, includedSports, excludedSports,
     filters, rank, lockedIds, sportMode, windowHours, refreshNonce, load, advancedSub],
    30_000,
  );

  const onModeChange = (m: "standard" | "high_risk" | "today_window" | "advanced") => {
    // Each mode tunes leg count + time window to match its optimizer profile:
    //   • standard      — 3 legs, 24h window (balanced default)
    //   • high_risk     — 10 legs, 72h window (10-20 leg lottery, looser floor)
    //   • today_window  — 3 legs, 5h window (same-day high-probability action)
    let legs: number;
    let windowHours: number;
    if (m === "high_risk") {
      legs = 10; windowHours = 72;
    } else if (m === "today_window") {
      legs = 3; windowHours = 5;
    } else if (m === "advanced") {
      // Advanced default: EV sub-mode, 3 legs, 24h window. Sub-mode (safer/ev)
      // is persisted independently so toggling Advanced doesn't reset it.
      legs = 3; windowHours = 24;
    } else {
      legs = 3; windowHours = 24;
    }
    updatePrefs({ mode: m, legs, windowHours });
    setRank(1);
    setLockedIds([]);
  };

  const onAdvancedSubChange = (sub: "safer" | "ev") => {
    updatePrefs({ advancedSub: sub });
    setRank(1);
  };

  // Pull-to-refresh — bumps both rank (next-best slot) AND nonce
  // (different seed). Net effect: a meaningfully different parlay.
  const onRefresh = () => {
    setRefreshing(true);
    setRank((r) => (r >= 4 ? 1 : r + 1));
    setRefreshNonce((n) => n + 1);
  };

  // Big "REGENERATE" button — bumps both nonce (different seed) AND
  // cycles to the next-best parlay rank. The combination guarantees a
  // visibly different parlay even when the underlying pick pool is
  // identical (just bumping the seed by itself can collapse back to
  // the same greedy-optimal build). Pinned legs still survive — the
  // optimizer respects lockedIds.
  const onRegenerate = useCallback(() => {
    setRefreshing(true);
    setRank((r) => (r >= 4 ? 1 : r + 1));
    setRefreshNonce((n) => n + 1);
  }, []);

  const togglePin = useCallback((legId: string) => {
    setLockedIds((prev) =>
      prev.includes(legId) ? prev.filter((id) => id !== legId) : [...prev, legId],
    );
    setRank(1);  // Pinning resets rank cursor
  }, []);

  const isHighRisk = mode === "high_risk";
  const isTodayWindow = mode === "today_window";
  const isAdvanced = mode === "advanced";
  const legOptions = isHighRisk ? [10, 15, 20] : [2, 3, 4, 5];
  const accentColor = isHighRisk ? COLORS.electricBlaze : COLORS.goldElite;
  // Sport list — kept in PARITY with the Home feed (src/theme.ts SPORTS)
  // so the user sees the same set of tabs everywhere. Order matches
  // the home feed for muscle-memory consistency.
  // NOTE: removed the legacy "mix" pseudo-chip — AUTO mode already
  // means "all sports allowed", so a separate MIX chip was redundant
  // and was the reason the sport tab row looked half-empty.
  const SPORT_OPTIONS = useMemo(
    () => [
      { id: "MLB",    label: "MLB" },
      { id: "NBA",    label: "NBA" },
      { id: "NFL",    label: "NFL" },
      { id: "Soccer", label: "SOCCER" },
      { id: "Tennis", label: "TENNIS" },
      { id: "UFC",    label: "UFC" },
    ],
    [],
  );

  return (
    <SafeAreaView style={styles.safe} edges={["top"]}>
      <PremiumHeader
        title="AUTO PARLAY"
        tagline={
          isHighRisk
            ? "HIGH RISK · 10-20 LEG LOTTERY"
            : isTodayWindow
              ? "TODAY · NEXT 1-5h · HIGH PROBABILITY"
              : isAdvanced
                ? (advancedSub === "safer" ? "ADVANCED · SAFER · HIT-RATE OPTIMIZED" : "ADVANCED · HIGH EV · LONG-RUN PROFIT")
                : "OPTIMIZER V1 · SAFE / BALANCED / AGGRESSIVE"
        }
        right={
          <Pressable
            onPress={() => router.push("/parlay-history")}
            hitSlop={10}
            testID="parlay-history-btn"
            style={({ pressed }) => [styles.histBtn, pressed && { opacity: 0.7 }]}
          >
            <Ionicons name="trophy-outline" size={16} color={COLORS.goldElite} />
            <Text style={styles.histTxt}>HISTORY</Text>
          </Pressable>
        }
      />

      {/* Regenerate button — shuffles the optimizer seed so the user gets
          a freshly-built parlay every tap. Pinned legs survive. Sits as a
          prominent CTA below the header so it can't be missed. User spec:
          "the app should try to build a better parlay with every refresh". */}
      <Pressable
        testID="parlay-regenerate"
        onPress={onRegenerate}
        disabled={refreshing}
        style={({ pressed }) => [
          styles.regenBtn,
          { borderColor: accentColor },
          (pressed || refreshing) && { opacity: 0.7 },
        ]}
      >
        <Ionicons name="sparkles" size={16} color={accentColor} />
        <Text style={[styles.regenTxt, { color: accentColor }]}>
          {refreshing ? "BUILDING…" : "REGENERATE PARLAY"}
        </Text>
        {refreshNonce > 0 && !refreshing ? (
          <Text style={[styles.regenCount, { color: accentColor }]}>
            {refreshNonce}×
          </Text>
        ) : null}
      </Pressable>


      <View style={styles.modeRow}>
        <Pressable
          testID="parlay-mode-standard"
          onPress={() => onModeChange("standard")}
          style={[styles.modeBtn, mode === "standard" && styles.modeBtnActive]}
        >
          <Text style={[styles.modeText, mode === "standard" && styles.modeTextActive]}>STANDARD</Text>
        </Pressable>
        <Pressable
          testID="parlay-mode-advanced"
          onPress={() => onModeChange("advanced")}
          style={[styles.modeBtn, isAdvanced && styles.modeBtnAdvancedActive]}
        >
          <Ionicons name="bulb" size={12} color={isAdvanced ? COLORS.bg : "#A78BFA"} />
          <Text style={[styles.modeText, isAdvanced && styles.modeTextAdvancedActive]}>ADVANCED</Text>
        </Pressable>
        <Pressable
          testID="parlay-mode-high-risk"
          onPress={() => onModeChange("high_risk")}
          style={[styles.modeBtn, isHighRisk && styles.modeBtnHighRiskActive]}
        >
          <Ionicons name="flame" size={12} color={isHighRisk ? COLORS.bg : COLORS.electricBlaze} />
          <Text style={[styles.modeText, isHighRisk && styles.modeTextHighRiskActive]}>HIGH RISK</Text>
        </Pressable>
      </View>

      {/* Advanced sub-toggle — only visible when Advanced mode active. */}
      {isAdvanced && (
        <View style={[styles.modeRow, { marginTop: 0, marginBottom: 8, gap: 6 }]}>
          <Pressable
            testID="parlay-adv-sub-safer"
            onPress={() => onAdvancedSubChange("safer")}
            style={[styles.subModeBtn, advancedSub === "safer" && styles.subModeBtnActive]}
          >
            <Ionicons name="shield-checkmark" size={11} color={advancedSub === "safer" ? COLORS.bg : COLORS.textSecondary} />
            <Text style={[styles.subModeText, advancedSub === "safer" && styles.subModeTextActive]}>SAFER · HIT RATE</Text>
          </Pressable>
          <Pressable
            testID="parlay-adv-sub-ev"
            onPress={() => onAdvancedSubChange("ev")}
            style={[styles.subModeBtn, advancedSub === "ev" && styles.subModeBtnActive]}
          >
            <Ionicons name="trending-up" size={11} color={advancedSub === "ev" ? COLORS.bg : COLORS.textSecondary} />
            <Text style={[styles.subModeText, advancedSub === "ev" && styles.subModeTextActive]}>HIGH EV · LONG-RUN PROFIT</Text>
          </Pressable>
        </View>
      )}

      <View style={styles.legSelector}>
        <Text style={styles.legLabel}>TARGET LEGS</Text>
        {legOptions.map((n) => (
          <Pressable
            key={n}
            testID={`parlay-legs-${n}`}
            onPress={() => { updatePrefs({ legs: n }); setRank(1); }}
            style={[styles.legChip, legs === n && styles.legChipActive]}
          >
            <Text style={[styles.legChipText, legs === n && styles.legChipTextActive]}>{n}</Text>
          </Pressable>
        ))}
      </View>

      {/* ── TIME WINDOW selector ──
          1-5H is now a pure window overlay — it works under ANY mode
          (Standard / Advanced / High Risk). Backend auto-applies the
          "today" guards (30-min start floor + auto-expand fallback)
          whenever the requested window is ≤8h, regardless of mode. */}
      <View style={styles.sportRowWrap}>
        <Text style={styles.legLabel}>WINDOW</Text>
        <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.sportRow}>
          {[
            { hours: 5, label: "1-5H · TODAY", isToday: true },
            { hours: 24, label: "24H" },
            { hours: 48, label: "48H" },
            { hours: 72, label: "72H" },
            { hours: 168, label: "WEEK" },
          ].map((w) => {
            const active = windowHours === w.hours;
            return (
              <Pressable
                key={w.hours}
                testID={`parlay-window-${w.hours}`}
                onPress={() => {
                  updatePrefs({ windowHours: w.hours });
                  setRank(1);
                }}
                style={[styles.sportChip, active && (w.isToday ? styles.sportChipTodayActive : styles.sportChipMixActive)]}
              >
                {w.isToday && (
                  <Ionicons name="time" size={10} color={active ? COLORS.bg : COLORS.voltBlue} />
                )}
                <Text style={[styles.sportChipText, active && styles.sportChipTextActive]}>{w.label}</Text>
              </Pressable>
            );
          })}
        </ScrollView>
      </View>

      {/* ── SPORT MODE selector (AUTO / CUSTOM / SINGLE) ── */}
      <View style={styles.sportModeRow}>
        {(["auto", "custom", "single"] as const).map((m) => {
          const active = sportMode === m;
          return (
            <Pressable
              key={m}
              testID={`parlay-sportmode-${m}`}
              onPress={() => { updatePrefs({ sportMode: m }); setRank(1); }}
              style={[styles.sportModeBtn, active && styles.sportModeBtnActive]}
            >
              <Text style={[styles.sportModeText, active && styles.sportModeTextActive]}>{m.toUpperCase()}</Text>
            </Pressable>
          );
        })}
      </View>

      {/* ── SPORT chips — ALWAYS visible. Behaviour adapts to sportMode:
           • AUTO   → chips are read-only indicators (all sports active,
                       letting the optimizer pick). Tapping a chip
                       auto-switches to SINGLE mode for that sport.
           • CUSTOM → chips toggle INCLUDE (multi-select).
           • SINGLE → chips are single-select; one sport at a time.
           Always showing the sport row fixes the user's complaint that
           the "sport tab / include tab" disappeared after picking AUTO. */}
      <View style={sportMode === "custom" ? styles.excludeRowWrap : styles.sportRowWrap}>
        <Text style={styles.legLabel}>
          {sportMode === "auto" ? "SPORT (TAP TO LOCK)"
            : sportMode === "custom" ? "INCLUDE"
            : "SPORT"}
        </Text>
        <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.sportRow}>
          {SPORT_OPTIONS.map((opt) => {
            // AUTO: every sport visually "live"; tapping locks to that sport.
            // CUSTOM: include toggle.
            // SINGLE: single-select.
            const isAuto = sportMode === "auto";
            const isCustom = sportMode === "custom";
            const isSingle = sportMode === "single";
            const includedInCustom = isCustom && includedSports.includes(opt.id);
            const customAllOn = isCustom && includedSports.length === 0; // nothing toggled = "all in"
            const chipActive =
              (isAuto)                               // all chips look active in AUTO
              || (isSingle && sport === opt.id)
              || includedInCustom
              || (customAllOn);
            const showCheck = includedInCustom;
            return (
              <Pressable
                key={`${sportMode}-${opt.id}`}
                testID={`parlay-sport-${opt.id}`}
                onPress={() => {
                  if (isAuto) {
                    // Tap a sport in AUTO → flip to SINGLE-locked on it.
                    updatePrefs({ sportMode: "single", sport: opt.id });
                  } else if (isSingle) {
                    updatePrefs({ sport: opt.id });
                  } else {
                    // CUSTOM: toggle include
                    const next = includedSports.includes(opt.id)
                      ? includedSports.filter((s) => s !== opt.id)
                      : [...includedSports, opt.id];
                    updatePrefs({ includedSports: next });
                  }
                  setRank(1);
                }}
                style={[
                  isCustom ? styles.excludeChip : styles.sportChip,
                  chipActive && (isCustom ? styles.includeChipActive : styles.sportChipActive),
                ]}
              >
                {showCheck && (
                  <Ionicons name="checkmark-circle" size={12} color={COLORS.bg} style={{ marginRight: 4 }} />
                )}
                <Text style={[
                  isCustom ? styles.excludeChipText : styles.sportChipText,
                  chipActive && (isCustom ? styles.excludeChipTextActive : styles.sportChipTextActive),
                ]}>
                  {opt.label}
                </Text>
              </Pressable>
            );
          })}
          {isCustomWithIncludes(sportMode, includedSports) && (
            <Pressable onPress={() => updatePrefs({ includedSports: [] })} style={styles.clearExcludeBtn} testID="parlay-include-clear">
              <Text style={styles.clearExcludeText}>CLEAR</Text>
            </Pressable>
          )}
        </ScrollView>
      </View>

      <LineTypeToggle value={lineType} onChange={(v) => { updatePrefs({ lineType: v }); setRank(1); }} testIDPrefix="parlay-line" />

      {sport !== "mix" && (
        <SportFilterBar sport={sport} filters={filters} onChange={(f) => { updatePrefs({ filters: f }); setRank(1); }} />
      )}

      {lockedIds.length > 0 && (
        <View style={styles.pinnedBar}>
          <Ionicons name="lock-closed" size={11} color={COLORS.goldElite} />
          <Text style={styles.pinnedTxt}>{lockedIds.length} pinned · survives refresh</Text>
          <Pressable onPress={() => { setLockedIds([]); setRank(1); }} hitSlop={6}>
            <Text style={styles.pinnedClear}>CLEAR</Text>
          </Pressable>
        </View>
      )}

      <ScrollView
        contentContainerStyle={styles.content}
        refreshControl={
          <RefreshControl
            tintColor={COLORS.textPrimary}
            refreshing={refreshing}
            onRefresh={onRefresh}
          />
        }
        showsVerticalScrollIndicator={false}
      >
        {loading ? (
          <SkeletonList count={3} testID="parlay-skeleton" />
        ) : error ? (
          <EmptyState
            variant="error"
            title="Couldn't build parlays"
            message={error}
            onRetry={() => {
              setLoading(true);
              setRefreshNonce((n) => n + 1);
            }}
            testID="parlay-error"
          />
        ) : parlays.length === 0 ? (
          <EmptyState
            icon="layers-outline"
            title="No parlay available"
            message={reason || "Not enough qualifying picks today (need Lock ≥ 88, Edge ≥ +3%, positive ROI)."}
            secondaryHint="Try relaxing filters, switching sport, or coming back later as the slate develops."
            testID="parlay-empty"
          />
        ) : (
          <>
            <Text style={styles.intro}>
              3 optimal parlays · scored on survival, edge, ROI, correlation & stability
            </Text>
            {parlays.map((card) => (
              <ParlayCardView
                key={card.label}
                card={card}
                lockedIds={lockedIds}
                preferredBook={preferredBook}
                onTogglePin={togglePin}
                onLegPress={(id) => router.push(`/pick/${id}`)}
                onSetPreferredBook={(book) => updatePrefs({ preferredBook: book })}
              />
            ))}
            <Text style={styles.disclaimer}>
              Optimizer V1: scored on survival, edge, ROI, correlation & stability.
              Quality stops early — no filler legs. Tap 🔒 to pin a leg across refreshes.
              Parlays are high-variance — bet responsibly.
            </Text>
          </>
        )}
      </ScrollView>
    </SafeAreaView>
  );
}

// ──────────────────────────────────────────────────────────────────────
// Parlay Card — one of 3 (SAFE / BALANCED / AGGRESSIVE)
// ──────────────────────────────────────────────────────────────────────
function ParlayCardView({
  card, lockedIds, preferredBook, onTogglePin, onLegPress, onSetPreferredBook,
}: {
  card: ParlayCard;
  lockedIds: string[];
  preferredBook: SportsbookId | null;
  onTogglePin: (legId: string) => void;
  onLegPress: (id: string) => void;
  onSetPreferredBook: (book: SportsbookId) => void;
}) {
  const accent = CARD_ACCENTS[card.label];
  const iconName = CARD_ICONS[card.label];
  const tagline = CARD_TAGLINES[card.label];
  const gradeColor = GRADE_TINT[card.grade] || COLORS.textPrimary;
  const [copied, setCopied] = useState(false);

  // DRY: build the same bet-slip text for both Copy and Open-Book actions.
  const buildSlipText = useCallback(() => formatBetSlip(card.legs, {
    label: card.label,
    combinedOdds: card.combined_american_odds,
    payout: card.payout_on_100,
    profit: card.profit_on_100,
    survival: card.survival_pct,
  }), [card]);

  const onCopy = useCallback(async () => {
    const ok = await copyBetSlip(buildSlipText());
    if (ok) {
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    } else {
      Alert.alert("Copy failed", "Could not copy bet slip to clipboard.");
    }
  }, [buildSlipText]);

  // ── Share-to-Gambly pipeline ────────────────────────────────────────
  const cardRef = useRef<View>(null);
  const [sharing, setSharing] = useState(false);

  const buildSharePayload = useCallback(() => ({
    legs: card.legs.map((l: any) => ({
      sport: l.sport,
      league: l.league,
      event: l.event,
      market: l.market,
      selection: l.market,
      book_odds: l.book_odds,
      bookmaker: l.bookmaker || l.book,
      confidence: typeof getDisplayLockRounded === "function"
        ? getDisplayLockRounded(l) : undefined,
    })),
    combined_odds: card.combined_american_odds,
    label: card.label,
    generated_at: new Date().toISOString(),
  }), [card]);

  const onShare = useCallback(async () => {
    if (sharing) return;
    setSharing(true);
    try {
      const text = buildGamblySlipText(buildSharePayload());
      await shareSlip(cardRef, text);
    } finally {
      setSharing(false);
    }
  }, [buildSharePayload, sharing]);

  const onSaveImage = useCallback(async () => {
    if (sharing) return;
    setSharing(true);
    try { await saveSlipImage(cardRef); } finally { setSharing(false); }
  }, [sharing]);

  return (
    <View ref={cardRef} collapsable={false} style={[styles.cardWrap, { borderColor: accent }]}>
      {/* Header */}
      <View style={styles.cardHeader}>
        <View style={{ flexDirection: "row", alignItems: "center", gap: 8, flex: 1 }}>
          <Ionicons name={iconName} size={18} color={accent} />
          <Text style={[styles.cardLabel, { color: accent }]}>{card.label}</Text>
        </View>
        <View style={[styles.gradeBadge, { borderColor: gradeColor }]}>
          <Text style={[styles.gradeText, { color: gradeColor }]}>{card.grade}</Text>
        </View>
      </View>
      <Text style={styles.cardTagline}>{tagline}</Text>

      {/* Headline payout row */}
      <View style={styles.headlineRow}>
        <View style={{ flex: 1 }}>
          <Text style={[styles.headlineNum, { color: accent }]}>{card.combined_american_odds}</Text>
          <Text style={styles.headlineLbl}>parlay odds</Text>
        </View>
        <View style={[styles.headlineDivider, { backgroundColor: accent + "33" }]} />
        <View style={{ flex: 1, alignItems: "flex-end" }}>
          <Text style={[styles.headlineNum, { color: COLORS.neonGreen }]}>
            +${card.profit_on_100.toFixed(0)}
          </Text>
          <Text style={styles.headlineLbl}>profit on $100</Text>
        </View>
      </View>

      {/* Strength score band */}
      <View style={styles.strengthBand}>
        <View style={{ flex: 1 }}>
          <Text style={styles.strengthLabel}>PARLAY STRENGTH</Text>
          <View style={styles.strengthBarBg}>
            <View
              style={[
                styles.strengthBarFg,
                { width: `${Math.min(100, card.strength_score)}%`, backgroundColor: accent },
              ]}
            />
          </View>
        </View>
        <Text style={[styles.strengthValue, { color: accent }]}>{card.strength_score.toFixed(0)}</Text>
      </View>

      {/* Stat grid */}
      <View style={styles.statGrid}>
        <MiniStat label="HIT %" value={`${card.survival_pct.toFixed(0)}%`} accent={accent} />
        <MiniStat label="EDGE" value={`${card.avg_edge_pct >= 0 ? "+" : ""}${card.avg_edge_pct.toFixed(1)}%`} accent={accent} />
        <MiniStat label="ROI" value={`${card.avg_roi_pct >= 0 ? "+" : ""}${card.avg_roi_pct.toFixed(1)}%`} accent={accent} />
        <MiniStat label="DIVERSE" value={`${card.diversification_pct.toFixed(0)}%`} accent={accent} />
        <MiniStat label="LEGS" value={String(card.leg_count)} accent={accent} />
      </View>

      {/* Why this parlay */}
      <View style={styles.reasonsBlock}>
        <Text style={styles.reasonsHeader}>WHY THIS PARLAY</Text>
        {card.reasons.map((r, i) => {
          const isPositive = r.startsWith("+");
          const isWarn = r.startsWith("!");
          const tone = isPositive
            ? COLORS.neonGreen
            : isWarn
              ? COLORS.electricBlaze
              : COLORS.textMuted;
          return (
            <Text key={i} style={[styles.reasonLine, { color: tone }]} numberOfLines={2}>
              {r}
            </Text>
          );
        })}
      </View>

      {/* Legs */}
      <Text style={styles.legsHeader}>LEGS</Text>
      {card.legs.map((leg, idx) => {
        const lockColor = (GRADE_COLORS as Record<string, string>)[leg.grade] || COLORS.textMuted;
        const pinned = lockedIds.includes(leg.id);
        return (
          <Pressable
            key={leg.id}
            testID={`parlay-leg-${card.label}-${idx}`}
            onPress={() => onLegPress(leg.id)}
            style={({ pressed }) => [
              styles.legCard,
              pinned && styles.legCardPinned,
              pressed && { opacity: 0.85 },
            ]}
          >
            <View style={[styles.legNum, { backgroundColor: accent }]}>
              <Text style={styles.legNumText}>{idx + 1}</Text>
            </View>
            <View style={{ flex: 1 }}>
              <Text style={styles.legSport}>
                {leg.sport.toUpperCase()} · {leg.league}
                {leg.elite_player ? "  ⭐" : ""}
              </Text>
              <Text style={styles.legEvent} numberOfLines={1}>{leg.event}</Text>
              {leg.event_time && (
                <Text style={styles.legTime}>{formatGameTime(leg.event_time)}</Text>
              )}
              <Text style={styles.legMarket} numberOfLines={2}>{leg.market}</Text>
              <View style={styles.legMeta}>
                <Text style={[styles.legLock, { color: lockColor }]}>Lock {getDisplayLockRounded(leg)}</Text>
                <Text style={styles.legEdge}>{leg.edge_percent >= 0 ? "+" : ""}{leg.edge_percent.toFixed(1)}% edge</Text>
                <Text style={styles.legOdds}>
                  {leg.book_odds > 0 ? `+${leg.book_odds}` : leg.book_odds}
                </Text>
              </View>
            </View>
            <Pressable
              hitSlop={10}
              onPress={(e) => { e.stopPropagation?.(); onTogglePin(leg.id); }}
              testID={`parlay-pin-${card.label}-${idx}`}
              style={styles.pinBtn}
            >
              <Ionicons
                name={pinned ? "lock-closed" : "lock-open-outline"}
                size={16}
                color={pinned ? COLORS.goldElite : COLORS.textMuted}
              />
            </Pressable>
          </Pressable>
        );
      })}

      {/* ── BET SLIP ACTION ROW — copy · save-to-history · share · save-image ── */}
      <View style={[styles.betSlipBar, { borderColor: accent + "55" }]}>
        <Pressable
          testID={`parlay-copy-${card.label}`}
          onPress={onCopy}
          style={({ pressed }) => [
            styles.actionBtn,
            { borderColor: accent, opacity: pressed ? 0.6 : 1 },
          ]}
        >
          <Ionicons
            name={copied ? "checkmark-circle" : "copy-outline"}
            size={14}
            color={copied ? COLORS.neonGreen : accent}
          />
          <Text style={[styles.actionTxt, { color: copied ? COLORS.neonGreen : accent }]}>
            {copied ? "COPIED" : "COPY"}
          </Text>
        </Pressable>

        {/* Save to History (Save-on-Tap) */}
        <Pressable
          testID={`parlay-save-${card.label}`}
          onPress={async () => {
            try {
              await api.saveParlay(card.legs as any, "standard", 10);
              Alert.alert("Saved", "Parlay added to your history. Tap History to see live status.");
            } catch (e: any) {
              Alert.alert("Save failed", String(e?.message || e));
            }
          }}
          style={({ pressed }) => [
            styles.actionBtn,
            { borderColor: COLORS.goldElite, opacity: pressed ? 0.6 : 1 },
          ]}
        >
          <Ionicons name="bookmark-outline" size={14} color={COLORS.goldElite} />
          <Text style={[styles.actionTxt, { color: COLORS.goldElite }]}>SAVE</Text>
        </Pressable>

        {/* ── Track to My Bets — 2026-07-21 ────────────────────────────
            Logs the parlay as a `bet_type="parlay"` user_bet with all
            leg IDs. Auto-settles when ALL legs are done (parlay wins iff
            every leg won, loses if any leg lost, pushes on mixed
            push+win). Feeds the /my-bets tab's personal ROI. */}
        <Pressable
          testID={`parlay-track-${card.label}`}
          onPress={async () => {
            try {
              const first = card.legs?.[0]?.id;
              const legIds = card.legs.map((l: any) => l.id).filter(Boolean);
              if (!first || legIds.length < 2) {
                Alert.alert("Nothing to track", "Parlay needs ≥ 2 legs.");
                return;
              }
              await api.trackBet({
                pick_id: first,
                bet_type: "parlay",
                stake_units: 1.0,
                parlay_legs: legIds,
              });
              Alert.alert(
                "✓ Tracked",
                `${legIds.length}-leg parlay logged at 1u. See My Bets for auto-graded status.`,
              );
            } catch (e: any) {
              Alert.alert("Track failed", String(e?.message || e));
            }
          }}
          style={({ pressed }) => [
            styles.actionBtn,
            { borderColor: COLORS.neonGreen, opacity: pressed ? 0.6 : 1 },
          ]}
        >
          <Ionicons name="wallet-outline" size={14} color={COLORS.neonGreen} />
          <Text style={[styles.actionTxt, { color: COLORS.neonGreen }]}>TRACK</Text>
        </Pressable>

        {/* Native share sheet — Gambly + any installed share target */}
        <Pressable
          testID={`parlay-share-${card.label}`}
          onPress={onShare}
          disabled={sharing}
          style={({ pressed }) => [
            styles.actionBtnPrimary,
            { backgroundColor: accent, opacity: pressed || sharing ? 0.65 : 1 },
          ]}
        >
          <Ionicons name="share-social-outline" size={14} color={COLORS.bg} />
          <Text style={[styles.actionTxt, { color: COLORS.bg }]}>SHARE</Text>
        </Pressable>

        {/* Save PNG to device gallery */}
        <Pressable
          testID={`parlay-save-image-${card.label}`}
          onPress={onSaveImage}
          disabled={sharing}
          hitSlop={6}
          style={({ pressed }) => [
            styles.iconBtn,
            { borderColor: COLORS.voltBlue, opacity: pressed || sharing ? 0.6 : 1 },
          ]}
        >
          <Ionicons name="download-outline" size={16} color={COLORS.voltBlue} />
        </Pressable>
      </View>
      <Text style={styles.betSlipHelp}>
        SHARE → opens system share sheet (Gambly, Messages, more) with PNG slip · text also copied to clipboard
      </Text>
    </View>
  );
}

function MiniStat({ label, value, accent }: { label: string; value: string; accent: string }) {
  return (
    <View style={styles.miniStat}>
      <Text style={styles.miniLabel}>{label}</Text>
      <Text style={[styles.miniValue, { color: accent }]}>{value}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: "transparent" },
  header: { paddingHorizontal: 20, paddingTop: 8, paddingBottom: 14, flexDirection: "row", justifyContent: "space-between", alignItems: "flex-end" },
  brand: { fontSize: 22, fontWeight: "900", color: COLORS.textPrimary, letterSpacing: 3 },
  tag: { fontSize: 10, color: COLORS.goldElite, fontWeight: "800", letterSpacing: 1.4, marginTop: 4 },
  histBtn: {
    flexDirection: "row", alignItems: "center", gap: 5,
    paddingHorizontal: 10, paddingVertical: 6, borderRadius: 16,
    borderWidth: 1, borderColor: COLORS.goldElite + "66",
    backgroundColor: COLORS.goldElite + "12",
  },
  histTxt: { color: COLORS.goldElite, fontSize: 10.5, fontWeight: "800", letterSpacing: 0.8 },
  refreshBadge: {
    flexDirection: "row", alignItems: "center", gap: 6,
    paddingHorizontal: 10, height: 30, borderRadius: 15, borderWidth: 1,
  },
  refreshTxt: { fontSize: 10, fontWeight: "900", letterSpacing: 1.5 },
  // Regenerate parlay CTA — big, obvious, sits between header and mode tabs.
  regenBtn: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: 8,
    marginHorizontal: 20,
    marginBottom: 12,
    paddingVertical: 12,
    borderRadius: 14,
    borderWidth: 1.5,
    backgroundColor: "rgba(255, 215, 0, 0.06)",
  },
  regenTxt: { fontSize: 13, fontWeight: "900", letterSpacing: 1.5 },
  regenCount: {
    fontSize: 11,
    fontWeight: "800",
    letterSpacing: 0.5,
    opacity: 0.6,
    marginLeft: 4,
  },
  modeRow: { flexDirection: "row", gap: 8, paddingHorizontal: 20, paddingBottom: 10 },
  modeBtn: { flex: 1, flexDirection: "row", alignItems: "center", justifyContent: "center", gap: 6, paddingVertical: 9, borderRadius: 10, borderWidth: 1, borderColor: COLORS.borderDefault },
  modeBtnActive: { backgroundColor: COLORS.goldElite, borderColor: COLORS.goldElite },
  modeBtnHighRiskActive: { backgroundColor: COLORS.electricBlaze, borderColor: COLORS.electricBlaze },
  modeBtnTodayActive: { backgroundColor: COLORS.voltBlue, borderColor: COLORS.voltBlue },
  sportChipTodayActive: { backgroundColor: COLORS.voltBlue, borderColor: COLORS.voltBlue },
  modeBtnAdvancedActive: { backgroundColor: "#A78BFA", borderColor: "#A78BFA" },
  modeTextAdvancedActive: { color: COLORS.bg },
  subModeBtn: { flex: 1, flexDirection: "row", justifyContent: "center", alignItems: "center", gap: 5, paddingVertical: 8, paddingHorizontal: 10, borderRadius: 8, borderWidth: 1, borderColor: COLORS.borderDefault, backgroundColor: "rgba(167,139,250,0.05)" },
  subModeBtnActive: { backgroundColor: "#A78BFA", borderColor: "#A78BFA" },
  subModeText: { color: COLORS.textSecondary, fontSize: 10, fontWeight: "700", letterSpacing: 0.6 },
  subModeTextActive: { color: COLORS.bg },
  modeText: { color: COLORS.textSecondary, fontSize: 11, fontWeight: "900", letterSpacing: 1.4 },
  modeTextActive: { color: COLORS.bg },
  modeTextHighRiskActive: { color: COLORS.bg },
  modeTextTodayActive: { color: COLORS.bg },
  legSelector: { flexDirection: "row", alignItems: "center", paddingHorizontal: 20, gap: 8, paddingBottom: 8 },
  legLabel: { color: COLORS.textMuted, fontSize: 10, fontWeight: "800", letterSpacing: 1.3, marginRight: 4 },
  legChip: { width: 40, height: 36, borderRadius: 18, borderWidth: 1, borderColor: COLORS.borderDefault, alignItems: "center", justifyContent: "center" },
  legChipActive: { backgroundColor: COLORS.textPrimary, borderColor: COLORS.textPrimary },
  legChipText: { color: COLORS.textSecondary, fontWeight: "800" },
  legChipTextActive: { color: COLORS.bg, fontWeight: "900" },
  sportRowWrap: { flexDirection: "row", alignItems: "center", paddingHorizontal: 20, paddingBottom: 12, gap: 8 },
  sportRow: { gap: 6, paddingRight: 20 },
  sportModeRow: {
    flexDirection: "row", gap: 6, paddingHorizontal: 20, paddingBottom: 10,
  },
  sportModeBtn: {
    flex: 1, paddingVertical: 8, borderRadius: 8,
    borderWidth: 1, borderColor: COLORS.borderDefault,
    alignItems: "center", justifyContent: "center",
  },
  sportModeBtnActive: { backgroundColor: COLORS.voltBlue, borderColor: COLORS.voltBlue },
  sportModeText: { color: COLORS.textSecondary, fontSize: 10, fontWeight: "900", letterSpacing: 1.4 },
  sportModeTextActive: { color: COLORS.bg },
  sportChip: { flexDirection: "row", alignItems: "center", paddingHorizontal: 12, height: 30, borderRadius: 15, borderWidth: 1, borderColor: COLORS.borderDefault, backgroundColor: "transparent" },
  sportChipActive: { backgroundColor: COLORS.textPrimary, borderColor: COLORS.textPrimary },
  sportChipMixActive: { backgroundColor: COLORS.voltBlue, borderColor: COLORS.voltBlue },
  sportChipText: { color: COLORS.textSecondary, fontSize: 11, fontWeight: "800", letterSpacing: 1 },
  sportChipTextActive: { color: COLORS.bg, fontWeight: "900" },
  excludeRowWrap: { flexDirection: "row", alignItems: "center", paddingHorizontal: 20, paddingBottom: 10, gap: 8 },
  excludeChip: { flexDirection: "row", alignItems: "center", paddingHorizontal: 11, height: 28, borderRadius: 14, borderWidth: 1, borderColor: COLORS.borderDefault, backgroundColor: "transparent" },
  excludeChipActive: { backgroundColor: COLORS.electricBlaze, borderColor: COLORS.electricBlaze },
  includeChipActive: { backgroundColor: COLORS.neonGreen, borderColor: COLORS.neonGreen },
  excludeChipText: { color: COLORS.textSecondary, fontSize: 10, fontWeight: "800", letterSpacing: 0.8 },
  excludeChipTextActive: { color: COLORS.bg, fontWeight: "900" },
  clearExcludeBtn: { paddingHorizontal: 10, alignSelf: "center" },
  clearExcludeText: { color: COLORS.textMuted, fontSize: 10, fontWeight: "800", letterSpacing: 1.2 },
  pinnedBar: {
    flexDirection: "row", alignItems: "center", gap: 8,
    marginHorizontal: 20, marginBottom: 8,
    paddingHorizontal: 12, paddingVertical: 8, borderRadius: 10,
    backgroundColor: "rgba(255, 215, 0, 0.08)",
    borderWidth: 1, borderColor: "rgba(255, 215, 0, 0.3)",
  },
  pinnedTxt: { flex: 1, color: COLORS.goldElite, fontSize: 11, fontWeight: "800", letterSpacing: 0.8 },
  pinnedClear: { color: COLORS.textMuted, fontSize: 10, fontWeight: "900", letterSpacing: 1.4 },
  content: { paddingHorizontal: 20, paddingTop: 8, paddingBottom: 30 },
  center: { paddingVertical: 80, alignItems: "center" },
  emptyTitle: { color: COLORS.textPrimary, fontSize: 16, fontWeight: "800", marginTop: 14 },
  emptyMsg: { color: COLORS.textMuted, fontSize: 13, marginTop: 6, textAlign: "center", paddingHorizontal: 30 },
  intro: { color: COLORS.textMuted, fontSize: 11, fontWeight: "700", letterSpacing: 0.6, marginBottom: 14, textAlign: "center" },
  cardWrap: {
    backgroundColor: COLORS.surfaceElevated ?? COLORS.surface,
    borderRadius: 18,
    padding: 16,
    marginBottom: 16,
    borderWidth: 1.5,
    // μ-closure UI3 (2026-06): Parlay cockpit polish — luminous
    // ambient shadow so each parlay card lifts off the deep-navy
    // background like a premium betting ticket.
    shadowColor: COLORS.voltBlue,
    shadowOpacity: 0.18,
    shadowRadius: 14,
    shadowOffset: { width: 0, height: 4 },
    elevation: 4,
  },
  cardHeader: {
    flexDirection: "row", alignItems: "center", justifyContent: "space-between",
    marginBottom: 4,
  },
  cardLabel: { fontSize: 14, fontWeight: "900", letterSpacing: 2.5 },
  cardTagline: { color: COLORS.textMuted, fontSize: 11, fontWeight: "700", marginBottom: 12 },
  gradeBadge: {
    minWidth: 30, height: 30, borderRadius: 8, borderWidth: 1.5,
    alignItems: "center", justifyContent: "center", paddingHorizontal: 6,
  },
  gradeText: { fontSize: 16, fontWeight: "900" },
  headlineRow: { flexDirection: "row", alignItems: "center", marginBottom: 14 },
  headlineDivider: { width: 1, height: 40, marginHorizontal: 14 },
  headlineNum: { fontSize: 28, fontWeight: "900", letterSpacing: -0.5 },
  headlineLbl: { color: COLORS.textMuted, fontSize: 9, fontWeight: "800", letterSpacing: 1.2, marginTop: 2 },
  strengthBand: { flexDirection: "row", alignItems: "center", gap: 12, marginBottom: 12 },
  strengthLabel: { color: COLORS.textMuted, fontSize: 9, fontWeight: "800", letterSpacing: 1.3, marginBottom: 4 },
  strengthBarBg: { height: 6, borderRadius: 3, backgroundColor: COLORS.borderDefault, overflow: "hidden" },
  strengthBarFg: { height: 6, borderRadius: 3 },
  strengthValue: { fontSize: 22, fontWeight: "900", minWidth: 38, textAlign: "right" },
  statGrid: { flexDirection: "row", justifyContent: "space-between", marginBottom: 12 },
  miniStat: { alignItems: "center" },
  miniLabel: { color: COLORS.textMuted, fontSize: 8, fontWeight: "800", letterSpacing: 1.2 },
  miniValue: { fontSize: 14, fontWeight: "900", marginTop: 3, letterSpacing: -0.2 },
  reasonsBlock: {
    paddingHorizontal: 12, paddingVertical: 10, marginBottom: 12,
    backgroundColor: COLORS.bg,
    borderRadius: 10,
  },
  reasonsHeader: { color: COLORS.textMuted, fontSize: 9, fontWeight: "800", letterSpacing: 1.4, marginBottom: 6 },
  reasonLine: { fontSize: 11, fontWeight: "700", lineHeight: 16, marginTop: 1 },
  legsHeader: { color: COLORS.textMuted, fontSize: 9, fontWeight: "800", letterSpacing: 1.4, marginBottom: 8 },
  legCard: {
    flexDirection: "row", gap: 12, padding: 12, marginBottom: 8,
    backgroundColor: COLORS.bg,
    borderRadius: 12,
    borderWidth: 1, borderColor: COLORS.borderDefault,
    alignItems: "center",
  },
  legCardPinned: { borderColor: COLORS.goldElite, backgroundColor: "rgba(255, 215, 0, 0.06)" },
  legNum: { width: 28, height: 28, borderRadius: 14, alignItems: "center", justifyContent: "center" },
  legNumText: { color: COLORS.bg, fontWeight: "900", fontSize: 13 },
  legSport: { color: COLORS.textMuted, fontSize: 9, fontWeight: "800", letterSpacing: 1.2 },
  legEvent: { color: COLORS.textSecondary, fontSize: 11, fontWeight: "600", marginTop: 2 },
  legTime: { color: COLORS.voltBlue, fontSize: 10, fontWeight: "800", letterSpacing: 0.4, marginTop: 2 },
  legMarket: { color: COLORS.textPrimary, fontSize: 13, fontWeight: "800", marginTop: 3, letterSpacing: -0.1 },
  legMeta: { flexDirection: "row", alignItems: "center", marginTop: 6, gap: 10 },
  legLock: { fontSize: 10, fontWeight: "900", letterSpacing: 0.8 },
  legEdge: { color: COLORS.neonGreen, fontSize: 10, fontWeight: "900", letterSpacing: 0.4 },
  legOdds: { color: COLORS.textPrimary, fontSize: 11, fontWeight: "900", marginLeft: "auto" },
  pinBtn: { padding: 6 },
  betSlipBar: {
    marginTop: 8,
    paddingTop: 12,
    borderTopWidth: 1,
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    flexWrap: "wrap",
  },
  actionBtn: {
    flexDirection: "row",
    alignItems: "center",
    gap: 5,
    paddingHorizontal: 11,
    height: 32,
    borderRadius: 16,
    borderWidth: 1.5,
  },
  actionBtnPrimary: {
    flexDirection: "row",
    alignItems: "center",
    gap: 5,
    paddingHorizontal: 14,
    height: 32,
    borderRadius: 16,
    marginLeft: "auto",
  },
  actionTxt: { fontSize: 10, fontWeight: "900", letterSpacing: 1.3 },
  iconBtn: {
    width: 32, height: 32, borderRadius: 16,
    borderWidth: 1.5,
    alignItems: "center", justifyContent: "center",
  },
  // legacy aliases (kept to avoid breaking any external refs)
  copyBtn: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    paddingHorizontal: 12,
    height: 32,
    borderRadius: 16,
    borderWidth: 1.5,
  },
  copyTxt: { fontSize: 10, fontWeight: "900", letterSpacing: 1.4 },
  bookRow: { flex: 1, flexDirection: "row", justifyContent: "flex-end", gap: 6 },
  bookBtn: {
    minWidth: 38,
    height: 32,
    paddingHorizontal: 8,
    borderRadius: 8,
    borderWidth: 1.5,
    alignItems: "center",
    justifyContent: "center",
  },
  bookTxt: { fontSize: 11, fontWeight: "900", letterSpacing: 0.8 },
  betSlipHelp: {
    color: COLORS.textMuted,
    fontSize: 10,
    fontWeight: "600",
    textAlign: "center",
    marginTop: 6,
  },
  disclaimer: { color: COLORS.textMuted, fontSize: 11, lineHeight: 17, marginTop: 12, textAlign: "center" },
});
