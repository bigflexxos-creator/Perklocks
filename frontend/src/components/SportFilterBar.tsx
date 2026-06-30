/**
 * Sport-specific filter bar.
 *
 * Renders BELOW the sport pills on screens like Locks/Rollover/Under-Lock.
 * Dynamically loads `/api/picks/markets/{sport}` whenever the user picks a
 * sport that has a known market list (Soccer / NBA / NFL / MLB / Tennis).
 *
 * Filters are pushed back via the `onChange` callback as a partial PickFilters
 * patch so the parent screen can merge it into the GET /picks/today query.
 */
import React, { useEffect, useState } from "react";
import { View, Text, StyleSheet, ScrollView, Pressable } from "react-native";
import { Ionicons } from "@expo/vector-icons";
import { useRouter } from "expo-router";

import { api, PickFilters, SportLeague, SportMarket } from "@/src/lib/api";
import { useFilters } from "@/src/stores/useFilters";
import { COLORS } from "@/src/theme";

const SUPPORTED = new Set(["Soccer", "NBA", "NFL", "MLB", "Tennis"]);

type Props = {
  sport: string;
  filters: PickFilters;
  onChange: (next: PickFilters) => void;
};

export function SportFilterBar({ sport, filters, onChange }: Props) {
  const router = useRouter();
  // ── Multi-select via the global filter store ──
  // MUST be called at the top — hooks can't be conditional. Aliased
  // setters to avoid colliding with the local useState below.
  const {
    state: storeState,
    toggleMarket, toggleLeague,
    setMarkets: setStoreMarkets,
    setLeagues: setStoreLeagues,
  } = useFilters();
  const selectedMarkets = storeState.markets;
  const selectedLeagues = storeState.leagues;

  const [markets, setMarkets] = useState<SportMarket[]>([]);
  const [leagues, setLeagues] = useState<SportLeague[]>([]);

  useEffect(() => {
    if (!SUPPORTED.has(sport)) {
      setMarkets([]); setLeagues([]);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const res = await api.sportMarkets(sport);
        if (cancelled) return;
        setMarkets(res.markets ?? []);
        setLeagues(res.leagues ?? []);
      } catch {
        if (!cancelled) { setMarkets([]); setLeagues([]); }
      }
    })();
    return () => { cancelled = true; };
  }, [sport]);

  if (!SUPPORTED.has(sport)) return null;

  // Tapping a chip ADDS it to the array; tapping again REMOVES it.
  // Tapping "All" clears the array. This replaces the old exclusive
  // single-select. Legacy callbacks still fire for `filters.market` /
  // `filters.league` so older consumers (parlay etc.) keep working
  // until they're migrated to the store.

  const onTapMarket = (token: string) => {
    toggleMarket(token);
    // Keep legacy single-field `filters.market` in sync with the FIRST
    // selected market so any consumer that still reads it sees something
    // sensible. When the toggle clears everything, fall back to undefined.
    const after = selectedMarkets.includes(token)
      ? selectedMarkets.filter((m) => m !== token)
      : [...selectedMarkets, token];
    onChange({ ...filters, market: after[0] });
  };
  const clearMarkets = () => {
    setStoreMarkets([]);
    if (filters.simEdgeOnly) toggleSimEdge();
    onChange({ ...filters, market: undefined });
  };
  const onTapLeague = (name: string) => {
    toggleLeague(name);
    const after = selectedLeagues.includes(name)
      ? selectedLeagues.filter((m) => m !== name)
      : [...selectedLeagues, name];
    onChange({ ...filters, league: after[0] });
  };
  const clearLeagues = () => {
    setStoreLeagues([]);
    onChange({ ...filters, league: undefined });
  };
  const toggleSimEdge = () => {
    onChange({ ...filters, simEdgeOnly: !filters.simEdgeOnly });
  };

  return (
    <View style={styles.wrap}>
      {(markets.length > 0 || true) && (
        <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.row}>
          <Pill
            label="All"
            active={selectedMarkets.length === 0 && !filters.simEdgeOnly}
            onPress={() => { clearMarkets(); }}
            testID="market-pill-all"
          />
          {/* SIM EDGE filter chip — surfaces only picks where Monte Carlo
              sim_win_probability ≥ 75%. Per the 30-day backtest, sim ≥75%
              is +6.1% ROI vs -2.7% blind betting, and sim ≥85% co-signs
              are +14.8% ROI. Keeping this as a sibling of the market
              pills (not buried in FilterSheet) so it's one tap away. */}
          <Pill
            label="🎲 SIM EDGE"
            active={!!filters.simEdgeOnly}
            onPress={toggleSimEdge}
            testID="market-pill-sim-edge"
            accent
          />
          {markets.map((m) => (
            <Pill
              key={m.token}
              label={m.label}
              active={selectedMarkets.includes(m.token)}
              onPress={() => onTapMarket(m.token)}
              testID={`market-pill-${m.token}`}
            />
          ))}
          {/* MLB Home-Run chip — special-case navigation (NOT a filter).
              Sits alongside Hits / H+R+RBI / Strikeouts / Outs Recorded
              so the user sees it in the same chip row, but tapping it
              routes to the dedicated /hr slate screen instead of
              filtering picks. Backed by /api/mlb/hr-slate. */}
          {sport === "MLB" && (
            <Pill
              label="🚀 HR"
              active={false}
              onPress={() => { try { router.push("/hr" as any); } catch {} }}
              testID="market-pill-hr"
              accent
            />
          )}
          {/* NFL Anytime-Touchdown chip — same pattern as MLB HR. Lives
              in the market row next to Receiving / Rushing / Receptions /
              Passing pills, but taps route to /atd (top-5 slate view)
              instead of filtering. Backed by /api/nfl/atd/leaderboard. */}
          {sport === "NFL" && (
            <Pill
              label="🏈 ATD"
              active={false}
              onPress={() => { try { router.push("/atd" as any); } catch {} }}
              testID="market-pill-atd"
              accent
            />
          )}
        </ScrollView>
      )}

      {leagues.length > 0 && (sport === "Soccer" || sport === "MLB" || sport === "NBA") && (
        <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.row}>
          <LeaguePill
            label="All leagues"
            active={selectedLeagues.length === 0}
            onPress={() => clearLeagues()}
          />
          {leagues.slice(0, 12).map((l) => (
            <LeaguePill
              key={l.name}
              label={l.name}
              count={l.count}
              active={selectedLeagues.includes(l.name)}
              onPress={() => onTapLeague(l.name)}
            />
          ))}
        </ScrollView>
      )}
    </View>
  );
}

function Pill({ label, active, onPress, testID, accent }: {
  label: string; active: boolean; onPress: () => void; testID?: string; accent?: boolean;
}) {
  return (
    <Pressable
      onPress={onPress}
      style={[
        styles.pill,
        active && (accent ? styles.pillAccentActive : styles.pillActive),
        accent && !active && styles.pillAccentIdle,
      ]}
      testID={testID}
      hitSlop={6}
    >
      <Text
        style={[
          styles.pillTxt,
          active && (accent ? styles.pillTxtAccentActive : styles.pillTxtActive),
          accent && !active && styles.pillTxtAccentIdle,
        ]}
      >
        {label}
      </Text>
    </Pressable>
  );
}

function LeaguePill({ label, count, active, onPress }: {
  label: string; count?: number; active: boolean; onPress: () => void;
}) {
  return (
    <Pressable
      onPress={onPress}
      style={[styles.leaguePill, active && styles.leaguePillActive]}
      hitSlop={6}
    >
      <Ionicons name="trophy-outline" size={11} color={active ? COLORS.bg : COLORS.textMuted} />
      <Text style={[styles.leaguePillTxt, active && styles.leaguePillTxtActive]} numberOfLines={1}>
        {label}{count != null ? ` · ${count}` : ""}
      </Text>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  wrap: { paddingBottom: 4 },
  row: {
    paddingHorizontal: 20,
    paddingVertical: 6,
    gap: 8,
    flexDirection: "row",
  },
  pill: {
    paddingHorizontal: 12, paddingVertical: 6,
    borderRadius: 16,
    borderWidth: 1, borderColor: COLORS.borderDefault,
    backgroundColor: COLORS.surface,
  },
  pillActive: {
    backgroundColor: COLORS.voltBlue,
    borderColor: COLORS.voltBlue,
  },
  pillTxt: { color: COLORS.textSecondary, fontSize: 11, fontWeight: "700", letterSpacing: 0.6 },
  pillTxtActive: { color: COLORS.bg },
  // SIM EDGE chip — idle uses neonGreen border to differentiate from
  // generic market pills, active fills with neonGreen for unambiguous
  // on-state. Per the iter35 backtest, sim≥75 is +6% ROI, so we want
  // this chip to feel special, not just "another market".
  pillAccentIdle: {
    borderColor: COLORS.neonGreen,
    backgroundColor: "transparent",
  },
  pillAccentActive: {
    backgroundColor: COLORS.neonGreen,
    borderColor: COLORS.neonGreen,
  },
  pillTxtAccentIdle: { color: COLORS.neonGreen },
  pillTxtAccentActive: { color: COLORS.bg, fontWeight: "900" },

  leaguePill: {
    flexDirection: "row", alignItems: "center", gap: 5,
    paddingHorizontal: 10, paddingVertical: 5,
    borderRadius: 14,
    borderWidth: StyleSheet.hairlineWidth, borderColor: COLORS.borderDefault,
    maxWidth: 180,
  },
  leaguePillActive: {
    backgroundColor: COLORS.goldElite,
    borderColor: COLORS.goldElite,
  },
  leaguePillTxt: { color: COLORS.textMuted, fontSize: 10, fontWeight: "700", letterSpacing: 0.3 },
  leaguePillTxtActive: { color: COLORS.bg },
});
