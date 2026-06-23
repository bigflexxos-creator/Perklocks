/**
 * StaleBuildBanner — proactive deploy-drift warning.
 *
 * Compares the bundled `APP_DATA_VERSION` (set at build time in
 * src/lib/cachebust.ts) against the backend's `/api/version`
 * `data_version` + `server_time`. If the bundled build date is more
 * than 1 day older than the server's UTC day, we render a yellow
 * banner on the home tab.
 *
 * The user can dismiss the banner — dismissal is persisted in
 * AsyncStorage keyed by the build's date, so a NEW stale build the
 * next day will surface a fresh banner instead of staying hidden.
 *
 * Why this exists
 * ----------------
 * The deployed app at bet-edge-ai-1.emergent.host is a static
 * snapshot from the last Publish, NOT a live mirror of preview. The
 * user can ship 12 backend features in a session and the deployed
 * app won't see any of them until they click "Update Deployment".
 * This banner is the visible tripwire.
 */
import React, { useEffect, useState } from "react";
import { View, Text, Pressable, StyleSheet } from "react-native";
import AsyncStorage from "@react-native-async-storage/async-storage";
import { COLORS } from "@/src/theme";
import { APP_DATA_VERSION } from "@/src/lib/cachebust";
import { api } from "@/src/lib/api";

const DISMISS_KEY_PREFIX = "perkslocks.stale_banner_dismissed.";

function buildDateFromAppVersion(): Date | null {
  // APP_DATA_VERSION format: "YYYYMMDD-NN-slug" e.g. "20260620-12-analytics-clean"
  const m = /^(\d{4})(\d{2})(\d{2})/.exec(APP_DATA_VERSION);
  if (!m) return null;
  const yyyy = Number(m[1]);
  const mm = Number(m[2]);
  const dd = Number(m[3]);
  if (!yyyy || !mm || !dd) return null;
  return new Date(Date.UTC(yyyy, mm - 1, dd));
}

function daysBetween(a: Date, b: Date): number {
  const MS = 1000 * 60 * 60 * 24;
  return Math.floor((b.getTime() - a.getTime()) / MS);
}

export function StaleBuildBanner() {
  const [staleDays, setStaleDays] = useState<number>(0);
  const [dismissed, setDismissed] = useState<boolean>(true);

  useEffect(() => {
    let cancelled = false;

    (async () => {
      try {
        const ver = await api.version();
        if (cancelled) return;
        const serverIso = (ver as any)?.server_time;
        const serverDate = serverIso ? new Date(serverIso) : new Date();
        const buildDate = buildDateFromAppVersion();
        if (!buildDate) return;
        const diff = daysBetween(buildDate, serverDate);
        if (diff <= 1) {
          setStaleDays(0);
          return;
        }
        // Check if user already dismissed THIS build's banner.
        const dismissKey = DISMISS_KEY_PREFIX + APP_DATA_VERSION;
        const wasDismissed = await AsyncStorage.getItem(dismissKey);
        if (wasDismissed === "1") {
          setDismissed(true);
          return;
        }
        setStaleDays(diff);
        setDismissed(false);
      } catch {
        // Quiet failure — banner just doesn't show. Don't break the home tab.
      }
    })();

    return () => { cancelled = true; };
  }, []);

  if (staleDays <= 1 || dismissed) return null;

  const onDismiss = async () => {
    try {
      await AsyncStorage.setItem(DISMISS_KEY_PREFIX + APP_DATA_VERSION, "1");
    } catch {
      /* ignore */
    }
    setDismissed(true);
  };

  return (
    <View style={styles.wrap}>
      <View style={styles.left}>
        <Text style={styles.icon}>🟡</Text>
        <View style={styles.textBlock}>
          <Text style={styles.title}>
            This deploy is {staleDays} {staleDays === 1 ? "day" : "days"} behind
          </Text>
          <Text style={styles.sub} numberOfLines={2}>
            New picks &amp; features are live in your editor. Tap{" "}
            <Text style={styles.kbd}>Deploy → Update Deployment</Text> in
            Emergent to push them.
          </Text>
        </View>
      </View>
      <Pressable onPress={onDismiss} hitSlop={10} style={styles.dismiss}>
        <Text style={styles.dismissTxt}>✕</Text>
      </Pressable>
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: {
    flexDirection: "row",
    alignItems: "flex-start",
    backgroundColor: "#3a2d05",
    borderLeftWidth: 3,
    borderLeftColor: "#f5c542",
    padding: 12,
    marginHorizontal: 16,
    marginTop: 8,
    marginBottom: 12,
    borderRadius: 10,
    gap: 10,
  },
  left: { flexDirection: "row", flex: 1, gap: 10 },
  icon: { fontSize: 18, lineHeight: 22 },
  textBlock: { flex: 1 },
  title: {
    color: "#fde68a",
    fontSize: 13,
    fontWeight: "800",
    letterSpacing: 0.2,
    marginBottom: 3,
  },
  sub: {
    color: "#e8d9a8",
    fontSize: 11.5,
    lineHeight: 16,
  },
  kbd: {
    color: "#fff",
    fontWeight: "800",
  },
  dismiss: {
    padding: 4,
    marginLeft: 4,
  },
  dismissTxt: {
    color: COLORS.textMuted,
    fontSize: 16,
    fontWeight: "700",
  },
});
