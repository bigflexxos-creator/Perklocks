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
import { api } from "@/src/lib/api";

// ── Module type ──────────────────────────────────────────────────────
type LabModule = "cheats" | "research" | "ev" | "sim" | "props" | "corr" | "backtest" | "patterns" | "dna";

const MODULES: { id: LabModule; label: string; icon: keyof typeof Ionicons.glyphMap }[] = [
  { id: "cheats",   label: "Cheatsheets", icon: "flash" },
  { id: "research", label: "Research",    icon: "search" },
  { id: "corr",     label: "Correlations", icon: "git-network" },
  { id: "backtest", label: "Backtest",    icon: "trending-up" },
  { id: "patterns", label: "Patterns",    icon: "sparkles" },
  { id: "dna",      label: "Matchup DNA", icon: "body" },
  { id: "ev",       label: "EV Calc",     icon: "calculator" },
  { id: "sim",      label: "Sim",         icon: "analytics" },
  { id: "props",    label: "Props",       icon: "list" },
];

// ── Root screen ──────────────────────────────────────────────────────
export default function LabScreen() {
  const insets = useSafeAreaInsets();
  const [module, setModule] = useState<LabModule>("cheats");
  const [picks, setPicks] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const res = await api.picksToday(undefined, undefined, "lock", undefined, "desc");
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
            Research · EV · Sim · Props
          </Text>
        </View>
        {/* Segmented control */}
        <ScrollView
          horizontal
          showsHorizontalScrollIndicator={false}
          contentContainerStyle={styles.segRow}
        >
          {MODULES.map((m) => {
            const active = m.id === module;
            return (
              <TouchableOpacity
                key={m.id}
                testID={`lab-tab-${m.id}`}
                style={[styles.segChip, active && styles.segChipActive]}
                onPress={() => setModule(m.id)}
              >
                <Ionicons
                  name={m.icon}
                  size={13}
                  color={active ? "#000" : COLORS.textMuted}
                  style={{ marginRight: 4 }}
                />
                <Text style={[styles.segTxt, active && styles.segTxtActive]}>
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
          {module === "cheats"   && <CheatsheetsModule picks={picks} />}
          {module === "research" && <ResearchModule picks={picks} />}
          {module === "corr"     && <CorrelationModule />}
          {module === "backtest" && <BacktestModule />}
          {module === "patterns" && <PatternsModule />}
          {module === "dna"      && <MatchupDNAModule />}
          {module === "ev"       && <EVCalcModule picks={picks} />}
          {module === "sim"      && <SimulationModule picks={picks} />}
          {module === "props"    && <PropExplorerModule picks={picks} />}
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
          <Text style={styles.detailEvent} numberOfLines={2}>{pick.event || ""}</Text>
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

      {/* Why this pick — market-filtered evidence */}
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
// Auto-generated stat proof cards for the highest-confidence picks.
// Mirrors the "Trending today" cheatsheet UX users see on modern
// sportsbook explorers — one glance shows the 3 most concrete
// hit-streak facts + the % badge for each. Everything is derived
// from data already on the pick (pick_rationale bullets + recent_form
// + player_form_streak). No new API round-trip.
function CheatsheetsModule({ picks }: { picks: any[] }) {
  const [sport, setSport] = useState<string>("All");

  const sportsAvail = useMemo(() => {
    const set = new Set<string>();
    picks.forEach((p) => p.sport && set.add(p.sport));
    return ["All", ...Array.from(set).sort()];
  }, [picks]);

  const cards = useMemo(() => buildCheatsheets(picks, sport), [picks, sport]);

  return (
    <View>
      <SectionHeader
        icon="flash"
        title="Cheatsheets"
        blurb="Auto-generated hit-streak proof cards for the day's most confident picks. Every fact is real data — no fluff."
      />
      <ScrollView
        horizontal
        showsHorizontalScrollIndicator={false}
        contentContainerStyle={styles.chipRow}
      >
        {sportsAvail.map((s) => (
          <TouchableOpacity
            key={s}
            onPress={() => setSport(s)}
            testID={`lab-cheats-sport-${s}`}
            style={[styles.filterChip, sport === s && styles.filterChipActive]}
          >
            <Text style={[styles.filterChipTxt, sport === s && styles.filterChipTxtActive]}>
              {s.toUpperCase()}
            </Text>
          </TouchableOpacity>
        ))}
      </ScrollView>

      {cards.length === 0 ? (
        <Text style={styles.disclaimer}>
          No cheatsheet-ready picks in this filter yet. Cards populate as picks
          accumulate rolling-form + matchup evidence.
        </Text>
      ) : (
        cards.map((c) => <CheatsheetCard key={c.pickId} card={c} />)
      )}
    </View>
  );
}

type CheatCard = {
  pickId: string;
  playerLabel: string;    // e.g. "B. Stott @ CIN"
  marketLabel: string;    // e.g. "Over 0.5 Hits"
  oddsLabel: string;      // e.g. "-150"
  sport: string;
  facts: { icon: keyof typeof Ionicons.glyphMap; text: string; pctLabel: string; tint: string }[];
};

function buildCheatsheets(picks: any[], sportFilter: string): CheatCard[] {
  const filtered = picks.filter((p) => {
    if (sportFilter !== "All" && p.sport !== sportFilter) return false;
    if ((p.lock_score || 0) < 80) return false;      // only high-confidence
    return true;
  });
  const cards: CheatCard[] = [];
  for (const p of filtered) {
    const facts = extractFacts(p);
    if (facts.length < 2) continue;                  // need at least 2 solid facts
    const player = shortenPlayer(p.player_name || p.player || "");
    const opp = extractOpponent(p);
    const playerLabel = player
      ? (opp ? `${player} · ${opp}` : player)
      : (p.event || p.selection || "");
    cards.push({
      pickId: p.id,
      playerLabel,
      marketLabel: cleanMarket(p.market || p.selection || ""),
      oddsLabel: p.book_odds != null ? formatOdds(p.book_odds) : "",
      sport: p.sport || "",
      facts: facts.slice(0, 3),
    });
    if (cards.length >= 30) break;
  }
  return cards;
}

function extractFacts(pick: any): CheatsheetCard["facts"] {
  const facts: CheatsheetCard["facts"] = [];
  const rationale = pick.pick_rationale || {};
  const evidence: any[] = rationale.evidence || rationale.evidence_all || [];
  const rf = rationale.recent_form || {};

  // 1) Recent-form streak facts (highest priority — matches the "Hit in 8 of last 8" pattern)
  const streakKeys: [string, string][] = [
    ["last5",  "last 5 games"],
    ["last10", "last 10 games"],
    ["last20", "last 20 games"],
    ["L5",     "last 5 games"],
    ["L10",    "last 10 games"],
    ["L20",    "last 20 games"],
  ];
  for (const [key, label] of streakKeys) {
    const v = rf[key];
    if (v == null) continue;
    // Accept {hits, n} shape
    if (typeof v === "object" && "hits" in v && "n" in v) {
      const hits = Number(v.hits);
      const n = Number(v.n);
      if (n > 0 && hits >= n * 0.6) {
        const pct = Math.round((hits / n) * 100);
        facts.push({
          icon: "flash",
          text: `Hit in ${hits} of ${label}`,
          pctLabel: `${pct}%`,
          tint: pctTint(pct),
        });
      }
    } else if (typeof v === "number" && v >= 0.6) {
      const pct = Math.round(v * 100);
      const label2 = label.replace("games", "");
      facts.push({
        icon: "flash",
        text: `Hit rate over ${label2}games`,
        pctLabel: `${pct}%`,
        tint: pctTint(pct),
      });
    }
  }

  // 2) Streak string on pick itself (player_form_streak like "5/6 vs CIN")
  const pfStreak = pick.player_form_streak;
  if (typeof pfStreak === "string" && pfStreak.includes("/")) {
    const m = pfStreak.match(/(\d+)\s*\/\s*(\d+)/);
    if (m) {
      const hits = Number(m[1]);
      const n = Number(m[2]);
      if (n > 0) {
        const pct = Math.round((hits / n) * 100);
        const suffix = pfStreak.replace(m[0], "").trim();
        facts.push({
          icon: "stats-chart",
          text: `Hit in ${hits} of last ${n}${suffix ? ` ${suffix}` : ""}`,
          pctLabel: `${pct}%`,
          tint: pctTint(pct),
        });
      }
    }
  }

  // 3) Evidence bullets that look like "Hit in X of Y ..." — normalise + score them.
  const streakRegex = /(\d+)\s*(?:of|\/)\s*(?:the\s+)?(?:last\s+)?(\d+)/i;
  for (const raw of evidence) {
    const text = typeof raw === "string" ? raw : (raw?.text || raw?.reason || "");
    if (!text) continue;
    const m = text.match(streakRegex);
    if (!m) continue;
    const hits = Number(m[1]);
    const n = Number(m[2]);
    if (!n || hits > n || n > 100) continue;
    const pct = Math.round((hits / n) * 100);
    if (pct < 60) continue;
    // Guess an icon by keywords.
    const t = text.toLowerCase();
    const icon: keyof typeof Ionicons.glyphMap =
      t.includes("home") || t.includes("away") ? "location"
      : t.includes(" vs ") || t.includes("against") ? "chatbubbles"
      : "flash";
    // Clean the text (strip emojis) for consistent rendering.
    const clean = text.replace(/[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}]/gu, "").trim();
    facts.push({ icon, text: clean, pctLabel: `${pct}%`, tint: pctTint(pct) });
    if (facts.length >= 5) break;
  }

  // Dedupe by leading text
  const seen = new Set<string>();
  return facts.filter((f) => {
    const key = f.text.slice(0, 40).toLowerCase();
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function CheatsheetCard({ card }: { card: CheatCard }) {
  const oddsColor = card.oddsLabel.startsWith("-") ? "#e46d6d" : "#40d18a";
  return (
    <View style={cheatStyles.card} testID={`cheatsheet-card-${card.pickId}`}>
      <View style={cheatStyles.headerRow}>
        <View style={{ flex: 1 }}>
          <Text style={cheatStyles.player} numberOfLines={1}>{card.playerLabel}</Text>
          <Text style={cheatStyles.market} numberOfLines={1}>{card.marketLabel}</Text>
        </View>
        {card.oddsLabel ? (
          <View style={[cheatStyles.oddsBadge, { borderColor: oddsColor }]}>
            <Text style={[cheatStyles.oddsTxt, { color: oddsColor }]}>{card.oddsLabel}</Text>
          </View>
        ) : null}
      </View>
      <View style={cheatStyles.factsBlock}>
        {card.facts.map((f, i) => (
          <View key={i} style={cheatStyles.factRow}>
            <Ionicons name={f.icon} size={13} color={f.tint} />
            <Text style={cheatStyles.factTxt} numberOfLines={2}>{f.text}</Text>
            <Text style={[cheatStyles.factPct, { color: f.tint }]}>{f.pctLabel}</Text>
          </View>
        ))}
      </View>
    </View>
  );
}

// ── cheatsheet helpers ────────────────────────────────────────────
function pctTint(pct: number): string {
  if (pct >= 90) return "#40d18a";
  if (pct >= 75) return "#c9d055";
  if (pct >= 60) return "#f5c542";
  return COLORS.textMuted;
}
function shortenPlayer(name: string): string {
  if (!name) return "";
  const parts = name.trim().split(/\s+/);
  if (parts.length >= 2) return `${parts[0][0]}. ${parts.slice(1).join(" ")}`;
  return name;
}
function extractOpponent(pick: any): string {
  const event = pick.event || "";
  const team = pick.team || "";
  if (event.includes("@")) {
    const [a, h] = event.split("@").map((s: string) => s.trim());
    // Return the team the player is playing AGAINST if we know the player's team
    if (team && team === a) return `@ ${abbr(h)}`;
    if (team && team === h) return `vs ${abbr(a)}`;
    // Fallback — show "@ HOME"
    return `@ ${abbr(h)}`;
  }
  return "";
}
function abbr(team: string): string {
  if (!team) return "";
  const t = team.trim();
  const words = t.split(/\s+/);
  if (words.length >= 2) return words.map((w) => w[0]).join("").toUpperCase().slice(0, 4);
  return t.slice(0, 3).toUpperCase();
}
function cleanMarket(market: string): string {
  return market
    .replace(/^.*?(Over|Under|Anytime|Total)/, "$1")
    .replace(/\s+/g, " ")
    .trim();
}

const cheatStyles = StyleSheet.create({
  card: {
    backgroundColor: "#1a1a1a",
    borderRadius: 14,
    borderWidth: 1,
    borderColor: COLORS.borderDefault,
    padding: 14,
    marginBottom: 10,
  },
  headerRow: { flexDirection: "row", alignItems: "center", marginBottom: 10, gap: 10 },
  player: { color: COLORS.textPrimary, fontSize: 15, fontWeight: "900", marginBottom: 2, letterSpacing: 0.2 },
  market: { color: COLORS.textMuted, fontSize: 12, fontWeight: "700" },
  oddsBadge: {
    paddingHorizontal: 10, paddingVertical: 6,
    borderRadius: 20, borderWidth: 1,
    backgroundColor: "rgba(255,255,255,0.03)",
  },
  oddsTxt: { fontSize: 13, fontWeight: "900", letterSpacing: 0.4 },
  factsBlock: { gap: 8 },
  factRow: { flexDirection: "row", alignItems: "center", gap: 10 },
  factTxt: { flex: 1, color: COLORS.textPrimary, fontSize: 12.5, fontWeight: "600" },
  factPct: { fontSize: 13, fontWeight: "900" },
});


// ═══════════════════════════════════════════════════════════════════
// MODULE 6: CORRELATION LAB
// ═══════════════════════════════════════════════════════════════════
// Data source: /api/lab/correlations (parlay_history aggregation).
function CorrelationModule() {
  const [sport, setSport] = useState<string>("");
  const [data, setData] = useState<any | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    api.labCorrelations({ sport: sport || undefined, min_pairs: 5, limit: 30 })
      .then(setData)
      .catch(() => setData({ rows: [], total_pairs_seen: 0 }))
      .finally(() => setLoading(false));
  }, [sport]);

  useEffect(() => { load(); }, [load]);

  const sports = ["", "MLB", "NBA", "NFL", "Soccer", "Tennis", "UFC"];
  return (
    <View>
      <SectionHeader
        icon="git-network"
        title="Correlation Lab"
        blurb="How often two market families co-hit in a parlay. Lift > 1.25 = legs cluster together (positive corr). Lift < 0.8 = anti-correlated."
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
      {loading ? (
        <ActivityIndicator color={COLORS.textPrimary} style={{ marginTop: 20 }} />
      ) : !data?.rows?.length ? (
        <Text style={styles.disclaimer}>
          No correlation data yet. Cards appear once we have ≥5 co-occurrences of a
          leg pair in settled parlays.
        </Text>
      ) : (
        data.rows.map((r: any, i: number) => (
          <View key={`${r.family_a}_${r.family_b}_${i}`} style={styles.pickRow}>
            <View style={{ flex: 1 }}>
              <Text style={styles.pickSport}>{r.family_a} × {r.family_b}</Text>
              <Text style={styles.pickMarket}>
                Both hit {Math.round(r.both_hit_rate * 100)}% · Leg A {Math.round(r.leg_a_hit_rate * 100)}% · Leg B {Math.round(r.leg_b_hit_rate * 100)}%
              </Text>
              <Text style={styles.pickEvent}>n={r.sample_size} · {r.verdict}</Text>
            </View>
            <View style={styles.pickRight}>
              <Text style={[styles.pickLock, { color: liftColor(r.lift) }]}>
                {r.lift != null ? r.lift.toFixed(2) : "—"}
              </Text>
              <Text style={styles.pickLockLabel}>LIFT</Text>
            </View>
          </View>
        ))
      )}
    </View>
  );
}

function liftColor(lift: number | null): string {
  if (lift == null) return COLORS.textMuted;
  if (lift >= 1.25) return "#40d18a";
  if (lift <= 0.8)  return "#e46d6d";
  return COLORS.textPrimary;
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
        title="Bet Backtester"
        blurb="How would this filter set have performed historically? Runs against every settled pick and reports win rate, ROI, best/worst day."
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
  root: { flex: 1, backgroundColor: "#0a0a0a" },
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
