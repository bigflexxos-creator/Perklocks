/**
 * HistoricalIntelligence — Universal on-demand historical actuals card.
 *
 * Session 5 · 2026-09-17.  ONE reusable component driving every sport
 * through the universal `/api/picks/{id}/historical-intelligence`
 * contract.  Tabs: GAME LOGS · VS OPP · SPLITS · DISTRIBUTION.  All
 * hit/miss/quantile numbers are computed against TODAY'S EXACT
 * sportsbook line by the backend.
 *
 * Contracts respected:
 *   - MISSING stays MISSING.  Never renders 0/0 or 0% for absent data.
 *   - Toggle changes trigger a fresh backend fetch (real filters).
 *   - Historical Intelligence is INDEPENDENT of Lock Score / Win Exp.
 *   - The Locks lite hot path is never touched by this component.
 */
import React, { useCallback, useEffect, useState } from "react";
import {
  View, Text, StyleSheet, Pressable, ScrollView, ActivityIndicator,
} from "react-native";
import Svg, { Circle, Line } from "react-native-svg";
import { COLORS } from "@/src/theme";
import { api, getBackendUrl, HistoricalIntelligenceResponse, HistoricalObservation } from "@/src/lib/api";
import { swrCacheRead, swrCacheWrite } from "@/src/lib/useSWR";
import { historicalIntelligenceKey } from "@/src/lib/serverStateKeys";

type SampleScope = "L5" | "L10" | "L20" | "SEASON";
type VenueScope  = "ALL" | "HOME" | "AWAY";
type Tab = "logs" | "vsopp" | "splits" | "distribution";

const SAMPLE_SCOPES: SampleScope[] = ["L5", "L10", "L20", "SEASON"];

function isTennis(sport?: string) { return sport === "Tennis"; }
function fmtPct(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `${Math.round(v * 100)}%`;
}
function fmtNum(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return Number(v).toFixed(digits);
}
function fmtDate(iso?: string | null): string {
  if (!iso) return "—";
  // ── §9 mobile readability — always render date WITH year so users
  // can distinguish current-season games from prior-year history.
  // Format: "YY M/D" (e.g. "24 9/14") — 6-8 chars, fits column width
  // without hiding the year like the previous "MM/DD" did.
  const m = iso.match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (!m) return iso.slice(0, 10);
  const yy = m[1].slice(2);
  const mm = String(parseInt(m[2], 10));
  const dd = String(parseInt(m[3], 10));
  return `${yy} ${mm}/${dd}`;
}

// ─── Sport/market → game-log column config ─────────────────────────
type LogColumn = {
  key: string;
  label: string;
  width?: number;
  render: (o: HistoricalObservation) => string;
};

function _shortTeam(name?: string | null): string {
  // ── §9 mobile readability — surgical name projection that
  // preserves team identity ("Illinois Fighting Illini" stays
  // recognisable as "Illinois", never "Illinois Fig").  Strategy:
  //   1. Return the primary school name (drop mascot suffix) when the
  //      combined name is long.
  //   2. Preserve compact names verbatim (e.g. "TCU", "USC", "LSU").
  //   3. Never truncate mid-word — that produced "Illinois Fig",
  //      "Nebraska Cor", "Maryland Ter", "Western Illi".
  if (!name) return "—";
  const n = name.trim();
  if (n.length <= 12) return n;
  // Common NCAA / NFL mascot suffixes — trim greedily from the end
  // and re-check the school-only length.
  const MASCOTS = [
    "Fighting Illini", "Thundering Herd", "Crimson Tide",
    "Fighting Irish", "Horned Frogs", "Tar Heels", "Blue Devils",
    "Yellow Jackets", "Demon Deacons", "Wolf Pack", "Wolverines",
    "Volunteers", "Commodores", "Golden Bears", "Golden Gophers",
    "Golden Hurricane", "Golden Eagles", "Boilermakers", "Cornhuskers",
    "Buckeyes", "Wildcats", "Bulldogs", "Longhorns", "Sooners",
    "Aggies", "Gators", "Seminoles", "Hurricanes", "Gamecocks",
    "Razorbacks", "Rebels", "Cougars", "Buffaloes", "Trojans",
    "Bruins", "Spartans", "Hoosiers", "Badgers", "Hawkeyes",
    "Cyclones", "Jayhawks", "Terrapins", "Panthers", "Bearcats",
    "Broncos", "Ducks", "Beavers", "Cavaliers", "Chanticleers",
    "Mountaineers", "Owls", "Bobcats", "Warhawks", "Tigers", "Jaguars",
    "Sun Devils", "Mustangs", "Red Raiders", "Miners", "Zips",
    "Rockets", "Blazers", "Hilltoppers", "Blue Raiders", "Sycamores",
    "Redbirds", "Flames", "Cardinals", "Rams", "Ravens", "Falcons",
    "Chiefs", "Chargers", "Bengals", "Steelers", "Browns", "Colts",
    "Titans", "Texans", "Cowboys", "Giants", "Eagles", "Redskins",
    "Commanders", "Vikings", "Packers", "Lions", "Bears",
    "49ers", "Seahawks", "Cardinals", "Bills", "Dolphins", "Patriots",
    "Jets", "Buccaneers", "Saints",
    // Soccer club suffixes
    "FC", "United", "City", "Athletic", "Rovers", "Wanderers",
    "Sporting Club",
  ];
  for (const m of MASCOTS) {
    if (n.endsWith(" " + m)) {
      const trimmed = n.slice(0, n.length - m.length - 1).trim();
      if (trimmed.length <= 14 && trimmed.length > 0) return trimmed;
    }
  }
  // Long school name (no mascot suffix). Use first 12 chars but ONLY
  // at a word boundary so we don't emit "Illinois Fig".
  const cutAt12 = n.slice(0, 12);
  const lastSpace = cutAt12.lastIndexOf(" ");
  if (lastSpace >= 6) return cutAt12.slice(0, lastSpace);
  // No safe boundary → return full name; the UI row will truncate
  // gracefully via numberOfLines/ellipsizeMode rather than destructive
  // mid-word slice.
  return n;
}

function logColumnsFor(sport: string, family: string): LogColumn[] {
  const dateCol: LogColumn = { key: "date", label: "DATE", width: 62,
    render: (o) => fmtDate(o.date) };
  const oppCol: LogColumn = { key: "opp", label: "OPP", width: 100,
    render: (o) => _shortTeam(o.opponent_name) };
  const haCol: LogColumn = { key: "ha", label: "H/A", width: 34,
    render: (o) => (o.home_away || "—").toUpperCase().slice(0, 1) };
  const actualCol = (label: string, digits = 0): LogColumn => ({
    key: "actual", label, width: 52,
    render: (o) => fmtNum(o.actual, digits),
  });
  const resultCol: LogColumn = { key: "result", label: "", width: 42,
    render: (o) => o.result || "" };

  // Sport-specific rich columns.
  if (sport === "NFL") {
    if (family === "pass_yds") return [
      dateCol, oppCol, haCol, actualCol("YDS"),
      { key: "cmpAtt", label: "CMP/ATT", width: 66,
        render: (o) => `${o.context?.completions ?? "—"}/${o.context?.attempts ?? "—"}` },
      { key: "td", label: "TD", width: 34,
        render: (o) => String(o.context?.pass_tds ?? "—") },
      resultCol,
    ];
    if (family === "rec_yds" || family === "receptions") return [
      dateCol, oppCol, haCol,
      { key: "rec", label: "REC", width: 40,
        render: (o) => String(o.context?.receptions ?? o.actual ?? "—") },
      { key: "yds", label: "YDS", width: 46,
        render: (o) => String(o.context?.rec_yds ?? "—") },
      resultCol,
    ];
    if (family === "rush_yds" || family === "rush_attempts") return [
      dateCol, oppCol, haCol, actualCol("YDS"),
      { key: "att", label: "ATT", width: 40,
        render: (o) => String(o.context?.rush_attempts ?? "—") },
      resultCol,
    ];
    return [dateCol, oppCol, haCol, actualCol("VAL"), resultCol];
  }
  if (sport === "MLB") {
    if (family === "hits" || family === "hits_runs_rbi" || family === "runs"
        || family === "rbi" || family === "home_runs" || family === "total_bases") return [
      dateCol, oppCol, haCol,
      { key: "h", label: "H/AB", width: 56,
        render: (o) => `${o.context?.h ?? "—"}/${o.context?.at_bats ?? "—"}` },
      { key: "sec", label: "TB/HR/RBI", width: 80,
        render: (o) => `${o.context?.tb ?? "—"}/${o.context?.hr ?? "—"}/${o.context?.rbi ?? "—"}` },
      resultCol,
    ];
    if (family === "strikeouts" || family === "outs") return [
      dateCol, oppCol, haCol,
      { key: "k", label: "K", width: 34, render: (o) => String(o.context?.k ?? "—") },
      { key: "outs", label: "OUTS", width: 46, render: (o) => String(o.context?.outs ?? "—") },
      resultCol,
    ];
    return [dateCol, oppCol, haCol, actualCol("VAL"), resultCol];
  }
  if (sport === "Soccer") {
    if (family === "goals" || family === "assists" || family === "goal_or_assist") return [
      dateCol, oppCol, haCol,
      { key: "min", label: "MIN", width: 40,
        render: (o) => String(o.context?.minutes ?? "—") },
      { key: "g", label: "G", width: 26,
        render: (o) => String(o.context?.goals ?? "—") },
      { key: "a", label: "A", width: 26,
        render: (o) => String(o.context?.assists ?? "—") },
      resultCol,
    ];
    if (family === "shots" || family === "sot") return [
      dateCol, oppCol, haCol,
      { key: "min", label: "MIN", width: 40,
        render: (o) => String(o.context?.minutes ?? "—") },
      { key: "sh", label: "SH", width: 36,
        render: (o) => String(o.context?.shots ?? "—") },
      { key: "sot", label: "SOT", width: 40,
        render: (o) => String(o.context?.sot ?? "—") },
      resultCol,
    ];
    // Team markets
    return [
      dateCol, oppCol, haCol,
      { key: "score", label: "GF-GA", width: 60,
        render: (o) => `${o.context?.gf ?? "—"}-${o.context?.ga ?? "—"}` },
      { key: "tot", label: "TOT", width: 40,
        render: (o) => String(o.context?.total ?? "—") },
      resultCol,
    ];
  }
  if (sport === "Tennis") {
    const surface: LogColumn = { key: "surface", label: "SURF", width: 52,
      render: (o) => (o.context?.surface || "—").slice(0, 5) };
    if (family === "moneyline") return [
      dateCol, oppCol, surface,
      { key: "score", label: "SCORE", width: 100,
        render: (o) => (o.context?.score || "—").slice(0, 14) },
      resultCol,
    ];
    return [
      dateCol, oppCol, surface,
      { key: "games", label: "G", width: 44,
        render: (o) => `${o.context?.player_games ?? "—"}-${o.context?.opp_games ?? "—"}` },
      { key: "tot", label: "TOT", width: 42,
        render: (o) => String(o.context?.total_games ?? "—") },
      resultCol,
    ];
  }
  if (sport === "CFB") {
    return [
      dateCol, oppCol, haCol,
      { key: "score", label: "SCORE", width: 62,
        render: (o) => `${o.context?.team_score ?? "—"}-${o.context?.opponent_score ?? "—"}` },
      { key: "tot", label: "TOT", width: 40,
        render: (o) => String(o.context?.total ?? "—") },
      resultCol,
    ];
  }
  return [dateCol, oppCol, haCol, actualCol("VAL"), resultCol];
}

// ─── Human labels for the market family badge ──────────────────────
const MARKET_LABEL: Record<string, string> = {
  pass_yds:      "Pass Yds",  pass_tds: "Pass TDs",
  rush_yds:      "Rush Yds",  rec_yds:  "Rec Yds",
  receptions:    "Receptions", atd:     "Anytime TD",
  hits:          "Hits",  total_bases: "Total Bases",
  home_runs:     "Home Runs", rbi: "RBI", runs: "Runs",
  hits_runs_rbi: "H+R+RBI",
  strikeouts:    "Strikeouts", outs: "Outs Recorded",
  moneyline:     "Moneyline", spread: "Spread", total: "Total",
  run_line:      "Run Line", "1x2": "Match Result",
  handicap:      "Handicap", btts: "BTTS", double_chance: "Double Chance",
  goals:         "Goals", assists: "Assists", goal_or_assist: "Goal+Assist",
  shots:         "Shots", sot: "Shots On Target",
  game_spread:   "Game Spread", game_total: "Game Total",
};

// ─── Component ──────────────────────────────────────────────────────
export type HIFailure = { message: string; status?: number; kind?: string; origin: string };

function _describeFailure(e: any): HIFailure {
  let origin = "";
  try { origin = getBackendUrl() || "same-origin"; } catch { origin = "unconfigured"; }
  const raw = String(e?.message || "");
  let message = raw;
  // 503 bodies carry {status:"QUERY_FAILED", message:...} (JSON-stringified by request()).
  try {
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === "object" && parsed.message) message = String(parsed.message);
  } catch {}
  if (e?.kind === "TIMEOUT") message = "History request timed out";
  else if (e?.kind === "NETWORK_OFFLINE" || /network request failed/i.test(raw)) message = "Network unreachable";
  else if (!message) message = "Historical Intelligence temporarily unavailable";
  return { message, status: e?.status, kind: e?.kind, origin };
}

export function HistoricalIntelligence({
  pickId,
  onData,
  onFailure,
}: {
  pickId: string;
  onData?: (d: HistoricalIntelligenceResponse) => void;
  onFailure?: (f: HIFailure | null) => void;
}) {
  const _hiSeed = swrCacheRead<HistoricalIntelligenceResponse>(historicalIntelligenceKey(pickId, "L10", "ALL"));
  const [data, setData]         = useState<HistoricalIntelligenceResponse | null>(_hiSeed ?? null);
  const [loading, setLoading]   = useState(_hiSeed === undefined);
  const [failure, setFailure]   = useState<HIFailure | null>(null);
  const [tab, setTab]           = useState<Tab>("logs");
  const [sample, setSample]     = useState<SampleScope>("L10");
  const [venue, setVenue]       = useState<VenueScope>("ALL");

  const load = useCallback(async (opts: { sample?: SampleScope; venue?: VenueScope } = {}) => {
    const s = opts.sample ?? sample;
    const v = opts.venue  ?? venue;
    setLoading(true); setFailure(null);
    try { onFailure?.(null); } catch {}
    // Cached scope → paint immediately, then revalidate quietly.
    const _key = historicalIntelligenceKey(pickId, s, v);
    const _cached = swrCacheRead<HistoricalIntelligenceResponse>(_key);
    if (_cached) { setData(_cached); try { onData?.(_cached); } catch {} }
    try {
      const r = await api.historicalIntelligence(pickId, {
        sampleScope: s, venueScope: v,
      });
      setData(r);
      swrCacheWrite(_key, r);
      try { onData?.(r); } catch {}
    } catch (e: any) {
      // QUERY_FAILED — the history source was NOT consulted successfully.
      // Never collapse this into "NO RECENT HISTORY".
      const f = _describeFailure(e);
      setFailure(f);
      try { onFailure?.(f); } catch {}
    } finally {
      setLoading(false);
    }
  }, [pickId, sample, venue, onData, onFailure]);

  useEffect(() => { void load(); }, [pickId]); // eslint-disable-line react-hooks/exhaustive-deps

  const onSample = useCallback((s: SampleScope) => {
    setSample(s); void load({ sample: s });
  }, [load]);
  const onVenue = useCallback((v: VenueScope) => {
    setVenue(v); void load({ venue: v });
  }, [load]);

  // ─── Empty / Error states ─────────────────────────────────────────
  if (loading && !data) {
    return (
      <View style={styles.card}>
        <Text style={styles.title}>HISTORICAL INTELLIGENCE</Text>
        <View style={{ paddingVertical: 24, alignItems: "center" }}>
          <ActivityIndicator color={COLORS.voltBlue} />
          <Text style={styles.dim}>Loading exact-line history…</Text>
        </View>
      </View>
    );
  }
  if (failure && !data) {
    return (
      <View style={styles.card} testID="hi-query-failed">
        <Text style={styles.title}>HISTORICAL INTELLIGENCE</Text>
        <View style={styles.emptyPad}>
          <Text style={styles.failTitle}>HISTORY QUERY FAILED</Text>
          <Text style={styles.dim}>{failure.message}</Text>
          <Text style={[styles.dim, { fontSize: 10, marginTop: 4 }]}>
            {`${failure.kind || (failure.status ? `HTTP ${failure.status}` : "ERROR")} · ${failure.origin}`}
          </Text>
          <Pressable onPress={() => void load()} style={styles.retryBtn} hitSlop={8} testID="hi-retry">
            <Text style={styles.retryTxt}>RETRY</Text>
          </Pressable>
        </View>
      </View>
    );
  }
  if (!data) return null;

  const sport  = data.sport;
  const family = data.market_family;
  const side   = data.scope?.side;
  const line   = data.current_threshold;
  const familyLabel = MARKET_LABEL[family] || (family || "Market").toUpperCase();
  const supportsHomeAway = !isTennis(sport);
  const hiStatus = data.status || (data.data_coverage.total_observations > 0 ? "AVAILABLE_WITH_DATA" : "AVAILABLE_EMPTY");

  return (
    <View style={styles.card}>
      <Text style={styles.title}>HISTORICAL INTELLIGENCE</Text>

      {/* Current line hero — obvious threshold banner */}
      <CurrentLineHero
        entity={data.pick?.player_name || data.entity_name || data.entity_id || "—"}
        opponent={data.pick?.opponent}
        familyLabel={familyLabel}
        line={line}
        side={side}
        summary={{ hits: data.hits, misses: data.misses, pushes: data.pushes,
                   n: data.sample_size, rate: data.hit_rate }}
        scope={sample}
      />

      {/* Scope toggles */}
      <SegmentedRow
        label="SAMPLE"
        options={SAMPLE_SCOPES}
        value={sample}
        onChange={onSample}
        testID="hi-sample-toggle"
      />

      {/* Home/Away toggle (skipped for Tennis) */}
      {supportsHomeAway && (
        <SegmentedRow
          label="VENUE"
          options={["ALL", "HOME", "AWAY"] as VenueScope[]}
          value={venue}
          onChange={onVenue}
          testID="hi-venue-toggle"
        />
      )}

      {/* Tab bar */}
      <TabBar tab={tab} setTab={setTab} />

      {loading && (
        <View style={{ paddingVertical: 8, alignItems: "center" }}>
          <ActivityIndicator color={COLORS.voltBlue} size="small" />
        </View>
      )}
      {failure && !loading && (
        <Pressable onPress={() => void load()} style={styles.failBanner} testID="hi-query-failed-inline">
          <Text style={styles.failBannerTxt}>{`HISTORY QUERY FAILED · ${failure.message} · TAP TO RETRY`}</Text>
        </Pressable>
      )}
      {hiStatus === "SOURCE_UNAVAILABLE" && (
        <View style={styles.emptyPad} testID="hi-source-unavailable">
          <Text style={styles.failTitle}>HISTORY SOURCE UNAVAILABLE</Text>
          <Text style={styles.dim}>No verified history source exists yet for this sport / market.</Text>
        </View>
      )}

      {hiStatus !== "SOURCE_UNAVAILABLE" && tab === "logs"        && <GameLogsTab data={data} />}
      {hiStatus !== "SOURCE_UNAVAILABLE" && tab === "vsopp"       && <VsOppTab data={data} />}
      {hiStatus !== "SOURCE_UNAVAILABLE" && tab === "splits"      && <SplitsTab data={data} supportsHomeAway={supportsHomeAway} />}
      {hiStatus !== "SOURCE_UNAVAILABLE" && tab === "distribution"&& <DistributionTab data={data} />}

      {/* Provenance / proxy footer */}
      {data.games.some((g: any) => g.context?.proxy) ? (
        <View style={styles.proxyBanner}>
          <Text style={styles.proxyText}>
            {`⚠ ${data.games[0].context?.proxy}`}
          </Text>
        </View>
      ) : null}
      <Text style={styles.provenance}>
        {`${data.data_coverage.total_observations} historical observations · ${data.provenance.join(", ")}${data.latency_ms ? `  ·  ${data.latency_ms.toFixed(0)}ms` : ""}`}
      </Text>
      <Text style={styles.provenance} testID="hi-status-line">
        {`${hiStatus} · ${data.scope.sample_scope} n=${data.sample_size}/${data.data_coverage.total_observations}${data.served_by?.host ? ` · src ${data.served_by.host}` : ""}`}
      </Text>
    </View>
  );
}

// ─── Current Line Hero ─────────────────────────────────────────────
function CurrentLineHero({
  entity, opponent, familyLabel, line, side, summary, scope,
}: {
  entity: string; opponent?: string | null; familyLabel: string;
  line: number | null; side?: string;
  summary: { hits: number; misses: number; pushes: number; n: number; rate: number | null };
  scope: string;
}) {
  const sideLetter =
    side === "under" ? "U" : side === "cover" ? "C" : side === "ml" ? "ML" : "O";
  const denom = summary.hits + summary.misses;
  // ── §9 mobile readability: give the entity/opponent line room to
  // breathe across TWO lines so canonical team identity survives.
  return (
    <View style={styles.hero}>
      <View style={{ flex: 1, paddingRight: 8 }}>
        <Text style={styles.heroLabel}>CURRENT LINE</Text>
        <Text style={styles.heroLine}>
          {line !== null ? `${sideLetter} ${line}` : "—"} {" "}
          <Text style={styles.heroFamily}>{familyLabel.toUpperCase()}</Text>
        </Text>
        <Text style={styles.heroEntity} numberOfLines={2} ellipsizeMode="tail">
          {entity}
          {opponent ? ` vs ${opponent}` : ""}
        </Text>
      </View>
      <View style={styles.heroStat}>
        <Text style={styles.heroBig}>
          {denom > 0 ? `${summary.hits}/${denom}` : `${summary.n} obs`}
        </Text>
        <Text style={[styles.heroPct,
          summary.rate !== null && summary.rate >= 0.6 ? { color: COLORS.neonGreen } :
          summary.rate !== null && summary.rate <= 0.4 ? { color: COLORS.electricBlaze } : null,
        ]}>
          {fmtPct(summary.rate)} HIT
        </Text>
        <Text style={styles.heroScope}>{scope} · n={summary.n}</Text>
      </View>
    </View>
  );
}

// ─── Segmented Row ─────────────────────────────────────────────────
function SegmentedRow<T extends string>({
  label, options, value, onChange, testID,
}: { label: string; options: T[]; value: T; onChange: (v: T) => void; testID?: string }) {
  return (
    <View style={styles.segRow} testID={testID}>
      <Text style={styles.segLabel}>{label}</Text>
      <View style={styles.segTrack}>
        {options.map((o) => {
          const active = o === value;
          return (
            <Pressable key={o} onPress={() => onChange(o)}
              style={[styles.segPill, active && styles.segPillActive]}
              hitSlop={6}>
              <Text style={[styles.segPillText, active && styles.segPillTextActive]}>
                {o}
              </Text>
            </Pressable>
          );
        })}
      </View>
    </View>
  );
}

// ─── Tab Bar ───────────────────────────────────────────────────────
function TabBar({ tab, setTab }: { tab: Tab; setTab: (t: Tab) => void }) {
  const tabs: { key: Tab; label: string }[] = [
    { key: "logs",         label: "GAME LOGS" },
    { key: "vsopp",        label: "VS OPP" },
    { key: "splits",       label: "SPLITS" },
    { key: "distribution", label: "DISTRIBUTION" },
  ];
  return (
    <View style={styles.tabBar}>
      {tabs.map((t) => (
        <Pressable key={t.key} onPress={() => setTab(t.key)}
          style={[styles.tabItem, tab === t.key && styles.tabItemActive]}>
          <Text style={[styles.tabText, tab === t.key && styles.tabTextActive]}>
            {t.label}
          </Text>
        </Pressable>
      ))}
    </View>
  );
}

// ─── Game Logs Tab ─────────────────────────────────────────────────
function GameLogsTab({ data }: { data: HistoricalIntelligenceResponse }) {
  const cols = logColumnsFor(data.sport, data.market_family);
  const isPlayerMarket = /pass_|rush_|rec_|receptions|targets|atd|hits|total_bases|home_runs|rbi|runs|hits_runs_rbi|strikeouts|outs|goals|assists|goal_or_assist|shots|sot/.test(data.market_family);
  if (data.games.length === 0) {
    return (
      <View style={styles.emptyPad}>
        <Text style={styles.dim}>
          {data.data_coverage.total_observations === 0
            ? (isPlayerMarket ? "PLAYER MATCH HISTORY UNAVAILABLE" : "TEAM MATCH HISTORY UNAVAILABLE")
            : "No games in this scope"}
        </Text>
        {data.data_coverage.total_observations === 0 && data.entity_name && (
          <Text style={[styles.dim, { fontSize: 10, marginTop: 4 }]}>
            {data.entity_name} not present in {(data.provenance[0] || "").replace("adapter:", "")}
          </Text>
        )}
      </View>
    );
  }
  // GAME LOGS tab shows ONLY the player's own game logs.
  // H2H vs opponent lives on the dedicated VS OPP tab — mixing them
  // here confused users (e.g., a "H2H VS ROYALS · NO PRIOR MATCHUPS"
  // strip appearing above a table of games vs Milwaukee / Miami / LA).
  return (
    <View style={{ marginTop: 8 }}>
      <View style={styles.logHead}>
        {cols.map((c) => (
          <Text key={c.key}
            style={[styles.logHeadText, { width: c.width || 60 }]}
            numberOfLines={1}
            ellipsizeMode="tail">{c.label}</Text>
        ))}
      </View>
      <ScrollView style={{ maxHeight: 340 }} nestedScrollEnabled>
        {data.games.map((g, i) => (
          <View key={i} style={[styles.logRow,
            g.result === "HIT" && { backgroundColor: COLORS.successBg },
            g.result === "MISS" && { backgroundColor: COLORS.dangerSurface },
            g.result === "PUSH" && { backgroundColor: COLORS.pushSurface },
          ]}>
            {cols.map((c) => (
              <Text key={c.key}
                style={[styles.logCell, { width: c.width || 60 },
                  c.key === "result" && g.result === "HIT" && { color: COLORS.neonGreen, fontWeight: "800" },
                  c.key === "result" && g.result === "MISS" && { color: COLORS.electricBlaze, fontWeight: "800" },
                ]}
                numberOfLines={1}
                ellipsizeMode="tail">
                {c.render(g)}
              </Text>
            ))}
          </View>
        ))}
      </ScrollView>
      <ThresholdChart games={data.games} threshold={data.current_threshold}
        side={data.scope.side} />
    </View>
  );
}

// ─── VS Opp Tab ────────────────────────────────────────────────────
function VsOppTab({ data }: { data: HistoricalIntelligenceResponse }) {
  const opp = data.opponent_summary;
  const oppName = data.pick?.opponent || "opponent";
  if (!opp || opp.n === 0) {
    return (
      <View style={styles.emptyPad}>
        <Text style={styles.dim}>NO PRIOR MATCHUPS vs {oppName}</Text>
      </View>
    );
  }
  const denom = opp.hits + opp.misses;
  return (
    <View style={{ marginTop: 10 }}>
      <Text style={styles.vsHead}>VS {oppName.toUpperCase()}</Text>
      <Text style={styles.vsBig}>
        {denom > 0 ? `${opp.hits}/${denom}` : `${opp.n} obs`}
      </Text>
      <Text style={styles.vsPct}>{fmtPct(opp.hit_rate)}   ·   n={opp.n}</Text>
      <View style={styles.vsQuant}>
        <QuantChip label="MEAN"   value={fmtNum(opp.mean, 1)} />
        <QuantChip label="MEDIAN" value={fmtNum(opp.median, 1)} />
        <QuantChip label="STDDEV" value={fmtNum(opp.stddev, 2)} />
      </View>
      {opp.games && (
        <View style={{ marginTop: 10 }}>
          {opp.games.map((g: HistoricalObservation, i: number) => {
            // Derive HIT / MISS / PUSH vs the CURRENT line when the
            // observation carries no precomputed result (same rule the
            // summary uses; missing actual stays "—", never a MISS).
            let res: string | undefined = g.result || undefined;
            if (!res && g.actual != null && data.current_threshold != null) {
              const side = (data.scope?.side || "over").toLowerCase();
              if (g.actual === data.current_threshold) res = "PUSH";
              else res = ((g.actual > data.current_threshold) === (side !== "under")) ? "HIT" : "MISS";
            }
            return (
              <View key={i} style={styles.vsRow}>
                <Text style={styles.vsRowDate}>{fmtDate(g.date)}</Text>
                <Text style={styles.vsRowActual}>{fmtNum(g.actual, 1)}</Text>
                <Text style={[styles.vsRowResult,
                  res === "HIT" && { color: COLORS.neonGreen },
                  res === "MISS" && { color: COLORS.electricBlaze },
                ]}>{res || "—"}</Text>
              </View>
            );
          })}
        </View>
      )}
    </View>
  );
}

// ─── Splits Tab ─────────────────────────────────────────────────────
function SplitsTab({ data, supportsHomeAway }:
    { data: HistoricalIntelligenceResponse; supportsHomeAway: boolean }) {
  const rows: { key: string; label: string; s: any }[] = [];
  if (supportsHomeAway) {
    if (data.home_summary) rows.push({ key: "home", label: "HOME", s: data.home_summary });
    if (data.away_summary) rows.push({ key: "away", label: "AWAY", s: data.away_summary });
  } else {
    // Tennis: expose surface splits from context bucket if available
    // Compute inline from data.games using context.surface.
    const bucket: Record<string, HistoricalObservation[]> = {};
    for (const g of data.games) {
      const sf = g.context?.surface;
      if (!sf) continue;
      (bucket[sf] ||= []).push(g);
    }
    for (const [sf, obs] of Object.entries(bucket)) {
      const denom = obs.filter(o => o.result === "HIT" || o.result === "MISS").length;
      const hits  = obs.filter(o => o.result === "HIT").length;
      const rate  = denom > 0 ? hits / denom : null;
      rows.push({ key: sf, label: sf.toUpperCase(), s: {
        n: obs.length, hits, hit_rate: rate,
      }});
    }
  }
  if (rows.length === 0) {
    return (
      <View style={styles.emptyPad}>
        <Text style={styles.dim}>
          {supportsHomeAway ? "SPLIT DATA UNAVAILABLE" : "SURFACE SPLIT UNAVAILABLE"}
        </Text>
      </View>
    );
  }
  return (
    <View style={{ marginTop: 10 }}>
      {rows.map((r) => (
        <View key={r.key} style={styles.splitRow}>
          <Text style={styles.splitLabel}>{r.label}</Text>
          <Text style={styles.splitVal}>
            {r.s.hits ?? 0}/{(r.s.hits ?? 0) + (r.s.misses ?? Math.max(0, (r.s.n ?? 0) - (r.s.hits ?? 0)))}
          </Text>
          <Text style={styles.splitPct}>{fmtPct(r.s.hit_rate)}</Text>
          <Text style={styles.splitN}>n={r.s.n}</Text>
        </View>
      ))}
    </View>
  );
}

// ─── Distribution Tab ──────────────────────────────────────────────
function DistributionTab({ data }: { data: HistoricalIntelligenceResponse }) {
  if (data.sample_size === 0) {
    return (
      <View style={styles.emptyPad}>
        <Text style={styles.dim}>NO OBSERVATIONS IN SCOPE</Text>
      </View>
    );
  }
  return (
    <View style={{ marginTop: 10 }}>
      <View style={styles.distGrid}>
        <QuantChip label="n"      value={String(data.sample_size)} />
        <QuantChip label="MEAN"   value={fmtNum(data.mean, 1)} />
        <QuantChip label="MEDIAN" value={fmtNum(data.median, 1)} />
        <QuantChip label="Q25"    value={fmtNum(data.q25, 1)} />
        <QuantChip label="Q75"    value={fmtNum(data.q75, 1)} />
        <QuantChip label="STDDEV" value={fmtNum(data.stddev, 2)} />
      </View>
      <ThresholdChart games={data.games} threshold={data.current_threshold}
        side={data.scope.side} />
    </View>
  );
}

// ─── Quant Chip ────────────────────────────────────────────────────
function QuantChip({ label, value }: { label: string; value: string }) {
  return (
    <View style={styles.chip}>
      <Text style={styles.chipLabel}>{label}</Text>
      <Text style={styles.chipVal}>{value}</Text>
    </View>
  );
}

// ─── Threshold Chart (lightweight SVG) ─────────────────────────────
function ThresholdChart({
  games, threshold, side,
}: { games: HistoricalObservation[]; threshold: number | null; side?: string }) {
  const points = games
    .filter((g) => g.actual !== null && g.actual !== undefined)
    .map((g) => Number(g.actual)) as number[];
  if (points.length === 0 || threshold === null) return null;

  const W = 320; const H = 100; const pad = 8;
  const allVals = [...points, threshold];
  const min = Math.min(...allVals);
  const max = Math.max(...allVals);
  const range = max - min || 1;
  const stepX = points.length > 1 ? (W - 2 * pad) / (points.length - 1) : 0;
  const yFor = (v: number) => H - pad - ((v - min) / range) * (H - 2 * pad);
  const isML = side === "ml" || side === "cover_ml_1x2";

  return (
    <View style={{ marginTop: 12, alignItems: "center" }}>
      <Svg width={W} height={H}>
        {/* Threshold line */}
        <Line x1={pad} y1={yFor(threshold)} x2={W - pad} y2={yFor(threshold)}
          stroke={COLORS.voltBlue} strokeDasharray="4,4" strokeWidth={1.2} />
        {/* Data points (reverse — most recent at right) */}
        {points.slice().reverse().map((v, i) => {
          const hit = isML
            ? v >= 1.0
            : side === "under"
              ? v < threshold
              : v > threshold;
          const cx = pad + stepX * i;
          const cy = yFor(v);
          return (
            <Circle key={i} cx={cx} cy={cy} r={3.4}
              fill={hit ? COLORS.neonGreen : COLORS.electricBlaze}
              opacity={0.92} />
          );
        })}
      </Svg>
      <Text style={styles.chartCaption}>
        Line {threshold} · {points.length} obs · most recent →
      </Text>
    </View>
  );
}

// ─── Styles ────────────────────────────────────────────────────────
const styles = StyleSheet.create({
  card: {
    marginTop: 12, padding: 14, borderRadius: 14,
    backgroundColor: COLORS.surface,
    borderWidth: 1, borderColor: COLORS.borderDefault,
  },
  title: {
    color: COLORS.textPrimary, fontSize: 12, fontWeight: "800",
    letterSpacing: 1.4, marginBottom: 10,
  },
  dim: { color: COLORS.textMuted, fontSize: 12, marginTop: 6 },

  hero: {
    flexDirection: "row", justifyContent: "space-between",
    alignItems: "flex-start", padding: 12,
    borderRadius: 12, backgroundColor: COLORS.surfaceElevated,
    borderWidth: 1, borderColor: COLORS.borderStrong,
    marginBottom: 12,
  },
  heroLabel: {
    color: COLORS.textMuted, fontSize: 10, letterSpacing: 1.2,
    fontWeight: "700", marginBottom: 3,
  },
  heroLine: { color: COLORS.textPrimary, fontSize: 22, fontWeight: "800" },
  heroFamily: { color: COLORS.textSecondary, fontSize: 12, fontWeight: "600" },
  heroEntity: { color: COLORS.textSecondary, fontSize: 12, marginTop: 4 },
  heroStat: { alignItems: "flex-end" },
  heroBig: { color: COLORS.textPrimary, fontSize: 22, fontWeight: "800" },
  heroPct: { color: COLORS.textSecondary, fontSize: 12, fontWeight: "700", marginTop: 1 },
  heroScope: { color: COLORS.textMuted, fontSize: 10, marginTop: 2 },

  segRow: { flexDirection: "row", alignItems: "center", marginBottom: 8 },
  segLabel: {
    width: 60, color: COLORS.textMuted, fontSize: 10, fontWeight: "700",
    letterSpacing: 1.2,
  },
  segTrack: {
    flex: 1, flexDirection: "row", backgroundColor: COLORS.surfaceInset,
    borderRadius: 10, padding: 2,
  },
  segPill: {
    flex: 1, paddingVertical: 6, alignItems: "center", borderRadius: 8,
  },
  segPillActive: {
    backgroundColor: COLORS.voltBlue + "35",
    borderWidth: 1, borderColor: COLORS.voltBlue,
  },
  segPillText: {
    color: COLORS.textMuted, fontSize: 11, fontWeight: "700",
    letterSpacing: 0.6,
  },
  segPillTextActive: { color: COLORS.textPrimary },

  tabBar: {
    flexDirection: "row", marginTop: 8, marginBottom: 6,
    borderBottomWidth: 1, borderBottomColor: COLORS.borderDefault,
  },
  tabItem: {
    flex: 1, paddingVertical: 8, alignItems: "center",
  },
  tabItemActive: {
    borderBottomWidth: 2, borderBottomColor: COLORS.voltBlue,
  },
  tabText: { color: COLORS.textMuted, fontSize: 10, fontWeight: "700",
    letterSpacing: 0.8 },
  tabTextActive: { color: COLORS.textPrimary },

  logHead: {
    flexDirection: "row", paddingVertical: 6, paddingHorizontal: 4,
    borderBottomWidth: 1, borderBottomColor: COLORS.borderDefault,
  },
  logHeadText: {
    color: COLORS.textMuted, fontSize: 10, fontWeight: "700",
    letterSpacing: 0.8,
  },
  logRow: {
    flexDirection: "row", paddingVertical: 6, paddingHorizontal: 4,
    borderBottomWidth: 1, borderBottomColor: COLORS.surfaceInset,
  },
  logCell: { color: COLORS.textSecondary, fontSize: 11 },

  vsHead: { color: COLORS.textMuted, fontSize: 10, fontWeight: "700",
    letterSpacing: 1.2 },
  vsBig:  { color: COLORS.textPrimary, fontSize: 22, fontWeight: "800", marginTop: 4 },
  vsPct:  { color: COLORS.textSecondary, fontSize: 12, marginTop: 2 },
  vsQuant:{ flexDirection: "row", marginTop: 8, flexWrap: "wrap" },
  vsRow:  { flexDirection: "row", alignItems: "center", paddingVertical: 4 },
  vsRowDate: { color: COLORS.textMuted, fontSize: 11, width: 60 },
  vsRowActual: { color: COLORS.textPrimary, fontSize: 12, fontWeight: "700", width: 60 },
  vsRowResult: { color: COLORS.textSecondary, fontSize: 11, fontWeight: "700" },

  splitRow: {
    flexDirection: "row", alignItems: "center", paddingVertical: 8,
    borderBottomWidth: 1, borderBottomColor: COLORS.surfaceInset,
  },
  splitLabel: { color: COLORS.textPrimary, fontSize: 12, fontWeight: "700",
    width: 90, letterSpacing: 0.8 },
  splitVal:   { color: COLORS.textSecondary, fontSize: 12, width: 68 },
  splitPct:   { color: COLORS.textPrimary, fontSize: 13, fontWeight: "800", width: 60 },
  splitN:     { color: COLORS.textMuted, fontSize: 11 },

  distGrid: { flexDirection: "row", flexWrap: "wrap", marginTop: 6 },
  chip: {
    width: "31%", marginRight: "2%", marginBottom: 8, padding: 8,
    borderRadius: 8, backgroundColor: COLORS.surfaceInset,
    borderWidth: 1, borderColor: COLORS.borderDefault,
  },
  chipLabel: { color: COLORS.textMuted, fontSize: 9, letterSpacing: 0.8,
    fontWeight: "700" },
  chipVal:   { color: COLORS.textPrimary, fontSize: 15, fontWeight: "800",
    marginTop: 2 },

  chartCaption: { color: COLORS.textMuted, fontSize: 10, marginTop: 4 },

  emptyPad: { paddingVertical: 22, alignItems: "center" },
  failTitle: { color: COLORS.electricBlaze, fontSize: 11, fontWeight: "900", letterSpacing: 0.8 },
  retryBtn: {
    marginTop: 12, minHeight: 44, minWidth: 120, paddingHorizontal: 18,
    alignItems: "center", justifyContent: "center", borderRadius: 10,
    borderWidth: 1, borderColor: COLORS.voltBlue,
  },
  retryTxt: { color: COLORS.voltBlue, fontSize: 11, fontWeight: "900", letterSpacing: 0.8 },
  failBanner: {
    marginTop: 8, minHeight: 44, justifyContent: "center", paddingHorizontal: 10,
    borderRadius: 8, borderWidth: 1, borderColor: COLORS.electricBlaze,
  },
  failBannerTxt: { color: COLORS.electricBlaze, fontSize: 10.5, fontWeight: "800", letterSpacing: 0.4 },

  provenance: {
    color: COLORS.textMuted, fontSize: 9, marginTop: 10,
    letterSpacing: 0.6, textAlign: "center",
  },
  proxyBanner: {
    marginTop: 10, padding: 8, borderRadius: 8,
    backgroundColor: COLORS.surfaceGloss,
    borderWidth: 1, borderColor: COLORS.borderGold,
  },
  proxyText: {
    color: COLORS.goldRich, fontSize: 10, fontWeight: "600",
    textAlign: "center",
  },
  h2hStrip: {
    flexDirection: "row", justifyContent: "space-between",
    alignItems: "center", paddingHorizontal: 10, paddingVertical: 6,
    borderRadius: 8, marginBottom: 6,
    backgroundColor: COLORS.voltBlue + "22",
    borderWidth: 1, borderColor: COLORS.voltBlue + "88",
  },
  h2hStripDim: {
    flexDirection: "row", justifyContent: "space-between",
    alignItems: "center", paddingHorizontal: 10, paddingVertical: 6,
    borderRadius: 8, marginBottom: 6,
    backgroundColor: COLORS.surfaceInset,
    borderWidth: 1, borderColor: COLORS.borderDefault,
  },
  h2hStripLabel: {
    color: COLORS.textPrimary, fontSize: 10, fontWeight: "800",
    letterSpacing: 1.0,
  },
  h2hStripStat: {
    color: COLORS.textPrimary, fontSize: 12, fontWeight: "800",
  },
});

export default HistoricalIntelligence;
