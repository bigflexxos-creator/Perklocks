/**
 * H2HPanel — head-to-head data on the /pick/[id] deep-dive screen.
 *
 * Design (2026-02, revision 2 — user feedback: "should be a more defiant
 * looks like it's mixing game day with player data"):
 *
 * Two DISTINCT sub-cards, so the user can never confuse team-level
 * historical meetings with player-level splits:
 *
 *   ╔═══════════════════════════════════════╗
 *   ║  HEAD-TO-HEAD  ·  compact summary pill ║
 *   ╚═══════════════════════════════════════╝
 *
 *   ┌───────────────────────────────────────┐
 *   │  BLUE CARD — PLAYER MATCHUP           │
 *   │  Player vs opponent primary stat,      │
 *   │  recent per-event splits.              │
 *   └───────────────────────────────────────┘
 *
 *   ┌───────────────────────────────────────┐
 *   │  AMBER CARD — GAME-DAY / TEAM H2H     │
 *   │  Team meetings, W-L record, avg total, │
 *   │  last N meeting scores.                │
 *   └───────────────────────────────────────┘
 *
 *   ┌───────────────────────────────────────┐
 *   │  GRAY CARD — SITUATIONAL (venue etc.) │
 *   └───────────────────────────────────────┘
 *
 * All three sub-cards use LIGHT surface colors + DARK ink per user spec
 * ("visible no dark colors"). Section headers use bold letter-spacing +
 * emoji + colored accent bar so it's obvious at a glance which type of
 * data you're reading.
 */
import React, { useEffect, useState } from "react";
import { View, Text, StyleSheet, ActivityIndicator } from "react-native";
import { api } from "@/src/lib/api";

// Team-total avg unit — MLB is runs, Soccer is goals, US-team sports
// are points. Prevents the "7.11 avg" label from reading as "avg hits"
// on a batter prop (user report — bug: '7.11 avg' looked wrong on a
// baseball hit pick because the number was actually avg runs).
function avgUnitLabel(sport?: string): string {
  switch (sport) {
    case "MLB":    return "RUNS";
    case "Soccer": return "GOALS";
    case "NBA":
    case "NFL":
    case "NHL":    return "PTS";
    default:       return "TOTAL";
  }
}

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
      <View style={styles.wrapper} testID="h2h-panel-loading">
        <View style={styles.header}>
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
    <View style={styles.wrapper} testID="h2h-panel">
      {/* Section header with the compact summary pill so the deep-dive
          mirrors the LockPickCard chip. */}
      <View style={styles.header}>
        <Text style={styles.headerIcon}>⚔️</Text>
        <Text style={styles.headerText}>HEAD-TO-HEAD</Text>
        {b?.summary ? (
          <Text style={styles.summaryPill} numberOfLines={1}>{b.summary}</Text>
        ) : null}
      </View>

      {/* ═══════ PLAYER MATCHUP — blue card ═══════ */}
      {hasPlayer && b?.player_h2h && (
        <View style={[styles.card, styles.cardPlayer]} testID="h2h-player">
          <View style={[styles.accentBar, styles.accentBarPlayer]} />
          <View style={styles.cardBody}>
            <View style={styles.cardHeaderRow}>
              <Text style={styles.cardHeaderIcon}>👤</Text>
              <Text style={[styles.cardHeaderText, styles.cardHeaderTextPlayer]}>
                PLAYER MATCHUP
              </Text>
              <View style={[styles.tag, styles.tagPlayer]}>
                <Text style={styles.tagText}>PLAYER DATA</Text>
              </View>
            </View>

            <Text style={[styles.headline, styles.headlinePlayer]}>
              {b.player_h2h.primary_value_display}
            </Text>
            <Text style={styles.subLine}>
              {b.player_h2h.player} vs {b.player_h2h.vs_opponent}
              {b.player_h2h.sample_size
                ? ` · ${b.player_h2h.sample_size} ${(b.player_h2h as any).sample_unit || "sample"}`
                : ""}
            </Text>
            {b.sport === "MLB" && b.player_h2h.season_avg_k != null && (
              <Text style={styles.subLine}>
                Season baseline: {b.player_h2h.season_avg_k?.toFixed(1)} K over
                {" "}{b.player_h2h.season_starts ?? 0} starts
              </Text>
            )}
            {Array.isArray(b.player_h2h.recent) && b.player_h2h.recent.length > 0 && (
              <View style={[styles.recentBox, styles.recentBoxPlayer]}>
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
        </View>
      )}

      {/* ═══════ GAME-DAY / TEAM MEETINGS — amber card ═══════ */}
      {hasTeam && b?.team_h2h && (
        <View style={[styles.card, styles.cardGame]} testID="h2h-team">
          <View style={[styles.accentBar, styles.accentBarGame]} />
          <View style={styles.cardBody}>
            <View style={styles.cardHeaderRow}>
              <Text style={styles.cardHeaderIcon}>🏟️</Text>
              <Text style={[styles.cardHeaderText, styles.cardHeaderTextGame]}>
                GAME-DAY H2H
              </Text>
              <View style={[styles.tag, styles.tagGame]}>
                <Text style={styles.tagText}>TEAM DATA</Text>
              </View>
            </View>

            <View style={styles.teamStatsRow}>
              <View style={styles.teamStatCell}>
                <Text style={styles.teamStatValue}>{b.team_h2h.record}</Text>
                <Text style={styles.teamStatLabel}>RECORD</Text>
              </View>
              <View style={styles.teamStatCell}>
                <Text style={styles.teamStatValue}>{b.team_h2h.meetings}</Text>
                <Text style={styles.teamStatLabel}>MEETINGS</Text>
              </View>
              {b.team_h2h.avg_total != null && !(b as any).is_player_prop && (
                <View style={styles.teamStatCell}>
                  <Text style={styles.teamStatValue}>{b.team_h2h.avg_total}</Text>
                  <Text style={styles.teamStatLabel}>
                    AVG {avgUnitLabel(b.sport)}
                  </Text>
                </View>
              )}
            </View>

            {Array.isArray(b.team_h2h.recent) && b.team_h2h.recent.length > 0 && (
              <View style={[styles.recentBox, styles.recentBoxGame]}>
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
        </View>
      )}

      {/* ═══════ SITUATIONAL — gray card ═══════ */}
      {hasSituational && b?.situational && (
        <View style={[styles.card, styles.cardSituational]} testID="h2h-situational">
          <View style={[styles.accentBar, styles.accentBarSituational]} />
          <View style={styles.cardBody}>
            <View style={styles.cardHeaderRow}>
              <Text style={styles.cardHeaderIcon}>📍</Text>
              <Text style={[styles.cardHeaderText, styles.cardHeaderTextSituational]}>
                SITUATIONAL
              </Text>
              <View style={[styles.tag, styles.tagSituational]}>
                <Text style={styles.tagText}>CONTEXT</Text>
              </View>
            </View>
            {!!b.situational.venue && <Text style={styles.subLine}>📍 {b.situational.venue}</Text>}
            {(b.situational.notes || []).map((n, i) => (
              <Text key={i} style={styles.subLine}>• {n}</Text>
            ))}
          </View>
        </View>
      )}

      {!anyData && (
        <View style={[styles.card, styles.cardEmpty]}>
          <View style={styles.cardBody}>
            <Text style={styles.emptyText}>
              No head-to-head sample yet for this matchup. As results settle over the coming weeks,
              H2H stats will populate automatically.
            </Text>
          </View>
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
  wrapper: {
    marginTop: 10,
    gap: 10,
  },
  // Top section header (outside the sub-cards).
  header: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    paddingHorizontal: 4,
    paddingVertical: 2,
  },
  headerIcon: { fontSize: 16 },
  headerText: {
    color: "#111827",             // near-black for max contrast on both light & dark app backgrounds
    fontSize: 13,
    fontWeight: "900",
    letterSpacing: 1.5,
  },
  summaryPill: {
    marginLeft: "auto",
    color: "#0F172A",
    fontSize: 11,
    fontWeight: "800",
    backgroundColor: "#FDE68A",   // amber-200
    paddingHorizontal: 9,
    paddingVertical: 3,
    borderRadius: 999,
    overflow: "hidden",
    maxWidth: 220,
  },
  // ── Sub-card scaffolding ────────────────────────────────────────
  card: {
    flexDirection: "row",
    borderRadius: 14,
    overflow: "hidden",
    borderWidth: 1,
  },
  cardBody: {
    flex: 1,
    padding: 12,
    gap: 6,
  },
  accentBar: {
    width: 6,
  },
  // Card headers — high-contrast, distinct emoji + tag per data type.
  cardHeaderRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    marginBottom: 4,
  },
  cardHeaderIcon: { fontSize: 15 },
  cardHeaderText: {
    fontSize: 12,
    fontWeight: "900",
    letterSpacing: 1.4,
  },
  cardHeaderTextPlayer:      { color: "#1E3A8A" }, // blue-900
  cardHeaderTextGame:        { color: "#78350F" }, // amber-900
  cardHeaderTextSituational: { color: "#374151" }, // gray-700
  tag: {
    marginLeft: "auto",
    paddingHorizontal: 8,
    paddingVertical: 3,
    borderRadius: 999,
    borderWidth: 1,
  },
  tagText: {
    fontSize: 9.5,
    fontWeight: "900",
    letterSpacing: 1.1,
    color: "#111827",
  },
  tagPlayer:      { backgroundColor: "#DBEAFE", borderColor: "#3B82F6" }, // blue-100 / blue-500
  tagGame:        { backgroundColor: "#FDE68A", borderColor: "#F59E0B" }, // amber-200 / amber-500
  tagSituational: { backgroundColor: "#E5E7EB", borderColor: "#9CA3AF" }, // gray-200 / gray-400

  // ── Card colour variants ────────────────────────────────────────
  // Player matchup — cool BLUE surface (distinct from team data).
  cardPlayer: {
    backgroundColor: "#EFF6FF",   // blue-50
    borderColor: "#3B82F6",       // blue-500
  },
  accentBarPlayer: { backgroundColor: "#3B82F6" },
  // Game-day team H2H — warm AMBER surface.
  cardGame: {
    backgroundColor: "#FEF3C7",   // amber-100
    borderColor: "#F59E0B",       // amber-500
  },
  accentBarGame: { backgroundColor: "#F59E0B" },
  // Situational — neutral GRAY surface.
  cardSituational: {
    backgroundColor: "#F3F4F6",   // gray-100
    borderColor: "#9CA3AF",       // gray-400
  },
  accentBarSituational: { backgroundColor: "#6B7280" },
  cardEmpty: {
    backgroundColor: "#F9FAFB",
    borderColor: "#D1D5DB",
    borderStyle: "dashed",
  },

  // ── Body typography ─────────────────────────────────────────────
  headline: {
    fontSize: 17,
    fontWeight: "900",
    letterSpacing: -0.2,
    marginTop: 2,
    color: "#0F172A",
  },
  headlinePlayer: { color: "#1E3A8A" }, // blue-900 for the primary player stat headline
  subLine: {
    color: "#374151",
    fontSize: 13,
    fontWeight: "600",
    marginTop: 3,
    lineHeight: 18,
  },

  // Team stats row (record / meetings / avg total).
  teamStatsRow: {
    flexDirection: "row",
    gap: 10,
    marginTop: 6,
    marginBottom: 4,
  },
  teamStatCell: {
    flex: 1,
    backgroundColor: "#FFFBEB",   // amber-50
    borderColor: "#FCD34D",       // amber-300
    borderWidth: 1,
    borderRadius: 10,
    paddingVertical: 10,
    paddingHorizontal: 6,
    alignItems: "center",
  },
  teamStatValue: {
    color: "#1F2937",
    fontSize: 20,
    fontWeight: "900",
    letterSpacing: -0.4,
  },
  teamStatLabel: {
    color: "#92400E",
    fontSize: 9,
    fontWeight: "800",
    letterSpacing: 1.2,
    marginTop: 2,
  },

  // Recent-events list — variant-tinted background.
  recentBox: {
    marginTop: 10,
    borderRadius: 10,
    borderWidth: 1,
    overflow: "hidden",
  },
  recentBoxPlayer: {
    borderColor: "#93C5FD",       // blue-300
    backgroundColor: "#F0F9FF",   // sky-50
  },
  recentBoxGame: {
    borderColor: "#FCD34D",       // amber-300
    backgroundColor: "#FFFBEB",   // amber-50
  },
  recentRow: {
    flexDirection: "row",
    alignItems: "center",
    paddingVertical: 6,
    paddingHorizontal: 10,
    gap: 8,
    borderBottomWidth: 1,
    borderBottomColor: "rgba(107, 114, 128, 0.15)",
  },
  recentDate: {
    color: "#374151",
    fontSize: 11,
    fontWeight: "800",
    width: 78,
  },
  recentBody: {
    flex: 1,
    color: "#4B5563",
    fontSize: 12,
    fontWeight: "600",
  },
  recentValue: {
    color: "#111827",
    fontSize: 12,
    fontWeight: "900",
    minWidth: 40,
    textAlign: "right",
  },
  emptyText: {
    color: "#4B5563",
    fontSize: 12,
    fontWeight: "600",
    lineHeight: 18,
    fontStyle: "italic",
  },
  sourceFootnote: {
    marginTop: 2,
    color: "#6B7280",
    fontSize: 10,
    fontWeight: "700",
    letterSpacing: 0.4,
    paddingHorizontal: 4,
  },
});
