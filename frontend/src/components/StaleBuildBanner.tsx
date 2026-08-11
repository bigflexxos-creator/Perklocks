/**
 * StaleBuildBanner — proactive deploy-drift warning.
 *
 * Trigger logic (Block 2C-cont Issue-6, 2026-08 — deploy-drift-fix v3):
 *   The banner surfaces ONLY when a truthful deploy identifier
 *   confirms drift.  Two independent signals are consulted:
 *
 *   1. `data_version` mismatch — bundled `APP_DATA_VERSION` differs
 *      from the backend's `data_version`.  Both are SOURCE-CODE
 *      constants that only change when someone explicitly bumps
 *      them on a release, so a mismatch is a genuine deploy-drift
 *      signal (never a runtime restart).
 *
 *   2. `deploy_metadata` — when the runtime exposes a real deploy
 *      identifier (deploy_id / git_commit_sha / deploy_timestamp),
 *      the banner may cite an age based on it.  When absent, we DO
 *      NOT invent an age from `server_started_at` (that is
 *      process-start time — advances on crash / pod / supervisor
 *      restart, and lying about it as "deploy age" is what caused
 *      the 2026-08-06 user report "why do it do that if it's
 *      deployed").
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
import { View, Text, Pressable, StyleSheet, Platform } from "react-native";
import AsyncStorage from "@react-native-async-storage/async-storage";
import Constants from "expo-constants";
import { COLORS } from "@/src/theme";
import { APP_DATA_VERSION } from "@/src/lib/cachebust";
import { api } from "@/src/lib/api";

const DISMISS_KEY_PREFIX = "perkslocks.stale_banner_dismissed.";

/**
 * Resolve the actual build/bundle date. Preferred source is the auto-injected
 * `buildDate` value from app.config.js (regenerated every bundle → always
 * accurate on Publish). Falls back to parsing the legacy hardcoded
 * `APP_DATA_VERSION` prefix if the extra field is missing for any reason
 * (e.g. very old cached bundle without the new config).
 */
function resolveBuildDate(): Date | null {
  try {
    const extra = (Constants?.expoConfig as any)?.extra || {};
    const iso: string | undefined = extra.buildTime || extra.buildDate;
    if (iso && typeof iso === "string") {
      const d = new Date(iso);
      if (!Number.isNaN(d.getTime())) return d;
    }
  } catch {
    /* fall through to legacy parse */
  }
  // Legacy fallback: APP_DATA_VERSION format "YYYYMMDD-NN-slug"
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

type DriftSignal =
  | { kind: "none" }
  | { kind: "data_version_mismatch"; bundle: string; server: string }
  | { kind: "deploy_metadata"; ageDays: number; identifier: string };

export function StaleBuildBanner() {
  const [signal, setSignal] = useState<DriftSignal>({ kind: "none" });
  const [dismissed, setDismissed] = useState<boolean>(true);

  useEffect(() => {
    let cancelled = false;

    // Only surface the "stale deploy" banner in the developer's own
    // workspaces (localhost dev preview + *.preview.emergentagent.com).
    // On the production deployed host (bet-edge-ai-1.emergent.host and
    // any *.emergent.host / *.emergent.sh / native builds) end users
    // have no way to "Update Deployment", so the banner is pure noise
    // for them. 2026-07-12 user report: "why do it do that if it's deployed".
    if (Platform.OS !== "web") return; // native builds → hide
    if (typeof window !== "undefined" && window.location?.hostname) {
      const host = window.location.hostname;
      const isDevHost =
        host === "localhost" ||
        host === "127.0.0.1" ||
        host.endsWith(".preview.emergentagent.com");
      if (!isDevHost) return; // production web → banner never shows
    }

    (async () => {
      try {
        const ver = await api.version();
        if (cancelled) return;
        // ─── Block 2C-cont (2026-08) — deploy-drift fix v3 ──────────
        // Truthful trigger hierarchy:
        //   (a) `deploy_metadata` from the backend (real deploy
        //       identifier) → age is defensible; cite it.
        //   (b) `data_version` mismatch (both are explicit source
        //       constants) → real deploy signal WITHOUT an age (we
        //       don't know the age without (a)).
        //   (c) Anything else — do nothing.  In particular, DO NOT
        //       compute an age from `server_started_at` — that is
        //       process-start time and advances on crash / pod /
        //       supervisor restart.
        const deployMd = (ver as any)?.deploy_metadata;
        const deployTs = deployMd?.deploy_timestamp;
        const buildDate = resolveBuildDate();

        // (a) — real deploy timestamp available.
        if (deployTs && buildDate) {
          const dt = new Date(deployTs);
          if (
            !Number.isNaN(dt.getTime()) &&
            dt.getTime() > buildDate.getTime()
          ) {
            const diff = daysBetween(buildDate, dt);
            if (diff >= 1) {
              const dismissKey = DISMISS_KEY_PREFIX + APP_DATA_VERSION;
              const wasDismissed = await AsyncStorage.getItem(dismissKey);
              if (wasDismissed === "1") {
                setDismissed(true);
                return;
              }
              const idParts = [
                deployMd.deploy_id,
                deployMd.git_commit_sha,
                deployMd.backend_release_id,
              ].filter(Boolean);
              setSignal({
                kind: "deploy_metadata",
                ageDays: diff,
                identifier: (idParts[0] || "backend").toString().slice(0, 12),
              });
              setDismissed(false);
              return;
            }
          }
        }

        // (b) — data_version mismatch fallback.
        const serverDataVer = (ver as any)?.data_version;
        if (
          serverDataVer &&
          typeof serverDataVer === "string" &&
          serverDataVer !== APP_DATA_VERSION
        ) {
          const dismissKey = DISMISS_KEY_PREFIX + APP_DATA_VERSION;
          const wasDismissed = await AsyncStorage.getItem(dismissKey);
          if (wasDismissed === "1") {
            setDismissed(true);
            return;
          }
          setSignal({
            kind: "data_version_mismatch",
            bundle: APP_DATA_VERSION,
            server: serverDataVer,
          });
          setDismissed(false);
          return;
        }

        // (c) — no truthful drift signal.  Do NOT infer from
        // server_started_at.
        setSignal({ kind: "none" });
      } catch {
        // Quiet failure — banner just doesn't show. Don't break the home tab.
      }
    })();

    return () => { cancelled = true; };
  }, []);

  if (signal.kind === "none" || dismissed) return null;

  const onDismiss = async () => {
    try {
      await AsyncStorage.setItem(DISMISS_KEY_PREFIX + APP_DATA_VERSION, "1");
    } catch {
      /* ignore */
    }
    setDismissed(true);
  };

  // ── Truthful copy per signal kind ────────────────────────────────
  //   data_version_mismatch: we KNOW builds differ but don't know
  //     the age; do NOT cite days.
  //   deploy_metadata: real deploy identifier proves the age; cite
  //     days truthfully.
  const title =
    signal.kind === "deploy_metadata"
      ? `Backend deployed ${signal.ageDays} ${
          signal.ageDays === 1 ? "day" : "days"
        } after this build`
      : "New backend build available";
  const sub =
    signal.kind === "deploy_metadata"
      ? `Deploy ${signal.identifier} is newer than your current bundle. Tap Deploy → Update Deployment in Emergent to push the frontend.`
      : "Your bundled data_version differs from the backend. Tap Deploy → Update Deployment in Emergent to refresh.";

  return (
    <View style={styles.wrap}>
      <View style={styles.left}>
        <Text style={styles.icon}>🟡</Text>
        <View style={styles.textBlock}>
          <Text style={styles.title}>{title}</Text>
          <Text style={styles.sub} numberOfLines={3}>
            {sub}
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
