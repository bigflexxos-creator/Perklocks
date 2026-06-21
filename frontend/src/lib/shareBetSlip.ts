/**
 * Share-to-Gambly betslip helpers.
 *
 * Pipeline (Share button tap):
 *   1) Build formatted slip TEXT (structured – Sport/League/Event/…)
 *   2) Capture the on-screen card View as a PNG via react-native-view-shot
 *   3) Stash the text on the clipboard (so any share target that *can*
 *      parse text gets it via paste; PNG is the universal fallback)
 *   4) Open the OS-native share sheet with the PNG
 *
 * Why image + clipboard text and not multi-attachment?
 *   expo-sharing only accepts ONE file URI per share. Multi-attachment
 *   (image + text) requires either iOS-only `Share.share({message,url})`
 *   or react-native-share. Going PNG + clipboard text keeps the path
 *   identical on iOS / Android / Web and satisfies the Gambly contract:
 *   if Gambly can parse the structured payload it pastes from clipboard,
 *   otherwise it falls back to the PNG screenshot.
 */
import { Alert, Linking, Platform, Share as RNShare } from "react-native";
import * as Sharing from "expo-sharing";
import * as MediaLibrary from "expo-media-library";
import * as Clipboard from "expo-clipboard";
import { captureRef } from "react-native-view-shot";

export const APP_NAME = "PerksLocks";

export type SlipLeg = {
  sport?: string;
  league?: string;
  event?: string;
  market?: string;       // human-readable line, e.g. "Aaron Judge Over 0.5 HR"
  selection?: string;    // optional explicit selection, falls back to market
  book_odds?: number | string;
  bookmaker?: string;
  confidence?: number | string;
};

export type SlipPayload = {
  // Multi-leg parlay path
  legs?: SlipLeg[];
  combined_odds?: number | string;
  label?: string;             // SAFE / BALANCED / AGGRESSIVE
  // Single-pick path
  single?: SlipLeg;
  // Common
  generated_at?: string;      // ISO timestamp; defaults to now
};

// ─── helpers ───────────────────────────────────────────────────────────
const fmtOdds = (o: any) => {
  const n = typeof o === "number" ? o : parseFloat(String(o));
  if (isNaN(n)) return String(o ?? "—");
  return n > 0 ? `+${n}` : `${n}`;
};

const fmtTs = (iso?: string) => {
  try {
    const d = iso ? new Date(iso) : new Date();
    return d.toLocaleString();
  } catch { return new Date().toLocaleString(); }
};

/** Build the structured betslip text per the Gambly contract. */
export function buildSlipText(payload: SlipPayload): string {
  const lines: string[] = [`🔒 ${APP_NAME}`, ""];
  const ts = fmtTs(payload.generated_at);

  if (payload.single) {
    const p = payload.single;
    lines.push(`Sport: ${p.sport ?? "—"}`);
    lines.push(`League: ${p.league ?? "—"}`);
    lines.push(`Event: ${p.event ?? "—"}`);
    lines.push(`Market: ${p.market ?? "—"}`);
    lines.push(`Selection: ${p.selection ?? p.market ?? "—"}`);
    lines.push(`Odds: ${fmtOdds(p.book_odds)}`);
    if (p.bookmaker) lines.push(`Bookmaker: ${p.bookmaker}`);
    if (p.confidence != null && p.confidence !== "") lines.push(`Confidence: ${p.confidence}`);
    lines.push(`Generated At: ${ts}`);
    return lines.join("\n");
  }

  if (payload.legs && payload.legs.length > 0) {
    const legs = payload.legs;
    const n = legs.length;
    const head = n > 1 ? `${n}-Leg Parlay` : "Single Pick";
    lines[0] = `🔒 ${APP_NAME} · ${head}${payload.label ? `  [${payload.label}]` : ""}`;
    lines.push("");
    legs.forEach((l, idx) => {
      const heading = [l.league || l.sport].filter(Boolean).join(" · ");
      if (heading) lines.push(heading);
      const sel = l.selection || l.market || "—";
      lines.push(`${sel} (${fmtOdds(l.book_odds)})`);
      if (l.event && l.event !== sel) lines.push(l.event);
      if (idx < legs.length - 1) lines.push("");
    });
    lines.push("");
    if (payload.combined_odds != null) {
      lines.push(`Total Odds: ${fmtOdds(payload.combined_odds)}`);
    }
    lines.push(`Generated At: ${ts}`);
    return lines.join("\n");
  }

  lines.push("(empty slip)");
  return lines.join("\n");
}

/** Copy structured slip text to the clipboard. */
export async function copySlipText(text: string): Promise<boolean> {
  try {
    await Clipboard.setStringAsync(text);
    return true;
  } catch (e) {
    console.warn("copySlipText failed", e);
    return false;
  }
}

/** Capture a View ref to a PNG temp file. Returns the file URI or null. */
async function captureViewToPng(viewRef: any): Promise<string | null> {
  try {
    if (!viewRef) return null;
    const target = viewRef.current ?? viewRef;
    if (!target) return null;
    const uri = await captureRef(target, {
      format: "png",
      quality: 1,
      result: "tmpfile",
    });
    return uri;
  } catch (e) {
    console.warn("captureViewToPng failed", e);
    return null;
  }
}

function openSettingsPrompt(title: string, msg: string) {
  Alert.alert(title, msg, [
    { text: "Cancel", style: "cancel" },
    { text: "Open Settings", onPress: () => Linking.openSettings() },
  ]);
}

/**
 * Open native share sheet with the betslip image + clipboard-stashed text.
 * Gambly (or any share target) gets the PNG; structured text is on the
 * clipboard for apps that support paste-import.
 */
export async function shareSlip(viewRef: any, text: string): Promise<boolean> {
  // Web path: navigator.share if available, else copy.
  if (Platform.OS === "web") {
    const nav: any = (globalThis as any).navigator;
    try {
      if (nav?.share) {
        await nav.share({ title: APP_NAME, text });
        return true;
      }
      if (nav?.clipboard?.writeText) {
        await nav.clipboard.writeText(text);
        Alert.alert("Copied", "Bet slip copied to clipboard.");
        return true;
      }
    } catch { /* user-cancelled or unsupported */ }
    return false;
  }

  // Always stash text on clipboard FIRST so Gambly/any target can paste it.
  await copySlipText(text);

  const uri = await captureViewToPng(viewRef);

  // If capture failed, fall back to text-only RN Share.
  if (!uri) {
    try {
      await RNShare.share({ message: text, title: APP_NAME });
      return true;
    } catch (e) {
      console.warn("RN share fallback failed", e);
      return false;
    }
  }

  // Prefer expo-sharing for cross-platform image+UTI handling.
  const available = await Sharing.isAvailableAsync();
  if (!available) {
    try {
      await RNShare.share({ message: text, url: uri, title: APP_NAME });
      return true;
    } catch (e) {
      console.warn("RN share with url failed", e);
      return false;
    }
  }

  try {
    await Sharing.shareAsync(uri, {
      mimeType: "image/png",
      dialogTitle: `Share ${APP_NAME} betslip`,
      UTI: "public.png",
    });
    return true;
  } catch (e: any) {
    console.warn("expo-sharing failed", e);
    return false;
  }
}

/**
 * Save the betslip PNG to the device gallery. Implements the
 * handle_permissions contract: check → ask (canAskAgain) → open Settings.
 */
export async function saveSlipImage(viewRef: any): Promise<boolean> {
  if (Platform.OS === "web") {
    Alert.alert(
      "Not available on web",
      "Open PerksLocks on iOS or Android to save the betslip image to Photos.",
    );
    return false;
  }

  // Permission flow
  let perm = await MediaLibrary.getPermissionsAsync();
  if (!perm.granted) {
    if (!perm.canAskAgain) {
      openSettingsPrompt(
        "Photos permission needed",
        "Enable Photos access to save your betslip image.",
      );
      return false;
    }
    perm = await MediaLibrary.requestPermissionsAsync();
    if (!perm.granted) {
      if (!perm.canAskAgain) {
        openSettingsPrompt(
          "Photos permission needed",
          "Enable Photos access in Settings to save your betslip image.",
        );
      } else {
        Alert.alert("Permission denied", "Photos access is required to save the image.");
      }
      return false;
    }
  }

  const uri = await captureViewToPng(viewRef);
  if (!uri) {
    Alert.alert("Capture failed", "Could not generate betslip image.");
    return false;
  }

  try {
    await MediaLibrary.saveToLibraryAsync(uri);
    Alert.alert("Saved!", "Betslip image saved to your Photos.");
    return true;
  } catch (e: any) {
    console.warn("saveToLibraryAsync failed", e);
    Alert.alert("Save failed", String(e?.message || e));
    return false;
  }
}
