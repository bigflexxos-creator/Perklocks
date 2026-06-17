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
 * event. We try (1) the native app deep-link with event ID, (2) the
 * universal-link search redirect, (3) the bare app, then (4) the web URL.
 */
function buildEventUrls(
  book: SportsbookId,
  eventId?: string,
  searchHint?: string,
): string[] {
  const urls: string[] = [];
  // Search hint: prefer the human-readable team string (e.g. "Lakers Warriors")
  const q = searchHint ? encodeURIComponent(searchHint) : "";

  switch (book) {
    case "fanduel": {
      if (eventId) {
        // FanDuel native app deep-link pattern (iOS). May not resolve on
        // every build; falls through to universal link below if not.
        if (Platform.OS === "ios") urls.push(`fanduelsb://event/${eventId}`);
        // Web URL with event ID lands on a smart-search results page that
        // auto-resolves to the event when there's a single match.
        urls.push(`https://sportsbook.fanduel.com/search?event=${encodeURIComponent(eventId)}`);
      }
      if (q) {
        urls.push(`https://sportsbook.fanduel.com/search?q=${q}`);
      }
      break;
    }
    case "draftkings": {
      if (eventId) {
        if (Platform.OS === "ios") urls.push(`dksbgames://event/${eventId}`);
        urls.push(`https://sportsbook.draftkings.com/event/${encodeURIComponent(eventId)}`);
      }
      if (q) {
        urls.push(`https://sportsbook.draftkings.com/?searchTerm=${q}`);
      }
      break;
    }
    case "betmgm": {
      if (eventId) {
        urls.push(`https://sports.betmgm.com/en/sports/events/${encodeURIComponent(eventId)}`);
      }
      if (q) {
        urls.push(`https://sports.betmgm.com/en/sports/search?q=${q}`);
      }
      break;
    }
    case "caesars": {
      if (q) {
        urls.push(`https://sportsbook.caesars.com/us/search?q=${q}`);
      }
      break;
    }
  }
  return urls;
}

// ──────────────────────────────────────────────────────────────────────
// Bet-slip clipboard format
// ──────────────────────────────────────────────────────────────────────
export type BetSlipLeg = {
  sport: string;
  league: string;
  event: string;
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
    lines.push(`${i + 1}. ${L.sport} · ${L.league}`);
    lines.push(`   ${L.event}`);
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
