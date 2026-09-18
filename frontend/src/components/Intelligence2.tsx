/**
 * PERKLOCKS INTELLIGENCE 2.0 — PRESENTATION over the SAME normalized evidence.
 *
 * Not another evidence engine.  The header + modules are derived from (a) the
 * canonical pick payload and (b) the Historical Intelligence response lifted
 * from the mounted <HistoricalIntelligence/> card (single fetch, single
 * contract).  Data coverage is informational — it never alters probability
 * or Lock Score.  No backend/Python class names are shown to users.
 *
 * Header : photo · player/team · opponent · market · current line · coverage
 * Modules: RECENT FORM · MATCHUP · ENVIRONMENT · DISTRIBUTION
 * Expand : GAME LOGS · VS OPP · SPLITS · MORE  (the HI card tabs)
 * Game markets use the team/game equivalent of the same shell.
 */
import React, { useCallback, useMemo, useState } from "react";
import { View, Text, StyleSheet, Pressable } from "react-native";
import { Ionicons } from "@expo/vector-icons";
import { COLORS } from "@/src/theme";
import type { Pick, HistoricalIntelligenceResponse, HistoricalSummary } from "@/src/lib/api";
import { HistoricalIntelligence, type HIFailure } from "@/src/components/HistoricalIntelligence";
import { PlayerAvatar } from "@/src/components/PlayerAvatar";

const fmtOdds = (o: any) => (o == null ? "—" : Number(o) > 0 ? `+${o}` : String(o));
const pct = (v: number | null | undefined) => (v == null ? "—" : `${Math.round(v * 100)}%`);
const num = (v: number | null | undefined, d = 1) => (v == null ? "—" : Number(v).toFixed(d));

function isPlayerProp(p: Pick): boolean {
  return !!(p.player_name || p.player_meta?.display_name || (p as any).canonical_player_id);
}

type HIState = { hi: HistoricalIntelligenceResponse | null; failure: HIFailure | null };

function coverageLabel(s: HIState): { label: string; tone: "ok" | "warn" | "muted" } {
  if (s.failure && !s.hi) return { label: "HISTORY QUERY FAILED", tone: "warn" };
  const hi = s.hi;
  if (!hi) return { label: "LOADING", tone: "muted" };
  if (hi.status === "SOURCE_UNAVAILABLE") return { label: "SOURCE UNAVAILABLE", tone: "warn" };
  const n = hi.data_coverage?.total_observations ?? 0;
  if (n === 0) return { label: "NO RECENT HISTORY", tone: "warn" };
  if (n < 5) return { label: "LIMITED SAMPLE", tone: "warn" };
  if (n >= 15) return { label: "STRONG DATA", tone: "ok" };
  return { label: "MODEL + HISTORY", tone: "ok" };
}

/** Text for an empty module — honest about WHY it is empty. */
function emptyText(s: HIState, whenEmpty: string): string {
  if (s.failure && !s.hi) return "HISTORY QUERY FAILED";
  if (!s.hi) return "Loading…";
  if (s.hi.status === "SOURCE_UNAVAILABLE") return "SOURCE UNAVAILABLE";
  return whenEmpty;
}

function Module({ title, children, testID }: { title: string; children: React.ReactNode; testID?: string }) {
  return (
    <View style={styles.module} testID={testID}>
      <Text style={styles.moduleTitle}>{title}</Text>
      {children}
    </View>
  );
}

function Stat({ k, v, hint }: { k: string; v: string; hint?: string }) {
  return (
    <View style={styles.stat}>
      <Text style={styles.statK}>{k}</Text>
      <Text style={styles.statV}>{v}</Text>
      {hint ? <Text style={styles.statHint}>{hint}</Text> : null}
    </View>
  );
}

function summaryLine(s: HistoricalSummary | null | undefined, threshold: number | null): string {
  if (!s || !s.n) return "NO PRIOR SAMPLE";
  const hr = s.hit_rate == null ? "—" : `${Math.round(s.hit_rate * 100)}%`;
  return `${s.hits}/${s.n} over ${threshold ?? "line"} · ${hr} · avg ${num(s.mean)}`;
}

export function Intelligence2({ pick }: { pick: Pick }) {
  const [hi, setHi] = useState<HistoricalIntelligenceResponse | null>(null);
  const [failure, setFailure] = useState<HIFailure | null>(null);
  const [expanded, setExpanded] = useState(true);
  const onData = useCallback((d: HistoricalIntelligenceResponse) => { setHi(d); setFailure(null); }, []);
  const onFailure = useCallback((f: HIFailure | null) => setFailure(f), []);
  const player = isPlayerProp(pick);
  const hiState: HIState = { hi, failure };
  const cov = coverageLabel(hiState);

  const header = useMemo(() => {
    const ev = (pick as any).event || `${pick.away_team ?? ""} @ ${pick.home_team ?? ""}`;
    const team = pick.player_team || pick.player_meta?.team || hi?.pick?.player_team || null;
    const opponent = hi?.pick?.opponent
      || (team && pick.home_team && pick.away_team
        ? (String(team).toLowerCase() === String(pick.home_team).toLowerCase() ? pick.away_team : pick.home_team)
        : null);
    return {
      title: player ? (pick.player_name || pick.player_meta?.display_name || pick.selection) : ev,
      sub: player ? [team, opponent ? `vs ${opponent}` : null].filter(Boolean).join(" · ") : (pick.league || pick.sport),
      market: pick.market,
      line: pick.line ?? hi?.current_threshold ?? null,
      odds: pick.book_odds,
    };
  }, [pick, hi, player]);

  const env = useMemo(() => {
    const items: Array<[string, string]> = [];
    const w = (pick as any).weather;
    if (w?.label || w?.weather_label) items.push(["Weather", String(w.label ?? w.weather_label)]);
    if (pick.venue) items.push(["Venue", String(pick.venue)]);
    if (pick.home_team && pick.away_team) items.push(["Matchup", `${pick.away_team} @ ${pick.home_team}`]);
    if (pick.event_time) {
      const d = new Date(pick.event_time);
      if (!isNaN(d.getTime())) items.push(["Kickoff", d.toLocaleString(undefined, { weekday: "short", hour: "numeric", minute: "2-digit" })]);
    }
    if (pick.lineup_status) items.push(["Lineup", String(pick.lineup_status)]);
    return items;
  }, [pick]);

  return (
    <View style={styles.wrap} testID="intel2">
      {/* ── HEADER ─────────────────────────────────────────── */}
      <View style={styles.header}>
        <PlayerAvatar
          name={header.title}
          sport={pick.sport}
          headshotUrl={pick.player_media?.verified ? pick.player_media?.headshot_url : pick.player_meta?.headshot_verified ? pick.player_meta?.headshot_url : null}
          providerHeadshotUrl={pick.player_media?.headshot_url || pick.player_meta?.headshot_url || null}
          teamLogoUrl={pick.player_media?.team_logo || pick.home_meta?.logo || pick.away_meta?.logo || null}
          size={52}
          testID="intel2-avatar"
        />
        <View style={{ flex: 1 }}>
          <Text style={styles.title} numberOfLines={1}>{header.title}</Text>
          {header.sub ? <Text style={styles.sub} numberOfLines={1}>{header.sub}</Text> : null}
          <Text style={styles.market} numberOfLines={1}>
            {header.market}{header.line != null ? ` · line ${header.line}` : ""} · {fmtOdds(header.odds)}
          </Text>
        </View>
        <View style={[styles.covPill, cov.tone === "warn" && styles.covWarn, cov.tone === "ok" && styles.covOk]}>
          <Text style={styles.covTxt}>{cov.label}</Text>
        </View>
      </View>

      {/* ── MODULES ────────────────────────────────────────── */}
      <View style={styles.grid}>
        <Module title="RECENT FORM" testID="intel2-form">
          {hi && hi.sample_size > 0 ? (
            <>
              <Stat k={`HIT RATE (${hi.scope.sample_scope})`} v={pct(hi.hit_rate)} hint={`${hi.hits}/${hi.sample_size}${hi.pushes ? ` · ${hi.pushes} push` : ""}`} />
              <Stat k="AVG / MEDIAN" v={`${num(hi.mean)} / ${num(hi.median)}`} hint={hi.trend ? `trend ${hi.trend}` : undefined} />
            </>
          ) : (
            <Text style={styles.missing}>{emptyText(hiState, "NO RECENT HISTORY")}</Text>
          )}
        </Module>
        <Module title="MATCHUP" testID="intel2-matchup">
          <Text style={styles.body}>{hi ? summaryLine(hi.opponent_summary, hi.current_threshold) : emptyText(hiState, "Loading…")}</Text>
          {hi?.opponent_summary?.note ? <Text style={styles.statHint}>{hi.opponent_summary.note}</Text> : null}
        </Module>
        <Module title="ENVIRONMENT" testID="intel2-env">
          {env.length ? env.map(([k, v]) => (
            <Text key={k} style={styles.body}><Text style={styles.statK}>{k}: </Text>{v}</Text>
          )) : <Text style={styles.missing}>DATA TEMPORARILY UNAVAILABLE</Text>}
        </Module>
        <Module title="DISTRIBUTION" testID="intel2-dist">
          {hi && hi.sample_size > 0 ? (
            <>
              <Stat k="Q25 · MEDIAN · Q75" v={`${num(hi.q25)} · ${num(hi.median)} · ${num(hi.q75)}`} />
              <Stat k="STD DEV" v={num(hi.stddev, 2)} hint={hi.current_threshold != null ? `current line ${hi.current_threshold}` : undefined} />
            </>
          ) : <Text style={styles.missing}>{emptyText(hiState, "NO PRIOR SAMPLE")}</Text>}
        </Module>
      </View>

      {/* ── EXPANDABLE: GAME LOGS · VS OPP · SPLITS · MORE ─── */}
      <Pressable onPress={() => setExpanded((e) => !e)} style={styles.expandBtn} testID="intel2-expand">
        <Text style={styles.expandTxt}>{expanded ? "HIDE" : "SHOW"} GAME LOGS · VS OPP · SPLITS · MORE</Text>
        <Ionicons name={expanded ? "chevron-up" : "chevron-down"} size={16} color={COLORS.voltBlue} />
      </Pressable>
      {/* The HI card is ALWAYS mounted (hidden when collapsed) so the single
          fetch drives header + modules — one evidence contract.  A query
          failure force-expands the card so the RETRY control is reachable. */}
      <View style={expanded || (failure && !hi) ? styles.shown : styles.hidden}>
        <HistoricalIntelligence pickId={pick.id} onData={onData} onFailure={onFailure} />
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: { marginTop: 12 },
  header: {
    flexDirection: "row", alignItems: "center", gap: 12,
    padding: 12, borderRadius: 14, borderWidth: 1,
    borderColor: COLORS.borderDefault, backgroundColor: COLORS.surface,
  },
  title: { color: COLORS.textPrimary, fontSize: 15, fontWeight: "900" },
  sub: { color: COLORS.textSecondary, fontSize: 12, marginTop: 2 },
  market: { color: COLORS.textMuted, fontSize: 11.5, marginTop: 3 },
  covPill: { paddingHorizontal: 8, paddingVertical: 5, borderRadius: 8, borderWidth: 1, borderColor: COLORS.borderDefault },
  covOk: { borderColor: COLORS.goldRich },
  covWarn: { borderColor: COLORS.textMuted },
  covTxt: { color: COLORS.textSecondary, fontSize: 9, fontWeight: "900", letterSpacing: 0.8 },
  grid: { flexDirection: "row", flexWrap: "wrap", gap: 8, marginTop: 8 },
  module: {
    flexBasis: "48%", flexGrow: 1, padding: 12, borderRadius: 12, borderWidth: 1,
    borderColor: COLORS.borderDefault, backgroundColor: COLORS.surface, minHeight: 84,
  },
  moduleTitle: { color: COLORS.textMuted, fontSize: 9.5, fontWeight: "900", letterSpacing: 1.2, marginBottom: 6 },
  stat: { marginBottom: 6 },
  statK: { color: COLORS.textMuted, fontSize: 10, fontWeight: "700", letterSpacing: 0.4 },
  statV: { color: COLORS.textPrimary, fontSize: 15, fontWeight: "900", fontVariant: ["tabular-nums"] },
  statHint: { color: COLORS.textMuted, fontSize: 10.5, marginTop: 1 },
  body: { color: COLORS.textSecondary, fontSize: 12, lineHeight: 17 },
  missing: { color: COLORS.textMuted, fontSize: 11, fontWeight: "800", letterSpacing: 0.6 },
  expandBtn: {
    flexDirection: "row", alignItems: "center", justifyContent: "center", gap: 6,
    minHeight: 44, marginTop: 8,
  },
  expandTxt: { color: COLORS.voltBlue, fontSize: 11, fontWeight: "900", letterSpacing: 0.8 },
  hidden: { height: 0, overflow: "hidden", opacity: 0, pointerEvents: "none" },
  shown: { pointerEvents: "auto" },
});
