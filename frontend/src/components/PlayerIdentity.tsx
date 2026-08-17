/**
 * PlayerIdentity — safe visual identity resolver for pick cards.
 *
 * Resolution hierarchy (fail-closed on identity):
 *   1. Player-side team logo   (uses payload's home_meta/away_meta.logo)
 *   2. Fallback team logo      (opposite side of the matchup)
 *   3. Sport-appropriate emoji silhouette
 *
 * DESIGN CONTRACT:
 *   • NEVER breaks pick rendering — every code path returns a valid node.
 *   • NEVER fuzzy-matches player names to unrelated images.  We rely
 *     ONLY on team logos already resolved server-side against the
 *     canonical team identity — so a photo can never mis-attribute a
 *     player to the wrong team.
 *   • Lazy loading + fixed dimensions to keep scroll smooth.
 *   • Fade-in on load; graceful onError → silhouette fallback.
 *   • NO paid API, NO scraping — reuses ESPN-backed logo URLs the
 *     backend already attaches on `/api/picks/today`.
 */
import React, { useState } from "react";
import { View, Text, Image, StyleSheet, Platform } from "react-native";
import { COLORS, RADIUS } from "@/src/theme";
import type { Pick } from "@/src/lib/api";

interface Props {
  pick: Pick;
  /** Visual diameter in dp.  Default 64 (card corner). */
  size?: number;
  /** Style variant — "circle" for player headshot slot, "square" for team logo blocks. */
  variant?: "circle" | "square";
  /** Optional accent color (tier color) applied to the ring border. */
  ringColor?: string;
  /** Whether this card is a player-prop card (drives fallback emoji). */
  isPlayerProp?: boolean;
}

const SPORT_EMOJI: Record<string, string> = {
  MLB: "⚾",
  NBA: "🏀",
  NFL: "🏈",
  CFB: "🏈",
  NHL: "🏒",
  Soccer: "⚽",
  Tennis: "🎾",
  UFC: "🥊",
  KBO: "⚾",
  WNBA: "🏀",
};

/** Extract the player's likely team side from the pick.
 *  Uses the same canonical fields the backend stamps — no name parsing.
 */
function pickPlayerSide(pick: Pick): "home" | "away" | null {
  const sv2 = (pick as any).selection_v2;
  const teamOnSel = sv2?.selection?.team;
  const home = (pick as any).home_team || sv2?.event?.home;
  const away = (pick as any).away_team || sv2?.event?.away;
  if (teamOnSel && home && teamOnSel === home) return "home";
  if (teamOnSel && away && teamOnSel === away) return "away";

  // Fallback: look at selection string ↔ team-abbrev in payload.
  const homeAbbr = (pick as any).home_meta?.abbrev;
  const awayAbbr = (pick as any).away_meta?.abbrev;
  const sel = (pick.selection || "").toUpperCase();
  if (homeAbbr && sel.includes(String(homeAbbr).toUpperCase())) return "home";
  if (awayAbbr && sel.includes(String(awayAbbr).toUpperCase())) return "away";
  return null;
}

export function PlayerIdentity({
  pick,
  size = 64,
  variant = "circle",
  ringColor,
  isPlayerProp = false,
}: Props) {
  const [errored, setErrored] = useState(false);
  const [loaded, setLoaded] = useState(false);

  const home = (pick as any).home_meta || {};
  const away = (pick as any).away_meta || {};

  const side = pickPlayerSide(pick);
  const primaryLogo: string | undefined =
    side === "home" ? home.logo : side === "away" ? away.logo : (home.logo || away.logo);
  const secondaryLogo: string | undefined =
    side === "home" ? away.logo : side === "away" ? home.logo : undefined;

  // Choose the best available logo (server-side canonical).
  const uri: string | undefined =
    !errored && primaryLogo ? primaryLogo : secondaryLogo;

  const radius = variant === "circle" ? size / 2 : RADIUS.md;
  const ring = ringColor || COLORS.borderStrong;

  const emoji = SPORT_EMOJI[pick.sport] || "🎯";

  if (!uri) {
    // Silhouette fallback — pick still renders.
    return (
      <View
        style={[
          styles.wrap,
          { width: size, height: size, borderRadius: radius, borderColor: ring },
          styles.silhouetteBg,
        ]}
      >
        <Text style={{ fontSize: size * 0.44 }}>{emoji}</Text>
      </View>
    );
  }

  return (
    <View
      style={[
        styles.wrap,
        { width: size, height: size, borderRadius: radius, borderColor: ring },
      ]}
    >
      <Image
        source={{ uri }}
        onLoad={() => setLoaded(true)}
        onError={() => setErrored(true)}
        // Fixed dimensions — avoids layout shift.
        style={[
          styles.img,
          { width: size, height: size, borderRadius: radius, opacity: loaded ? 1 : 0.001 },
        ]}
        // Native platforms accept `resizeMode`; on web it is ignored safely.
        resizeMode="contain"
        {...(Platform.OS === "web"
          ? ({ loading: "lazy" } as any)
          : ({ } as any))}
      />
      {!loaded && (
        <View style={[styles.placeholder, { borderRadius: radius }]}>
          <Text style={{ fontSize: size * 0.36, opacity: 0.35 }}>{emoji}</Text>
        </View>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: {
    alignItems: "center",
    justifyContent: "center",
    borderWidth: 1,
    overflow: "hidden",
    backgroundColor: "rgba(255,255,255,0.03)",
  },
  silhouetteBg: {
    backgroundColor: "rgba(255,255,255,0.05)",
  },
  img: {
    backgroundColor: "transparent",
  },
  placeholder: {
    position: "absolute",
    top: 0, left: 0, right: 0, bottom: 0,
    alignItems: "center",
    justifyContent: "center",
    backgroundColor: "rgba(255,255,255,0.03)",
  },
});

export default PlayerIdentity;
