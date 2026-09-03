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
import { View, Text, StyleSheet, Platform } from "react-native";
// SLICE 3 (2026-09-02) — expo-image gives us cross-mount decoded image
// cache + native placeholder + smoother cross-fade for team logos.
// Every board row mounts one per team; the memory cache means the same
// crest painted 40× (list virtualization) decodes exactly once.
import { Image } from "expo-image";
import type { Pick } from "@/src/lib/api";
import { COLORS } from "@/src/theme";

type Props = {
  pick: Pick;
  size?: "card" | "detail";   // card=32px logos, detail=44px
  showInjuryChip?: boolean;
};

export function PickEventRow({ pick, size = "card", showInjuryChip = false }: Props) {
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

  // 2026-07-12 — team-level injury chip removed from the card per user
  // feedback. Chips are noisy when 3rd-string players are on the IR
  // but the starters are healthy. Only the *player-specific* chip
  // (i.e. subject player of a prop bet is on the injury report)
  // renders below via `pick.subject_player_hurt`.
  const subjectHurt = (pick as any).subject_player_hurt;

  // Fallback path — no logos at all → render legacy single-line event
  if (!hasLogos) {
    return (
      <View style={styles.wrap}>
        <Text style={styles.eventFallback} numberOfLines={1}>
          {pick.event}
        </Text>
        {subjectHurt && (
          <View style={styles.injuryPill}>
            <Text style={styles.injuryPillText}>
              🚑 {subjectHurt.athlete} {subjectHurt.status}
            </Text>
          </View>
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
          injuryBadge={0}
        />
        <Text style={[styles.atSign, size === "detail" && { fontSize: 16 }]}>@</Text>
        <TeamCell
          logo={home?.logo}
          color={home?.color}
          name={home?.abbrev || homeName}
          fullName={homeName}
          size={logoSize}
          side="home"
          injuryBadge={0}
        />
      </View>
      {subjectHurt && (
        <View style={styles.injuryPill}>
          <Text style={styles.injuryPillText}>
            🚑 {subjectHurt.athlete} {subjectHurt.status}
          </Text>
        </View>
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
            contentFit="contain"
            cachePolicy="memory-disk"
            transition={120}
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

// InjuryPill removed 2026-07-12 — team-level chips are noisy; only
// subject-player-hurt chip renders now (inline above).

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
    color: COLORS.textPrimary,
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
    color: COLORS.textPrimary,
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
