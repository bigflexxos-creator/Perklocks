/**
 * LAB — Betting research terminal.
 *
 * Session 1 (2026-07-07) ships FOUR core modules inside a single
 * segmented-control shell:
 *
 *  1. **Research**     — pick any live pick, see rolled-up L5/L10/L20
 *                        form, matchup, evidence bullets, sim results.
 *  2. **EV Calc**      — implied vs model probability, edge %, and
 *                        expected value per $100 for any pick or
 *                        manually-entered odds line.
 *  3. **Simulation**   — surfaces the Monte Carlo output already
 *                        computed by `backend/sim_engine.py`: hit
 *                        probability, scenario breakdown, best/worst
 *                        case, variance risk.
 *  4. **Prop Explorer**— search + sort every live player prop by
 *                        lock score / edge / win prob / sample size.
 *
 * Design intent: Lab replaces the old per-sport "🌍 SOCCER LAB" style
 * button which duplicated the home slate. Lab is where the app
 * PROVES its reasoning. Picks show the recommendation; Lab explains,
 * tests, and audits it.
 *
 * Future sessions will layer in: Correlation Lab, Bet Backtester,
 * Pattern Finder, Matchup DNA, Market Intelligence, Pick Autopsy,
 * AI Assistant, Experiment Builder.
 */
import React, { useCallback, useEffect, useMemo, useState } from "react";
import {
  View, Text, ScrollView, TouchableOpacity, StyleSheet,
  ActivityIndicator, TextInput, RefreshControl, Pressable,
} from "react-native";
import { SafeAreaView, useSafeAreaInsets } from "react-native-safe-area-context";
import { Ionicons } from "@expo/vector-icons";
import { COLORS } from "@/src/theme";
import { api, getBackendUrl } from "@/src/lib/api";
import { PickEventRow } from "@/src/components/PickEventRow";
import { StrategyLabWorkstation } from "@/src/components/StrategyLabWorkstation";

// ── §1 Lab navigation consolidation (2026-08-28) ────────────────────
// Four primary sections with progressive disclosure. The 12-chip legacy
// menu was collapsed but every existing module is still reachable via
// its parent section. StrategyLabWorkstation remains the canonical
// research shell (§2) — never duplicated.
type LabModule = "workstation" | "cheats" | "hot" | "research" | "ev" | "sim" | "props" | "corr" | "backtest" | "patterns" | "dna" | "analytics";

type LabGroup = "TODAY" | "QUANT" | "PLAYER" | "MARKET";

const GROUPS: { id: LabGroup; label: string; icon: keyof typeof Ionicons.glyphMap }[] = [
  { id: "TODAY",  label: "Today",  icon: "flame" },
  { id: "QUANT",  label: "Quant",  icon: "stats-chart" },
  { id: "PLAYER", label: "Player", icon: "person" },
  { id: "MARKET", label: "Market", icon: "trending-up" },
];

// Progressive disclosure — each group exposes ordered submodules.
const GROUP_MODULES: Record<LabGroup, { id: LabModule; label: string; icon: keyof typeof Ionicons.glyphMap }[]> = {
  TODAY: [
    { id: "workstation", label: "Research Feed", icon: "flask" },
    { id: "hot",         label: "Trend Radar",   icon: "flame" },
    { id: "cheats",      label: "Cheatsheets",   icon: "flash" },
    { id: "props",       label: "Prop Explorer", icon: "list" },
  ],
  QUANT: [
    { id: "corr",      label: "Correlations", icon: "git-network" },
    { id: "backtest",  label: "Backtest",     icon: "layers" },
    { id: "analytics", label: "Calibration",  icon: "speedometer" },
    { id: "patterns",  label: "Patterns 3.0", icon: "sparkles" },
  ],
  PLAYER: [
    { id: "workstation", label: "Player Research", icon: "flask" },
    { id: "dna",         label: "Matchup DNA",     icon: "body" },
    { id: "research",    label: "Research (Legacy)", icon: "search" },
  ],
  MARKET: [
    { id: "workstation", label: "Line Value / Fair Price", icon: "trending-up" },
    { id: "sim",         label: "Simulation",              icon: "analytics" },
    { id: "ev",          label: "EV Calc",                 icon: "calculator" },
  ],
};

// Legacy flat MODULES table kept for backwards-compatible imports.
const MODULES: { id: LabModule; label: string; icon: keyof typeof Ionicons.glyphMap }[] = [
  { id: "workstation",label: "Workstation",  icon: "flask" },
  { id: "cheats",    label: "Cheatsheets",  icon: "flash" },
  { id: "hot",       label: "Hot Hitters",  icon: "flame" },
  { id: "analytics", label: "Analytics",    icon: "stats-chart" },
  { id: "research",  label: "Research",     icon: "search" },
  { id: "corr",      label: "Correlations", icon: "git-network" },
  { id: "backtest",  label: "Backtest",     icon: "layers" },
  { id: "patterns",  label: "Patterns",     icon: "sparkles" },
  { id: "dna",       label: "Matchup DNA",  icon: "body" },
  { id: "ev",        label: "EV Calc",      icon: "calculator" },
  { id: "sim",       label: "Sim",          icon: "analytics" },
  { id: "props",     label: "Props",        icon: "list" },
];

// ── Root screen ──────────────────────────────────────────────────────
export default function LabScreen() {
  const insets = useSafeAreaInsets();
  // §1 4-group navigation. `group` picks the primary section; `module`
  // picks the submodule within that group.
  const [group, setGroup] = useState<LabGroup>("TODAY");
  const [module, setModule] = useState<LabModule>("workstation");
  const [picks, setPicks] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // When the group changes, snap to that group's first submodule so the
  // sub-chips + rendered body always agree.
  const onSelectGroup = useCallback((g: LabGroup) => {
    setGroup(g);
    const first = GROUP_MODULES[g][0];
    if (first) setModule(first.id);
  }, []);

  const load = useCallback(async () => {
    setError(null);
    try {
      // NOTE: use `picksAll` NOT `picksToday` — the latter hardcodes
      // `lite=true` which strips `pick_rationale`, `recent_form`, and
      // `player_form_streak`. Those are exactly the fields the
      // Cheatsheet + Research modules use to generate "Hit in X of Y"
      // proof bullets. `picksAll` returns the full document (~200 rows,
      // no lite stripping) which is fine for Lab's use-cases.
      const res = await api.picksAll();
      setPicks(res.picks || []);
    } catch (e: any) {
      setError(e?.message || "Failed to load picks");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  return (
    <SafeAreaView style={styles.root} edges={["top"]}>
      {/* Header */}
      <View style={[styles.header, { paddingTop: 4 }]}>
        <View style={styles.headerRow}>
          <View style={styles.headerLeft}>
            <Ionicons name="flask" size={22} color={COLORS.textPrimary} />
            <Text style={styles.headerTitle}>LAB</Text>
          </View>
          <Text style={styles.headerHint} numberOfLines={1}>
            Today · Quant · Player · Market
          </Text>
        </View>
        {/* §1 4-group primary segmented control */}
        <ScrollView
          horizontal
          showsHorizontalScrollIndicator={false}
          contentContainerStyle={styles.segRow}
        >
          {GROUPS.map((g) => {
            const active = g.id === group;
            return (
              <TouchableOpacity
                key={g.id}
                testID={`lab-group-${g.id}`}
                style={[styles.segChip, active && styles.segChipActive]}
                onPress={() => onSelectGroup(g.id)}
              >
                <Ionicons
                  name={g.icon}
                  size={13}
                  color={active ? "#000" : COLORS.textMuted}
                  style={{ marginRight: 4 }}
                />
                <Text style={[styles.segTxt, active && styles.segTxtActive]}>
                  {g.label.toUpperCase()}
                </Text>
              </TouchableOpacity>
            );
          })}
        </ScrollView>
        {/* Progressive disclosure — group's submodules */}
        <ScrollView
          horizontal
          showsHorizontalScrollIndicator={false}
          contentContainerStyle={styles.segRow}
        >
          {GROUP_MODULES[group].map((m) => {
            const active = m.id === module;
            return (
              <TouchableOpacity
                key={`${group}-${m.id}`}
                testID={`lab-sub-${group}-${m.id}`}
                style={[styles.segSubChip, active && styles.segSubChipActive]}
                onPress={() => setModule(m.id)}
              >
                <Ionicons
                  name={m.icon}
                  size={11}
                  color={active ? COLORS.textPrimary : COLORS.textMuted}
                  style={{ marginRight: 4 }}
                />
                <Text style={[styles.segSubTxt, active && styles.segSubTxtActive]}>
                  {m.label}
                </Text>
              </TouchableOpacity>
            );
          })}
        </ScrollView>
      </View>

      {/* Body */}
      {loading ? (
        <View style={styles.centered}>
          <ActivityIndicator color={COLORS.textPrimary} />
        </View>
      ) : error ? (
        <View style={styles.centered}>
          <Text style={styles.errTxt}>{error}</Text>
          <TouchableOpacity onPress={load} style={styles.retryBtn}>
            <Text style={styles.retryTxt}>RETRY</Text>
          </TouchableOpacity>
        </View>
      ) : (
        <ScrollView
          style={styles.body}
          contentContainerStyle={{ padding: 12, paddingBottom: insets.bottom + 80 }}
          refreshControl={
            <RefreshControl
              refreshing={refreshing}
              onRefresh={() => { setRefreshing(true); load(); }}
              tintColor={COLORS.textPrimary}
            />
          }
        >
          {module === "workstation"&& <StrategyLabWorkstation picks={picks} />}
          {module === "cheats"    && <CheatsheetsModule picks={picks} />}
          {module === "hot"       && <HotHittersModule />}
          {module === "analytics" && <AnalyticsModule />}
          {module === "research"  && <ResearchModule picks={picks} />}
          {module === "corr"      && <CorrelationModule />}
          {module === "backtest"  && <BacktestModule />}
          {module === "patterns"  && <PatternsModule />}
          {module === "dna"       && <MatchupDNAModule />}
          {module === "ev"        && <EVCalcModule picks={picks} />}
          {module === "sim"       && <SimulationModule picks={picks} />}
          {module === "props"     && <PropExplorerModule picks={picks} />}
        </ScrollView>
      )}
    </SafeAreaView>
  );
}


// ═══════════════════════════════════════════════════════════════════
// MODULE 1: BET RESEARCH ENGINE
// ═══════════════════════════════════════════════════════════════════
function ResearchModule({ picks }: { picks: any[] }) {
  const [query, setQuery] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<any | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    const base = picks.slice(0, 300);
    if (!q) return base.slice(0, 40);
    return base.filter((p) => {
      const hay = [p.market, p.event, p.selection, p.team, p.player_name,
                   p.league, p.sport].filter(Boolean).join(" ").toLowerCase();
      return hay.includes(q);
    }).slice(0, 40);
  }, [picks, query]);

  useEffect(() => {
    if (!selectedId) { setDetail(null); return; }
    setDetailLoading(true);
    api.pickDetail(selectedId)
      .then((d) => setDetail(d))
      .catch(() => setDetail(null))
      .finally(() => setDetailLoading(false));
  }, [selectedId]);

  return (
    <View>
      <SectionHeader
        icon="search"
        title="Research Engine"
        blurb="Search any pick, see its full evidence stack + rolling form + matchup + simulation output."
      />
      <View style={styles.searchWrap}>
        <Ionicons name="search" size={14} color={COLORS.textMuted} />
        <TextInput
          testID="lab-research-search"
          value={query}
          onChangeText={setQuery}
          placeholder="Search team, player, market…"
          placeholderTextColor={COLORS.textMuted}
          style={styles.searchInput}
          autoCorrect={false}
          autoCapitalize="none"
        />
        {query.length > 0 && (
          <TouchableOpacity onPress={() => setQuery("")}>
            <Ionicons name="close-circle" size={16} color={COLORS.textMuted} />
          </TouchableOpacity>
        )}
      </View>

      {!selectedId ? (
        <View>
          <Text style={styles.subhead}>
            {filtered.length} pick{filtered.length === 1 ? "" : "s"}
          </Text>
          {filtered.map((p) => (
            <PickRow
              key={p.id}
              pick={p}
              onPress={() => setSelectedId(p.id)}
            />
          ))}
        </View>
      ) : (
        <View>
          <TouchableOpacity
            testID="lab-research-back"
            onPress={() => { setSelectedId(null); setDetail(null); }}
            style={styles.backBtn}
          >
            <Ionicons name="chevron-back" size={16} color={COLORS.textPrimary} />
            <Text style={styles.backTxt}>Back to results</Text>
          </TouchableOpacity>
          {detailLoading ? (
            <ActivityIndicator color={COLORS.textPrimary} style={{ marginTop: 24 }} />
          ) : detail ? (
            <ResearchDetail pick={detail} />
          ) : (
            <Text style={styles.errTxt}>Detail load failed.</Text>
          )}
        </View>
      )}
    </View>
  );
}

function ResearchDetail({ pick }: { pick: any }) {
  const rationale = pick.pick_rationale || {};
  const evidence: any[] = rationale.evidence || [];
  const evidenceAll: any[] = rationale.evidence_all || evidence;
  const evFamily: string | undefined = rationale.evidence_family;
  const recentForm = rationale.recent_form || {};
  const sim = pick.sim_result || pick.simulation || {};

  const impl = pick.implied_probability ?? null;
  const wp = pick.win_probability ?? null;
  const edge = pick.edge_percent ?? null;

  return (
    <View>
      {/* Card header */}
      <View style={styles.detailHeader}>
        <View style={{ flex: 1 }}>
          <Text style={styles.detailSport}>
            {pick.sport}{pick.league ? ` · ${pick.league}` : ""}
          </Text>
          <PickEventRow pick={pick} size="detail" />
          <Text style={styles.detailMarket} numberOfLines={2}>
            {pick.market || pick.selection}
          </Text>
        </View>
        <View style={styles.lockBadge}>
          <Text style={styles.lockScore}>{Math.round(pick.lock_score || 0)}</Text>
          <Text style={styles.lockLabel}>LOCK</Text>
        </View>
      </View>

      {/* Model vs market */}
      <StatGrid
        cells={[
          { label: "Model", value: wp != null ? `${Number(wp).toFixed(1)}%` : "—" },
          { label: "Implied", value: impl != null ? `${Number(impl).toFixed(1)}%` : "—" },
          { label: "Edge", value: edge != null ? `${Number(edge) >= 0 ? "+" : ""}${Number(edge).toFixed(1)}%` : "—",
            tint: edge != null ? (Number(edge) >= 0 ? "#40d18a" : "#e46d6d") : undefined },
          { label: "Odds", value: pick.book_odds != null ? formatOdds(pick.book_odds) : "—" },
        ]}
      />

      {/* Why this pick — market-filtered evidence.
          Note: ESPN Signal Engine bullets (injuries, form, probability
          delta) are injected server-side into `pick_rationale.evidence`
          via `services/espn_signal_engine.apply_signals`. That is why
          the redundant "🧠 ESPN Signal" and "🚑 Injury Report" side-
          sections that used to live below this block were removed —
          the analysis flows through the same evidence list every other
          rationale bullet does. */}
      {evidence.length > 0 && (
        <Section title="Why This Pick" chip={evFamily}>
          {evidence.map((line: any, i: number) => (
            <Bullet key={i} text={stringifyBullet(line)} />
          ))}
        </Section>
      )}

      {/* Rolling form (L5/L10/L20 when present) */}
      {Object.keys(recentForm).length > 0 && (
        <Section title="Recent Form">
          <View style={styles.formGrid}>
            {["last5", "last10", "last20", "L5", "L10", "L20"].map((k) => {
              const v = recentForm[k];
              if (v === undefined || v === null) return null;
              return (
                <View key={k} style={styles.formCell}>
                  <Text style={styles.formLabel}>{k.toUpperCase()}</Text>
                  <Text style={styles.formValue}>{formatFormValue(v)}</Text>
                </View>
              );
            })}
          </View>
          {recentForm.summary ? (
            <Text style={styles.formSummary}>{String(recentForm.summary)}</Text>
          ) : null}
        </Section>
      )}

      {/* Simulation snapshot */}
      {(sim.win_probability != null || sim.scenario_breakdown || sim.runs != null) && (
        <Section title="Simulation">
          <StatGrid
            cells={[
              { label: "Sim Prob", value: sim.win_probability != null
                  ? `${Number(sim.win_probability).toFixed(1)}%` : "—" },
              { label: "Runs", value: sim.runs != null ? `${Number(sim.runs).toLocaleString()}` : "—" },
              { label: "CI Low", value: sim.ci_lower != null ? `${Number(sim.ci_lower).toFixed(1)}%` : "—" },
              { label: "CI Hi", value: sim.ci_upper != null ? `${Number(sim.ci_upper).toFixed(1)}%` : "—" },
            ]}
          />
        </Section>
      )}

      {/* Full evidence (dropped by market filter) */}
      {evidenceAll.length > evidence.length && (
        <Section title={`All Evidence (${evidenceAll.length})`}>
          <Text style={styles.disclaimer}>
            The evidence above is the top-{evidence.length} ranked for THIS market family.
            Everything else the model considered is listed below.
          </Text>
          {evidenceAll.map((line: any, i: number) => (
            <Bullet key={i} text={stringifyBullet(line)} muted />
          ))}
        </Section>
      )}

      {/* Concerns */}
      {(rationale.concerns || []).length > 0 && (
        <Section title="Concerns">
          {rationale.concerns.map((line: any, i: number) => (
            <Bullet key={i} text={stringifyBullet(line)} tint="#e46d6d" />
          ))}
        </Section>
      )}
    </View>
  );
}


// ═══════════════════════════════════════════════════════════════════
// MODULE 2: EV CALCULATOR
// ═══════════════════════════════════════════════════════════════════
function EVCalcModule({ picks }: { picks: any[] }) {
  const [oddsInput, setOddsInput] = useState("+150");
  const [modelInput, setModelInput] = useState("55");
  const [selectedId, setSelectedId] = useState<string | null>(null);

  // Compute EV from inputs
  const parsedOdds = useMemo(() => {
    const s = oddsInput.trim();
    if (!s) return null;
    const n = parseInt(s.replace(/[^-+\d]/g, ""), 10);
    return isFinite(n) ? n : null;
  }, [oddsInput]);
  const impl = useMemo(() => parsedOdds != null ? impliedProb(parsedOdds) : null, [parsedOdds]);
  const modelPct = useMemo(() => {
    const n = parseFloat(modelInput);
    return isFinite(n) ? n : null;
  }, [modelInput]);
  const edge = (modelPct != null && impl != null) ? (modelPct - impl) : null;
  const ev = (modelPct != null && parsedOdds != null)
    ? computeEV(modelPct / 100, parsedOdds) : null;

  // Auto-fill from selected pick
  useEffect(() => {
    if (!selectedId) return;
    const p = picks.find((x) => x.id === selectedId);
    if (!p) return;
    if (p.book_odds != null) setOddsInput(formatOdds(p.book_odds));
    if (p.win_probability != null) setModelInput(String(p.win_probability));
  }, [selectedId, picks]);

  const topPicks = useMemo(
    () => picks.slice(0, 12),
    [picks]
  );

  return (
    <View>
      <SectionHeader
        icon="calculator"
        title="EV Calculator"
        blurb="Break down any American-odds line: implied prob, edge %, expected value per $100 wagered."
      />

      {/* Input row */}
      <View style={styles.evInputRow}>
        <View style={styles.evField}>
          <Text style={styles.evFieldLabel}>Book Odds</Text>
          <TextInput
            testID="lab-ev-odds"
            value={oddsInput}
            onChangeText={setOddsInput}
            placeholder="+150 or -220"
            placeholderTextColor={COLORS.textMuted}
            keyboardType="numbers-and-punctuation"
            style={styles.evInput}
          />
        </View>
        <View style={styles.evField}>
          <Text style={styles.evFieldLabel}>Model Win %</Text>
          <TextInput
            testID="lab-ev-model"
            value={modelInput}
            onChangeText={setModelInput}
            placeholder="55"
            placeholderTextColor={COLORS.textMuted}
            keyboardType="decimal-pad"
            style={styles.evInput}
          />
        </View>
      </View>

      {/* Results */}
      <StatGrid
        cells={[
          { label: "Implied", value: impl != null ? `${impl.toFixed(1)}%` : "—" },
          { label: "Model", value: modelPct != null ? `${modelPct.toFixed(1)}%` : "—" },
          { label: "Edge", value: edge != null ? `${edge >= 0 ? "+" : ""}${edge.toFixed(1)}%` : "—",
            tint: edge != null ? (edge >= 0 ? "#40d18a" : "#e46d6d") : undefined },
          { label: "EV / $100", value: ev != null ? `${ev >= 0 ? "+$" : "-$"}${Math.abs(ev).toFixed(2)}` : "—",
            tint: ev != null ? (ev >= 0 ? "#40d18a" : "#e46d6d") : undefined },
        ]}
      />

      {/* Recommended confidence */}
      {edge != null && (
        <View style={styles.recRow}>
          <Text style={styles.recLabel}>Recommendation</Text>
          <Text style={[styles.recValue, { color: edgeTint(edge) }]}>
            {edgeVerdict(edge)}
          </Text>
        </View>
      )}

      {/* Auto-fill from live picks */}
      <SectionHeader
        icon="list"
        title="Auto-fill from top locks"
        blurb="Tap a pick to load its odds + model prob into the calculator above."
      />
      {topPicks.map((p) => (
        <PickRow
          key={p.id}
          pick={p}
          onPress={() => setSelectedId(p.id)}
          selected={p.id === selectedId}
        />
      ))}
    </View>
  );
}


// ═══════════════════════════════════════════════════════════════════
// MODULE 3: SIMULATION LAB
// ═══════════════════════════════════════════════════════════════════
function SimulationModule({ picks }: { picks: any[] }) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<any | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!selectedId) { setDetail(null); return; }
    setLoading(true);
    api.pickDetail(selectedId)
      .then((d) => setDetail(d))
      .catch(() => setDetail(null))
      .finally(() => setLoading(false));
  }, [selectedId]);

  // Filter to picks that have sim data attached
  const simPicks = useMemo(
    () => picks.filter((p) => p.sim_result || p.simulation).slice(0, 40),
    [picks]
  );

  if (!selectedId) {
    return (
      <View>
        <SectionHeader
          icon="analytics"
          title="Simulation Lab"
          blurb={`Monte Carlo output for any pick — hit probability, scenario breakdown, best/worst case, variance risk. ${simPicks.length} picks have live simulation data.`}
        />
        {simPicks.length === 0 ? (
          <Text style={styles.disclaimer}>
            No picks with simulation output in the current slate. Sim results
            attach after the sim_engine finishes its cycle (~5 min after new
            picks land).
          </Text>
        ) : (
          simPicks.map((p) => (
            <PickRow key={p.id} pick={p} onPress={() => setSelectedId(p.id)} />
          ))
        )}
      </View>
    );
  }

  return (
    <View>
      <TouchableOpacity
        testID="lab-sim-back"
        onPress={() => { setSelectedId(null); setDetail(null); }}
        style={styles.backBtn}
      >
        <Ionicons name="chevron-back" size={16} color={COLORS.textPrimary} />
        <Text style={styles.backTxt}>Back to sim list</Text>
      </TouchableOpacity>
      {loading ? (
        <ActivityIndicator color={COLORS.textPrimary} style={{ marginTop: 24 }} />
      ) : detail ? (
        <SimulationDetail pick={detail} />
      ) : (
        <Text style={styles.errTxt}>Sim load failed.</Text>
      )}
    </View>
  );
}

function SimulationDetail({ pick }: { pick: any }) {
  const sim = pick.sim_result || pick.simulation || {};
  const scenarios = sim.scenario_breakdown || sim.scenarios || {};
  const scenarioEntries: [string, number][] = Object.entries(scenarios)
    .filter(([, v]) => typeof v === "number") as [string, number][];

  const runs = sim.runs || sim.n_runs || 0;
  const hitProb = sim.win_probability ?? sim.hit_probability ?? null;
  const ciLo = sim.ci_lower ?? null;
  const ciHi = sim.ci_upper ?? null;
  const variance = (ciLo != null && ciHi != null) ? (ciHi - ciLo) : null;

  return (
    <View>
      <View style={styles.detailHeader}>
        <View style={{ flex: 1 }}>
          <Text style={styles.detailSport}>{pick.sport}</Text>
          <Text style={styles.detailEvent} numberOfLines={2}>{pick.event || ""}</Text>
          <Text style={styles.detailMarket} numberOfLines={2}>{pick.market || pick.selection}</Text>
        </View>
        <View style={styles.lockBadge}>
          <Text style={styles.lockScore}>{Math.round(pick.lock_score || 0)}</Text>
          <Text style={styles.lockLabel}>LOCK</Text>
        </View>
      </View>

      <StatGrid
        cells={[
          { label: "Hit Prob", value: hitProb != null ? `${Number(hitProb).toFixed(1)}%` : "—",
            tint: hitProb != null && Number(hitProb) >= 55 ? "#40d18a" : undefined },
          { label: "Runs", value: runs ? Number(runs).toLocaleString() : "—" },
          { label: "CI Low", value: ciLo != null ? `${Number(ciLo).toFixed(1)}%` : "—" },
          { label: "CI High", value: ciHi != null ? `${Number(ciHi).toFixed(1)}%` : "—" },
        ]}
      />

      {variance != null && (
        <View style={styles.recRow}>
          <Text style={styles.recLabel}>Variance</Text>
          <Text style={[styles.recValue, { color: variance <= 8 ? "#40d18a" : variance <= 15 ? "#f5c542" : "#e46d6d" }]}>
            {variance <= 8 ? "LOW" : variance <= 15 ? "MEDIUM" : "HIGH"} ({variance.toFixed(1)} pt CI band)
          </Text>
        </View>
      )}

      {scenarioEntries.length > 0 && (
        <Section title="Scenario Breakdown">
          <Text style={styles.disclaimer}>
            % of Monte Carlo runs that finished in each scenario. Useful to
            see how robust the pick is across game scripts.
          </Text>
          {scenarioEntries.sort((a, b) => b[1] - a[1]).map(([name, pct]) => (
            <ScenarioBar key={name} label={name} pct={pct} />
          ))}
        </Section>
      )}

      {(sim.hist_hit != null || sim.usage_factor != null || sim.EV_units != null) && (
        <Section title="Signals">
          <StatGrid
            cells={[
              ...(sim.hist_hit != null ? [{ label: "Hist Hit", value: `${Number(sim.hist_hit * 100).toFixed(1)}%` }] : []),
              ...(sim.usage_factor != null ? [{ label: "Usage", value: `${Number(sim.usage_factor).toFixed(2)}x` }] : []),
              ...(sim.EV_units != null ? [{ label: "EV (u)", value: `${Number(sim.EV_units).toFixed(2)}`,
                tint: Number(sim.EV_units) >= 0 ? "#40d18a" : "#e46d6d" }] : []),
            ]}
          />
        </Section>
      )}

      {/* Best / worst case */}
      {ciLo != null && ciHi != null && (
        <Section title="Range of Outcomes">
          <View style={styles.rangeRow}>
            <View style={[styles.rangeBox, { backgroundColor: "#2b1e1e" }]}>
              <Text style={styles.rangeLabel}>Worst Case</Text>
              <Text style={styles.rangeValue}>{Number(ciLo).toFixed(1)}%</Text>
              <Text style={styles.rangeSub}>5th percentile</Text>
            </View>
            <View style={[styles.rangeBox, { backgroundColor: "#1e2b1e" }]}>
              <Text style={styles.rangeLabel}>Best Case</Text>
              <Text style={styles.rangeValue}>{Number(ciHi).toFixed(1)}%</Text>
              <Text style={styles.rangeSub}>95th percentile</Text>
            </View>
          </View>
        </Section>
      )}
    </View>
  );
}


// ═══════════════════════════════════════════════════════════════════
// MODULE 4: PROP EXPLORER
// ═══════════════════════════════════════════════════════════════════
type PropSort = "lock" | "edge" | "prob" | "sample";
function PropExplorerModule({ picks }: { picks: any[] }) {
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<PropSort>("lock");
  const [minLock, setMinLock] = useState<number>(0);

  // Player prop = pick that has a player_name AND market includes over/under/anytime.
  const props = useMemo(() => {
    const list = picks.filter((p) => {
      const m = (p.market || "").toLowerCase();
      const hasPlayer = !!(p.player_name || p.player);
      const looksLikeProp = /over|under|anytime|first|to score|to record/i.test(m);
      return hasPlayer || looksLikeProp;
    });

    const q = query.trim().toLowerCase();
    const filtered = list.filter((p) => {
      if (minLock > 0 && Number(p.lock_score || 0) < minLock) return false;
      if (!q) return true;
      const hay = [p.market, p.event, p.selection, p.player_name, p.team,
                   p.league, p.sport].filter(Boolean).join(" ").toLowerCase();
      return hay.includes(q);
    });

    const sorter = (a: any, b: any): number => {
      const va = (x: any) => {
        if (sort === "lock")   return Number(x.lock_score || 0);
        if (sort === "edge")   return Number(x.edge_percent || 0);
        if (sort === "prob")   return Number(x.win_probability || 0);
        if (sort === "sample") return Number(x.player_form_n || x.pick_rationale?.sample_size || 0);
        return 0;
      };
      return va(b) - va(a);
    };
    return filtered.sort(sorter).slice(0, 200);
  }, [picks, query, sort, minLock]);

  return (
    <View>
      <SectionHeader
        icon="list"
        title="Prop Explorer"
        blurb={`Search every live prop. Sort by lock, edge, or sample size. ${props.length} match your filters.`}
      />
      <View style={styles.searchWrap}>
        <Ionicons name="search" size={14} color={COLORS.textMuted} />
        <TextInput
          testID="lab-props-search"
          value={query}
          onChangeText={setQuery}
          placeholder="Player, team, market…"
          placeholderTextColor={COLORS.textMuted}
          style={styles.searchInput}
          autoCorrect={false}
          autoCapitalize="none"
        />
      </View>

      {/* Sort chips */}
      <ScrollView
        horizontal
        showsHorizontalScrollIndicator={false}
        contentContainerStyle={styles.chipRow}
      >
        {(["lock", "edge", "prob", "sample"] as PropSort[]).map((s) => (
          <TouchableOpacity
            key={s}
            testID={`lab-props-sort-${s}`}
            onPress={() => setSort(s)}
            style={[styles.filterChip, sort === s && styles.filterChipActive]}
          >
            <Text style={[styles.filterChipTxt, sort === s && styles.filterChipTxtActive]}>
              SORT: {s.toUpperCase()}
            </Text>
          </TouchableOpacity>
        ))}
        {[80, 90, 95].map((v) => (
          <TouchableOpacity
            key={v}
            testID={`lab-props-minlock-${v}`}
            onPress={() => setMinLock(minLock === v ? 0 : v)}
            style={[styles.filterChip, minLock === v && styles.filterChipActive]}
          >
            <Text style={[styles.filterChipTxt, minLock === v && styles.filterChipTxtActive]}>
              LOCK ≥ {v}
            </Text>
          </TouchableOpacity>
        ))}
      </ScrollView>

      {props.map((p) => (
        <PropRow key={p.id} pick={p} sort={sort} />
      ))}
    </View>
  );
}


// ═══════════════════════════════════════════════════════════════════
// MODULE 5: CHEATSHEETS
// ═══════════════════════════════════════════════════════════════════
// Themed rails (100% Recent Form, 100% Home/Away, 100% Head-to-Head)
// with tap-to-detail Trend Analysis modal. All data from
// /api/lab/cheatsheets + /api/lab/cheatsheet-detail.
// ═══════════════════════════════════════════════════════════════════
// MODULE 5: CHEATSHEETS  (Linemate-style tile deck — 2026-07-07 redesign)
// -------------------------------------------------------------------
// Each card is a self-contained tile with:
//   • Team-abbreviation logo chip (left)
//   • Player name + opponent (top-left, bold)
//   • Cleaned market line (below name)
//   • Book odds (top-right, badge)
//   • 3-5 stat facts, each with icon + text + right-aligned percentage
//
// Three tabs at the top switch how cards are ordered:
//   • "Most Locked"  — sorted by lock_score desc (default; catches the
//                       book's highest-confidence bets)
//   • "By Game"      — grouped by event so the user can build SGPs
//   • "Deepest Trend" — sorted by the strongest single fact percentage
//                       so users spot outliers like "8/8 last 8".
// ═══════════════════════════════════════════════════════════════════
type CheatTab = "locked" | "by_game" | "deepest";

// ═══════════════════════════════════════════════════════════════════
// MODULE: HOT HITTERS  (Stats-driven best-bets, book-agnostic)
// -------------------------------------------------------------------
// Ranks every active MLB hitter by a composite heat score built from
// L15 avg + OBP + OPS + current hit streak — INDEPENDENT of book
// odds coverage.  Surfaces niche batters (Otto Lopez, Gabriel
// Rincones etc.) that sportsbooks systematically skip even when
// they're on legitimate streaks.  Backed by `backend/hot_hitters.py`.
// ═══════════════════════════════════════════════════════════════════
function HotHittersModule() {
  const [data, setData] = useState<any | null>(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    setLoading(true);
    api.labHotHitters({ limit: 30 })
      .then(setData)
      .catch(() => setData({ hitters: [], total_ranked: 0 }))
      .finally(() => setLoading(false));
  }, []);

  const hitters: any[] = data?.hitters || [];
  return (
    <View>
      <SectionHeader
        icon="flame"
        title="Hot Hitters"
        blurb="Stats-driven leaderboard — L15 avg + OBP + OPS + hit streak. Book-agnostic so niche batters (Otto Lopez, Rincones, etc.) surface when they're hot."
      />
      {loading ? (
        <ActivityIndicator color={COLORS.textPrimary} style={{ marginTop: 20 }} />
      ) : hitters.length === 0 ? (
        <Text style={styles.disclaimer}>
          No hot-hitter data yet — MLB Stats API is silent or the L15
          window has no qualifying batters. Try again later.
        </Text>
      ) : (
        hitters.map((h) => <HotHitterCard key={h.player_id} hitter={h} />)
      )}
    </View>
  );
}


function HotHitterCard({ hitter }: { hitter: any }) {
  const playing = !!hitter.playing_today;
  const heatColor = hitter.heat_score >= 65
    ? "#40d18a"
    : hitter.heat_score >= 50
    ? COLORS.goldElite
    : COLORS.textMuted;
  const oppLine = hitter.next_opponent_abbr
    ? `vs ${hitter.next_opponent_abbr}${hitter.next_pitcher ? ` · ${hitter.next_pitcher}` : ""}`
    : "off tonight";
  return (
    <View style={cheatStyles.card}>
      <View style={cheatStyles.headerRow}>
        <View style={cheatStyles.teamChip}>
          <Text style={cheatStyles.teamChipTxt}>{hitter.team_abbr || "?"}</Text>
        </View>
        <View style={{ flex: 1 }}>
          <View style={{ flexDirection: "row", alignItems: "baseline", gap: 6 }}>
            <Text style={cheatStyles.player} numberOfLines={1}>
              {hitter.player_name}
            </Text>
            <Text style={cheatStyles.playerOpp} numberOfLines={1}>
              {playing ? oppLine : "— off day"}
            </Text>
          </View>
          <Text style={cheatStyles.market} numberOfLines={1}>
            L15 .{String(Math.round((hitter.l15_avg || 0) * 1000)).padStart(3, "0")}
            {" · OPS "}
            {(hitter.l15_ops || 0).toFixed(3).replace(/^0/, "")}
            {" · "}
            {hitter.l15_games}G
          </Text>
        </View>
        <View style={[cheatStyles.oddsBadge, { backgroundColor: "rgba(64,209,138,0.08)" }]}>
          <Text style={[cheatStyles.oddsTxt, { color: heatColor }]}>
            🔥 {hitter.heat_score}
          </Text>
        </View>
      </View>
      <View style={cheatStyles.factsBlock}>
        {(hitter.reasons || []).slice(0, 4).map((r: string, i: number) => (
          <View key={i} style={cheatStyles.factRow}>
            <Text style={cheatStyles.factTxt}>{r}</Text>
          </View>
        ))}
        {!playing && (
          <Text style={{ color: COLORS.textMuted, fontSize: 11, marginTop: 4 }}>
            Not on today&apos;s slate — bookmark for their next start.
          </Text>
        )}
      </View>
    </View>
  );
}


function CheatsheetsModule({ picks: _picks }: { picks: any[] }) {
  const [sport, setSport] = useState<string>("All");
  const [tab, setTab] = useState<CheatTab>("locked");
  const [data, setData] = useState<any | null>(null);
  const [loading, setLoading] = useState(true);
  const [detailPickId, setDetailPickId] = useState<string | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    api.labCheatsheets({ sport: sport === "All" ? undefined : sport, limit: 60 })
      .then(setData).catch(() => setData({ cards: [], groups: [] }))
      .finally(() => setLoading(false));
  }, [sport]);
  useEffect(() => { load(); }, [load]);

  const sportsAvail = ["All", "MLB", "NBA", "NFL", "Soccer", "Tennis"];
  const cards: any[] = useMemo(() => data?.cards || [], [data]);

  // Sort / group cards based on the active tab.
  const view = useMemo(() => {
    if (tab === "locked") {
      return [...cards].sort((a, b) => (b.lock_score || 0) - (a.lock_score || 0));
    }
    if (tab === "deepest") {
      // Score cards by best single fact percentage so 8/8 (100%) rises
      // above 5/8 (62%).
      const bestPct = (c: any) => Math.max(0,
        ...(c.facts || []).map((f: any) => (typeof f.pct === "number" ? f.pct : 0)));
      return [...cards].sort((a, b) => bestPct(b) - bestPct(a));
    }
    // by_game — return cards untouched; grouped downstream
    return cards;
  }, [cards, tab]);

  // Group cards by event for the "By Game" tab.
  const gameGroups = useMemo(() => {
    const map: Record<string, any[]> = {};
    for (const c of view) {
      const ev = c.event || c.opponent || "Other";
      if (!map[ev]) map[ev] = [];
      map[ev].push(c);
    }
    // Keep games with 2+ cards on top — more actionable for SGP building.
    return Object.entries(map)
      .sort(([, a], [, b]) => b.length - a.length);
  }, [view]);

  return (
    <View>
      <SectionHeader
        icon="flash"
        title="Cheatsheets"
        blurb="Real hit-streak proof cards. Tap any card for the full trend analysis + game log."
      />

      {/* Sport filter row */}
      <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.chipRow}>
        {sportsAvail.map((s) => (
          <TouchableOpacity key={s} onPress={() => setSport(s)}
            testID={`lab-cheats-sport-${s}`}
            style={[styles.filterChip, sport === s && styles.filterChipActive]}>
            <Text style={[styles.filterChipTxt, sport === s && styles.filterChipTxtActive]}>
              {s.toUpperCase()}
            </Text>
          </TouchableOpacity>
        ))}
      </ScrollView>

      {/* Cheat-mode tab switch */}
      <View style={cheatStyles.tabBar}>
        {([
          { id: "locked",   label: "Most Locked"   },
          { id: "by_game",  label: "By Game"       },
          { id: "deepest",  label: "Deepest Trend" },
        ] as { id: CheatTab; label: string }[]).map((t) => (
          <TouchableOpacity
            key={t.id}
            onPress={() => setTab(t.id)}
            style={[cheatStyles.tabBtn, tab === t.id && cheatStyles.tabBtnActive]}
            testID={`cheatsheet-tab-${t.id}`}
          >
            <Text style={[cheatStyles.tabTxt, tab === t.id && cheatStyles.tabTxtActive]}>
              {t.label}
            </Text>
          </TouchableOpacity>
        ))}
      </View>

      {loading ? (
        <ActivityIndicator color={COLORS.textPrimary} style={{ marginTop: 20 }} />
      ) : cards.length === 0 ? (
        <Text style={styles.disclaimer}>
          No cheatsheet-ready picks yet in {sport}. Cards populate as settled-pick
          history accumulates for players in today&apos;s slate.
        </Text>
      ) : tab === "by_game" ? (
        // "By Game" mode — group cards under an event header
        gameGroups.map(([ev, evCards]) => (
          <View key={ev} style={{ marginTop: 14 }}>
            <Text style={cheatStyles.gameHeader}>{ev}</Text>
            {evCards.map((c: any) => (
              <CheatsheetCard key={c.pick_id} card={c} onTap={setDetailPickId} />
            ))}
          </View>
        ))
      ) : (
        view.map((c: any) => (
          <CheatsheetCard key={c.pick_id} card={c} onTap={setDetailPickId} />
        ))
      )}

      {detailPickId ? (
        <CheatsheetDetailModal pickId={detailPickId} onClose={() => setDetailPickId(null)} />
      ) : null}
    </View>
  );
}


// Linemate-style card tile — team chip, player @ opp, market, odds,
// then N fact rows with icon + right-aligned percentage/value.
function CheatsheetCard({ card, onTap }:
  { card: any; onTap: (pickId: string) => void }) {
  const facts = card.facts || [];
  const oddsColor = (typeof card.book_odds === "number" && card.book_odds > 0)
    ? "#40d18a"                        // plus-money = green
    : COLORS.textPrimary;              // chalk = neutral
  const fmtOdds = (o: any) => {
    if (typeof o !== "number") return "";
    return o > 0 ? `+${o}` : String(o);
  };
  const teamAbbr = (card.team_abbr || card.opponent?.replace(/^(vs|@)\s+/, "") || "").slice(0, 3);
  return (
    <TouchableOpacity
      onPress={() => onTap(card.pick_id)}
      testID={`cheatsheet-card-${card.pick_id}`}
      style={cheatStyles.card}
      activeOpacity={0.85}
    >
      <View style={cheatStyles.headerRow}>
        {/* Team-abbr logo chip */}
        <View style={cheatStyles.teamChip}>
          <Text style={cheatStyles.teamChipTxt}>{teamAbbr}</Text>
        </View>
        {/* Player name + market */}
        <View style={{ flex: 1 }}>
          <View style={{ flexDirection: "row", alignItems: "baseline", gap: 6 }}>
            <Text style={cheatStyles.player} numberOfLines={1}>
              {card.player_display}
            </Text>
            {card.opponent ? (
              <Text style={cheatStyles.playerOpp} numberOfLines={1}>
                {card.opponent}
              </Text>
            ) : null}
          </View>
          <Text style={cheatStyles.market} numberOfLines={1}>
            {card.market_clean}
          </Text>
        </View>
        {/* Odds badge */}
        {card.book_odds != null ? (
          <View style={cheatStyles.oddsBadge}>
            <Text style={[cheatStyles.oddsTxt, { color: oddsColor }]}>
              {fmtOdds(card.book_odds)}
            </Text>
          </View>
        ) : null}
      </View>
      <View style={cheatStyles.factsBlock}>
        {facts.map((f: any, i: number) => (
          <View key={i} style={cheatStyles.factRow}>
            <Ionicons
              name={((f.icon || "flash") as any)}
              size={13}
              color={COLORS.textMuted}
              style={{ width: 16 }}
            />
            <Text style={cheatStyles.factTxt} numberOfLines={2}>
              {f.text}
            </Text>
            {typeof f.pct === "number" ? (
              <Text style={[cheatStyles.factPct, { color: pctTint(f.pct) }]}>
                {f.pct}%
              </Text>
            ) : (
              <Text style={[cheatStyles.factPct, { color: COLORS.textMuted, fontSize: 11 }]}>
                {f.hits}/{f.n}
              </Text>
            )}
          </View>
        ))}
      </View>
    </TouchableOpacity>
  );
}

function CheatsheetDetailModal({ pickId, onClose }:
  { pickId: string; onClose: () => void }) {
  const [detail, setDetail] = useState<any | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    api.labCheatsheetDetail(pickId)
      .then(setDetail).catch(() => setDetail(null))
      .finally(() => setLoading(false));
  }, [pickId]);

  return (
    <View style={cheatStyles.modalOverlay}>
      <View style={cheatStyles.modalCard}>
        <View style={cheatStyles.modalHeader}>
          <Text style={cheatStyles.modalTitle}>Trend Analysis</Text>
          <TouchableOpacity onPress={onClose} testID="cheatsheet-modal-close">
            <Ionicons name="close" size={22} color={COLORS.textPrimary} />
          </TouchableOpacity>
        </View>
        {loading ? (
          <ActivityIndicator color={COLORS.textPrimary} style={{ padding: 24 }} />
        ) : !detail ? (
          <Text style={styles.errTxt}>Detail load failed.</Text>
        ) : (
          <ScrollView style={{ maxHeight: 600 }}>
            <View style={{ padding: 4 }}>
              <Text style={cheatStyles.modalPlayer}>
                {detail.player_display}
                {detail.opponent ? <Text style={cheatStyles.modalOpp}>{"  vs "}{detail.opponent}</Text> : null}
              </Text>
              <Text style={cheatStyles.modalMarket}>{detail.market}</Text>
            </View>
            <View style={cheatStyles.modalStatsGrid}>
              <View style={cheatStyles.modalStatCell}>
                <View style={{ flexDirection: "row", alignItems: "center", gap: 4 }}>
                  <Ionicons name="flash" size={11} color={COLORS.textMuted} />
                  <Text style={cheatStyles.modalStatLabel}>Recent Form</Text>
                </View>
                <Text style={[cheatStyles.modalStatValue, { color: pctTint(detail.recent_form.pct) }]}>
                  {detail.recent_form.pct}%
                </Text>
                <Text style={cheatStyles.modalStatSub}>
                  {detail.recent_form.hits}/L{detail.recent_form.n} Games
                </Text>
              </View>
              <View style={cheatStyles.modalStatCell}>
                <View style={{ flexDirection: "row", alignItems: "center", gap: 4 }}>
                  <Ionicons name="chatbubbles" size={11} color={COLORS.textMuted} />
                  <Text style={cheatStyles.modalStatLabel}>Head to Head</Text>
                </View>
                <Text style={[cheatStyles.modalStatValue, { color: pctTint(detail.head_to_head.pct) }]}>
                  {detail.head_to_head.n > 0 ? `${detail.head_to_head.pct}%` : "—"}
                </Text>
                <Text style={cheatStyles.modalStatSub}>
                  {detail.head_to_head.n > 0
                    ? `${detail.head_to_head.hits}/L${detail.head_to_head.n} vs ${detail.head_to_head.opponent}`
                    : "no history"}
                </Text>
              </View>
              {detail.venue_split ? (
                <View style={cheatStyles.modalStatCell}>
                  <View style={{ flexDirection: "row", alignItems: "center", gap: 4 }}>
                    <Ionicons name="location" size={11} color={COLORS.textMuted} />
                    <Text style={cheatStyles.modalStatLabel}>{detail.venue_split.venue === "home" ? "Home Split" : "Away Split"}</Text>
                  </View>
                  <Text style={[cheatStyles.modalStatValue, { color: pctTint(detail.venue_split.pct) }]}>
                    {detail.venue_split.pct}%
                  </Text>
                  <Text style={cheatStyles.modalStatSub}>
                    {detail.venue_split.hits}/L{detail.venue_split.n} {detail.venue_split.venue}
                  </Text>
                </View>
              ) : null}
            </View>

            <View style={cheatStyles.gamesHeader}>
              <Ionicons name="calendar" size={13} color={COLORS.textPrimary} />
              <Text style={cheatStyles.gamesTitle}>Games Played</Text>
            </View>
            <View style={cheatStyles.gamesTableHeader}>
              <Text style={[cheatStyles.gamesCol, { flex: 0.6 }]}>Hit</Text>
              <Text style={[cheatStyles.gamesCol, { flex: 1.4 }]}>Date</Text>
              <Text style={[cheatStyles.gamesCol, { flex: 1.4 }]}>Versus</Text>
            </View>
            {detail.games.length === 0 ? (
              <Text style={styles.disclaimer}>No prior game log data.</Text>
            ) : (
              detail.games.map((g: any, i: number) => (
                <View key={i} style={cheatStyles.gamesRow}>
                  <View style={[cheatStyles.gamesCol, { flex: 0.6, flexDirection: "row" }]}>
                    <View style={[cheatStyles.hitDot, {
                      backgroundColor: g.hit ? "#40d18a" : "#e46d6d",
                    }]}>
                      <Ionicons name={g.hit ? "checkmark" : "close"} size={11} color="#0a0a0a" />
                    </View>
                  </View>
                  <Text style={[cheatStyles.gamesCol, cheatStyles.gamesCell, { flex: 1.4 }]}>
                    {g.date}
                  </Text>
                  <Text style={[cheatStyles.gamesCol, cheatStyles.gamesCell, { flex: 1.4, fontWeight: "700" }]}>
                    {g.opponent || "—"}
                  </Text>
                </View>
              ))
            )}
          </ScrollView>
        )}
      </View>
    </View>
  );
}

// Legacy client-side cheatsheet builder — replaced by /api/lab/cheatsheets
// which computes real streak facts from settled-pick history. The
// helper functions below (pctTint, shortenPlayer, extractOpponent,
// abbr, cleanMarket, cheatStyles) are still used by CheatsheetServerCard.

// ── cheatsheet helpers ────────────────────────────────────────────
function pctTint(pct: number): string {
  if (pct >= 90) return "#40d18a";
  if (pct >= 75) return "#c9d055";
  if (pct >= 60) return "#f5c542";
  return COLORS.textMuted;
}

// (shortenPlayer/extractOpponent/abbr/cleanMarket removed — cheatsheet
// server endpoint now handles all these formatting concerns.)

const cheatStyles = StyleSheet.create({
  // ── Tab bar (Most Locked / By Game / Deepest Trend) ────────────
  tabBar: { flexDirection: "row", gap: 8, marginTop: 12, marginBottom: 12 },
  tabBtn: {
    flex: 1, paddingVertical: 10, paddingHorizontal: 12,
    borderRadius: 10, alignItems: "center",
    backgroundColor: "rgba(255,255,255,0.04)",
    borderWidth: 1, borderColor: COLORS.borderDefault,
  },
  tabBtnActive: {
    backgroundColor: COLORS.textPrimary,
    borderColor: COLORS.textPrimary,
  },
  tabTxt: {
    color: COLORS.textMuted, fontSize: 12, fontWeight: "800", letterSpacing: 0.3,
  },
  tabTxtActive: { color: "#0a0a0a" },
  gameHeader: {
    color: COLORS.textPrimary, fontSize: 12, fontWeight: "900", letterSpacing: 0.4,
    textTransform: "uppercase", paddingBottom: 6, marginBottom: 6,
    borderBottomWidth: 1, borderBottomColor: COLORS.borderDefault,
  },

  // ── Linemate-style card tile ───────────────────────────────────
  card: {
    backgroundColor: "#141414",
    borderRadius: 14,
    borderWidth: 1,
    borderColor: COLORS.borderDefault,
    padding: 14,
    marginBottom: 10,
  },
  headerRow: { flexDirection: "row", alignItems: "center", marginBottom: 12, gap: 10 },
  teamChip: {
    width: 36, height: 36, borderRadius: 18,
    backgroundColor: "rgba(255,255,255,0.06)",
    borderWidth: 1, borderColor: COLORS.borderDefault,
    alignItems: "center", justifyContent: "center",
  },
  teamChipTxt: {
    color: COLORS.textPrimary, fontSize: 11, fontWeight: "900", letterSpacing: 0.3,
  },
  player: { color: COLORS.textPrimary, fontSize: 15, fontWeight: "900", letterSpacing: 0.2 },
  playerOpp: { color: COLORS.textMuted, fontSize: 13, fontWeight: "600" },
  market: { color: COLORS.textMuted, fontSize: 12.5, fontWeight: "600", marginTop: 2 },
  oddsBadge: {
    paddingHorizontal: 10, paddingVertical: 6,
    borderRadius: 8,
    backgroundColor: "rgba(255,255,255,0.05)",
    borderWidth: 1, borderColor: COLORS.borderDefault,
  },
  oddsTxt: { fontSize: 13, fontWeight: "900", letterSpacing: 0.4 },

  factsBlock: { gap: 10 },
  factRow: { flexDirection: "row", alignItems: "center", gap: 10 },
  factTxt: { flex: 1, color: COLORS.textPrimary, fontSize: 13, fontWeight: "500" },
  factPct: { fontSize: 14, fontWeight: "900", minWidth: 44, textAlign: "right" },

  // ── Legacy group-rail styles (kept in case other modules use them) ──
  groupHeader: { flexDirection: "row", alignItems: "center", gap: 6,
    paddingBottom: 8, marginBottom: 4,
    borderBottomWidth: 1, borderBottomColor: COLORS.borderDefault },
  groupTitle: { color: COLORS.textPrimary, fontSize: 13, fontWeight: "900", letterSpacing: 0.4 },
  groupRow: { flexDirection: "row", alignItems: "center",
    paddingVertical: 10, gap: 10 },
  groupRowPlayer: { color: COLORS.textPrimary, fontSize: 13 },
  groupRowMarket: { color: COLORS.textMuted, fontWeight: "500" },
  groupRowRight: { paddingLeft: 8 },
  groupRowRatio: { fontSize: 14, fontWeight: "900", letterSpacing: 0.3 },

  // ── Detail modal (Trend Analysis screen) ───────────────────────
  modalOverlay: { position: "absolute", top: 0, left: 0, right: 0, bottom: 0,
    backgroundColor: "rgba(0,0,0,0.85)",
    justifyContent: "center", alignItems: "stretch", padding: 12, zIndex: 999 },
  modalCard: { backgroundColor: "#141414", borderRadius: 14,
    borderWidth: 1, borderColor: COLORS.borderDefault, padding: 14, maxHeight: "90%" },
  modalHeader: { flexDirection: "row", alignItems: "center",
    justifyContent: "space-between", marginBottom: 10 },
  modalTitle: { color: COLORS.textPrimary, fontSize: 17, fontWeight: "900", letterSpacing: 0.3 },
  modalPlayer: { color: COLORS.textPrimary, fontSize: 18, fontWeight: "900", marginBottom: 2 },
  modalOpp: { color: COLORS.textMuted, fontSize: 15, fontWeight: "600" },
  modalMarket: { color: COLORS.textMuted, fontSize: 13, marginBottom: 12 },
  modalStatsGrid: { flexDirection: "row", gap: 8, marginBottom: 16 },
  modalStatCell: { flex: 1, padding: 10, borderRadius: 8,
    backgroundColor: "rgba(255,255,255,0.04)",
    borderWidth: 1, borderColor: COLORS.borderDefault },
  modalStatLabel: { color: COLORS.textMuted, fontSize: 10, fontWeight: "700", letterSpacing: 0.5 },
  modalStatValue: { color: COLORS.textPrimary, fontSize: 20, fontWeight: "900", marginTop: 4 },
  modalStatSub: { color: COLORS.textMuted, fontSize: 11, marginTop: 2 },
  gamesHeader: { flexDirection: "row", alignItems: "center", gap: 6,
    paddingBottom: 8, borderBottomWidth: 1, borderBottomColor: COLORS.borderDefault, marginBottom: 6 },
  gamesTitle: { color: COLORS.textPrimary, fontSize: 14, fontWeight: "900", letterSpacing: 0.3 },
  gamesTableHeader: { flexDirection: "row", paddingVertical: 6,
    borderBottomWidth: 1, borderBottomColor: "rgba(255,255,255,0.04)" },
  gamesCol: { color: COLORS.textMuted, fontSize: 11, letterSpacing: 0.5, fontWeight: "800" },
  gamesRow: { flexDirection: "row", alignItems: "center",
    paddingVertical: 8, borderBottomWidth: 1, borderBottomColor: "rgba(255,255,255,0.04)" },
  gamesCell: { color: COLORS.textPrimary, fontSize: 12, fontWeight: "600" },
  hitDot: { width: 22, height: 22, borderRadius: 12,
    alignItems: "center", justifyContent: "center" },
});


// ═══════════════════════════════════════════════════════════════════
// MODULE 6: CORRELATION LAB (v2 — actionable parlay-building intel)
// ═══════════════════════════════════════════════════════════════════
function CorrelationModule() {
  const [sport, setSport] = useState<string>("");
  const [data, setData] = useState<any | null>(null);
  const [loading, setLoading] = useState(false);
  const [detailRow, setDetailRow] = useState<any | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    // Phase 1 (2026-08-11): route through the centralized backend URL
    // resolver so lab.tsx obeys the same production/native fail-loud
    // contract as every other consumer.
    let base: string;
    try { base = getBackendUrl(); }
    catch (e) {
      setData({ sections: {}, error: String((e as Error)?.message || e) });
      setLoading(false);
      return;
    }
    fetch(`${base}/api/lab/correlations-v2?limit_per_section=10${sport ? `&sport=${sport}` : ""}`)
      .then((r) => r.json())
      .then(setData)
      .catch(() => setData({ sections: {} }))
      .finally(() => setLoading(false));
  }, [sport]);
  useEffect(() => { load(); }, [load]);

  const sports = ["", "MLB", "NBA", "NFL", "Soccer", "Tennis"];
  const sections = data?.sections || {};

  return (
    <View>
      <SectionHeader
        icon="git-network"
        title="Correlation Lab"
        blurb="Which two bets should you parlay? Sections below show combinations that historically win together, high-ROI pairs, and pairs to avoid."
      />
      <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.chipRow}>
        {sports.map((s) => (
          <TouchableOpacity key={s || "all"} onPress={() => setSport(s)}
            style={[styles.filterChip, sport === s && styles.filterChipActive]}>
            <Text style={[styles.filterChipTxt, sport === s && styles.filterChipTxtActive]}>
              {(s || "ALL").toUpperCase()}
            </Text>
          </TouchableOpacity>
        ))}
      </ScrollView>
      {loading ? (
        <ActivityIndicator color={COLORS.textPrimary} style={{ marginTop: 20 }} />
      ) : (
        ["todays_best", "best_historical", "highest_roi", "avoid_negative"].map((k) => {
          const sec = sections[k];
          if (!sec || !sec.rows?.length) return null;
          return (
            <View key={k} style={styles.subSection}>
              <View style={styles.subSectionRow}>
                <Text style={styles.subSectionTitle}>{sec.title}</Text>
              </View>
              <Text style={styles.disclaimer}>{sec.blurb}</Text>
              {sec.rows.map((r: any, i: number) => (
                <TouchableOpacity
                  key={`${k}_${i}`}
                  onPress={() => setDetailRow(r)}
                  style={styles.pickRow}
                >
                  <View style={{ flex: 1 }}>
                    <Text style={styles.pickMarket} numberOfLines={2}>
                      {r.leg_a_display} + {r.leg_b_display}
                    </Text>
                    <Text style={styles.pickEvent}>{r.plain_english}</Text>
                    {r.sample_size > 0 ? (
                      <Text style={styles.pickEvent}>
                        n={r.sample_size} · cohit {r.cohit_pct}% · ROI {r.roi_pct >= 0 ? "+" : ""}{r.roi_pct}%
                      </Text>
                    ) : (
                      <Text style={styles.pickEvent}>
                        SGP · combo {r.combo_odds > 0 ? "+" : ""}{r.combo_odds} · avg lock {r.avg_lock}
                      </Text>
                    )}
                  </View>
                  <View style={styles.pickRight}>
                    <View style={{
                      paddingHorizontal: 8, paddingVertical: 4, borderRadius: 6,
                      backgroundColor: badgeBg(r.badge?.tint),
                    }}>
                      <Text style={{ color: badgeText(r.badge?.tint), fontSize: 10, fontWeight: "900", letterSpacing: 0.4 }}>
                        {r.badge?.label || "—"}
                      </Text>
                    </View>
                    <Text style={[styles.pickLockLabel, { marginTop: 4 }]}>{r.ai_confidence}% CONF</Text>
                  </View>
                </TouchableOpacity>
              ))}
            </View>
          );
        })
      )}
      {detailRow ? (
        <View style={cheatStyles.modalOverlay}>
          <View style={cheatStyles.modalCard}>
            <View style={cheatStyles.modalHeader}>
              <Text style={cheatStyles.modalTitle}>Why This Pairing?</Text>
              <TouchableOpacity onPress={() => setDetailRow(null)}>
                <Ionicons name="close" size={22} color={COLORS.textPrimary} />
              </TouchableOpacity>
            </View>
            <ScrollView>
              <Text style={cheatStyles.modalPlayer}>
                {detailRow.leg_a_display}
              </Text>
              <Text style={cheatStyles.modalOpp}>+ {detailRow.leg_b_display}</Text>
              <View style={{ height: 12 }} />
              <Text style={styles.pickMarket}>{detailRow.plain_english}</Text>
              <View style={{ height: 12 }} />
              <Text style={styles.disclaimer}>{detailRow.explanation}</Text>
              {detailRow.sample_size > 0 ? (
                <StatGrid
                  cells={[
                    { label: "SAMPLE", value: String(detailRow.sample_size) },
                    { label: "CO-HIT", value: `${detailRow.cohit_pct}%` },
                    { label: "ROI", value: `${detailRow.roi_pct >= 0 ? "+" : ""}${detailRow.roi_pct}%`,
                      tint: detailRow.roi_pct >= 0 ? "#40d18a" : "#e46d6d" },
                    { label: "AI CONF", value: `${detailRow.ai_confidence}%` },
                  ]}
                />
              ) : (
                <StatGrid
                  cells={[
                    { label: "COMBO ODDS", value: `${detailRow.combo_odds > 0 ? "+" : ""}${detailRow.combo_odds}` },
                    { label: "AVG LOCK", value: `${detailRow.avg_lock}` },
                    { label: "TYPE", value: "SGP" },
                  ]}
                />
              )}
            </ScrollView>
          </View>
        </View>
      ) : null}
    </View>
  );
}

function badgeBg(tint?: string): string {
  if (tint === "green") return "rgba(64,209,138,0.15)";
  if (tint === "lime")  return "rgba(201,208,85,0.15)";
  if (tint === "red")   return "rgba(228,109,109,0.15)";
  return "rgba(255,255,255,0.06)";
}
function badgeText(tint?: string): string {
  if (tint === "green") return "#40d18a";
  if (tint === "lime")  return "#c9d055";
  if (tint === "red")   return "#e46d6d";
  return COLORS.textMuted;
}


// ═══════════════════════════════════════════════════════════════════
// MODULE 7: BET BACKTESTER
// ═══════════════════════════════════════════════════════════════════
function BacktestModule() {
  const [sport, setSport] = useState<string>("");
  const [family, setFamily] = useState<string>("");
  const [oddsMin, setOddsMin] = useState<string>("");
  const [oddsMax, setOddsMax] = useState<string>("");
  const [edgeMin, setEdgeMin] = useState<string>("");
  const [lockMin, setLockMin] = useState<string>("");
  const [data, setData] = useState<any | null>(null);
  const [loading, setLoading] = useState(false);

  const run = useCallback(() => {
    setLoading(true);
    api.labBacktest({
      sport: sport || undefined,
      market_family: family || undefined,
      odds_min: oddsMin ? parseInt(oddsMin, 10) : undefined,
      odds_max: oddsMax ? parseInt(oddsMax, 10) : undefined,
      edge_min: edgeMin ? parseFloat(edgeMin) : undefined,
      lock_min: lockMin ? parseFloat(lockMin) : undefined,
    }).then(setData).catch(() => setData(null)).finally(() => setLoading(false));
  }, [sport, family, oddsMin, oddsMax, edgeMin, lockMin]);

  useEffect(() => { run(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const sports = ["", "MLB", "NBA", "NFL", "Soccer", "Tennis"];
  return (
    <View>
      <SectionHeader
        icon="trending-up"
        title="Perklocks Performance Backtest"
        blurb="Historical performance of PERKLOCKS PUBLISHED PICKS only — not every historical sportsbook opportunity. Filter to see how a strategy would have graded across the settled Perklocks board."
      />

      <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.chipRow}>
        {sports.map((s) => (
          <TouchableOpacity
            key={s || "all"}
            onPress={() => setSport(s)}
            style={[styles.filterChip, sport === s && styles.filterChipActive]}
          >
            <Text style={[styles.filterChipTxt, sport === s && styles.filterChipTxtActive]}>
              {(s || "ALL").toUpperCase()}
            </Text>
          </TouchableOpacity>
        ))}
      </ScrollView>

      <View style={styles.evInputRow}>
        <View style={styles.evField}>
          <Text style={styles.evFieldLabel}>Odds Min</Text>
          <TextInput value={oddsMin} onChangeText={setOddsMin} placeholder="-200" placeholderTextColor={COLORS.textMuted} keyboardType="numbers-and-punctuation" style={styles.evInput} />
        </View>
        <View style={styles.evField}>
          <Text style={styles.evFieldLabel}>Odds Max</Text>
          <TextInput value={oddsMax} onChangeText={setOddsMax} placeholder="+300" placeholderTextColor={COLORS.textMuted} keyboardType="numbers-and-punctuation" style={styles.evInput} />
        </View>
      </View>
      <View style={styles.evInputRow}>
        <View style={styles.evField}>
          <Text style={styles.evFieldLabel}>Min Edge %</Text>
          <TextInput value={edgeMin} onChangeText={setEdgeMin} placeholder="3" placeholderTextColor={COLORS.textMuted} keyboardType="decimal-pad" style={styles.evInput} />
        </View>
        <View style={styles.evField}>
          <Text style={styles.evFieldLabel}>Min Lock</Text>
          <TextInput value={lockMin} onChangeText={setLockMin} placeholder="80" placeholderTextColor={COLORS.textMuted} keyboardType="decimal-pad" style={styles.evInput} />
        </View>
        <View style={styles.evField}>
          <Text style={styles.evFieldLabel}>Family (opt)</Text>
          <TextInput value={family} onChangeText={setFamily} placeholder="MLB_HR" placeholderTextColor={COLORS.textMuted} autoCapitalize="characters" style={styles.evInput} />
        </View>
      </View>

      <TouchableOpacity onPress={run} style={[styles.retryBtn, { alignSelf: "flex-start", marginBottom: 12 }]}>
        <Text style={styles.retryTxt}>{loading ? "…" : "RUN BACKTEST"}</Text>
      </TouchableOpacity>

      {data ? (
        <View>
          <StatGrid
            cells={[
              { label: "N", value: String(data.sample_size) },
              { label: "Hit%", value: `${(data.hit_rate * 100).toFixed(1)}%`,
                tint: data.hit_rate >= 0.55 ? "#40d18a" : data.hit_rate >= 0.5 ? undefined : "#e46d6d" },
              { label: "ROI", value: `${(data.roi * 100).toFixed(1)}%`,
                tint: data.roi >= 0.03 ? "#40d18a" : data.roi >= -0.02 ? undefined : "#e46d6d" },
              { label: "Units", value: `${data.units_profit >= 0 ? "+" : ""}${data.units_profit.toFixed(1)}u`,
                tint: data.units_profit >= 0 ? "#40d18a" : "#e46d6d" },
            ]}
          />
          <View style={styles.recRow}>
            <Text style={styles.recLabel}>Verdict</Text>
            <Text style={[styles.recValue, { color: verdictTint(data.roi, data.sample_size) }]}>
              {data.verdict}
            </Text>
          </View>
          {data.best_day && data.worst_day ? (
            <View style={styles.rangeRow}>
              <View style={[styles.rangeBox, { backgroundColor: "#1e2b1e" }]}>
                <Text style={styles.rangeLabel}>Best Day</Text>
                <Text style={styles.rangeValue}>+{data.best_day.units.toFixed(1)}u</Text>
                <Text style={styles.rangeSub}>{data.best_day.date}</Text>
              </View>
              <View style={[styles.rangeBox, { backgroundColor: "#2b1e1e" }]}>
                <Text style={styles.rangeLabel}>Worst Day</Text>
                <Text style={styles.rangeValue}>{data.worst_day.units.toFixed(1)}u</Text>
                <Text style={styles.rangeSub}>{data.worst_day.date}</Text>
              </View>
            </View>
          ) : null}
          {data.family_breakdown?.length ? (
            <Section title="By Market Family">
              {data.family_breakdown.map((f: any) => (
                <View key={f.family} style={styles.pickRow}>
                  <View style={{ flex: 1 }}>
                    <Text style={styles.pickMarket}>{f.family}</Text>
                    <Text style={styles.pickEvent}>n={f.n} · hit {(f.hit_rate * 100).toFixed(0)}% · ROI {(f.roi * 100).toFixed(1)}%</Text>
                  </View>
                  <View style={styles.pickRight}>
                    <Text style={[styles.pickLock, { color: f.units_profit >= 0 ? "#40d18a" : "#e46d6d" }]}>
                      {f.units_profit >= 0 ? "+" : ""}{f.units_profit.toFixed(1)}
                    </Text>
                    <Text style={styles.pickLockLabel}>UNITS</Text>
                  </View>
                </View>
              ))}
            </Section>
          ) : null}
        </View>
      ) : null}
    </View>
  );
}

function verdictTint(roi: number, n: number): string {
  if (n < 30) return COLORS.textMuted;
  if (roi >= 0.1) return "#40d18a";
  if (roi >= 0.03) return "#c9d055";
  if (roi >= -0.02) return COLORS.textPrimary;
  return "#e46d6d";
}


// ═══════════════════════════════════════════════════════════════════
// MODULE 8: PATTERN FINDER
// ═══════════════════════════════════════════════════════════════════
function PatternsModule() {
  const [axis, setAxis] = useState<string>("family_odds");
  const [sport, setSport] = useState<string>("");
  const [minN, setMinN] = useState<number>(20);
  const [data, setData] = useState<any | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    api.labPatterns({ axis, sport: sport || undefined, min_n: minN, limit: 25 })
      .then(setData).catch(() => setData(null)).finally(() => setLoading(false));
  }, [axis, sport, minN]);

  useEffect(() => { load(); }, [load]);

  const axes: [string, string][] = [
    ["family_odds", "FAMILY × ODDS"],
    ["family_edge", "FAMILY × EDGE"],
    ["family_lock", "FAMILY × LOCK"],
    ["sport_odds",  "SPORT × ODDS"],
    ["dow",         "DAY OF WEEK"],
  ];
  const sports = ["", "MLB", "NBA", "NFL", "Soccer", "Tennis"];
  return (
    <View>
      <SectionHeader
        icon="sparkles"
        title="Pattern Finder"
        blurb="Auto-mined profitable buckets across settled picks. Ranked by Wilson lower bound so big-sample wins float to the top and small-sample flukes stay hidden."
      />
      <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.chipRow}>
        {axes.map(([a, label]) => (
          <TouchableOpacity key={a} onPress={() => setAxis(a)}
            style={[styles.filterChip, axis === a && styles.filterChipActive]}>
            <Text style={[styles.filterChipTxt, axis === a && styles.filterChipTxtActive]}>{label}</Text>
          </TouchableOpacity>
        ))}
      </ScrollView>
      <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.chipRow}>
        {sports.map((s) => (
          <TouchableOpacity key={s || "all"} onPress={() => setSport(s)}
            style={[styles.filterChip, sport === s && styles.filterChipActive]}>
            <Text style={[styles.filterChipTxt, sport === s && styles.filterChipTxtActive]}>{(s || "ALL").toUpperCase()}</Text>
          </TouchableOpacity>
        ))}
        {[10, 20, 50, 100].map((n) => (
          <TouchableOpacity key={`n${n}`} onPress={() => setMinN(n)}
            style={[styles.filterChip, minN === n && styles.filterChipActive]}>
            <Text style={[styles.filterChipTxt, minN === n && styles.filterChipTxtActive]}>MIN N={n}</Text>
          </TouchableOpacity>
        ))}
      </ScrollView>

      {loading ? (
        <ActivityIndicator color={COLORS.textPrimary} style={{ marginTop: 20 }} />
      ) : !data?.rows?.length ? (
        <Text style={styles.disclaimer}>No patterns pass the sample-size cutoff. Lower Min N or change axis.</Text>
      ) : (
        <View>
          <Text style={styles.subhead}>
            {data.rows.length} of {data.buckets_considered} buckets · sorted by Wilson lower bound
          </Text>
          {data.rows.map((r: any, i: number) => (
            <View key={`${r.bucket}_${i}`} style={styles.pickRow}>
              <View style={{ flex: 1 }}>
                <Text style={styles.pickMarket} numberOfLines={2}>{r.bucket}</Text>
                <Text style={styles.pickEvent}>
                  n={r.n} · hit {(r.hit_rate * 100).toFixed(1)}% · Wilson {(r.wilson_lower * 100).toFixed(1)}% · ROI {(r.roi * 100).toFixed(1)}%
                </Text>
              </View>
              <View style={styles.pickRight}>
                <Text style={[styles.pickLock, { color: r.units_profit >= 0 ? "#40d18a" : "#e46d6d" }]}>
                  {r.units_profit >= 0 ? "+" : ""}{r.units_profit.toFixed(1)}
                </Text>
                <Text style={styles.pickLockLabel}>UNITS</Text>
              </View>
            </View>
          ))}
        </View>
      )}
    </View>
  );
}


// ═══════════════════════════════════════════════════════════════════
// MODULE 9: MATCHUP DNA
// ═══════════════════════════════════════════════════════════════════
function MatchupDNAModule() {
  const [sport, setSport] = useState<string>("MLB");
  const [subject, setSubject] = useState<string>("");
  const [opponent, setOpponent] = useState<string>("");
  const [data, setData] = useState<any | null>(null);
  const [loading, setLoading] = useState(false);

  const run = useCallback(() => {
    if (!subject.trim()) { setData(null); return; }
    setLoading(true);
    api.labMatchupDNA(sport, subject.trim(), opponent.trim() || undefined)
      .then(setData).catch(() => setData(null)).finally(() => setLoading(false));
  }, [sport, subject, opponent]);

  const sports = ["MLB", "NBA", "NFL", "Soccer", "Tennis", "UFC"];
  return (
    <View>
      <SectionHeader
        icon="body"
        title="Matchup DNA"
        blurb="Deep profile for any player from settled-pick history. Overall record, by-market breakdown, home/away split, hot/cold streak, optional vs-opponent filter."
      />
      <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.chipRow}>
        {sports.map((s) => (
          <TouchableOpacity key={s} onPress={() => setSport(s)}
            style={[styles.filterChip, sport === s && styles.filterChipActive]}>
            <Text style={[styles.filterChipTxt, sport === s && styles.filterChipTxtActive]}>{s.toUpperCase()}</Text>
          </TouchableOpacity>
        ))}
      </ScrollView>
      <View style={styles.searchWrap}>
        <Ionicons name="person" size={14} color={COLORS.textMuted} />
        <TextInput
          value={subject}
          onChangeText={setSubject}
          placeholder="Player name (e.g. Alonso)"
          placeholderTextColor={COLORS.textMuted}
          style={styles.searchInput}
          autoCorrect={false}
          onSubmitEditing={run}
        />
      </View>
      <View style={styles.searchWrap}>
        <Ionicons name="shield" size={14} color={COLORS.textMuted} />
        <TextInput
          value={opponent}
          onChangeText={setOpponent}
          placeholder="Opponent (optional — e.g. Reds, Nadal)"
          placeholderTextColor={COLORS.textMuted}
          style={styles.searchInput}
          autoCorrect={false}
          onSubmitEditing={run}
        />
      </View>
      <TouchableOpacity onPress={run} style={[styles.retryBtn, { alignSelf: "flex-start", marginBottom: 12 }]}>
        <Text style={styles.retryTxt}>{loading ? "…" : "LOOKUP"}</Text>
      </TouchableOpacity>

      {data ? (
        <View>
          <View style={styles.detailHeader}>
            <View style={{ flex: 1 }}>
              <Text style={styles.detailSport}>{data.sport}</Text>
              <Text style={styles.detailEvent}>{data.subject}</Text>
              <Text style={styles.detailMarket}>{data.hot_cold}</Text>
            </View>
          </View>

          <StatGrid
            cells={[
              { label: "N", value: String(data.overall.n) },
              { label: "Won", value: String(data.overall.won), tint: "#40d18a" },
              { label: "Lost", value: String(data.overall.lost), tint: "#e46d6d" },
              { label: "Hit%", value: `${(data.overall.hit_rate * 100).toFixed(1)}%`,
                tint: data.overall.hit_rate >= 0.55 ? "#40d18a" : undefined },
            ]}
          />
          <StatGrid
            cells={[
              { label: "Units", value: `${data.overall.units_profit >= 0 ? "+" : ""}${data.overall.units_profit.toFixed(2)}u`,
                tint: data.overall.units_profit >= 0 ? "#40d18a" : "#e46d6d" },
              { label: "ROI", value: `${(data.overall.roi * 100).toFixed(1)}%`,
                tint: data.overall.roi >= 0 ? "#40d18a" : "#e46d6d" },
              { label: "Home n", value: String(data.home_away?.home?.n || 0) },
              { label: "Away n", value: String(data.home_away?.away?.n || 0) },
            ]}
          />

          {data.vs_opponent ? (
            <Section title={`vs ${data.vs_opponent.opponent}`}>
              {data.vs_opponent.n > 0 ? (
                <StatGrid
                  cells={[
                    { label: "N", value: String(data.vs_opponent.n) },
                    { label: "Hit%", value: `${((data.vs_opponent.hit_rate || 0) * 100).toFixed(1)}%` },
                    { label: "Units", value: `${(data.vs_opponent.units_profit || 0) >= 0 ? "+" : ""}${(data.vs_opponent.units_profit || 0).toFixed(2)}u`,
                      tint: (data.vs_opponent.units_profit || 0) >= 0 ? "#40d18a" : "#e46d6d" },
                    { label: "ROI", value: `${((data.vs_opponent.roi || 0) * 100).toFixed(1)}%` },
                  ]}
                />
              ) : (
                <Text style={styles.disclaimer}>No prior picks vs this opponent.</Text>
              )}
            </Section>
          ) : null}

          {data.by_market?.length ? (
            <Section title="By Market">
              {data.by_market.map((f: any) => (
                <View key={f.family} style={styles.pickRow}>
                  <View style={{ flex: 1 }}>
                    <Text style={styles.pickMarket}>{f.family}</Text>
                    <Text style={styles.pickEvent}>
                      n={f.n} · {f.won}-{f.lost} · hit {(f.hit_rate * 100).toFixed(0)}%
                    </Text>
                  </View>
                  <View style={styles.pickRight}>
                    <Text style={[styles.pickLock, { color: f.units_profit >= 0 ? "#40d18a" : "#e46d6d" }]}>
                      {f.units_profit >= 0 ? "+" : ""}{f.units_profit.toFixed(1)}
                    </Text>
                    <Text style={styles.pickLockLabel}>UNITS</Text>
                  </View>
                </View>
              ))}
            </Section>
          ) : null}

          {data.recent_form?.length ? (
            <Section title="Recent Form">
              <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 4 }}>
                {data.recent_form.map((r: any, i: number) => (
                  <View
                    key={i}
                    style={{
                      width: 14, height: 14, borderRadius: 3,
                      backgroundColor:
                        r.status === "won" ? "#40d18a" :
                        r.status === "lost" ? "#e46d6d" :
                        "#666",
                    }}
                  />
                ))}
              </View>
              <Text style={[styles.disclaimer, { marginTop: 6 }]}>
                Latest {data.recent_form.length} settled picks (left = newest).
              </Text>
            </Section>
          ) : null}
        </View>
      ) : subject.trim() ? (
        !loading && <Text style={styles.disclaimer}>No settled picks found for &quot;{subject}&quot; in {sport}.</Text>
      ) : (
        <Text style={styles.disclaimer}>Enter a player name and tap LOOKUP.</Text>
      )}
    </View>
  );
}


function SectionHeader({ icon, title, blurb }:
  { icon: keyof typeof Ionicons.glyphMap; title: string; blurb?: string }) {
  return (
    <View style={styles.sectionHeader}>
      <View style={styles.sectionHeaderRow}>
        <Ionicons name={icon} size={16} color={COLORS.textPrimary} />
        <Text style={styles.sectionTitle}>{title}</Text>
      </View>
      {blurb ? <Text style={styles.sectionBlurb}>{blurb}</Text> : null}
    </View>
  );
}

// ─────────────────────────────────────────────────────────────────────
//  ANALYTICS MODULE (Phase 5) — CLV Tracker + Kelly Calc + Steam Alerts
// ─────────────────────────────────────────────────────────────────────
type AnalyticsTab = "clv" | "kelly" | "steam";

function AnalyticsModule() {
  const [tab, setTab] = useState<AnalyticsTab>("clv");
  return (
    <View>
      <SectionHeader
        icon="stats-chart"
        title="Advanced Analytics"
        blurb="Sharp-bettor tools — Closing Line Value tracker, ¼-Kelly staking calc, and live steam-move detection."
      />
      <View style={[styles.chipRow, { flexDirection: "row", flexWrap: "wrap" }]}>
        {[
          { id: "clv" as const, label: "CLV" },
          { id: "kelly" as const, label: "Kelly Calc" },
          { id: "steam" as const, label: "Steam" },
        ].map((t) => (
          <TouchableOpacity
            key={t.id}
            onPress={() => setTab(t.id)}
            style={[styles.filterChip, tab === t.id && styles.filterChipActive]}
          >
            <Text style={[styles.filterChipTxt, tab === t.id && styles.filterChipTxtActive]}>
              {t.label}
            </Text>
          </TouchableOpacity>
        ))}
      </View>
      {tab === "clv" && <CLVSection />}
      {tab === "kelly" && <KellySection />}
      {tab === "steam" && <SteamSection />}
    </View>
  );
}

function CLVSection() {
  const [days, setDays] = useState<30 | 7 | 90>(30);
  const [data, setData] = useState<any | null>(null);
  const [loading, setLoading] = useState(false);
  const load = useCallback(() => {
    setLoading(true);
    api.clvReport(days).then(setData).catch(() => setData(null)).finally(() => setLoading(false));
  }, [days]);
  useEffect(() => { load(); }, [load]);
  if (loading && !data) return <ActivityIndicator color={COLORS.textPrimary} style={{ marginTop: 24 }} />;
  if (!data) return <Text style={styles.emptyTxt}>Failed to load CLV report.</Text>;
  const o = data.overall || {};
  const beatClose = o.beat_close_pct;
  return (
    <View>
      <View style={[styles.chipRow, { flexDirection: "row", flexWrap: "wrap" }]}>
        {[7, 30, 90].map((d) => (
          <TouchableOpacity
            key={d}
            onPress={() => setDays(d as any)}
            style={[styles.filterChip, days === d && styles.filterChipActive]}
          >
            <Text style={[styles.filterChipTxt, days === d && styles.filterChipTxtActive]}>
              {d}d
            </Text>
          </TouchableOpacity>
        ))}
      </View>
      <StatGrid
        cells={[
          { label: "N", value: String(o.n || 0) },
          { label: "Win%", value: `${(o.win_pct || 0).toFixed(1)}%`,
            tint: o.win_pct >= 55 ? "#40d18a" : o.win_pct >= 50 ? undefined : "#e46d6d" },
          { label: "ROI/100u", value: `${(o.roi_per_100u >= 0 ? "+" : "")}${(o.roi_per_100u || 0).toFixed(1)}`,
            tint: o.roi_per_100u >= 0 ? "#40d18a" : "#e46d6d" },
          { label: "Beat Close", value: beatClose != null ? `${beatClose.toFixed(1)}%` : "—",
            tint: beatClose == null ? undefined : beatClose >= 55 ? "#40d18a" : beatClose >= 50 ? undefined : "#e46d6d" },
        ]}
      />
      <Section title="By Odds Band">
        {(data.bands || []).map((b: any) => (
          <View key={b.label} style={styles.pickRow}>
            <View style={{ flex: 1 }}>
              <Text style={styles.pickMarket}>{b.label}</Text>
              <Text style={styles.pickEvent}>
                n={b.n} · hit {(b.win_pct || 0).toFixed(0)}% · CLV {b.avg_clv_pp != null ? `${b.avg_clv_pp >= 0 ? "+" : ""}${b.avg_clv_pp.toFixed(2)}pp` : "—"}
              </Text>
            </View>
            <View style={styles.pickRight}>
              <Text style={[styles.pickLock, {
                color: b.roi_per_100u >= 0 ? "#40d18a" : "#e46d6d",
              }]}>
                {b.roi_per_100u >= 0 ? "+" : ""}{(b.roi_per_100u || 0).toFixed(1)}
              </Text>
              <Text style={styles.pickLockLabel}>ROI</Text>
            </View>
          </View>
        ))}
      </Section>
      {data.snapshot_coverage && (
        <View style={styles.blurbBox}>
          <Text style={styles.blurbTxt}>
            {data.snapshot_coverage.note}
          </Text>
        </View>
      )}
      {data.notes && (
        <View style={styles.blurbBox}>
          <Text style={styles.blurbTxt}>{data.notes}</Text>
        </View>
      )}
    </View>
  );
}

function KellySection() {
  const [probStr, setProbStr] = useState("58");
  const [oddsStr, setOddsStr] = useState("-110");
  const [bankStr, setBankStr] = useState("1000");
  const [fractionStr, setFractionStr] = useState("0.25");
  const [data, setData] = useState<any | null>(null);
  const [loading, setLoading] = useState(false);
  const run = useCallback(() => {
    const p = parseFloat(probStr);
    const o = parseFloat(oddsStr);
    const b = parseFloat(bankStr) || 100;
    const f = parseFloat(fractionStr) || 0.25;
    if (!isFinite(p) || !isFinite(o)) { setData(null); return; }
    setLoading(true);
    api.kelly({ win_probability: p, american_odds: o, bankroll: b, fraction: f })
      .then(setData).catch(() => setData(null)).finally(() => setLoading(false));
  }, [probStr, oddsStr, bankStr, fractionStr]);
  useEffect(() => { run(); }, [run]);
  return (
    <View>
      <View style={styles.evInputRow}>
        <View style={styles.evField}>
          <Text style={styles.evFieldLabel}>Win Prob %</Text>
          <TextInput value={probStr} onChangeText={setProbStr} placeholder="58"
            placeholderTextColor={COLORS.textMuted} keyboardType="decimal-pad" style={styles.evInput} />
        </View>
        <View style={styles.evField}>
          <Text style={styles.evFieldLabel}>Odds</Text>
          <TextInput value={oddsStr} onChangeText={setOddsStr} placeholder="-110"
            placeholderTextColor={COLORS.textMuted} keyboardType="numbers-and-punctuation" style={styles.evInput} />
        </View>
      </View>
      <View style={styles.evInputRow}>
        <View style={styles.evField}>
          <Text style={styles.evFieldLabel}>Bankroll</Text>
          <TextInput value={bankStr} onChangeText={setBankStr} placeholder="1000"
            placeholderTextColor={COLORS.textMuted} keyboardType="decimal-pad" style={styles.evInput} />
        </View>
        <View style={styles.evField}>
          <Text style={styles.evFieldLabel}>Kelly Fraction</Text>
          <TextInput value={fractionStr} onChangeText={setFractionStr} placeholder="0.25"
            placeholderTextColor={COLORS.textMuted} keyboardType="decimal-pad" style={styles.evInput} />
        </View>
      </View>
      <View style={[styles.chipRow, { flexDirection: "row", flexWrap: "wrap" }]}>
        {[
          { label: "⅛-Kelly", v: "0.125" },
          { label: "¼-Kelly", v: "0.25" },
          { label: "½-Kelly", v: "0.5" },
          { label: "Full", v: "1.0" },
        ].map((k) => (
          <TouchableOpacity
            key={k.v}
            onPress={() => setFractionStr(k.v)}
            style={[styles.filterChip, fractionStr === k.v && styles.filterChipActive]}
          >
            <Text style={[styles.filterChipTxt, fractionStr === k.v && styles.filterChipTxtActive]}>
              {k.label}
            </Text>
          </TouchableOpacity>
        ))}
      </View>
      {loading && !data ? (
        <ActivityIndicator color={COLORS.textPrimary} style={{ marginTop: 16 }} />
      ) : data ? (
        <View>
          <StatGrid
            cells={[
              { label: "Stake", value: `$${data.stake.toFixed(2)}`,
                tint: data.stake > 0 ? "#40d18a" : COLORS.textMuted },
              { label: "% Bank", value: `${data.stake_pct.toFixed(2)}%`,
                tint: data.stake_pct > 0 ? "#40d18a" : COLORS.textMuted },
              { label: "Edge", value: `${data.edge_pp >= 0 ? "+" : ""}${data.edge_pp.toFixed(2)}pp`,
                tint: data.edge_pp > 0 ? "#40d18a" : "#e46d6d" },
              { label: "EV/unit", value: `${data.expected_value >= 0 ? "+" : ""}${data.expected_value.toFixed(3)}`,
                tint: data.expected_value > 0 ? "#40d18a" : "#e46d6d" },
            ]}
          />
          <View style={styles.blurbBox}>
            <Text style={styles.blurbTxt}>{data.note}</Text>
          </View>
          <View style={styles.blurbBox}>
            <Text style={styles.blurbTxt}>
              Full-Kelly = {(data.kelly_f * 100).toFixed(2)}% · Fractional = {(data.fractional_kelly * 100).toFixed(2)}%
            </Text>
          </View>
        </View>
      ) : (
        <Text style={styles.emptyTxt}>Enter values above.</Text>
      )}
    </View>
  );
}

function SteamSection() {
  const [dir, setDir] = useState<"" | "toward" | "away">("");
  const [hours, setHours] = useState<6 | 12 | 24>(6);
  const [data, setData] = useState<any | null>(null);
  const [loading, setLoading] = useState(false);
  const load = useCallback(() => {
    setLoading(true);
    api.steamPicks(hours, dir || undefined, 50)
      .then(setData).catch(() => setData(null)).finally(() => setLoading(false));
  }, [dir, hours]);
  useEffect(() => { load(); }, [load]);
  return (
    <View>
      <View style={[styles.chipRow, { flexDirection: "row", flexWrap: "wrap" }]}>
        {[6, 12, 24].map((h) => (
          <TouchableOpacity
            key={h}
            onPress={() => setHours(h as any)}
            style={[styles.filterChip, hours === h && styles.filterChipActive]}
          >
            <Text style={[styles.filterChipTxt, hours === h && styles.filterChipTxtActive]}>
              {h}h
            </Text>
          </TouchableOpacity>
        ))}
        {[
          { label: "ALL", v: "" as const },
          { label: "→ Toward", v: "toward" as const },
          { label: "← Away", v: "away" as const },
        ].map((f) => (
          <TouchableOpacity
            key={f.v || "all"}
            onPress={() => setDir(f.v)}
            style={[styles.filterChip, dir === f.v && styles.filterChipActive]}
          >
            <Text style={[styles.filterChipTxt, dir === f.v && styles.filterChipTxtActive]}>
              {f.label}
            </Text>
          </TouchableOpacity>
        ))}
      </View>
      {loading && !data ? (
        <ActivityIndicator color={COLORS.textPrimary} style={{ marginTop: 24 }} />
      ) : !data || data.count === 0 ? (
        <View style={styles.blurbBox}>
          <Text style={styles.blurbTxt}>
            {`No steam detected in the last ${hours}h. Steam alerts fire when a pick's implied probability moves ≥3pp (≈5¢) inside a 5-minute window.`}
          </Text>
        </View>
      ) : (
        <Section title={`${data.count} Steam Alert${data.count === 1 ? "" : "s"}`}>
          {(data.picks || []).map((p: any) => {
            const s = p.steam || {};
            const toward = s.direction === "toward";
            return (
              <View key={p.id} style={styles.pickRow}>
                <View style={{ flex: 1 }}>
                  <Text style={styles.pickMarket}>{p.selection}</Text>
                  <Text style={styles.pickEvent}>
                    {p.sport} · {p.market} · {p.event}
                  </Text>
                  <Text style={[styles.pickEvent, { color: toward ? "#40d18a" : "#e46d6d" }]}>
                    {toward ? "→ TOWARD" : "← AWAY"} · {s.magnitude_pp?.toFixed(1)}pp move ·
                    {" "}{s.american_start > 0 ? "+" : ""}{s.american_start} → {s.american_end > 0 ? "+" : ""}{s.american_end}
                  </Text>
                </View>
                <View style={styles.pickRight}>
                  <Text style={[styles.pickLock, { color: toward ? "#40d18a" : "#e46d6d" }]}>
                    {toward ? "+" : ""}{s.magnitude_pp?.toFixed(1)}
                  </Text>
                  <Text style={styles.pickLockLabel}>pp</Text>
                </View>
              </View>
            );
          })}
        </Section>
      )}
    </View>
  );
}


function Section({ title, chip, children }: { title: string; chip?: string; children: React.ReactNode }) {
  return (
    <View style={styles.subSection}>
      <View style={styles.subSectionRow}>
        <Text style={styles.subSectionTitle}>{title}</Text>
        {chip ? <View style={styles.chipBadge}><Text style={styles.chipBadgeTxt}>{chip}</Text></View> : null}
      </View>
      {children}
    </View>
  );
}

function Bullet({ text, muted, tint }: { text: string; muted?: boolean; tint?: string }) {
  return (
    <View style={styles.bulletRow}>
      <View style={[styles.bulletDot, tint ? { backgroundColor: tint } : muted ? styles.bulletDotMuted : null]} />
      <Text style={[styles.bulletTxt, muted && styles.bulletTxtMuted, tint ? { color: tint } : null]}>{text}</Text>
    </View>
  );
}

function StatGrid({ cells }: { cells: { label: string; value: string; tint?: string }[] }) {
  return (
    <View style={styles.statGrid}>
      {cells.map((c, i) => (
        <View key={i} style={styles.statCell}>
          <Text style={styles.statLabel}>{c.label}</Text>
          <Text style={[styles.statValue, c.tint ? { color: c.tint } : null]}>{c.value}</Text>
        </View>
      ))}
    </View>
  );
}

function PickRow({ pick, onPress, selected }: { pick: any; onPress: () => void; selected?: boolean }) {
  return (
    <Pressable
      onPress={onPress}
      style={[styles.pickRow, selected && styles.pickRowSelected]}
      testID={`lab-pick-row-${pick.id}`}
    >
      <View style={{ flex: 1 }}>
        <Text style={styles.pickSport}>{pick.sport}{pick.league ? ` · ${pick.league}` : ""}</Text>
        <Text style={styles.pickMarket} numberOfLines={2}>{pick.market || pick.selection}</Text>
        <Text style={styles.pickEvent} numberOfLines={1}>{pick.event || ""}</Text>
      </View>
      <View style={styles.pickRight}>
        <Text style={styles.pickLock}>{Math.round(pick.lock_score || 0)}</Text>
        <Text style={styles.pickLockLabel}>LOCK</Text>
      </View>
    </Pressable>
  );
}

function PropRow({ pick, sort }: { pick: any; sort: PropSort }) {
  const primary =
    sort === "lock"   ? { l: "LOCK", v: Math.round(pick.lock_score || 0), s: "" } :
    sort === "edge"   ? { l: "EDGE", v: pick.edge_percent != null ? Number(pick.edge_percent).toFixed(1) : "—", s: "%" } :
    sort === "prob"   ? { l: "WIN%", v: pick.win_probability != null ? Number(pick.win_probability).toFixed(1) : "—", s: "%" } :
                        { l: "SAMPLE", v: pick.player_form_n || pick.pick_rationale?.sample_size || "—", s: "" };
  return (
    <View style={styles.pickRow}>
      <View style={{ flex: 1 }}>
        <Text style={styles.pickSport}>
          {pick.sport}{pick.player_name ? ` · ${pick.player_name}` : ""}
        </Text>
        <Text style={styles.pickMarket} numberOfLines={2}>{pick.market || pick.selection}</Text>
        <Text style={styles.pickEvent} numberOfLines={1}>
          {pick.event || ""}{pick.book_odds != null ? ` · ${formatOdds(pick.book_odds)}` : ""}
        </Text>
      </View>
      <View style={styles.pickRight}>
        <Text style={styles.pickLock}>{primary.v}{primary.s}</Text>
        <Text style={styles.pickLockLabel}>{primary.l}</Text>
      </View>
    </View>
  );
}

function ScenarioBar({ label, pct }: { label: string; pct: number }) {
  const clean = label.replace(/_/g, " ").replace(/\b\w/g, (m) => m.toUpperCase());
  const width = Math.max(0, Math.min(100, pct * 100));
  return (
    <View style={styles.scenarioRow}>
      <Text style={styles.scenarioLabel} numberOfLines={1}>{clean}</Text>
      <View style={styles.scenarioBarWrap}>
        <View style={[styles.scenarioBarFill, { width: `${width}%` }]} />
      </View>
      <Text style={styles.scenarioPct}>{width.toFixed(0)}%</Text>
    </View>
  );
}

// ── helpers ──────────────────────────────────────────────────────
function impliedProb(americanOdds: number): number {
  if (!americanOdds) return 50;
  if (americanOdds > 0) return (100 / (americanOdds + 100)) * 100;
  return (-americanOdds / (-americanOdds + 100)) * 100;
}
function computeEV(modelProb: number, americanOdds: number): number {
  // EV per $100 wagered
  const wager = 100;
  const profit = americanOdds > 0 ? americanOdds : (10000 / -americanOdds);
  return modelProb * profit - (1 - modelProb) * wager;
}
function formatOdds(v: number | string): string {
  const n = typeof v === "number" ? v : parseInt(String(v), 10);
  if (!isFinite(n)) return "—";
  return n > 0 ? `+${n}` : `${n}`;
}
function edgeVerdict(edge: number): string {
  if (edge >= 8)   return "STRONG — significant edge over market";
  if (edge >= 4)   return "PLAYABLE — solid EV";
  if (edge >= 1)   return "LEAN — marginal edge";
  if (edge >= -1)  return "PASS — coin flip";
  return "AVOID — negative EV";
}
function edgeTint(edge: number): string {
  if (edge >= 4)  return "#40d18a";
  if (edge >= 1)  return "#c9d055";
  if (edge >= -1) return COLORS.textMuted;
  return "#e46d6d";
}
function stringifyBullet(b: any): string {
  if (typeof b === "string") return b;
  if (b && typeof b === "object") {
    return b.text || b.reason || b.explanation_text || b.label || JSON.stringify(b);
  }
  return String(b);
}
function formatFormValue(v: any): string {
  if (typeof v === "number") {
    if (v >= 1 && v <= 100 && v % 1 !== 0) return v.toFixed(1);
    return String(v);
  }
  if (typeof v === "object" && v != null) {
    if ("hits" in v && "n" in v) return `${v.hits}/${v.n}`;
    if ("rate" in v) return `${(v.rate * 100).toFixed(0)}%`;
    return JSON.stringify(v);
  }
  return String(v);
}

// ── styles ─────────────────────────────────────────────────────────
const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: "transparent" },
  header: {
    paddingHorizontal: 14,
    paddingBottom: 8,
    borderBottomWidth: 1,
    borderBottomColor: COLORS.borderDefault,
    backgroundColor: "#0a0a0a",
  },
  headerRow: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", paddingVertical: 8 },
  headerLeft: { flexDirection: "row", alignItems: "center", gap: 8 },
  headerTitle: { color: COLORS.textPrimary, fontSize: 20, fontWeight: "900", letterSpacing: 2 },
  headerHint: { color: COLORS.textMuted, fontSize: 11, fontWeight: "700", letterSpacing: 1 },

  segRow: { gap: 6, paddingRight: 12, paddingVertical: 4 },
  segChip: {
    flexDirection: "row", alignItems: "center",
    paddingHorizontal: 12, paddingVertical: 6,
    borderRadius: 20,
    backgroundColor: "rgba(255,255,255,0.05)",
    borderWidth: 1,
    borderColor: COLORS.borderDefault,
  },
  segChipActive: { backgroundColor: COLORS.textPrimary, borderColor: COLORS.textPrimary },
  segTxt: { color: COLORS.textMuted, fontSize: 11, fontWeight: "700", letterSpacing: 0.8 },
  segTxtActive: { color: "#000" },
  // Progressive-disclosure submodule chips (§1)
  segSubChip: {
    flexDirection: "row", alignItems: "center",
    paddingHorizontal: 10, paddingVertical: 4,
    borderRadius: 14,
    backgroundColor: "transparent",
    borderWidth: 1, borderColor: "rgba(255,255,255,0.08)",
  },
  segSubChipActive: {
    backgroundColor: "rgba(255,255,255,0.06)",
    borderColor: COLORS.textPrimary,
  },
  segSubTxt: { color: COLORS.textMuted, fontSize: 10, fontWeight: "700", letterSpacing: 0.5 },
  segSubTxtActive: { color: COLORS.textPrimary },

  body: { flex: 1 },
  centered: { flex: 1, alignItems: "center", justifyContent: "center", padding: 24 },
  errTxt: { color: "#e46d6d", fontSize: 13, textAlign: "center", marginBottom: 12 },
  retryBtn: { paddingHorizontal: 18, paddingVertical: 10, backgroundColor: COLORS.textPrimary, borderRadius: 6 },
  retryTxt: { color: "#000", fontWeight: "900", letterSpacing: 1 },

  sectionHeader: { marginBottom: 12, marginTop: 4 },
  sectionHeaderRow: { flexDirection: "row", alignItems: "center", gap: 6, marginBottom: 4 },
  sectionTitle: { color: COLORS.textPrimary, fontSize: 15, fontWeight: "900", letterSpacing: 0.5 },
  sectionBlurb: { color: COLORS.textMuted, fontSize: 12, lineHeight: 17 },

  searchWrap: {
    flexDirection: "row", alignItems: "center",
    borderWidth: 1, borderColor: COLORS.borderDefault,
    borderRadius: 8, paddingHorizontal: 10, gap: 8,
    backgroundColor: "rgba(255,255,255,0.03)",
    marginBottom: 10,
  },
  searchInput: { flex: 1, color: COLORS.textPrimary, fontSize: 14, paddingVertical: 10 },

  subhead: { color: COLORS.textMuted, fontSize: 11, marginBottom: 8, letterSpacing: 0.8 },
  chipRow: { gap: 6, paddingRight: 12, paddingVertical: 4 },
  filterChip: {
    paddingHorizontal: 10, paddingVertical: 5, borderRadius: 14,
    backgroundColor: "rgba(255,255,255,0.04)",
    borderWidth: 1, borderColor: COLORS.borderDefault,
  },
  filterChipActive: { backgroundColor: COLORS.textPrimary, borderColor: COLORS.textPrimary },
  filterChipTxt: { color: COLORS.textMuted, fontSize: 10, fontWeight: "800", letterSpacing: 0.8 },
  filterChipTxtActive: { color: "#000" },

  // Analytics module — CLV / Kelly / Steam
  blurbBox: {
    padding: 10, borderRadius: 8,
    backgroundColor: "rgba(255,255,255,0.03)",
    borderWidth: 1, borderColor: COLORS.borderDefault,
    marginTop: 10,
  },
  blurbTxt: { color: COLORS.textMuted, fontSize: 11, lineHeight: 16 },
  emptyTxt: { color: COLORS.textMuted, fontSize: 12, textAlign: "center", marginTop: 24 },

  pickRow: {
    flexDirection: "row", alignItems: "center",
    padding: 10, borderRadius: 8, gap: 10,
    backgroundColor: "rgba(255,255,255,0.03)",
    borderWidth: 1, borderColor: COLORS.borderDefault,
    marginBottom: 6,
  },
  pickRowSelected: { borderColor: COLORS.textPrimary, backgroundColor: "rgba(255,255,255,0.08)" },
  pickSport: { color: COLORS.textMuted, fontSize: 10, fontWeight: "800", letterSpacing: 1, marginBottom: 2 },
  pickMarket: { color: COLORS.textPrimary, fontSize: 13, fontWeight: "700", marginBottom: 1 },
  pickEvent: { color: COLORS.textMuted, fontSize: 11 },
  pickRight: { alignItems: "center", minWidth: 46 },
  pickLock: { color: COLORS.textPrimary, fontSize: 18, fontWeight: "900" },
  pickLockLabel: { color: COLORS.textMuted, fontSize: 9, letterSpacing: 0.8, fontWeight: "800" },

  backBtn: { flexDirection: "row", alignItems: "center", gap: 4, paddingVertical: 8, marginBottom: 4 },
  backTxt: { color: COLORS.textPrimary, fontSize: 12, fontWeight: "700" },

  detailHeader: { flexDirection: "row", padding: 12, borderRadius: 8, backgroundColor: "rgba(255,255,255,0.05)", marginBottom: 12, gap: 12 },
  detailSport: { color: COLORS.textMuted, fontSize: 10, fontWeight: "800", letterSpacing: 1, marginBottom: 2 },
  detailEvent: { color: COLORS.textPrimary, fontSize: 15, fontWeight: "800", marginBottom: 4 },
  detailMarket: { color: COLORS.textMuted, fontSize: 12 },
  lockBadge: { alignItems: "center", justifyContent: "center", paddingHorizontal: 10 },
  lockScore: { color: COLORS.textPrimary, fontSize: 26, fontWeight: "900" },
  lockLabel: { color: COLORS.textMuted, fontSize: 9, fontWeight: "800", letterSpacing: 1 },

  statGrid: { flexDirection: "row", gap: 6, marginBottom: 12 },
  statCell: {
    flex: 1, padding: 10, borderRadius: 6,
    backgroundColor: "rgba(255,255,255,0.04)",
    borderWidth: 1, borderColor: COLORS.borderDefault,
    alignItems: "center",
  },
  statLabel: { color: COLORS.textMuted, fontSize: 9, letterSpacing: 0.8, fontWeight: "800", marginBottom: 3 },
  statValue: { color: COLORS.textPrimary, fontSize: 14, fontWeight: "900" },

  subSection: { marginTop: 6, marginBottom: 14 },
  subSectionRow: { flexDirection: "row", alignItems: "center", gap: 8, marginBottom: 6 },
  subSectionTitle: { color: COLORS.textPrimary, fontSize: 13, fontWeight: "800", letterSpacing: 0.6 },
  chipBadge: { paddingHorizontal: 6, paddingVertical: 2, backgroundColor: "rgba(255,255,255,0.08)", borderRadius: 4 },
  chipBadgeTxt: { color: COLORS.textMuted, fontSize: 9, fontWeight: "800", letterSpacing: 0.8 },

  bulletRow: { flexDirection: "row", gap: 8, alignItems: "flex-start", paddingVertical: 4 },
  bulletDot: { width: 5, height: 5, borderRadius: 3, backgroundColor: COLORS.textPrimary, marginTop: 6 },
  bulletDotMuted: { backgroundColor: COLORS.textMuted },
  bulletTxt: { flex: 1, color: COLORS.textPrimary, fontSize: 12, lineHeight: 17 },
  bulletTxtMuted: { color: COLORS.textMuted },
  disclaimer: { color: COLORS.textMuted, fontSize: 11, lineHeight: 15, marginBottom: 6, fontStyle: "italic" },

  evInputRow: { flexDirection: "row", gap: 8, marginBottom: 10 },
  evField: { flex: 1 },
  evFieldLabel: { color: COLORS.textMuted, fontSize: 10, letterSpacing: 0.8, fontWeight: "800", marginBottom: 4 },
  evInput: {
    color: COLORS.textPrimary, fontSize: 15, fontWeight: "700",
    borderWidth: 1, borderColor: COLORS.borderDefault,
    borderRadius: 6, paddingHorizontal: 10, paddingVertical: 10,
    backgroundColor: "rgba(255,255,255,0.03)",
  },
  recRow: {
    flexDirection: "row", alignItems: "center", justifyContent: "space-between",
    paddingVertical: 10, paddingHorizontal: 12, marginBottom: 12,
    borderRadius: 6, backgroundColor: "rgba(255,255,255,0.04)",
    borderWidth: 1, borderColor: COLORS.borderDefault,
  },
  recLabel: { color: COLORS.textMuted, fontSize: 11, fontWeight: "800", letterSpacing: 0.6 },
  recValue: { fontSize: 12, fontWeight: "900", letterSpacing: 0.4 },

  formGrid: { flexDirection: "row", flexWrap: "wrap", gap: 6, marginBottom: 6 },
  formCell: {
    paddingHorizontal: 10, paddingVertical: 8, borderRadius: 6,
    backgroundColor: "rgba(255,255,255,0.04)",
    borderWidth: 1, borderColor: COLORS.borderDefault,
    minWidth: 68, alignItems: "center",
  },
  formLabel: { color: COLORS.textMuted, fontSize: 9, letterSpacing: 0.8, fontWeight: "800", marginBottom: 2 },
  formValue: { color: COLORS.textPrimary, fontSize: 13, fontWeight: "900" },
  formSummary: { color: COLORS.textMuted, fontSize: 11, lineHeight: 15 },

  scenarioRow: { flexDirection: "row", alignItems: "center", gap: 8, paddingVertical: 4 },
  scenarioLabel: { color: COLORS.textPrimary, fontSize: 11, fontWeight: "700", width: 100 },
  scenarioBarWrap: { flex: 1, height: 8, borderRadius: 4, backgroundColor: "rgba(255,255,255,0.06)", overflow: "hidden" },
  scenarioBarFill: { height: "100%", backgroundColor: COLORS.textPrimary },
  scenarioPct: { color: COLORS.textMuted, fontSize: 10, fontWeight: "800", width: 34, textAlign: "right" },

  rangeRow: { flexDirection: "row", gap: 8 },
  rangeBox: { flex: 1, padding: 12, borderRadius: 8, alignItems: "center" },
  rangeLabel: { color: COLORS.textMuted, fontSize: 10, letterSpacing: 0.8, fontWeight: "800", marginBottom: 4 },
  rangeValue: { color: COLORS.textPrimary, fontSize: 20, fontWeight: "900", marginBottom: 2 },
  rangeSub: { color: COLORS.textMuted, fontSize: 9, letterSpacing: 0.6 },
});
