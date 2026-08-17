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
  // Two-stage error state — a broken player headshot must degrade to
  // the team-logo chain, and a broken team logo must degrade to the
  // sport silhouette.  Never to a broken-image icon; the card ALWAYS
  // renders.
  const [headshotErrored, setHeadshotErrored] = useState(false);
  const [logoErrored, setLogoErrored] = useState(false);
  const [loaded, setLoaded] = useState(false);

  const home = (pick as any).home_meta || {};
  const away = (pick as any).away_meta || {};

  const side = pickPlayerSide(pick);
  // ── REAL PLAYER HEADSHOT (2026-06) ────────────────────────────────
  // Prefer the verified server-canonical headshot when the backend
  // has stamped ``player_meta.headshot_url`` (via the shared
  // ``player_meta_decorator``).  This is present ONLY for
  // player-prop cards where canonical identity resolved cleanly
  // against ``db.players`` (ESPN athletes + MLB Stats).
  //
  // Contract:
  //   • Missing player_meta ⇒ never fabricate — falls through to
  //     the team-logo chain.
  //   • ``headshot_verified === true`` is REQUIRED — a non-verified
  //     stamp is treated as absent.
  const pm: any = (pick as any).player_meta;
  const verifiedHeadshot: string | undefined =
    pm && pm.headshot_verified === true && typeof pm.headshot_url === "string"
      ? pm.headshot_url
      : undefined;

  const primaryLogo: string | undefined =
    side === "home" ? home.logo : side === "away" ? away.logo : (home.logo || away.logo);
  const secondaryLogo: string | undefined =
    side === "home" ? away.logo : side === "away" ? home.logo : undefined;

  // Resolution priority:
  //   1. verified player headshot (only when present + verified)
  //   2. player-side team logo
  //   3. opposite-side team logo
  //   4. sport silhouette (rendered by the fallback branch below)
  //
  // Two-stage graceful degradation:
  //   • headshot fails → drops to team logo
  //   • team logo also fails → drops to silhouette
  let uri: string | undefined;
  let isHeadshot = false;
  if (verifiedHeadshot && !headshotErrored) {
    uri = verifiedHeadshot;
    isHeadshot = true;
  } else if (primaryLogo && !logoErrored) {
    uri = primaryLogo;
  } else if (secondaryLogo && !logoErrored) {
    uri = secondaryLogo;
  } else {
    uri = undefined;
  }

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
        onError={() => {
          // Two-stage degradation — headshot fail drops to team logo;
          // a subsequent team-logo fail drops to the silhouette branch.
          if (isHeadshot) {
            setHeadshotErrored(true);
            setLoaded(false);
          } else {
            setLogoErrored(true);
            setLoaded(false);
          }
        }}
        // For headshots use ``cover`` so faces fill the circle cleanly;
        // for team logos keep ``contain`` so crests don't get cropped.
        resizeMode={isHeadshot ? "cover" : "contain"}
        // Fixed dimensions — avoids layout shift.
        style={[
          styles.img,
          { width: size, height: size, borderRadius: radius, opacity: loaded ? 1 : 0.001 },
        ]}
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
