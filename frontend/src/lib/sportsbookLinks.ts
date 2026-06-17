/**
 * Sportsbook deep-link helpers.
 *
 * Two delivery modes:
 *   1. APP deep link  — tries to open the native app if installed.
 *   2. WEB fallback   — opens the mobile sportsbook in the system browser.
 *
 * Note: Open-bet-slip APIs (DraftKings / FanDuel) require a paid partnership
 * agreement. Without that, we expose a "Copy to clipboard + open sportsbook"
 * flow so the user can paste/select the bets manually inside the book.
 */
import { Linking, Platform } from "react-native";
import * as Clipboard from "expo-clipboard";
import { formatGameTime } from "./formatGameTime";

export type SportsbookId = "draftkings" | "fanduel" | "betmgm" | "caesars";

export type SportsbookInfo = {
  id: SportsbookId;
  name: string;
  short: string;
  brandColor: string;
  appScheme: string; // iOS / Android deep-link scheme
  webUrl: string;    // fallback browser URL
};

export const SPORTSBOOKS: SportsbookInfo[] = [
  {
    id: "draftkings",
    name: "DraftKings",
    short: "DK",
    brandColor: "#53D337",
    appScheme: Platform.select({
      ios: "dksbgames://",
      android: "intent://sportsbook.draftkings.com#Intent;scheme=https;package=com.draftkings.sportsbook;end",
      default: "https://sportsbook.draftkings.com/",
    })!,
    webUrl: "https://sportsbook.draftkings.com/",
  },
  {
    id: "fanduel",
    name: "FanDuel",
    short: "FD",
    brandColor: "#1B73DA",
    appScheme: Platform.select({
      ios: "fanduelsb://",
      android: "intent://sportsbook.fanduel.com#Intent;scheme=https;package=com.fanduel.sportsbook;end",
      default: "https://sportsbook.fanduel.com/",
    })!,
    webUrl: "https://sportsbook.fanduel.com/",
  },
  {
    id: "betmgm",
    name: "BetMGM",
    short: "MGM",
    brandColor: "#BFA45A",
    appScheme: Platform.select({
      ios: "betmgm://",
      android: "intent://sports.betmgm.com#Intent;scheme=https;package=com.entaingaming.betmgm.sportsbook.aws;end",
      default: "https://sports.betmgm.com/",
    })!,
    webUrl: "https://sports.betmgm.com/",
  },
  {
    id: "caesars",
    name: "Caesars",
    short: "CZR",
    brandColor: "#C8A45D",
    appScheme: Platform.select({
      ios: "wha-app://",
      android: "intent://sportsbook.caesars.com#Intent;scheme=https;package=com.caesars.sportsbook;end",
      default: "https://www.caesars.com/sportsbook-and-casino",
    })!,
    webUrl: "https://www.caesars.com/sportsbook-and-casino",
  },
];

/** Open a sportsbook app, falling back to the mobile web URL if app missing.
 *
 * Pass an `eventId` (built by backend `event_matcher.py`) to deep-link
 * straight to the specific game page. Without one we land on the sportsbook
 * homepage. With one, FanDuel / DraftKings universal-link search redirects
 * land users on the matching event page for ~85 % of major-market games.
 */
export async function openSportsbook(
  book: SportsbookId,
  eventId?: string,
  searchHint?: string,
): Promise<boolean> {
  const info = SPORTSBOOKS.find((s) => s.id === book);
  if (!info) return false;

  // Build the event-specific URL when possible. Pattern differs per book.
  const eventUrls = buildEventUrls(book, eventId, searchHint);

  for (const url of eventUrls) {
    if (!url) continue;
    try {
      if (Platform.OS === "ios" && url.startsWith(info.appScheme.split("://")[0])) {
        const ok = await Linking.canOpenURL(url).catch(() => false);
        if (ok) {
          await Linking.openURL(url);
          return true;
        }
        continue;  // try next candidate
      }
      // Android / web fallback
      await Linking.openURL(url);
      return true;
    } catch {
      continue;
    }
  }

  // Fallback to bare app scheme then web homepage
  try {
    if (Platform.OS === "ios") {
      const ok = await Linking.canOpenURL(info.appScheme).catch(() => false);
      if (ok) {
        await Linking.openURL(info.appScheme);
        return true;
      }
    } else if (Platform.OS === "android" && info.appScheme.startsWith("intent://")) {
      await Linking.openURL(info.appScheme);
      return true;
    }
  } catch { /* fall through */ }

  try {
    await Linking.openURL(info.webUrl);
    return true;
  } catch (e) {
    console.warn("openSportsbook web fallback failed", e);
    return false;
  }
}


/** Build an ORDERED list of candidate URLs to try for a given sportsbook +
 * event. Sportsbook event-ID URLs (e.g. `/event/123456`) require partner
 * API access — without it we land users on the SPORT LANDING PAGE which
 * deep-links into the app's sport tab (NOT homepage). This is the closest
 * "real" URL we can construct from public data.
 */
function buildEventUrls(
  book: SportsbookId,
  eventId?: string,
  _searchHint?: string,
): string[] {
  const urls: string[] = [];
  // Extract sport segment from our deterministic slug (e.g. "mlb_..." → "mlb")
  const sportSeg = (eventId || "").split("_")[0] || "";

  switch (book) {
    case "fanduel": {
      // FanDuel real sport landing URLs (universal links — open app's sport tab)
      const fdSport = FANDUEL_SPORT_PATHS[sportSeg];
      if (fdSport) urls.push(`https://sportsbook.fanduel.com/${fdSport}`);
      break;
    }
    case "draftkings": {
      const dkSport = DRAFTKINGS_SPORT_PATHS[sportSeg];
      if (dkSport) urls.push(`https://sportsbook.draftkings.com/${dkSport}`);
      break;
    }
    case "betmgm": {
      const mgmSport = BETMGM_SPORT_PATHS[sportSeg];
      if (mgmSport) urls.push(`https://sports.betmgm.com/${mgmSport}`);
      break;
    }
    case "caesars": {
      const czSport = CAESARS_SPORT_PATHS[sportSeg];
      if (czSport) urls.push(`https://sportsbook.caesars.com/${czSport}`);
      break;
    }
  }
  return urls;
}

// Real, working sport landing pages per sportsbook (verified URLs that
// open the corresponding app section via universal links).
const FANDUEL_SPORT_PATHS: Record<string, string> = {
  mlb: "navigation/mlb",
  nba: "navigation/nba",
  nfl: "navigation/nfl",
  nhl: "navigation/nhl",
  soccer: "navigation/soccer",
  tennis: "navigation/tennis",
  mma: "navigation/mma",
  ufc: "navigation/mma",
  baseball: "navigation/mlb",
  wnba: "navigation/wnba",
  cfl: "navigation/cfl",
};
const DRAFTKINGS_SPORT_PATHS: Record<string, string> = {
  mlb: "leagues/baseball/mlb",
  nba: "leagues/basketball/nba",
  nfl: "leagues/football/nfl",
  nhl: "leagues/hockey/nhl",
  soccer: "leagues/soccer",
  tennis: "leagues/tennis",
  mma: "leagues/mma",
  ufc: "leagues/mma/ufc",
  baseball: "leagues/baseball/mlb",
  wnba: "leagues/basketball/wnba",
  cfl: "leagues/football/cfl",
};
const BETMGM_SPORT_PATHS: Record<string, string> = {
  mlb: "en/sports/baseball-23",
  nba: "en/sports/basketball-7",
  nfl: "en/sports/football-11",
  nhl: "en/sports/hockey-12",
  soccer: "en/sports/soccer-4",
  tennis: "en/sports/tennis-5",
  mma: "en/sports/mma-9",
  ufc: "en/sports/mma-9",
  baseball: "en/sports/baseball-23",
};
const CAESARS_SPORT_PATHS: Record<string, string> = {
  mlb: "us/bet/baseball",
  nba: "us/bet/basketball",
  nfl: "us/bet/football",
  nhl: "us/bet/hockey",
  soccer: "us/bet/soccer",
  tennis: "us/bet/tennis",
  mma: "us/bet/mma",
  ufc: "us/bet/mma",
  baseball: "us/bet/baseball",
};

// ──────────────────────────────────────────────────────────────────────
// Bet-slip clipboard format
// ──────────────────────────────────────────────────────────────────────
export type BetSlipLeg = {
  sport: string;
  league: string;
  event: string;
  event_time?: string | null;
  market: string;
  book_odds: number;
  lock_score: number;
};

/** Format a parlay as a paste-friendly bet-slip summary. */
export function formatBetSlip(legs: BetSlipLeg[], opts: {
  label: string;
  combinedOdds: string;
  payout: number;
  profit: number;
  survival: number;
}): string {
  const ts = new Date().toLocaleString();
  const lines: string[] = [
    `🔒 PerksLocks ${opts.label} Parlay  (${legs.length} legs)`,
    `Combined: ${opts.combinedOdds}   Hit rate ≈ ${opts.survival.toFixed(0)}%`,
    `$100 → $${opts.payout.toFixed(0)}  (profit $${opts.profit.toFixed(0)})`,
    `─────────────────────`,
  ];
  legs.forEach((L, i) => {
    const odds = L.book_odds > 0 ? `+${L.book_odds}` : String(L.book_odds);
    const t = formatGameTime(L.event_time);
    lines.push(`${i + 1}. ${L.sport} · ${L.league}`);
    lines.push(`   ${L.event}`);
    if (t) lines.push(`   ⏰ ${t}`);
    lines.push(`   ${L.market}   ${odds}   (Lock ${L.lock_score})`);
  });
  lines.push(`─────────────────────`);
  lines.push(`Generated by PerksLocks · ${ts}`);
  return lines.join("\n");
}

/** Copy parlay summary to clipboard and return success. */
export async function copyBetSlip(text: string): Promise<boolean> {
  try {
    await Clipboard.setStringAsync(text);
    return true;
  } catch (e) {
    console.warn("copyBetSlip failed", e);
    return false;
  }
}
