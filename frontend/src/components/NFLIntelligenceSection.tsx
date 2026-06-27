/**
 * NFLIntelligenceSection — surfaces the three NFL engines as compact,
 * horizontally-scrollable cards. Shown on the home tab whenever the
 * user's sport tab is "NFL" (or "All" with NFL active in the multi-select).
 *
 * The three feeds:
 *   1. SAFE LOCKS         — top player-prop hit-rate locks
 *      Source: GET /api/nfl/safe-bets
 *   2. ATD LEADERBOARD    — ranked P(TD ≥ 1)
 *      Source: GET /api/nfl/atd/leaderboard
 *   3. GAME BETS          — best ML / Spread / Total across the slate
 *      Source: GET /api/nfl/games/safe-bets  (off-season may return [])
 *
 * UX:
 *   • Each row scrolls horizontally so we don't blow up the home tab.
 *   • Cards use the same colour language as LockPickCard (gold / volt).
 *   • A "VIEW ALL" pill on each header pushes to a sport-specific full
 *     screen later (out of scope for this drop — the row is the MVP).
 *   • Tapping a card → no-op for now (the back-end picks aren't yet in
 *     the picks collection so detail screens don't exist). We just
 *     copy the matchup label to clipboard as a stub.
 *
 * Performance: each row fetches lazily on mount and caches in-memory.
 * Pull-to-refresh is delegated to the parent ScrollView (no internal
 * RefreshControl — we re-fetch when the user toggles sports off/on).
 */
import React, { useEffect, useState } from "react";
import {
  View, Text, StyleSheet, ScrollView, ActivityIndicator,
} from "react-native";
import { Ionicons } from "@expo/vector-icons";
import { COLORS } from "@/src/theme";
import {
  api, NFLSafePick, NFLAtdPick, NFLGamePick,
} from "@/src/lib/api";

type Props = {
  /** Refresh when this number ticks. Parent passes the same counter
   *  that drives picks-feed refresh so user-triggered refresh hits
   *  all three NFL feeds too. */
  refreshTick?: number;
};

export function NFLIntelligenceSection({ refreshTick = 0 }: Props) {
  return (
    <View style={styles.wrap} testID="nfl-intel-section">
      <SafeLocksRow refreshTick={refreshTick} />
      <AtdLeaderboardRow refreshTick={refreshTick} />
      <GameBetsRow refreshTick={refreshTick} />
    </View>
  );
}

// ─────────────────────────── Safe Locks row ───────────────────────────

function SafeLocksRow({ refreshTick }: { refreshTick: number }) {
  const [data, setData] = useState<NFLSafePick[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancel = false;
    (async () => {
      setLoading(true);
      setError(null);
      try {
        const res = await api.nflSafeBets(8, 0.78);
        if (!cancel) setData(res.picks || []);
      } catch (e: any) {
        if (!cancel) setError(e?.message || "Failed to load");
      } finally {
        if (!cancel) setLoading(false);
      }
    })();
    return () => { cancel = true; };
  }, [refreshTick]);

  return (
    <SectionShell
      title="NFL SAFE LOCKS"
      subtitle="High-hit-rate player props"
      accent={COLORS.goldElite}
      icon="lock-closed"
      testID="nfl-safe-locks"
    >
      {loading ? (
        <RowSpinner />
      ) : error ? (
        <RowError msg={error} />
      ) : !data || data.length === 0 ? (
        <RowEmpty msg="No qualifying NFL props right now" />
      ) : (
        <ScrollView
          horizontal
          showsHorizontalScrollIndicator={false}
          contentContainerStyle={styles.hScroll}
        >
          {data.map((p, i) => (
            <SafeLockCard key={`${p.player_id}-${p.prop}-${i}`} pick={p} />
          ))}
        </ScrollView>
      )}
    </SectionShell>
  );
}

function SafeLockCard({ pick }: { pick: NFLSafePick }) {
  const pct = Math.round(pick.probability * 100);
  return (
    <View style={[styles.card, { borderColor: COLORS.goldElite }]} testID={`nfl-safe-lock-${pick.player_id}`}>
      <View style={styles.cardHeader}>
        <Text style={styles.cardProb}>{pct}%</Text>
        <Text style={styles.cardOdds}>
          {pick.implied_american_odds != null
            ? (pick.implied_american_odds > 0 ? `+${pick.implied_american_odds}` : pick.implied_american_odds)
            : "—"}
        </Text>
      </View>
      <Text style={styles.cardPlayer} numberOfLines={1}>{pick.player_name}</Text>
      <Text style={styles.cardTeam} numberOfLines={1}>{pick.team}</Text>
      <View style={styles.divider} />
      <Text style={styles.cardMarket} numberOfLines={2}>{pick.market}</Text>
      <Text style={styles.cardStat} numberOfLines={1}>
        L{pick.sample_size} · {pick.hits}/{pick.sample_size} hit
      </Text>
      <Text style={styles.cardStat} numberOfLines={1}>
        med {pick.median} · floor {pick.floor_p10}
      </Text>
    </View>
  );
}

// ─────────────────────────── ATD Leaderboard row ───────────────────────

function AtdLeaderboardRow({ refreshTick }: { refreshTick: number }) {
  const [data, setData] = useState<NFLAtdPick[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancel = false;
    (async () => {
      setLoading(true);
      setError(null);
      try {
        const res = await api.nflAtdLeaderboard(12, 0.30, "med");
        if (!cancel) setData(res.picks || []);
      } catch (e: any) {
        if (!cancel) setError(e?.message || "Failed to load");
      } finally {
        if (!cancel) setLoading(false);
      }
    })();
    return () => { cancel = true; };
  }, [refreshTick]);

  return (
    <SectionShell
      title="ATD LEADERBOARD"
      subtitle="Anytime touchdown probability"
      accent={COLORS.electricBlaze}
      icon="trophy"
      testID="nfl-atd-leaderboard"
    >
      {loading ? (
        <RowSpinner />
      ) : error ? (
        <RowError msg={error} />
      ) : !data || data.length === 0 ? (
        <RowEmpty msg="No eligible NFL TD scorers right now" />
      ) : (
        <ScrollView
          horizontal
          showsHorizontalScrollIndicator={false}
          contentContainerStyle={styles.hScroll}
        >
          {data.map((p, i) => (
            <AtdCard key={`${p.player_id}-${i}`} rank={i + 1} pick={p} />
          ))}
        </ScrollView>
      )}
    </SectionShell>
  );
}

function AtdCard({ pick, rank }: { pick: NFLAtdPick; rank: number }) {
  const pct = Math.round(pick.td_probability * 100);
  const oppColor =
    pick.opportunity_rating === "high"
      ? COLORS.neonGreen
      : pick.opportunity_rating === "med"
        ? COLORS.electricBlaze
        : COLORS.textMuted;
  return (
    <View style={[styles.card, { borderColor: COLORS.electricBlaze }]} testID={`nfl-atd-${pick.player_id}`}>
      <View style={styles.cardHeader}>
        <Text style={styles.cardProb}>{pct}%</Text>
        <Text style={styles.rankBadge}>#{rank}</Text>
      </View>
      <Text style={styles.cardPlayer} numberOfLines={1}>{pick.player_name}</Text>
      <Text style={styles.cardTeam} numberOfLines={1}>{pick.team}</Text>
      <View style={styles.divider} />
      <Text style={styles.cardMarket}>Anytime TD</Text>
      <View style={styles.tagRow}>
        <Text style={[styles.tag, { color: oppColor, borderColor: oppColor }]}>
          {(pick.opportunity_rating || "").toUpperCase()}
        </Text>
        {pick.is_rb_archetype && (
          <Text style={[styles.tag, { color: COLORS.textSecondary, borderColor: COLORS.borderDefault }]}>
            RB
          </Text>
        )}
      </View>
      <Text style={styles.cardStat} numberOfLines={1}>
        {pick.weighted_touches_recent.toFixed(1)} tch/g · {pick.weighted_tds_recent.toFixed(2)} TD/g
      </Text>
    </View>
  );
}

// ─────────────────────────── Game Bets row ───────────────────────────

function GameBetsRow({ refreshTick }: { refreshTick: number }) {
  const [data, setData] = useState<NFLGamePick[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [matchupsEvaluated, setMatchupsEvaluated] = useState(0);

  useEffect(() => {
    let cancel = false;
    (async () => {
      setLoading(true);
      setError(null);
      try {
        const res = await api.nflGameSafeBets(10, 0.78);
        if (!cancel) {
          setData(res.bets || []);
          setMatchupsEvaluated(res.matchups_evaluated || 0);
        }
      } catch (e: any) {
        if (!cancel) setError(e?.message || "Failed to load");
      } finally {
        if (!cancel) setLoading(false);
      }
    })();
    return () => { cancel = true; };
  }, [refreshTick]);

  return (
    <SectionShell
      title="GAME BETS"
      subtitle={`ML · Spread · Total${matchupsEvaluated ? ` · ${matchupsEvaluated} matchups` : ""}`}
      accent={COLORS.voltBlue}
      icon="american-football"
      testID="nfl-game-bets"
    >
      {loading ? (
        <RowSpinner />
      ) : error ? (
        <RowError msg={error} />
      ) : !data || data.length === 0 ? (
        <RowEmpty msg="No upcoming NFL matchups on the slate" />
      ) : (
        <ScrollView
          horizontal
          showsHorizontalScrollIndicator={false}
          contentContainerStyle={styles.hScroll}
        >
          {data.map((b, i) => (
            <GameBetCard key={`${b.matchup}-${b.market}-${i}`} bet={b} />
          ))}
        </ScrollView>
      )}
    </SectionShell>
  );
}

function GameBetCard({ bet }: { bet: NFLGamePick }) {
  const pct = Math.round(bet.true_probability * 100);
  let selectionLabel = "";
  if (bet.market === "moneyline") {
    selectionLabel = bet.pick.team ? `${bet.pick.team} ML` : "Moneyline";
  } else if (bet.market === "spread") {
    const s = bet.pick.spread;
    selectionLabel = `${bet.pick.team || "—"} ${s != null ? (s > 0 ? `+${s}` : s) : ""}`;
  } else if (bet.market === "total") {
    selectionLabel = `${bet.pick.side || ""} ${bet.pick.total ?? ""}`;
  }
  return (
    <View style={[styles.card, { borderColor: COLORS.voltBlue }]}>
      <View style={styles.cardHeader}>
        <Text style={styles.cardProb}>{pct}%</Text>
        <Text style={[styles.marketTag, { color: COLORS.voltBlue, borderColor: COLORS.voltBlue }]}>
          {bet.market.toUpperCase()}
        </Text>
      </View>
      <Text style={styles.cardPlayer} numberOfLines={1}>{bet.matchup}</Text>
      <View style={styles.divider} />
      <Text style={styles.cardMarket} numberOfLines={2}>{selectionLabel}</Text>
      {bet.expected_margin != null && (
        <Text style={styles.cardStat}>
          margin {bet.expected_margin > 0 ? "+" : ""}{bet.expected_margin?.toFixed(1)}
        </Text>
      )}
      {bet.expected_total != null && (
        <Text style={styles.cardStat}>exp total {bet.expected_total?.toFixed(1)}</Text>
      )}
    </View>
  );
}

// ─────────────────────────── Shared shell ───────────────────────────

function SectionShell({
  title, subtitle, accent, icon, testID, children,
}: {
  title: string;
  subtitle?: string;
  accent: string;
  icon: keyof typeof Ionicons.glyphMap;
  testID?: string;
  children: React.ReactNode;
}) {
  return (
    <View style={styles.section} testID={testID}>
      <View style={styles.sectionHeader}>
        <Ionicons name={icon} size={14} color={accent} />
        <Text style={[styles.sectionTitle, { color: accent }]}>{title}</Text>
        {subtitle && <Text style={styles.sectionSub}>· {subtitle}</Text>}
      </View>
      {children}
    </View>
  );
}

function RowSpinner() {
  return (
    <View style={styles.placeholder}>
      <ActivityIndicator color={COLORS.voltBlue} />
    </View>
  );
}

function RowError({ msg }: { msg: string }) {
  return (
    <View style={styles.placeholder}>
      <Text style={styles.placeholderText} numberOfLines={2}>{msg}</Text>
    </View>
  );
}

function RowEmpty({ msg }: { msg: string }) {
  return (
    <View style={styles.placeholder}>
      <Text style={styles.placeholderText}>{msg}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: { marginTop: 8 },
  section: {
    paddingHorizontal: 0,
    marginBottom: 12,
  },
  sectionHeader: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    paddingHorizontal: 16,
    marginBottom: 8,
  },
  sectionTitle: {
    fontSize: 12,
    fontWeight: "900",
    letterSpacing: 1.4,
  },
  sectionSub: {
    fontSize: 10.5,
    color: COLORS.textMuted,
    fontWeight: "700",
    letterSpacing: 0.5,
    flexShrink: 1,
  },
  hScroll: {
    paddingHorizontal: 12,
    gap: 10,
    paddingBottom: 4,
  },
  card: {
    width: 180,
    minHeight: 168,
    backgroundColor: COLORS.surfaceElevated || "rgba(255,255,255,0.04)",
    borderWidth: 1,
    borderRadius: 12,
    padding: 12,
    marginRight: 10,
  },
  cardHeader: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    marginBottom: 6,
  },
  cardProb: {
    color: COLORS.textPrimary,
    fontSize: 22,
    fontWeight: "900",
    letterSpacing: -0.5,
  },
  cardOdds: {
    color: COLORS.textSecondary,
    fontSize: 12,
    fontWeight: "800",
  },
  rankBadge: {
    color: COLORS.textMuted,
    fontSize: 11,
    fontWeight: "900",
    letterSpacing: 0.6,
  },
  cardPlayer: {
    color: COLORS.textPrimary,
    fontSize: 13,
    fontWeight: "800",
    letterSpacing: -0.2,
  },
  cardTeam: {
    color: COLORS.textMuted,
    fontSize: 10.5,
    fontWeight: "700",
    letterSpacing: 0.3,
    marginTop: 1,
  },
  divider: {
    height: StyleSheet.hairlineWidth,
    backgroundColor: COLORS.borderDefault,
    marginVertical: 8,
  },
  cardMarket: {
    color: COLORS.textPrimary,
    fontSize: 12,
    fontWeight: "700",
    marginBottom: 4,
  },
  cardStat: {
    color: COLORS.textMuted,
    fontSize: 10.5,
    fontWeight: "700",
    letterSpacing: 0.3,
    marginTop: 1,
  },
  tagRow: {
    flexDirection: "row",
    gap: 6,
    marginVertical: 4,
  },
  tag: {
    fontSize: 9.5,
    fontWeight: "900",
    letterSpacing: 0.6,
    paddingHorizontal: 6,
    paddingVertical: 2,
    borderRadius: 4,
    borderWidth: 1,
  },
  marketTag: {
    fontSize: 9.5,
    fontWeight: "900",
    letterSpacing: 0.7,
    paddingHorizontal: 6,
    paddingVertical: 2,
    borderRadius: 4,
    borderWidth: 1,
  },
  placeholder: {
    paddingVertical: 18,
    paddingHorizontal: 18,
    alignItems: "center",
    justifyContent: "center",
    minHeight: 72,
  },
  placeholderText: {
    color: COLORS.textMuted,
    fontSize: 12,
    fontWeight: "700",
    letterSpacing: 0.3,
    textAlign: "center",
  },
});
