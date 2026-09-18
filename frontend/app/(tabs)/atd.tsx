/**
 * NFL Anytime-Touchdown (ATD) slate screen — MLB HR-style experience.
 *
 * 2026-06-11 · Restructured per user directive to mirror MLB HR PICKS:
 *  • Two view modes toggled at the top:
 *      🔥 TOP 5 TODAY  — true five highest-ranked ATD wagers across
 *                        the whole active slate.
 *      📋 BY GAME       — ATD candidates grouped by canonical matchup.
 *  • BY GAME is backed by /api/nfl/atd/by-game so the "one player =
 *    one ATD score" invariant is preserved (identical tie-breaking to
 *    the global leaderboard).
 *  • Real sportsbook ATD odds surfaced when a canonical publication
 *    row exists. Never synthesised.
 *
 * The canonical NFL ATD pipeline (nfl_atd_engine + xTD v2 + Bayesian
 * shrinkage) is UNCHANGED — this screen is a READ-only UX rewrite.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  RefreshControl, ScrollView, StyleSheet,
  Text, View, Pressable,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { Stack, useRouter } from "expo-router";
import { Ionicons } from "@expo/vector-icons";
import {
  api,
  getBackendUrl,
  type NFLAtdSlateResponse,
  type NFLAtdSlateGame,
  type NFLAtdPick,
} from "@/src/lib/api";
import { storage } from "@/src/utils/storage";
import { classifyError, ErrorKind } from "@/src/lib/errorTaxonomy";
import { COLORS } from "@/src/theme";
import { SkeletonList } from "@/src/components/Skeleton";
import { EmptyState } from "@/src/components/EmptyState";
import { safeBack } from "@/src/utils/safeBack";

type ViewMode = "topDay" | "byGame";

/** P0.4 — no client-derived grade.  Canonical published rows show the
 *  backend grade; on-demand candidates show an evidence/data badge. */
function dataBadge(pick: NFLAtdPick): string {
  if (pick.candidate_state === "PUBLISHED" && pick.grade) return String(pick.grade).toUpperCase();
  if ((pick.sample_games ?? 0) >= 8) return "STRONG DATA";
  if ((pick.sample_games ?? 0) >= 4) return "MODEL";
  return "LIMITED DATA";
}
function badgeColor(b: string): string {
  if (b.includes("LOCK")) return "rgba(74, 222, 128, 0.92)";
  if (b === "PLAYABLE" || b === "STRONG DATA") return "rgba(132, 204, 22, 0.85)";
  if (b === "MODEL") return "rgba(234, 179, 8, 0.85)";
  return "rgba(148, 163, 184, 0.7)";
}
function oppRatingChip(r: string): { bg: string; fg: string; label: string } {
  if (r === "high") return { bg: "rgba(74, 222, 128, 0.18)",  fg: "#86efac", label: "HIGH OPP" };
  if (r === "med")  return { bg: "rgba(234, 179, 8, 0.18)",   fg: "#fde68a", label: "MED OPP" };
  return                  { bg: "rgba(239, 68, 68, 0.18)",   fg: "#fca5a5", label: "LOW OPP" };
}

/** Format American odds → "+150" / "-210". Returns "" when missing. */
function fmtOdds(o?: number | null): string {
  if (o === null || o === undefined || !Number.isFinite(o)) return "";
  return o > 0 ? `+${Math.round(o)}` : `${Math.round(o)}`;
}

function PickCard({ pick, rank, hideRank = false }:
                  { pick: NFLAtdPick; rank: number; hideRank?: boolean }) {
  const grade = dataBadge(pick);
  const opp = oppRatingChip(pick.opportunity_rating);
  const oddsStr = fmtOdds(pick.book_odds);
  return (
    <View style={styles.card}>
      <View style={styles.headerRow}>
        {!hideRank && <Text style={styles.rank}>#{rank}</Text>}
        <View style={[styles.gradeChip, { backgroundColor: badgeColor(grade) }]}>
          <Text style={styles.gradeText}>{grade}</Text>
        </View>
        <View style={{ flex: 1, marginLeft: 10 }}>
          <Text style={styles.name}>
            {pick.player_name}
            {pick.is_rb_archetype ? " 🏃" : ""}
          </Text>
          <Text style={styles.sub}>
            {pick.team}{pick.opponent ? ` vs ${pick.opponent}` : ""}
            {pick.position ? ` · ${pick.position}` : ""}
          </Text>
        </View>
        <View style={{ alignItems: "flex-end" }}>
          <Text style={styles.score}>{(pick.td_probability * 100).toFixed(0)}%</Text>
          <Text style={styles.pct}>conf {(pick.confidence * 100).toFixed(0)}%</Text>
        </View>
      </View>

      <View style={styles.metaRow}>
        <View style={[styles.metaChip, { backgroundColor: opp.bg, borderColor: opp.fg + "40" }]}>
          <Text style={[styles.metaText, { color: opp.fg }]}>{opp.label}</Text>
        </View>
        {oddsStr ? (
          <View style={[styles.metaChip, styles.oddsChip]}>
            <Text style={styles.oddsText}>ATD {oddsStr}</Text>
          </View>
        ) : null}
        {typeof pick.weighted_touches_recent === "number" && (
          <View style={styles.metaChip}>
            <Text style={styles.metaText}>
              {pick.weighted_touches_recent.toFixed(1)} touches
            </Text>
          </View>
        )}
        {typeof pick.weighted_tds_recent === "number" && (
          <View style={styles.metaChip}>
            <Text style={styles.metaText}>
              {pick.weighted_tds_recent.toFixed(1)} recent TD
            </Text>
          </View>
        )}
      </View>

      {pick.reasons && pick.reasons.length > 0 && (
        <View style={styles.bullets}>
          {pick.reasons.slice(0, 5).map((b, i) => (
            <Text key={`r-${i}`} style={styles.bullet}>• {b}</Text>
          ))}
        </View>
      )}
    </View>
  );
}

// ── P4.4 · last-good ATD slate cache keyed by API origin + board_version ──
const ATD_CACHE_KEY = "atd_slate_cache_v1";
type AtdCache = { origin: string; slate: NFLAtdSlateResponse; ts: number };
let _atdMem: AtdCache | null = null;

function fmtKickoff(iso?: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  return d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

/** Compact ranked row for the By Game list (sportsbook-like). */
function CandidateRow({ pick, rank }: { pick: NFLAtdPick; rank: number }) {
  const oddsStr = fmtOdds(pick.book_odds);
  const badge = dataBadge(pick);
  return (
    <View style={styles.candRow} testID={`atd-cand-${pick.player_id || rank}`}>
      <Text style={styles.candRank}>{rank}.</Text>
      <View style={{ flex: 1 }}>
        <Text style={styles.candName} numberOfLines={1}>{pick.player_name}</Text>
        <Text style={styles.candSub} numberOfLines={1}>
          {pick.team}{pick.position ? ` · ${pick.position}` : ""}{badge ? ` · ${badge}` : ""}
        </Text>
      </View>
      <Text style={styles.candProb}>{(pick.td_probability * 100).toFixed(1)}%</Text>
      <Text style={styles.candOdds}>{oddsStr || "—"}</Text>
      {typeof pick.lock_score === "number" && pick.candidate_state === "PUBLISHED" ? (
        <Text style={styles.candLock}>L{Math.round(pick.lock_score)}</Text>
      ) : <View style={{ width: 34 }} />}
    </View>
  );
}

function GameCard({ game }: { game: NFLAtdSlateGame }) {
  const [expanded, setExpanded] = useState(false);
  const rows = expanded ? game.candidates : game.top;
  const title = game.away_team && game.home_team
    ? `${game.away_team} @ ${game.home_team}`
    : game.event;
  return (
    <View style={styles.gameGroup} testID={`atd-game-${game.canonical_event_id}`}>
      <View style={styles.gameHead}>
        <Text style={styles.gameGroupTitle} numberOfLines={1}>{title}</Text>
        <Text style={styles.gameTime}>
          {game.state === "STARTED" ? "STARTED" : fmtKickoff(game.commence_time)}
        </Text>
      </View>
      <Text style={styles.gameKicker}>TOP ATD CANDIDATES</Text>
      {rows.map((p, i) => (
        <CandidateRow key={`${game.canonical_event_id}-${p.player_id || p.player_name}-${i}`} pick={p} rank={i + 1} />
      ))}
      {game.candidates_in_game > game.top.length && (
        <Pressable onPress={() => setExpanded((e) => !e)} style={styles.viewAll} testID={`atd-viewall-${game.canonical_event_id}`}>
          <Text style={styles.viewAllTxt}>
            {expanded ? "SHOW LESS" : `VIEW ALL (${game.candidates_in_game})`}
          </Text>
        </Pressable>
      )}
    </View>
  );
}

export default function NFLAtdScreen() {
  useRouter();
  const [slate, setSlate] = useState<NFLAtdSlateResponse | null>(_atdMem?.slate ?? null);
  const [loading, setLoading] = useState(!_atdMem);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [stale, setStale] = useState<boolean>(!!_atdMem);
  const [mode, setMode] = useState<ViewMode>("topDay");
  const ctrlRef = useRef<AbortController | null>(null);

  // Restore last-good slate for THIS API origin (cold boot).
  useEffect(() => {
    if (_atdMem) return;
    (async () => {
      try {
        const raw = await storage.getItem<string>(ATD_CACHE_KEY, "");
        if (!raw) return;
        const c: AtdCache = JSON.parse(raw as any);
        let origin = ""; try { origin = getBackendUrl(); } catch {}
        if (!c?.slate || (c.origin && origin && c.origin !== origin)) return;
        if (!slate) { setSlate(c.slate); setStale(true); setLoading(false); }
      } catch { /* corrupt cache — ignore */ }
    })();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const load = useCallback(async () => {
    if (ctrlRef.current) { try { ctrlRef.current.abort(); } catch {} }
    const ctrl = new AbortController();
    ctrlRef.current = ctrl;
    try {
      // ONE slate request — Top 5 and By Game derive from the same universe.
      const res = await api.nflAtdSlate(0.10, 5, 3, { signal: ctrl.signal });
      if (ctrlRef.current !== ctrl) return;
      setSlate(res);
      setStale(false);
      setError(null);
      let origin = ""; try { origin = getBackendUrl(); } catch {}
      _atdMem = { origin, slate: res, ts: Date.now() };
      storage.setItem(ATD_CACHE_KEY, JSON.stringify(_atdMem)).catch?.(() => {});
    } catch (e: any) {
      if (classifyError(e) === ErrorKind.ABORTED_SUPERSEDED) return;
      if (ctrlRef.current !== ctrl) return;
      // Keep last-good visible; only a true no-data failure blanks the screen.
      setStale(true);
      setError(e?.message || "Failed to load ATD board");
    } finally {
      if (ctrlRef.current === ctrl) {
        setLoading(false);
        setRefreshing(false);
        ctrlRef.current = null;
      }
    }
  }, []);

  useEffect(() => {
    load();
    return () => { if (ctrlRef.current) { try { ctrlRef.current.abort(); } catch {} } };
  }, [load]);

  const onRefresh = useCallback(() => {
    setRefreshing(true);
    load();
  }, [load]);

  const topFive: NFLAtdPick[] = useMemo(() => slate?.top5 ?? [], [slate]);
  const games: NFLAtdSlateGame[] = useMemo(() => slate?.games ?? [], [slate]);

  const summary = useMemo(() => {
    if (!slate) return "";
    if (mode === "topDay") return `Top ${topFive.length} of ${slate.universe_count}`;
    return `${games.length}g · ${slate.universe_count} candidates`;
  }, [mode, topFive.length, games.length, slate]);

  const emptyForCurrentMode =
    (mode === "topDay" && topFive.length === 0) ||
    (mode === "byGame" && games.length === 0);
  const hardError = !!error && !slate;

  return (
    <SafeAreaView style={styles.safe} edges={["top"]}>
      <Stack.Screen options={{ headerShown: false }} />
      <View style={styles.header}>
        <Pressable
          onPress={() => safeBack()}
          hitSlop={12}
          style={styles.backBtn}
          testID="atd-back"
        >
          <Ionicons name="chevron-back" size={22} color={COLORS.textPrimary} />
        </Pressable>
        <Ionicons name="american-football-outline" size={20} color={COLORS.goldElite} />
        <Text style={styles.headerTitle}>NFL ANYTIME TOUCHDOWNS</Text>
        {!!slate && <Text style={styles.headerSub}>{summary}</Text>}
      </View>

      <View style={styles.toggleRow}>
        <Pressable
          onPress={() => setMode("topDay")}
          style={[styles.toggleBtn, mode === "topDay" && styles.toggleBtnActive]}
          testID="atd-toggle-top"
        >
          <Text style={[styles.toggleText, mode === "topDay" && styles.toggleTextActive]}>
            🔥 Top 5 Today
          </Text>
        </Pressable>
        <Pressable
          onPress={() => setMode("byGame")}
          style={[styles.toggleBtn, mode === "byGame" && styles.toggleBtnActive]}
          testID="atd-toggle-game"
        >
          <Text style={[styles.toggleText, mode === "byGame" && styles.toggleTextActive]}>
            📋 By Game
          </Text>
        </Pressable>
      </View>

      {/* P4.4 / P0.11 — stale-while-revalidate strip; never a full-screen error
          while last-good data exists. */}
      {!!slate && (stale || refreshing) && (
        <Pressable onPress={onRefresh} style={styles.staleStrip} testID="atd-stale-strip">
          <Text style={styles.staleTxt}>
            {refreshing ? "UPDATING" : error ? "OFFLINE · SHOWING LAST GOOD" : "STALE"}
            {" · BOARD "}{String(slate.board_version).slice(0, 8)}
            {slate.data_as_of ? ` · AS OF ${fmtKickoff(slate.data_as_of)}` : ""}
          </Text>
        </Pressable>
      )}

      {loading && !slate ? (
        <View style={styles.scroll} testID="atd-skeleton">
          <SkeletonList count={4} />
        </View>
      ) : hardError ? (
        <ScrollView
          contentContainerStyle={styles.scroll}
          refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={COLORS.voltBlue} />}
        >
          <EmptyState
            variant="error"
            title="Couldn't load ATD board"
            message={error || ""}
            onRetry={onRefresh}
            secondaryHint="Pull down to retry manually."
            testID="atd-error"
          />
        </ScrollView>
      ) : emptyForCurrentMode ? (
        <ScrollView
          contentContainerStyle={styles.scroll}
          refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={COLORS.voltBlue} />}
        >
          <EmptyState
            icon="american-football-outline"
            title="No qualifying ATD picks"
            message={
              mode === "topDay"
                ? "The slate is quiet — no players clear the ATD model floor yet."
                : "No games with qualifying ATD candidates on today's slate."
            }
            secondaryHint="Check back closer to gameday — the slate refreshes automatically."
            testID="atd-empty"
          />
        </ScrollView>
      ) : (
        <ScrollView
          style={{ flex: 1 }}
          contentContainerStyle={styles.scroll}
          refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={COLORS.voltBlue} />}
        >
          {mode === "topDay" ? (
            <>
              <Text style={styles.sectionTitle}>🔥 TOP 5 TODAY</Text>
              <Text style={styles.intro}>
                Highest-ranked legitimate ATD candidates across the ENTIRE slate —
                no per-game quota, real sportsbook odds when available.
              </Text>
              {topFive.map((p, i) => (
                <PickCard key={`top-${p.player_id}-${i}`} pick={p} rank={i + 1} />
              ))}
            </>
          ) : (
            <>
              <Text style={styles.sectionTitle}>📋 BY GAME</Text>
              <Text style={styles.intro}>
                Ranked within each matchup. One player = one ATD probability
                across every view — same universe as Top 5.
              </Text>
              {games.map((g) => (
                <GameCard key={g.canonical_event_id || g.event} game={g} />
              ))}
            </>
          )}
          <View style={{ height: 32 }} />
        </ScrollView>
      )}
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  staleStrip: {
    marginHorizontal: 14, marginBottom: 8, paddingHorizontal: 12, paddingVertical: 7,
    borderRadius: 8, borderWidth: 1, borderColor: COLORS.borderDefault, backgroundColor: COLORS.surface,
  },
  staleTxt: { color: COLORS.goldRich, fontSize: 10.5, fontWeight: "800", letterSpacing: 0.8 },
  gameHead: { flexDirection: "row", alignItems: "center", justifyContent: "space-between" },
  gameTime: { color: COLORS.textMuted, fontSize: 11, fontWeight: "700", marginLeft: 8 },
  gameKicker: { color: COLORS.textMuted, fontSize: 9.5, fontWeight: "800", letterSpacing: 1.2, marginTop: 6, marginBottom: 4 },
  candRow: { flexDirection: "row", alignItems: "center", paddingVertical: 8, borderTopWidth: StyleSheet.hairlineWidth, borderTopColor: COLORS.borderDefault, gap: 8 },
  candRank: { color: COLORS.textMuted, fontSize: 12, fontWeight: "800", width: 20 },
  candName: { color: COLORS.textPrimary, fontSize: 13.5, fontWeight: "700" },
  candSub: { color: COLORS.textMuted, fontSize: 10.5, marginTop: 1 },
  candProb: { color: COLORS.goldElite, fontSize: 13.5, fontWeight: "900", width: 56, textAlign: "right", fontVariant: ["tabular-nums"] },
  candOdds: { color: COLORS.textSecondary, fontSize: 12, fontWeight: "700", width: 48, textAlign: "right", fontVariant: ["tabular-nums"] },
  candLock: { color: COLORS.voltBlue, fontSize: 11, fontWeight: "800", width: 34, textAlign: "right" },
  viewAll: { alignSelf: "flex-start", paddingVertical: 8, paddingHorizontal: 4, minHeight: 44, justifyContent: "center" },
  viewAllTxt: { color: COLORS.voltBlue, fontSize: 11.5, fontWeight: "800", letterSpacing: 0.8 },
  safe: { flex: 1, backgroundColor: COLORS.deepBlack },
  header: {
    flexDirection: "row", alignItems: "center", paddingHorizontal: 14,
    paddingVertical: 12, borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: "#1e293b",
  },
  backBtn: { marginRight: 8, padding: 4 },
  headerTitle: {
    color: COLORS.textPrimary, fontSize: 17, fontWeight: "800",
    marginLeft: 8, letterSpacing: 0.5, flex: 1,
  },
  headerSub: { color: COLORS.textMuted, fontSize: 12 },
  toggleRow: {
    flexDirection: "row",
    paddingHorizontal: 14,
    paddingVertical: 10,
    gap: 8,
  },
  toggleBtn: {
    flex: 1,
    paddingVertical: 9,
    borderRadius: 10,
    borderWidth: 1,
    borderColor: "#1e293b",
    backgroundColor: "rgba(15,23,42,0.6)",
    alignItems: "center",
  },
  toggleBtnActive: {
    borderColor: COLORS.goldElite,
    backgroundColor: "rgba(255,215,0,0.10)",
  },
  toggleText: {
    color: COLORS.textMuted, fontSize: 13, fontWeight: "700",
    letterSpacing: 0.4,
  },
  toggleTextActive: { color: COLORS.textPrimary },
  scroll: { padding: 12, paddingBottom: 24 },
  sectionTitle: {
    color: COLORS.textPrimary, fontSize: 14, fontWeight: "800",
    letterSpacing: 1.0, marginTop: 8, marginBottom: 4,
  },
  intro: { color: COLORS.textMuted, fontSize: 12, marginBottom: 8 },
  gameGroup: { marginTop: 10 },
  gameGroupTitle: {
    color: COLORS.textPrimary, fontSize: 13, fontWeight: "700",
    letterSpacing: 0.4, marginBottom: 6, marginTop: 6,
  },
  card: {
    backgroundColor: "rgba(15,23,42,0.65)",
    borderRadius: 14, padding: 12,
    marginBottom: 10, borderWidth: 1, borderColor: "#1e293b",
  },
  headerRow: { flexDirection: "row", alignItems: "center" },
  rank: {
    color: COLORS.textPrimary, fontSize: 15, fontWeight: "800",
    width: 34,
  },
  gradeChip: {
    paddingHorizontal: 8, paddingVertical: 3, borderRadius: 8,
    minWidth: 32, alignItems: "center",
  },
  gradeText: { color: "#0f172a", fontSize: 13, fontWeight: "800" },
  name: { color: COLORS.textPrimary, fontSize: 15, fontWeight: "700" },
  sub: { color: COLORS.textMuted, fontSize: 12 },
  score: { color: COLORS.goldElite, fontSize: 20, fontWeight: "800" },
  pct: { color: COLORS.textMuted, fontSize: 11 },
  metaRow: { flexDirection: "row", flexWrap: "wrap", marginTop: 8 },
  metaChip: {
    paddingHorizontal: 8, paddingVertical: 3, borderRadius: 6,
    backgroundColor: "rgba(30,41,59,0.85)",
    borderWidth: StyleSheet.hairlineWidth, borderColor: "#334155",
    marginRight: 6, marginBottom: 6,
  },
  metaText: { color: COLORS.textPrimary, fontSize: 11, fontWeight: "600" },
  oddsChip: {
    backgroundColor: "rgba(255,215,0,0.12)",
    borderColor: "rgba(255,215,0,0.45)",
  },
  oddsText: {
    color: COLORS.goldElite,
    fontSize: 11,
    fontWeight: "800",
    letterSpacing: 0.4,
  },
  bullets: { marginTop: 8 },
  bullet: { color: COLORS.textMuted, fontSize: 12, marginBottom: 3 },
});
