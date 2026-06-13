// Sportsbook opener — tries the native app first via URI scheme, falls back
// to the universal link in an in-app browser. On every tap we also drop the
// slip details into the clipboard so the user can paste the matchup/line
// directly into the sportsbook search bar.
//
// Note on slip injection: pre-populating a parlay into FanDuel/DraftKings/
// BetMGM requires an affiliate/partner agreement. Without one, the most we
// can do is open the native app to the right sport page and hand the user
// the bet text on the clipboard for one-tap paste-and-search.

import { Linking, Platform, Alert } from "react-native";
import * as WebBrowser from "expo-web-browser";
import * as Clipboard from "expo-clipboard";
import { Pick } from "@/src/lib/api";
import { computeParlay } from "@/src/contexts/BetSlipContext";

// Sport key → web fallback path per sportsbook.
const WEB_URLS: Record<string, Record<string, string>> = {
  FanDuel: {
    MLB: "https://sportsbook.fanduel.com/navigation/mlb",
    NBA: "https://sportsbook.fanduel.com/navigation/nba",
    WNBA: "https://sportsbook.fanduel.com/navigation/wnba",
    NFL: "https://sportsbook.fanduel.com/navigation/nfl",
    Soccer: "https://sportsbook.fanduel.com/navigation/soccer",
    Tennis: "https://sportsbook.fanduel.com/navigation/tennis",
    UFC: "https://sportsbook.fanduel.com/navigation/mma",
    KBO: "https://sportsbook.fanduel.com/navigation/baseball",
    _default: "https://sportsbook.fanduel.com/",
  },
  DraftKings: {
    MLB: "https://sportsbook.draftkings.com/leagues/baseball/mlb",
    NBA: "https://sportsbook.draftkings.com/leagues/basketball/nba",
    WNBA: "https://sportsbook.draftkings.com/leagues/basketball/wnba",
    NFL: "https://sportsbook.draftkings.com/leagues/football/nfl",
    Soccer: "https://sportsbook.draftkings.com/leagues/soccer",
    Tennis: "https://sportsbook.draftkings.com/leagues/tennis",
    UFC: "https://sportsbook.draftkings.com/leagues/mma/ufc",
    KBO: "https://sportsbook.draftkings.com/leagues/baseball",
    _default: "https://sportsbook.draftkings.com/",
  },
  BetMGM: {
    MLB: "https://sports.betmgm.com/en/sports/baseball-23",
    NBA: "https://sports.betmgm.com/en/sports/basketball-7",
    WNBA: "https://sports.betmgm.com/en/sports/basketball-7",
    NFL: "https://sports.betmgm.com/en/sports/football-11",
    Soccer: "https://sports.betmgm.com/en/sports/soccer-4",
    Tennis: "https://sports.betmgm.com/en/sports/tennis-5",
    UFC: "https://sports.betmgm.com/en/sports/mma-15",
    KBO: "https://sports.betmgm.com/en/sports/baseball-23",
    _default: "https://sports.betmgm.com/",
  },
};

// Primary URI scheme to attempt + ordered fallbacks. We attempt each in
// order and use the first one the OS reports as openable. If none work,
// we hand off to the universal link.
const APP_SCHEMES: Record<string, string[]> = {
  FanDuel: ["fanduel://", "fanduelsb://"],
  DraftKings: ["dksb://", "draftkings://"],
  BetMGM: ["mgmsports://", "betmgm://"],
};

export const SPORTSBOOKS = ["FanDuel", "DraftKings", "BetMGM"] as const;
export type SportsbookName = (typeof SPORTSBOOKS)[number];

export function dominantSport(picks: Pick[]): string | null {
  if (!picks.length) return null;
  const first = picks[0].sport;
  return picks.every((p) => p.sport === first) ? first : null;
}

function webUrl(book: SportsbookName, sport: string | null): string {
  const map = WEB_URLS[book];
  if (!map) return "";
  return (sport && map[sport]) || map._default;
}

// Produce a short clipboard-friendly summary the user can paste/search
// inside the sportsbook app. We keep it tight so the search bar isn't
// overwhelmed.
function buildClipboardText(picks: Pick[]): string {
  if (!picks.length) return "";
  if (picks.length === 1) {
    const p = picks[0];
    const odds = p.book_odds > 0 ? `+${p.book_odds}` : `${p.book_odds}`;
    return `${p.market} (${odds}) — ${p.event}`;
  }
  const parlay = computeParlay(picks);
  const head = `${parlay.legCount}-Leg Parlay · Combined ${parlay.americanOdds} · $${parlay.payoutOn100.toFixed(0)} on $100`;
  const legs = picks
    .map((p, i) => {
      const odds = p.book_odds > 0 ? `+${p.book_odds}` : `${p.book_odds}`;
      return `${i + 1}. ${p.market} (${odds}) — ${p.event}`;
    })
    .join("\n");
  return `${head}\n${legs}`;
}

async function tryAppScheme(book: SportsbookName): Promise<boolean> {
  if (Platform.OS === "web") return false;
  const schemes = APP_SCHEMES[book] || [];
  for (const scheme of schemes) {
    try {
      const ok = await Linking.canOpenURL(scheme);
      if (ok) {
        await Linking.openURL(scheme);
        return true;
      }
    } catch {
      // canOpenURL can throw on iOS if scheme isn't whitelisted in
      // LSApplicationQueriesSchemes — skip and try the next one.
    }
  }
  return false;
}

// Main entry point: copies slip to clipboard, opens the sportsbook native
// app if installed, otherwise opens the universal link in an in-app browser
// (web: new tab). Returns true if anything opened.
export async function openSportsbookWithSlip(
  book: SportsbookName,
  picks: Pick[],
): Promise<void> {
  const sport = dominantSport(picks);
  const fallbackUrl = webUrl(book, sport);
  const clip = buildClipboardText(picks);

  // 1) Best-effort clipboard copy so user can paste/search in the book.
  if (clip) {
    try {
      await Clipboard.setStringAsync(clip);
    } catch {
      // Silently ignore — UX still works without clipboard.
    }
  }

  // 2) Web: open in a new tab. App handoff isn't possible from a browser.
  if (Platform.OS === "web") {
    if (!fallbackUrl) return;
    if (typeof window !== "undefined") {
      const popup = window.open(fallbackUrl, "_blank", "noopener,noreferrer");
      if (!popup) window.location.href = fallbackUrl;
    }
    return;
  }

  // 3) Native: try the app's URI scheme first.
  const appOpened = await tryAppScheme(book);
  if (appOpened) {
    // Tiny confirmation so the user knows the picks are on the clipboard.
    setTimeout(() => {
      Alert.alert(
        `Opening ${book}…`,
        clip
          ? "Your picks are on your clipboard — long-press the search bar inside the app to paste & find each leg."
          : "App opened.",
      );
    }, 400);
    return;
  }

  // 4) Fallback: open the universal link in an in-app browser. On modern
  //    iOS the universal link should *still* punt to the installed app
  //    automatically; if not, the user gets the mobile site.
  try {
    await WebBrowser.openBrowserAsync(fallbackUrl, {
      showTitle: true,
      enableBarCollapsing: true,
      dismissButtonStyle: "close",
    });
  } catch {
    try {
      await Linking.openURL(fallbackUrl);
    } catch {
      Alert.alert(
        "Couldn't open " + book,
        `Open ${fallbackUrl} manually. Your picks are on the clipboard.`,
      );
    }
  }
}
