/**
 * XGFormPanel — Soccer goalscorer deep-dive panel.
 *
 * Renders the Understat-derived per-player season form for goalscorer
 * markets (Anytime, First, Score-or-Assist). Surfaces the math behind
 * the HOT/COLD chip on the card: xG/90, npxG/90, goals over expected,
 * and the ±6pp probability lift baked into the lock score.
 *
 * Loaded from `GET /api/picks/{id}/player-form`. The endpoint cleanly
 * 404s when the pick isn't a goalscorer market or the player isn't in
 * the Top 5 European leagues — in those cases this panel renders nothing.
 */
import React, { useEffect, useState } from "react";
import { View, Text, StyleSheet, ActivityIndicator } from "react-native";
import { COLORS } from "@/src/theme";
import { api } from "@/src/lib/api";

type Form = {
  player_name: string;
  team: string;
  league: string;
  season: string;
  position: string;
  games: number;
  minutes: number;
  goals: number;
  xg: number;
  npxg: number;
  assists: number;
  xa: number;
  shots: number;
  key_passes: number;
  xg_per_90: number;
  npxg_per_90: number;
  goals_per_90: number;
  shots_per_90: number;
  goals_over_xg: number;
  form_label: "HOT" | "COLD" | "NEUTRAL";
  form_score: number;
  form_lift: number;          // -0.06 / 0.0 / +0.06
  updated_at: string | null;
  source: string;
};

export function XGFormPanel({ pickId }: { pickId: string }) {
  const [data, setData] = useState<Form | null>(null);
  const [loading, setLoading] = useState(true);
  const [unsupported, setUnsupported] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await api.playerForm(pickId);
        if (cancelled) return;
        setData(res);
      } catch {
        // 404 means non-goalscorer or unknown player — hide silently.
        if (cancelled) return;
        setUnsupported(true);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [pickId]);

  if (unsupported) return null;
  if (loading) {
    return (
      <View style={styles.wrap}>
        <ActivityIndicator color={COLORS.voltBlue} />
      </View>
    );
  }
  if (!data) return null;

  const liftPp = (data.form_lift ?? 0) * 100;
  const labelColor =
    data.form_label === "HOT" ? "#FCA5A5"
    : data.form_label === "COLD" ? "#93C5FD"
    : COLORS.textMuted;
  const headerIcon =
    data.form_label === "HOT" ? "🔥"
    : data.form_label === "COLD" ? "❄️"
    : "📊";

  return (
    <View style={styles.wrap} testID="xg-form-panel">
      <View style={styles.headerRow}>
        <View style={styles.titleBlock}>
          <Text style={styles.kicker}>xG FORM · UNDERSTAT</Text>
          <Text style={styles.title}>{data.player_name}</Text>
          <Text style={styles.sub}>
            {data.team} · {prettyLeague(data.league)} {data.season}/{(Number(data.season) + 1) % 100}
          </Text>
        </View>
        <View style={styles.scoreBlock}>
          <Text style={styles.scoreIcon}>{headerIcon}</Text>
          <Text style={[styles.scoreLabel, { color: labelColor }]}>
            {data.form_label}
          </Text>
          {data.form_label !== "NEUTRAL" && (
            <Text style={[styles.lift, { color: labelColor }]}>
              {liftPp >= 0 ? "+" : ""}{liftPp.toFixed(0)}pp lift
            </Text>
          )}
        </View>
      </View>

      <View style={styles.metricsGrid}>
        <Metric label="GOALS" value={String(data.goals)} sub={`${data.games} GP`} />
        <Metric
          label="xG"
          value={data.xg?.toFixed(1) ?? "—"}
          sub={`vs ${data.goals} goals`}
        />
        <Metric
          label="GOALS / xG"
          value={data.goals_over_xg?.toFixed(2) ?? "—"}
          sub={
            data.goals_over_xg >= 1.05 ? "overperforming"
            : data.goals_over_xg <= 0.85 ? "underperforming"
            : "in line"
          }
          color={
            data.goals_over_xg >= 1.05 ? "#86EFAC"
            : data.goals_over_xg <= 0.85 ? "#FCA5A5"
            : COLORS.textPrimary
          }
        />
      </View>

      <View style={styles.metricsGrid}>
        <Metric label="xG / 90" value={data.xg_per_90?.toFixed(2) ?? "—"} />
        <Metric
          label="npxG / 90"
          value={data.npxg_per_90?.toFixed(2) ?? "—"}
          sub="non-pen"
        />
        <Metric label="SHOTS / 90" value={data.shots_per_90?.toFixed(1) ?? "—"} />
      </View>

      <Text style={styles.footnote}>
        Source: Understat · refreshed every 12 h · {data.minutes} mins played
      </Text>
    </View>
  );
}

function Metric({
  label, value, sub, color,
}: {
  label: string; value: string; sub?: string; color?: string;
}) {
  return (
    <View style={styles.metric}>
      <Text style={styles.metricLabel}>{label}</Text>
      <Text style={[styles.metricValue, color ? { color } : null]}>{value}</Text>
      {sub ? <Text style={styles.metricSub}>{sub}</Text> : null}
    </View>
  );
}

function prettyLeague(slug: string): string {
  switch (slug) {
    case "EPL":         return "Premier League";
    case "La_liga":     return "La Liga";
    case "Bundesliga":  return "Bundesliga";
    case "Serie_A":     return "Serie A";
    case "Ligue_1":     return "Ligue 1";
    default: return slug;
  }
}

const styles = StyleSheet.create({
  wrap: {
    backgroundColor: COLORS.surface,
    borderRadius: 14,
    borderWidth: 1,
    borderColor: COLORS.borderDefault,
    padding: 16,
    marginVertical: 8,
  },
  headerRow: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "flex-start",
    marginBottom: 14,
  },
  titleBlock: { flex: 1, paddingRight: 10 },
  kicker: {
    color: COLORS.voltBlue,
    fontSize: 10,
    fontWeight: "900",
    letterSpacing: 1.4,
    marginBottom: 4,
  },
  title: {
    color: COLORS.textPrimary,
    fontSize: 16,
    fontWeight: "900",
    letterSpacing: -0.3,
  },
  sub: {
    color: COLORS.textMuted,
    fontSize: 11,
    fontWeight: "600",
    marginTop: 2,
    letterSpacing: 0.2,
  },
  scoreBlock: { alignItems: "flex-end", minWidth: 80 },
  scoreIcon: { fontSize: 22, lineHeight: 24 },
  scoreLabel: {
    fontSize: 11,
    fontWeight: "900",
    letterSpacing: 1.2,
    marginTop: 2,
  },
  lift: {
    fontSize: 10,
    fontWeight: "800",
    letterSpacing: 0.6,
    marginTop: 2,
    fontVariant: ["tabular-nums"],
  },
  metricsGrid: {
    flexDirection: "row",
    justifyContent: "space-between",
    gap: 8,
    marginBottom: 8,
  },
  metric: { flex: 1 },
  metricLabel: {
    color: COLORS.textMuted,
    fontSize: 9,
    fontWeight: "800",
    letterSpacing: 1.3,
  },
  metricValue: {
    color: COLORS.textPrimary,
    fontSize: 17,
    fontWeight: "900",
    letterSpacing: -0.3,
    marginTop: 3,
    fontVariant: ["tabular-nums"],
  },
  metricSub: {
    color: COLORS.textMuted,
    fontSize: 10,
    fontWeight: "600",
    marginTop: 1,
  },
  footnote: {
    color: COLORS.textMuted,
    fontSize: 10,
    fontWeight: "600",
    marginTop: 6,
    letterSpacing: 0.2,
  },
});
