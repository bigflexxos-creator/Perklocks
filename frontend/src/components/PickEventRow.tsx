/**
 * PickEventRow — renders the event line on a pick card with:
 *   • Tiny team crest badges (32px on card, 44px on detail)
 *   • Team-color accent bar on each crest
 *   • Compact injury chip when 1+ starters are Out/Doubtful/Questionable
 *
 * Data source: `pick.home_meta` / `pick.away_meta` / `pick.injury_chip`
 * populated by the backend `_decorate_with_espn_meta` decorator that
 * runs on `/api/picks/today` and detail endpoints.
 *
 * Works silently when meta absent (e.g. Tennis, Golf, obscure soccer
 * leagues not in ESPN's registry). The event text always renders.
 */
import React from "react";
import { View, Text, StyleSheet, Image, Platform } from "react-native";
import type { Pick } from "@/src/lib/api";
import { COLORS } from "@/src/theme";

type Props = {
  pick: Pick;
  size?: "card" | "detail";   // card=32px logos, detail=44px
  showInjuryChip?: boolean;
};

export function PickEventRow({ pick, size = "card", showInjuryChip = true }: Props) {
  const logoSize = size === "detail" ? 44 : 26;
  const away = pick.away_meta;
  const home = pick.home_meta;
  const hasLogos = !!(away?.logo || home?.logo);

  // Parse "Away @ Home" so we can render the two sides even if metadata
  // is only partial on one side.
  let awayName = "";
  let homeName = "";
  const event = pick.event || "";
  if (event.includes(" @ ")) {
    const [a, h] = event.split(" @ ");
    awayName = a.trim();
    homeName = h.trim();
  } else if (event.includes(" vs ")) {
    const [a, h] = event.split(" vs ");
    awayName = a.trim();
    homeName = h.trim();
  }

  const injury = pick.injury_chip;
  const chipCount = (side: "home" | "away") => {
    if (!injury) return 0;
    const b = injury[side];
    return (b?.out || 0) + (b?.doubtful || 0) + (b?.questionable || 0);
  };
  const homeInjuryCount = chipCount("home");
  const awayInjuryCount = chipCount("away");
  const anyInjury = showInjuryChip && (homeInjuryCount + awayInjuryCount) > 0;

  // Fallback path — no logos at all → render legacy single-line event
  if (!hasLogos) {
    return (
      <View style={styles.wrap}>
        <Text style={styles.eventFallback} numberOfLines={1}>
          {pick.event}
        </Text>
        {anyInjury && (
          <InjuryPill
            homeCount={homeInjuryCount}
            awayCount={awayInjuryCount}
            worstSide={injury?.worst_side || null}
          />
        )}
      </View>
    );
  }

  return (
    <View style={styles.wrap}>
      <View style={styles.row}>
        <TeamCell
          logo={away?.logo}
          color={away?.color}
          name={away?.abbrev || awayName}
          fullName={awayName}
          size={logoSize}
          side="away"
          injuryBadge={awayInjuryCount > 0 && showInjuryChip ? awayInjuryCount : 0}
        />
        <Text style={[styles.atSign, size === "detail" && { fontSize: 16 }]}>@</Text>
        <TeamCell
          logo={home?.logo}
          color={home?.color}
          name={home?.abbrev || homeName}
          fullName={homeName}
          size={logoSize}
          side="home"
          injuryBadge={homeInjuryCount > 0 && showInjuryChip ? homeInjuryCount : 0}
        />
      </View>
      {anyInjury && (
        <InjuryPill
          homeCount={homeInjuryCount}
          awayCount={awayInjuryCount}
          worstSide={injury?.worst_side || null}
        />
      )}
    </View>
  );
}

function TeamCell({
  logo,
  color,
  name,
  fullName,
  size,
  side,
  injuryBadge,
}: {
  logo?: string;
  color?: string;
  name: string;
  fullName: string;
  size: number;
  side: "home" | "away";
  injuryBadge: number;
}) {
  const accent = color ? `#${color}` : COLORS.textMuted;
  return (
    <View style={styles.teamCell}>
      <View style={{ width: size, height: size }}>
        {logo ? (
          <Image
            source={{ uri: logo }}
            style={[
              styles.logo,
              {
                width: size,
                height: size,
                borderRadius: size / 2,
                borderColor: accent,
                borderWidth: 1.5,
                backgroundColor: "#FFFFFF",
              },
            ]}
            resizeMode="contain"
          />
        ) : (
          <View
            style={[
              styles.logoFallback,
              {
                width: size,
                height: size,
                borderRadius: size / 2,
                backgroundColor: accent + "22",
                borderColor: accent,
              },
            ]}
          >
            <Text style={[styles.logoFallbackText, { color: accent }]}>
              {(name || "?").slice(0, 2).toUpperCase()}
            </Text>
          </View>
        )}
        {injuryBadge > 0 && (
          <View style={styles.injuryDot}>
            <Text style={styles.injuryDotText}>{injuryBadge}</Text>
          </View>
        )}
      </View>
      <Text
        style={[styles.teamName, size >= 40 && { fontSize: 14 }]}
        numberOfLines={1}
      >
        {name || fullName}
      </Text>
    </View>
  );
}

function InjuryPill({
  homeCount,
  awayCount,
  worstSide,
}: {
  homeCount: number;
  awayCount: number;
  worstSide: "home" | "away" | null;
}) {
  const total = homeCount + awayCount;
  const label =
    worstSide === "home"
      ? `🚑 Home ${homeCount}`
      : worstSide === "away"
        ? `🚑 Away ${awayCount}`
        : `🚑 ${total}`;
  return (
    <View style={styles.injuryPill}>
      <Text style={styles.injuryPillText}>{label} inj</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    marginTop: 2,
    marginBottom: 2,
    flexWrap: "wrap",
  },
  row: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    flex: 1,
  },
  teamCell: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    flex: 1,
    minWidth: 0,
  },
  logo: {
    ...Platform.select({
      ios: { shadowColor: "#000", shadowOffset: { width: 0, height: 1 }, shadowOpacity: 0.15, shadowRadius: 2 },
      android: { elevation: 1 },
      default: {},
    }),
  },
  logoFallback: {
    borderWidth: 1.5,
    alignItems: "center",
    justifyContent: "center",
  },
  logoFallbackText: {
    fontSize: 10,
    fontWeight: "700",
  },
  teamName: {
    color: COLORS.text,
    fontSize: 13,
    fontWeight: "600",
    flexShrink: 1,
  },
  atSign: {
    color: COLORS.textMuted,
    fontSize: 12,
    fontWeight: "600",
    paddingHorizontal: 2,
  },
  eventFallback: {
    color: COLORS.text,
    fontSize: 14,
    fontWeight: "600",
    flex: 1,
  },
  injuryPill: {
    paddingHorizontal: 8,
    paddingVertical: 3,
    borderRadius: 10,
    backgroundColor: "#7F1D1D22",
    borderWidth: 1,
    borderColor: "#EF444488",
  },
  injuryPillText: {
    color: "#FCA5A5",
    fontSize: 11,
    fontWeight: "700",
  },
  injuryDot: {
    position: "absolute",
    right: -4,
    top: -4,
    minWidth: 14,
    height: 14,
    borderRadius: 7,
    backgroundColor: "#DC2626",
    paddingHorizontal: 3,
    alignItems: "center",
    justifyContent: "center",
    borderWidth: 1,
    borderColor: "#FFFFFF",
  },
  injuryDotText: {
    color: "#FFFFFF",
    fontSize: 8,
    fontWeight: "800",
    lineHeight: 12,
  },
});
