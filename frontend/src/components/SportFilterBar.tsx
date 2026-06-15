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

import { api, PickFilters, SportLeague, SportMarket } from "@/src/lib/api";
import { COLORS } from "@/src/theme";

const SUPPORTED = new Set(["Soccer", "NBA", "NFL", "MLB", "Tennis"]);

type Props = {
  sport: string;
  filters: PickFilters;
  onChange: (next: PickFilters) => void;
};

export function SportFilterBar({ sport, filters, onChange }: Props) {
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

  const setMarket = (token: string | undefined) => {
    onChange({ ...filters, market: token });
  };
  const setLeague = (name: string | undefined) => {
    onChange({ ...filters, league: name });
  };

  return (
    <View style={styles.wrap}>
      {markets.length > 0 && (
        <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.row}>
          <Pill
            label="All"
            active={!filters.market}
            onPress={() => setMarket(undefined)}
            testID="market-pill-all"
          />
          {markets.map((m) => (
            <Pill
              key={m.token}
              label={m.label}
              active={filters.market === m.token}
              onPress={() => setMarket(m.token)}
              testID={`market-pill-${m.token}`}
            />
          ))}
        </ScrollView>
      )}

      {leagues.length > 0 && (sport === "Soccer" || sport === "MLB" || sport === "NBA") && (
        <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.row}>
          <LeaguePill
            label="All leagues"
            active={!filters.league}
            onPress={() => setLeague(undefined)}
          />
          {leagues.slice(0, 12).map((l) => (
            <LeaguePill
              key={l.name}
              label={l.name}
              count={l.count}
              active={filters.league === l.name}
              onPress={() => setLeague(l.name)}
            />
          ))}
        </ScrollView>
      )}
    </View>
  );
}

function Pill({ label, active, onPress, testID }: {
  label: string; active: boolean; onPress: () => void; testID?: string;
}) {
  return (
    <Pressable
      onPress={onPress}
      style={[styles.pill, active && styles.pillActive]}
      testID={testID}
      hitSlop={6}
    >
      <Text style={[styles.pillTxt, active && styles.pillTxtActive]}>{label}</Text>
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
