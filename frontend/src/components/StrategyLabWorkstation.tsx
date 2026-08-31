/**
 * StrategyLabWorkstation.tsx — Strategy Lab 10X professional quant panel.
 *
 * Consumes the canonical research contract from `/api/lab/research/*`.
 * Shows FACTUAL research (safe, real data) and SHADOW_SIGNAL flags
 * (experimental, UI-only) clearly separated.
 *
 * Sports supported: MLB, NFL, NBA (per user directive).
 *
 * Sections:
 *   1. Sport + Subject picker (auto-suggests from live slate)
 *   2. Facts panel — grouped by section (Form / Matchup / Statcast / …)
 *   3. Distribution + Line explorer — enter a line, see fair price
 *   4. Matchup DNA — vs opponent history
 *   5. Calibration Center — historical hit-rate by bucket
 *   6. Pattern Discovery 3.0 — SHADOW_SIGNAL trend candidates
 */
import React, { useCallback, useEffect, useMemo, useState } from "react";
import {
  View, Text, ScrollView, TouchableOpacity, StyleSheet,
  ActivityIndicator, TextInput,
} from "react-native";
import { Ionicons } from "@expo/vector-icons";
import { COLORS } from "@/src/theme";
import { api } from "@/src/lib/api";

type Sport = "MLB" | "NFL" | "NBA";
const SPORTS: Sport[] = ["MLB", "NFL", "NBA"];

type Subsection =
  | "overview" | "facts" | "scorecard" | "distribution"
  | "dna" | "calibration" | "patterns";

const SUBSECTIONS: { id: Subsection; label: string; icon: keyof typeof Ionicons.glyphMap }[] = [
  { id: "overview",    label: "Overview",     icon: "grid" },
  { id: "facts",       label: "Facts",        icon: "reader" },
  { id: "scorecard",   label: "Scorecard",    icon: "podium" },
  { id: "distribution",label: "Line Value",   icon: "trending-up" },
  { id: "dna",         label: "Matchup DNA",  icon: "body" },
  { id: "calibration", label: "Calibration",  icon: "speedometer" },
  { id: "patterns",    label: "Patterns 3.0", icon: "sparkles" },
];

export function StrategyLabWorkstation({ picks }: { picks: any[] }) {
  const [sport, setSport] = useState<Sport>("MLB");
  const [subject, setSubject] = useState<string>("");
  const [opponent, setOpponent] = useState<string>("");
  const [section, setSection] = useState<Subsection>("overview");
  const [snapshot, setSnapshot] = useState<any | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [line, setLine] = useState<string>("");
  const [marketHint, setMarketHint] = useState<string>("");

  // Suggested subjects from live slate — filter picks by sport +
  // player_name present. Users can override manually.
  const suggestions = useMemo(() => {
    const seen = new Set<string>();
    const out: { name: string; opp: string; market: string }[] = [];
    for (const p of picks) {
      if (String(p.sport || "").toUpperCase() !== sport) continue;
      const n = p.player_name || p.player;
      if (!n || seen.has(n)) continue;
      seen.add(n);
      out.push({ name: n, opp: p.opponent || p.event || "", market: p.market || "" });
      if (out.length >= 24) break;
    }
    return out;
  }, [picks, sport]);

  const loadSnapshot = useCallback(async () => {
    if (!subject) { setSnapshot(null); return; }
    setLoading(true); setError(null);
    try {
      const role = sport === "MLB"
        ? (marketHint.toLowerCase().includes("strike") || marketHint.toLowerCase().includes("outs")
            ? "pitcher" : "batter")
        : "player";
      const data = await api.labResearchContext({
        sport, subject, opponent: opponent || undefined,
        include_shadow: true, include_distribution: true,
        include_calibration: false, role,
        market_hint: marketHint || undefined,
      });
      setSnapshot(data);
    } catch (e: any) {
      setError(e?.message || "Failed to load snapshot");
      setSnapshot(null);
    } finally {
      setLoading(false);
    }
  }, [sport, subject, opponent, marketHint]);

  useEffect(() => { loadSnapshot(); }, [loadSnapshot]);

  return (
    <View>
      <View style={s.headerRow}>
        <Ionicons name="flask" size={16} color={COLORS.goldElite} />
        <Text style={s.headerTitle}>Strategy Lab · Quant Workstation</Text>
        <View style={s.badge}>
          <Text style={s.badgeTxt}>10X</Text>
        </View>
      </View>
      <Text style={s.blurb}>
        Professional research terminal. FACTUAL data feeds the production
        model; SHADOW signals remain research-only.
      </Text>

      {/* Sport picker */}
      <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={s.chipRow}>
        {SPORTS.map((sp) => (
          <TouchableOpacity
            key={sp}
            onPress={() => { setSport(sp); setSubject(""); setOpponent(""); }}
            style={[s.chip, sport === sp && s.chipActive]}
            testID={`sl-sport-${sp}`}
          >
            <Text style={[s.chipTxt, sport === sp && s.chipTxtActive]}>{sp}</Text>
          </TouchableOpacity>
        ))}
      </ScrollView>

      {/* Subject input + suggestions */}
      <View style={s.inputRow}>
        <View style={s.inputField}>
          <Text style={s.inputLabel}>Subject</Text>
          <TextInput
            testID="sl-subject"
            value={subject} onChangeText={setSubject}
            placeholder={sport === "MLB" ? "e.g. Aaron Judge" :
                         sport === "NFL" ? "e.g. Josh Allen" : "e.g. Nikola Jokic"}
            placeholderTextColor={COLORS.textMuted}
            style={s.input}
            autoCapitalize="words"
          />
        </View>
        <View style={s.inputField}>
          <Text style={s.inputLabel}>Opponent</Text>
          <TextInput
            testID="sl-opponent"
            value={opponent} onChangeText={setOpponent}
            placeholder="opp team / pitcher"
            placeholderTextColor={COLORS.textMuted}
            style={s.input}
            autoCapitalize="words"
          />
        </View>
      </View>

      {suggestions.length > 0 && (
        <ScrollView horizontal showsHorizontalScrollIndicator={false}
          contentContainerStyle={s.chipRow}>
          {suggestions.map((sg, i) => (
            <TouchableOpacity
              key={sg.name + i}
              onPress={() => {
                setSubject(sg.name);
                if (sg.opp && !opponent) setOpponent(sg.opp.replace(/^(vs|@)\s*/i, ""));
                if (sg.market && !marketHint) setMarketHint(sg.market);
              }}
              style={s.suggChip}
            >
              <Text style={s.suggChipTxt} numberOfLines={1}>{sg.name}</Text>
            </TouchableOpacity>
          ))}
        </ScrollView>
      )}

      {/* Subsection tabs */}
      <ScrollView horizontal showsHorizontalScrollIndicator={false}
        contentContainerStyle={s.chipRow}>
        {SUBSECTIONS.map((ss) => (
          <TouchableOpacity
            key={ss.id}
            onPress={() => setSection(ss.id)}
            style={[s.chip, section === ss.id && s.chipActive]}
            testID={`sl-section-${ss.id}`}
          >
            <Ionicons name={ss.icon} size={11} color={section === ss.id ? "#000" : COLORS.textMuted}
              style={{ marginRight: 4 }} />
            <Text style={[s.chipTxt, section === ss.id && s.chipTxtActive]}>{ss.label}</Text>
          </TouchableOpacity>
        ))}
      </ScrollView>

      {loading && (
        <ActivityIndicator color={COLORS.textPrimary} style={{ marginTop: 16 }} />
      )}
      {error && (
        <Text style={s.errTxt}>{error}</Text>
      )}

      {!loading && !error && !subject && (
        <TodayFeed sport={sport} onPickSubject={(name, opp) => {
          setSubject(name);
          if (opp) setOpponent(opp);
        }} />
      )}

      {!loading && subject && snapshot && (
        <>
          {section === "overview"    && <OverviewPanel snap={snapshot} />}
          {section === "facts"       && <FactsPanel snap={snapshot} />}
          {section === "scorecard"   && (
            <ScorecardPanel sport={sport} subject={subject}
              opponent={opponent} marketHint={marketHint} line={line} />
          )}
          {section === "distribution"&& (
            <DistributionPanel snap={snapshot} sport={sport} subject={subject}
              line={line} setLine={setLine} marketHint={marketHint} setMarketHint={setMarketHint} />
          )}
          {section === "dna"         && <MatchupDNAPanel snap={snapshot} />}
          {section === "calibration" && <CalibrationPanel sport={sport} />}
          {section === "patterns"    && <PatternsPanel sport={sport} />}
        </>
      )}
    </View>
  );
}

// ── Overview ─────────────────────────────────────────────────────
function OverviewPanel({ snap }: { snap: any }) {
  const factual = (snap.facts || []).filter((f: any) => f.provenance === "FACTUAL");
  const shadowFacts = (snap.facts || []).filter((f: any) => f.provenance === "SHADOW_SIGNAL");
  const shadow = snap.shadow || [];
  const dna = snap.matchup_dna || {};
  return (
    <View>
      <View style={s.miniStatRow}>
        <Stat label="FACTUAL" value={String(snap.factual_count || 0)} tint="#40d18a" />
        <Stat label="SHADOW"  value={String(snap.shadow_count || 0)}  tint="#c9d055" />
        <Stat label="DNA n"   value={dna?.available ? String(dna.sample_size || 0) : "—"} />
        <Stat label="ver"     value={String(snap.generation_version || "v1")} muted />
      </View>
      {factual.length === 0 && shadowFacts.length === 0 && shadow.length === 0 && (
        <Text style={s.dim}>
          No research data yet for this subject. The adapter checked
          player_game_logs, Statcast, and intel caches — the subject may not have
          historical rows in this pod. Try a well-established player, or check
          back after the next ingest cycle.
        </Text>
      )}
      {factual.length > 0 && (
        <>
          <Text style={s.h3}>Top factual signals</Text>
          {factual.slice(0, 8).map((f: any, i: number) => (
            <FactRow key={i} fact={f} />
          ))}
        </>
      )}
      {shadow.length > 0 && (
        <>
          <Text style={s.h3}>Shadow signals (UI-only)</Text>
          {shadow.map((sig: any, i: number) => (
            <ShadowRow key={i} sig={sig} />
          ))}
        </>
      )}
      {(snap.notes || []).length > 0 && (
        <Text style={[s.dim, { marginTop: 12 }]}>{snap.notes.join(" · ")}</Text>
      )}
    </View>
  );
}

function FactsPanel({ snap }: { snap: any }) {
  const facts = snap.facts || [];
  const bySection: Record<string, any[]> = {};
  for (const f of facts) {
    const k = f.section || "OTHER";
    if (!bySection[k]) bySection[k] = [];
    bySection[k].push(f);
  }
  const keys = Object.keys(bySection).sort();
  if (keys.length === 0) return <Text style={s.dim}>No facts available.</Text>;
  return (
    <View>
      {keys.map((k) => (
        <View key={k} style={{ marginBottom: 10 }}>
          <Text style={s.h3}>{k.replace(/_/g, " ")}</Text>
          {bySection[k].map((f: any, i: number) => <FactRow key={i} fact={f} />)}
        </View>
      ))}
    </View>
  );
}

function DistributionPanel({
  snap, sport, subject, line, setLine, marketHint, setMarketHint,
}: any) {
  const [lineResult, setLineResult] = useState<any | null>(null);
  const [running, setRunning] = useState(false);
  const dist = snap.distribution;
  const runLine = useCallback(async () => {
    const l = parseFloat(line);
    if (!isFinite(l)) return;
    setRunning(true);
    try {
      const r = await api.labResearchLineExplorer(sport, subject, l, marketHint || undefined);
      setLineResult(r);
    } catch { setLineResult(null); }
    finally { setRunning(false); }
  }, [sport, subject, line, marketHint]);

  if (!dist?.available) {
    return (
      <View>
        <Text style={s.dim}>
          No distribution data — insufficient history for {subject} in{" "}
          {marketHint || "primary stat"}.
        </Text>
        <View style={s.inputRow}>
          <View style={s.inputField}>
            <Text style={s.inputLabel}>Market hint</Text>
            <TextInput value={marketHint} onChangeText={setMarketHint}
              placeholder="hits · targets · pra …"
              placeholderTextColor={COLORS.textMuted}
              style={s.input} />
          </View>
        </View>
      </View>
    );
  }
  return (
    <View>
      <Text style={s.h3}>Empirical distribution · {dist.stat}</Text>
      <View style={s.miniStatRow}>
        <Stat label="n" value={String(dist.sample_size)} />
        <Stat label="mean" value={String(dist.mean)} />
        <Stat label="std" value={String(dist.std)} />
        <Stat label="med" value={String(dist.median)} />
      </View>
      <View style={s.miniStatRow}>
        <Stat label="p10" value={String(dist.p10)} />
        <Stat label="p25" value={String(dist.p25)} />
        <Stat label="p75" value={String(dist.p75)} />
        <Stat label="p90" value={String(dist.p90)} />
      </View>
      <Text style={s.h3}>Fair-price line explorer</Text>
      <View style={s.inputRow}>
        <View style={s.inputField}>
          <Text style={s.inputLabel}>Line</Text>
          <TextInput value={line} onChangeText={setLine}
            placeholder="e.g. 1.5"
            placeholderTextColor={COLORS.textMuted}
            keyboardType="decimal-pad" style={s.input} />
        </View>
        <View style={s.inputField}>
          <Text style={s.inputLabel}>Market hint</Text>
          <TextInput value={marketHint} onChangeText={setMarketHint}
            placeholder="hits · targets · pra …"
            placeholderTextColor={COLORS.textMuted}
            style={s.input} />
        </View>
      </View>
      <TouchableOpacity onPress={runLine}
        style={s.runBtn} testID="sl-run-line">
        <Text style={s.runBtnTxt}>{running ? "…" : "COMPUTE FAIR PRICE"}</Text>
      </TouchableOpacity>
      {lineResult?.available && (
        <View style={s.miniStatRow}>
          <Stat label="Over %" value={`${Math.round((lineResult.empirical_over_rate || 0) * 100)}%`} tint="#40d18a" />
          <Stat label="Under %" value={`${Math.round((lineResult.empirical_under_rate || 0) * 100)}%`} tint="#c9d055" />
          <Stat label="Fair O" value={lineResult.fair_over_odds != null ? fmtOdds(lineResult.fair_over_odds) : "—"} />
          <Stat label="Fair U" value={lineResult.fair_under_odds != null ? fmtOdds(lineResult.fair_under_odds) : "—"} />
        </View>
      )}
    </View>
  );
}

function MatchupDNAPanel({ snap }: { snap: any }) {
  const dna = snap.matchup_dna || {};
  if (!dna?.available) {
    return <Text style={s.dim}>No H2H history vs opponent in this pod.</Text>;
  }
  const rows = Object.entries(dna).filter(([k]) =>
    !["available", "reason", "vs", "sample_size"].includes(k));
  return (
    <View>
      <Text style={s.h3}>vs {dna.vs}  ·  n={dna.sample_size}</Text>
      <View style={s.tableRow}>
        {rows.map(([k, v]) => (
          <View key={k} style={s.tableCell}>
            <Text style={s.tableLbl}>{k.replace(/_/g, " ")}</Text>
            <Text style={s.tableVal}>{String(v)}</Text>
          </View>
        ))}
      </View>
    </View>
  );
}

function CalibrationPanel({ sport }: { sport: string }) {
  const [data, setData] = useState<any | null>(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    setLoading(true);
    api.labResearchCalibration(sport)
      .then(setData).catch(() => setData(null))
      .finally(() => setLoading(false));
  }, [sport]);
  if (loading) return <ActivityIndicator color={COLORS.textPrimary} />;
  if (!data?.available) return <Text style={s.dim}>No calibration data yet.</Text>;
  return (
    <View>
      <Text style={s.h3}>Model calibration · {sport}</Text>
      <View style={s.tblHeader}>
        <Text style={[s.tblCol, { flex: 1 }]}>Bucket</Text>
        <Text style={[s.tblCol, { flex: 0.8 }]}>N</Text>
        <Text style={[s.tblCol, { flex: 1 }]}>Pred</Text>
        <Text style={[s.tblCol, { flex: 1 }]}>Actual</Text>
        <Text style={[s.tblCol, { flex: 0.9 }]}>Gap</Text>
        <Text style={[s.tblCol, { flex: 1 }]}>ROI</Text>
      </View>
      {(data.rows || []).map((r: any) => (
        <View key={r.bucket} style={s.tblRow}>
          <Text style={[s.tblCell, { flex: 1 }]}>{r.bucket}</Text>
          <Text style={[s.tblCell, { flex: 0.8 }]}>{r.n}</Text>
          <Text style={[s.tblCell, { flex: 1 }]}>{Math.round(r.avg_pred_prob * 100)}%</Text>
          <Text style={[s.tblCell, { flex: 1 }]}>{Math.round(r.actual_hit_rate * 100)}%</Text>
          <Text style={[s.tblCell, { flex: 0.9, color: r.gap_pp > 3 ? "#e46d6d" : r.gap_pp < -3 ? "#40d18a" : COLORS.textPrimary }]}>
            {r.gap_pp > 0 ? "+" : ""}{r.gap_pp}
          </Text>
          <Text style={[s.tblCell, { flex: 1, color: r.roi_pct >= 0 ? "#40d18a" : "#e46d6d" }]}>
            {r.roi_pct >= 0 ? "+" : ""}{r.roi_pct}%
          </Text>
        </View>
      ))}
      <Text style={s.dim}>
        Read-only projection over settled picks. Never mutates canonical
        settlement or published Lock scores.
      </Text>
    </View>
  );
}

function PatternsPanel({ sport }: { sport: string }) {
  const [data, setData] = useState<any | null>(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    setLoading(true);
    api.labResearchPatterns(sport, { limit: 25, min_sample: 25 })
      .then(setData).catch(() => setData(null))
      .finally(() => setLoading(false));
  }, [sport]);
  if (loading) return <ActivityIndicator color={COLORS.textPrimary} />;
  if (!data?.available) return <Text style={s.dim}>No pattern data yet.</Text>;
  const rows = data.signals || [];
  return (
    <View>
      <Text style={s.h3}>Pattern Discovery 3.0 · SHADOW ONLY</Text>
      <Text style={s.dim}>
        Discovered market/odds buckets with statistically-credible historical
        hit rates. Wilson-lower-bound ranked. These never influence live Locks —
        they are quant hypotheses for further study.
      </Text>
      {rows.length === 0 && <Text style={s.dim}>No patterns cleared the sample threshold.</Text>}
      {rows.map((r: any, i: number) => (
        <View key={i} style={s.patternRow}>
          <View style={{ flex: 1 }}>
            <Text style={s.patternKey} numberOfLines={2}>{r.bucket}</Text>
            <Text style={s.patternMeta}>
              n={r.n} · HR {Math.round(r.hit_rate * 100)}% · WLB {Math.round(r.wilson_lower * 100)}%
            </Text>
          </View>
          <View style={[s.strengthPill, r.strength === "strong" ? s.strengthStrong : r.strength === "moderate" ? s.strengthMod : s.strengthWeak]}>
            <Text style={s.strengthTxt}>{r.strength.toUpperCase()}</Text>
          </View>
        </View>
      ))}
    </View>
  );
}

// ── Atoms ────────────────────────────────────────────────────────
function Stat({ label, value, tint, muted }: { label: string; value: string; tint?: string; muted?: boolean }) {
  return (
    <View style={s.statBox}>
      <Text style={s.statLbl}>{label}</Text>
      <Text style={[s.statVal, tint ? { color: tint } : null, muted && { color: COLORS.textMuted }]}>{value}</Text>
    </View>
  );
}

function FactRow({ fact }: { fact: any }) {
  const val = fact.value;
  const rendered = typeof val === "object" && val
    ? Object.entries(val).slice(0, 4).map(([k, v]) => `${k}: ${v}`).join(" · ")
    : String(val);
  return (
    <View style={s.factRow}>
      <View style={s.factDot} />
      <View style={{ flex: 1 }}>
        <Text style={s.factLbl}>{fact.label}
          <Text style={s.factQ}>  · {fact.quality}</Text>
        </Text>
        <Text style={s.factVal} numberOfLines={2}>{rendered}</Text>
      </View>
      {fact.sample_size != null && (
        <View style={s.factN}>
          <Text style={s.factNTxt}>n={fact.sample_size}</Text>
        </View>
      )}
    </View>
  );
}

function ShadowRow({ sig }: { sig: any }) {
  return (
    <View style={s.shadowRow}>
      <View style={s.shadowBadge}>
        <Text style={s.shadowBadgeTxt}>SHADOW</Text>
      </View>
      <View style={{ flex: 1 }}>
        <Text style={s.factLbl}>{sig.label}</Text>
        <Text style={s.factVal} numberOfLines={2}>{sig.description}</Text>
        <Text style={s.factMeta}>
          n={sig.n} · HR {Math.round(sig.hit_rate * 100)}% · WLB {Math.round(sig.wilson_lower * 100)}% · {sig.strength}
        </Text>
      </View>
    </View>
  );
}

function fmtOdds(n: number): string {
  if (!isFinite(n)) return "—";
  return n > 0 ? `+${n}` : `${n}`;
}

// ── Scorecard §5-§14 aggregated research panel ───────────────────
function ScorecardPanel({ sport, subject, opponent, marketHint, line }: any) {
  const [data, setData] = useState<any | null>(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    setLoading(true);
    api.labResearchScorecard({
      sport, subject, opponent: opponent || undefined,
      stat_field: marketHint || undefined,
      line: line ? parseFloat(line) : undefined,
    }).then(setData).catch(() => setData(null))
      .finally(() => setLoading(false));
  }, [sport, subject, opponent, marketHint, line]);
  if (loading) return <ActivityIndicator color={COLORS.textPrimary} style={{ marginTop: 12 }} />;
  if (!data) return <Text style={s.dim}>No scorecard data available.</Text>;
  const dims = data.scorecard?.dimensions || {};
  const gradeColor = (g: string) =>
    g === "HIGH" ? "#40d18a" : g === "MEDIUM" ? "#c9d055" : COLORS.textMuted;
  const dimRows = [
    ["OPPORTUNITY",     data.role_change?.classification],
    ["MATCHUP",         data.opponent_context?.classification],
    ["UNDERLYING_SKILL",data.regression?.classification],
    ["FORM_STABILITY",  data.sample_stability?.classification],
    ["PRICE_QUALITY",   data.price_quality?.classification],
    ["DATA_QUALITY",    null],
  ];
  return (
    <View>
      <View style={s.miniStatRow}>
        <Stat label="Overall" value={data.scorecard?.research_quality || "—"}
              tint={gradeColor(data.scorecard?.research_quality || "")} />
        <Stat label="Drift" value={data.model_drift?.classification || "—"} />
        <Stat label="H2H" value={data.h2h_quality?.classification || "—"} />
        <Stat label="Line" value={data.line_sensitivity?.classification || "—"} />
      </View>
      <Text style={s.h3}>Research dimensions</Text>
      {dimRows.map(([label, cls]) => (
        <View key={label as string} style={s.scoreRow}>
          <Text style={s.scoreLbl}>{(label as string).replace(/_/g, " ")}</Text>
          <View style={{ flex: 1 }}>
            {cls != null && (
              <Text style={s.scoreCls} numberOfLines={1}>{String(cls).replace(/_/g, " ")}</Text>
            )}
          </View>
          <View style={[s.gradePill, { backgroundColor: gradeColor(dims[label as string] || "LOW") + "22", borderColor: gradeColor(dims[label as string] || "LOW") + "55" }]}>
            <Text style={[s.gradePillTxt, { color: gradeColor(dims[label as string] || "LOW") }]}>
              {dims[label as string] || "LOW"}
            </Text>
          </View>
        </View>
      ))}
      {data.market_disagreement?.available && (
        <>
          <Text style={s.h3}>Market disagreement</Text>
          <View style={s.miniStatRow}>
            <Stat label="Model" value={`${Math.round((data.market_disagreement.model_prob || 0) * 100)}%`} />
            <Stat label="Market" value={`${Math.round((data.market_disagreement.market_prob || 0) * 100)}%`} />
            <Stat label="Δ pp" value={`${Math.round((data.market_disagreement.difference || 0) * 100)}`} tint={data.market_disagreement.difference > 0 ? "#40d18a" : "#e46d6d"} />
            <Stat label="Class" value={(data.market_disagreement.classification || "—").split("_").pop() || "—"} />
          </View>
        </>
      )}
      {data.line_sensitivity?.available && (
        <>
          <Text style={s.h3}>Line sensitivity  (Model thresholds — NOT sportsbook lines)</Text>
          {(data.line_sensitivity.curve || []).map((c: any, i: number) => (
            <View key={i} style={s.senseRow}>
              <Text style={s.senseLbl}>{c.model_threshold}</Text>
              <View style={s.senseBar}>
                <View style={[s.senseFill, { width: `${Math.round(c.empirical_over * 100)}%` }]} />
              </View>
              <Text style={s.senseVal}>{Math.round(c.empirical_over * 100)}%</Text>
            </View>
          ))}
        </>
      )}
      <Text style={s.dim}>{data.scorecard?.note}</Text>
    </View>
  );
}

// ── Professional Today Feed (§8) ─────────────────────────────────
function TodayFeed({ sport, onPickSubject }: {
  sport: Sport; onPickSubject: (name: string, opponent?: string) => void;
}) {
  const [data, setData] = useState<any | null>(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    setLoading(true);
    api.labResearchToday({ sport, limit: 12 })
      .then(setData).catch(() => setData(null))
      .finally(() => setLoading(false));
  }, [sport]);
  if (loading) return <ActivityIndicator color={COLORS.textPrimary} style={{ marginTop: 16 }} />;
  const sections = data?.sections || {};
  const order: [string, string][] = [
    ["TOP_RESEARCHED", "Top Researched"],
    ["TREND_RADAR", "Trend Radar"],
    ["POSITIVE_REGRESSION", "Positive Regression"],
    ["STRONG_MATCHUPS", "Strong Matchups"],
    ["ROLE_CHANGES", "Role Changes"],
    ["RISK_FLAGS", "Risk / Trap Flags"],
  ];
  const nonEmpty = order.filter(([k]) => (sections[k] || []).length > 0);
  return (
    <View>
      <Text style={s.dim}>{data?.note || ""}</Text>
      {nonEmpty.length === 0 && (
        <Text style={s.dim}>
          No {sport} research signals surfaced yet. Populate by opening a
          subject above or check back after the next data cycle.
        </Text>
      )}
      {nonEmpty.map(([k, label]) => (
        <View key={k} style={{ marginTop: 8 }}>
          <Text style={s.h3}>{label}</Text>
          {(sections[k] || []).slice(0, 6).map((row: any, i: number) => (
            <TouchableOpacity
              key={i}
              onPress={() => onPickSubject(row.subject, row.opponent)}
              style={s.todayRow}
              testID={`today-${k}-${i}`}
            >
              <View style={{ flex: 1 }}>
                <View style={{ flexDirection: "row", alignItems: "center", gap: 6 }}>
                  <Text style={s.todaySport}>{row.sport}</Text>
                  <Text style={s.todayName} numberOfLines={1}>{row.subject}</Text>
                </View>
                <Text style={s.todayMeta} numberOfLines={1}>
                  {(row.trend?.trend_type || "").replace(/_/g, " ")}
                  {row.trend?.direction ? ` · ${row.trend.direction}` : ""}
                  {row.trend?.strength ? ` · ${row.trend.strength}` : ""}
                </Text>
                {(row.market_relevance || row.market) && (
                  <Text style={s.todayMeta} numberOfLines={1}>
                    {(row.market_relevance || [row.market]).slice(0, 2).join(" · ")}
                  </Text>
                )}
              </View>
              <View style={[
                s.reachPill,
                row.reachability === "ON_LOCKS" ? s.reachOnLock : s.reachResearch,
              ]}>
                <Text style={s.reachPillTxt}>
                  {row.reachability === "ON_LOCKS" ? "ON LOCKS" : "RESEARCH"}
                </Text>
              </View>
            </TouchableOpacity>
          ))}
        </View>
      ))}
    </View>
  );
}

const s = StyleSheet.create({
  headerRow: { flexDirection: "row", alignItems: "center", gap: 6, marginBottom: 4 },
  headerTitle: { color: COLORS.textPrimary, fontSize: 15, fontWeight: "900", letterSpacing: 0.5 },
  badge: { paddingHorizontal: 6, paddingVertical: 2, backgroundColor: "rgba(255,215,0,0.12)", borderRadius: 4, marginLeft: 6, borderWidth: 1, borderColor: "rgba(255,215,0,0.3)" },
  badgeTxt: { color: COLORS.goldElite, fontSize: 9, fontWeight: "900", letterSpacing: 0.8 },
  blurb: { color: COLORS.textMuted, fontSize: 11, lineHeight: 16, marginBottom: 10 },
  chipRow: { gap: 6, paddingRight: 12, paddingVertical: 4, flexDirection: "row" },
  chip: {
    flexDirection: "row", alignItems: "center",
    paddingHorizontal: 10, paddingVertical: 5, borderRadius: 14,
    backgroundColor: "rgba(255,255,255,0.04)",
    borderWidth: 1, borderColor: COLORS.borderDefault,
  },
  chipActive: { backgroundColor: COLORS.textPrimary, borderColor: COLORS.textPrimary },
  chipTxt: { color: COLORS.textMuted, fontSize: 10, fontWeight: "800", letterSpacing: 0.6 },
  chipTxtActive: { color: "#000" },
  suggChip: {
    paddingHorizontal: 10, paddingVertical: 5, borderRadius: 14,
    backgroundColor: "rgba(255,215,0,0.08)",
    borderWidth: 1, borderColor: "rgba(255,215,0,0.24)",
  },
  suggChipTxt: { color: COLORS.goldElite, fontSize: 11, fontWeight: "800" },
  inputRow: { flexDirection: "row", gap: 8, marginBottom: 8 },
  inputField: { flex: 1 },
  inputLabel: { color: COLORS.textMuted, fontSize: 9, fontWeight: "800", letterSpacing: 0.5, marginBottom: 3 },
  input: {
    color: COLORS.textPrimary, fontSize: 13, fontWeight: "700",
    borderWidth: 1, borderColor: COLORS.borderDefault,
    borderRadius: 6, paddingHorizontal: 10, paddingVertical: 8,
    backgroundColor: "rgba(255,255,255,0.03)",
  },
  errTxt: { color: "#e46d6d", fontSize: 12, marginTop: 8 },
  emptyBox: {
    padding: 14, borderRadius: 10,
    backgroundColor: "rgba(255,255,255,0.03)",
    borderWidth: 1, borderColor: COLORS.borderDefault,
    marginTop: 8,
  },
  emptyTitle: { color: COLORS.textPrimary, fontSize: 13, fontWeight: "900", marginBottom: 4 },
  emptyBody: { color: COLORS.textMuted, fontSize: 12, lineHeight: 17 },
  dim: { color: COLORS.textMuted, fontSize: 11, lineHeight: 15, marginTop: 6, fontStyle: "italic" },
  h3: { color: COLORS.textPrimary, fontSize: 12, fontWeight: "900", letterSpacing: 0.5, marginTop: 12, marginBottom: 6, textTransform: "uppercase" },
  miniStatRow: { flexDirection: "row", gap: 6, marginBottom: 8 },
  statBox: {
    flex: 1, padding: 8, borderRadius: 6,
    backgroundColor: "rgba(255,255,255,0.04)",
    borderWidth: 1, borderColor: COLORS.borderDefault,
    alignItems: "center",
  },
  statLbl: { color: COLORS.textMuted, fontSize: 8.5, fontWeight: "800", letterSpacing: 0.8 },
  statVal: { color: COLORS.textPrimary, fontSize: 13, fontWeight: "900", marginTop: 2 },
  factRow: {
    flexDirection: "row", alignItems: "flex-start", gap: 8,
    padding: 8, borderRadius: 6, marginBottom: 4,
    backgroundColor: "rgba(64,209,138,0.05)",
    borderLeftWidth: 2, borderLeftColor: "#40d18a",
  },
  factDot: { width: 4, height: 4, borderRadius: 2, backgroundColor: "#40d18a", marginTop: 6 },
  factLbl: { color: COLORS.textPrimary, fontSize: 12, fontWeight: "800" },
  factQ: { color: COLORS.textMuted, fontSize: 9, fontWeight: "700", letterSpacing: 0.4 },
  factVal: { color: COLORS.textMuted, fontSize: 11, marginTop: 2 },
  factMeta: { color: COLORS.textMuted, fontSize: 10, marginTop: 3, fontStyle: "italic" },
  factN: { paddingHorizontal: 4, paddingVertical: 2, backgroundColor: "rgba(255,255,255,0.06)", borderRadius: 3 },
  factNTxt: { color: COLORS.textMuted, fontSize: 9, fontWeight: "800" },
  shadowRow: {
    flexDirection: "row", alignItems: "flex-start", gap: 8,
    padding: 8, borderRadius: 6, marginBottom: 4,
    backgroundColor: "rgba(201,208,85,0.05)",
    borderLeftWidth: 2, borderLeftColor: "#c9d055",
  },
  shadowBadge: {
    paddingHorizontal: 5, paddingVertical: 2,
    backgroundColor: "rgba(201,208,85,0.15)",
    borderRadius: 3, height: 18,
  },
  shadowBadgeTxt: { color: "#c9d055", fontSize: 8.5, fontWeight: "900", letterSpacing: 0.6 },
  tableRow: { flexDirection: "row", flexWrap: "wrap", gap: 6 },
  tableCell: {
    minWidth: 100, flex: 1, padding: 8, borderRadius: 6,
    backgroundColor: "rgba(255,255,255,0.04)",
    borderWidth: 1, borderColor: COLORS.borderDefault,
  },
  tableLbl: { color: COLORS.textMuted, fontSize: 9, fontWeight: "800", letterSpacing: 0.5 },
  tableVal: { color: COLORS.textPrimary, fontSize: 13, fontWeight: "900", marginTop: 2 },
  runBtn: {
    alignSelf: "flex-start", paddingHorizontal: 14, paddingVertical: 8,
    backgroundColor: COLORS.textPrimary, borderRadius: 6, marginTop: 4, marginBottom: 8,
  },
  runBtnTxt: { color: "#000", fontWeight: "900", letterSpacing: 0.8, fontSize: 11 },
  tblHeader: {
    flexDirection: "row", paddingVertical: 6, paddingHorizontal: 4,
    borderBottomWidth: 1, borderBottomColor: COLORS.borderDefault, marginBottom: 4,
  },
  tblCol: { color: COLORS.textMuted, fontSize: 9, fontWeight: "800", letterSpacing: 0.5 },
  tblRow: {
    flexDirection: "row", paddingVertical: 6, paddingHorizontal: 4,
    borderBottomWidth: 1, borderBottomColor: "rgba(255,255,255,0.04)",
  },
  tblCell: { color: COLORS.textPrimary, fontSize: 11, fontWeight: "700" },
  patternRow: {
    flexDirection: "row", alignItems: "center", gap: 8,
    padding: 10, borderRadius: 8, marginBottom: 6,
    backgroundColor: "rgba(255,255,255,0.03)",
    borderWidth: 1, borderColor: COLORS.borderDefault,
  },
  patternKey: { color: COLORS.textPrimary, fontSize: 12, fontWeight: "800" },
  patternMeta: { color: COLORS.textMuted, fontSize: 10, marginTop: 2 },
  strengthPill: { paddingHorizontal: 8, paddingVertical: 4, borderRadius: 4 },
  strengthStrong: { backgroundColor: "rgba(64,209,138,0.15)" },
  strengthMod:    { backgroundColor: "rgba(201,208,85,0.15)" },
  strengthWeak:   { backgroundColor: "rgba(255,255,255,0.05)" },
  strengthTxt: { color: COLORS.textPrimary, fontSize: 9, fontWeight: "900", letterSpacing: 0.6 },

  // ── Today Feed rows ────────────────────────────────────────────
  todayRow: {
    flexDirection: "row", alignItems: "center", gap: 8,
    padding: 10, borderRadius: 8, marginBottom: 5,
    backgroundColor: "rgba(255,255,255,0.03)",
    borderWidth: 1, borderColor: COLORS.borderDefault,
  },
  todaySport: { color: COLORS.goldElite, fontSize: 10, fontWeight: "900", letterSpacing: 0.6 },
  todayName: { color: COLORS.textPrimary, fontSize: 13, fontWeight: "800", flex: 1 },
  todayMeta: { color: COLORS.textMuted, fontSize: 10.5, marginTop: 2 },
  reachPill: { paddingHorizontal: 6, paddingVertical: 3, borderRadius: 4 },
  reachOnLock: { backgroundColor: "rgba(64,209,138,0.15)" },
  reachResearch: { backgroundColor: "rgba(201,208,85,0.10)" },
  reachPillTxt: { color: COLORS.textPrimary, fontSize: 8.5, fontWeight: "900", letterSpacing: 0.6 },
  // Scorecard §14
  scoreRow: {
    flexDirection: "row", alignItems: "center", gap: 8,
    paddingVertical: 6, paddingHorizontal: 8, marginBottom: 3,
    backgroundColor: "rgba(255,255,255,0.02)",
    borderRadius: 6, borderWidth: 1, borderColor: COLORS.borderDefault,
  },
  scoreLbl: { color: COLORS.textPrimary, fontSize: 10, fontWeight: "900", letterSpacing: 0.6, width: 130 },
  scoreCls: { color: COLORS.textMuted, fontSize: 10.5 },
  gradePill: { paddingHorizontal: 8, paddingVertical: 3, borderRadius: 4, borderWidth: 1 },
  gradePillTxt: { fontSize: 9, fontWeight: "900", letterSpacing: 0.6 },
  senseRow: { flexDirection: "row", alignItems: "center", gap: 8, paddingVertical: 4 },
  senseLbl: { color: COLORS.textMuted, fontSize: 10, width: 44, fontWeight: "700" },
  senseBar: { flex: 1, height: 6, borderRadius: 3, backgroundColor: "rgba(255,255,255,0.06)", overflow: "hidden" },
  senseFill: { height: "100%", backgroundColor: COLORS.goldElite },
  senseVal: { color: COLORS.textPrimary, fontSize: 11, fontWeight: "800", width: 44, textAlign: "right" },
});
