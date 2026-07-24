/**
 * H2HPanel — unified head-to-head deep-dive block.
 *
 * Renders on the /pick/[id] screen for every pick. Fetches the full H2H
 * bundle from /api/picks/{id}/h2h (backed by services.h2h_enricher) and
 * lays out:
 *   • one-liner summary at the top (matches the LockPickCard chip)
 *   • team-level H2H (last N meetings, W-L record, avg total, recent list)
 *   • player-level H2H (sport-dependent — pitcher vs team, tennis A-vs-B,
 *     soccer player hit-rate vs opponent)
 *   • situational block (venue / weather / referee) when available
 *
 * Visual language: LIGHT theme card (amber-tinted surface, dark ink) so
 * the H2H section is always high-contrast and readable — user spec was
 * "make it look good, ensure you can see visible no dark colors".
 *
 * If the backend returns { ok: false } (no data), the panel renders a
 * subtle "No H2H sample yet" note instead of nothing — so the user knows
 * the section exists and isn't missing.
 */
import React, { useEffect, useState } from "react";
import { View, Text, StyleSheet, ActivityIndicator } from "react-native";
import { api } from "@/src/lib/api";

export function H2HPanel({ pickId }: { pickId: string }) {
  const [bundle, setBundle] = useState<Awaited<ReturnType<typeof api.h2h>> | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const b = await api.h2h(pickId);
        if (!cancelled) setBundle(b);
      } catch {
        if (!cancelled) setBundle({ ok: false } as any);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [pickId]);

  if (loading) {
    return (
      <View style={[styles.card, styles.cardLight]} testID="h2h-panel-loading">
        <View style={styles.headerRow}>
          <Text style={styles.headerIcon}>⚔️</Text>
          <Text style={styles.headerText}>HEAD-TO-HEAD</Text>
        </View>
        <ActivityIndicator size="small" color="#B45309" style={{ marginTop: 10 }} />
      </View>
    );
  }

  const b = bundle;
  const hasTeam = !!(b && b.team_h2h && (b.team_h2h.home_wins + b.team_h2h.away_wins > 0));
  const hasPlayer = !!(b && b.player_h2h && (b.player_h2h.sample_size ?? 0) > 0);
  const hasSituational = !!(b && b.situational && (b.situational.venue || (b.situational.notes || []).length > 0));
  const anyData = hasTeam || hasPlayer || hasSituational;

  return (
    <View style={[styles.card, styles.cardLight]} testID="h2h-panel">
      <View style={styles.headerRow}>
        <Text style={styles.headerIcon}>⚔️</Text>
        <Text style={styles.headerText}>HEAD-TO-HEAD</Text>
        {b?.summary ? (
          <Text style={styles.summaryPill} numberOfLines={1}>{b.summary}</Text>
        ) : null}
      </View>

      {/* ── Player-level H2H ── */}
      {hasPlayer && b?.player_h2h && (
        <View style={styles.section} testID="h2h-player">
          <Text style={styles.sectionLabel}>PLAYER vs OPPONENT</Text>
          <Text style={styles.playerHeadline}>{b.player_h2h.primary_value_display}</Text>
          <Text style={styles.subLine}>
            {b.player_h2h.player} vs {b.player_h2h.vs_opponent}
            {b.player_h2h.sample_size ? ` · ${b.player_h2h.sample_size} sample` : ""}
          </Text>
          {b.sport === "MLB" && b.player_h2h.season_avg_k != null && (
            <Text style={styles.subLine}>
              Season baseline: {b.player_h2h.season_avg_k?.toFixed(1)} K over {b.player_h2h.season_starts ?? 0} starts
            </Text>
          )}
          {Array.isArray(b.player_h2h.recent) && b.player_h2h.recent.length > 0 && (
            <View style={styles.recentBox}>
              {b.player_h2h.recent.slice(0, 5).map((r, i) => (
                <View key={i} style={styles.recentRow}>
                  <Text style={styles.recentDate}>{String(r.date || "").slice(0, 10)}</Text>
                  <Text style={styles.recentBody} numberOfLines={1}>
                    {r.opp ? `vs ${r.opp}` : (r.event || r.result || "")}
                  </Text>
                  <Text style={styles.recentValue}>
                    {r.k != null ? `${r.k} K` : (r.result ? String(r.result).toUpperCase() : "")}
                  </Text>
                </View>
              ))}
            </View>
          )}
        </View>
      )}

      {/* ── Team-level H2H ── */}
      {hasTeam && b?.team_h2h && (
        <View style={styles.section} testID="h2h-team">
          <Text style={styles.sectionLabel}>TEAM MEETINGS</Text>
          <View style={styles.teamStatsRow}>
            <View style={styles.teamStatCell}>
              <Text style={styles.teamStatValue}>{b.team_h2h.record}</Text>
              <Text style={styles.teamStatLabel}>RECORD</Text>
            </View>
            <View style={styles.teamStatCell}>
              <Text style={styles.teamStatValue}>{b.team_h2h.meetings}</Text>
              <Text style={styles.teamStatLabel}>MEETINGS</Text>
            </View>
            {b.team_h2h.avg_total != null && (
              <View style={styles.teamStatCell}>
                <Text style={styles.teamStatValue}>{b.team_h2h.avg_total}</Text>
                <Text style={styles.teamStatLabel}>AVG TOTAL</Text>
              </View>
            )}
          </View>
          {Array.isArray(b.team_h2h.recent) && b.team_h2h.recent.length > 0 && (
            <View style={styles.recentBox}>
              {b.team_h2h.recent.slice(0, 5).map((r, i) => (
                <View key={i} style={styles.recentRow}>
                  <Text style={styles.recentDate}>{String(r.date || "").slice(0, 10)}</Text>
                  <Text style={styles.recentBody} numberOfLines={1}>{r.venue || ""}</Text>
                  <Text style={styles.recentValue}>{r.score}</Text>
                </View>
              ))}
            </View>
          )}
        </View>
      )}

      {/* ── Situational block ── */}
      {hasSituational && b?.situational && (
        <View style={styles.section} testID="h2h-situational">
          <Text style={styles.sectionLabel}>SITUATIONAL</Text>
          {!!b.situational.venue && <Text style={styles.subLine}>📍 {b.situational.venue}</Text>}
          {(b.situational.notes || []).map((n, i) => (
            <Text key={i} style={styles.subLine}>• {n}</Text>
          ))}
        </View>
      )}

      {!anyData && (
        <View style={styles.emptyBox}>
          <Text style={styles.emptyText}>
            No head-to-head sample yet for this matchup. As results settle over the coming weeks,
            H2H stats will populate automatically.
          </Text>
        </View>
      )}

      {Array.isArray(b?.sources) && (b?.sources?.length ?? 0) > 0 && (
        <Text style={styles.sourceFootnote}>
          Sources: {b?.sources?.join(", ")}
        </Text>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  // Card container — light amber surface, dark ink for high contrast.
  card: {
    borderRadius: 12,
    padding: 14,
    marginTop: 10,
    borderWidth: 1,
  },
  cardLight: {
    backgroundColor: "#FEF3C7",   // amber-100
    borderColor: "#F59E0B",       // amber-500
  },
  headerRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
  },
  headerIcon: { fontSize: 15 },
  headerText: {
    color: "#78350F",             // amber-900
    fontSize: 12,
    fontWeight: "900",
    letterSpacing: 1.4,
  },
  summaryPill: {
    marginLeft: "auto",
    color: "#78350F",
    fontSize: 11,
    fontWeight: "800",
    backgroundColor: "#FDE68A",   // amber-200
    paddingHorizontal: 8,
    paddingVertical: 3,
    borderRadius: 999,
    overflow: "hidden",
    maxWidth: 220,
  },
  section: {
    marginTop: 12,
    paddingTop: 10,
    borderTopWidth: 1,
    borderTopColor: "rgba(180, 83, 9, 0.28)", // amber-700 / 28%
  },
  sectionLabel: {
    color: "#92400E",            // amber-800
    fontSize: 10,
    fontWeight: "900",
    letterSpacing: 1.4,
    marginBottom: 6,
  },
  playerHeadline: {
    color: "#1F2937",             // gray-800 for max legibility
    fontSize: 17,
    fontWeight: "900",
    letterSpacing: -0.2,
  },
  subLine: {
    color: "#374151",             // gray-700
    fontSize: 13,
    fontWeight: "600",
    marginTop: 3,
    lineHeight: 18,
  },
  teamStatsRow: {
    flexDirection: "row",
    gap: 10,
    marginTop: 4,
    marginBottom: 6,
  },
  teamStatCell: {
    flex: 1,
    backgroundColor: "#FFFBEB",   // amber-50
    borderColor: "#FCD34D",       // amber-300
    borderWidth: 1,
    borderRadius: 8,
    paddingVertical: 8,
    paddingHorizontal: 6,
    alignItems: "center",
  },
  teamStatValue: {
    color: "#1F2937",
    fontSize: 18,
    fontWeight: "900",
    letterSpacing: -0.4,
  },
  teamStatLabel: {
    color: "#92400E",
    fontSize: 9,
    fontWeight: "800",
    letterSpacing: 1.1,
    marginTop: 2,
  },
  recentBox: {
    marginTop: 8,
    borderRadius: 8,
    borderWidth: 1,
    borderColor: "#FCD34D",
    backgroundColor: "#FFFBEB",
    overflow: "hidden",
  },
  recentRow: {
    flexDirection: "row",
    alignItems: "center",
    paddingVertical: 6,
    paddingHorizontal: 10,
    gap: 8,
    borderBottomWidth: 1,
    borderBottomColor: "rgba(180, 83, 9, 0.14)",
  },
  recentDate: {
    color: "#78350F",
    fontSize: 11,
    fontWeight: "800",
    width: 78,
  },
  recentBody: {
    flex: 1,
    color: "#374151",
    fontSize: 12,
    fontWeight: "600",
  },
  recentValue: {
    color: "#1F2937",
    fontSize: 12,
    fontWeight: "900",
    minWidth: 40,
    textAlign: "right",
  },
  emptyBox: {
    marginTop: 12,
    paddingTop: 10,
    borderTopWidth: 1,
    borderTopColor: "rgba(180, 83, 9, 0.28)",
  },
  emptyText: {
    color: "#78350F",
    fontSize: 12,
    fontWeight: "600",
    lineHeight: 18,
    fontStyle: "italic",
  },
  sourceFootnote: {
    marginTop: 10,
    color: "#B45309",             // amber-700
    fontSize: 10,
    fontWeight: "700",
    letterSpacing: 0.4,
  },
});
