/**
 * Client-side telemetry — reports uncaught errors to the backend so
 * we can see what's crashing without user reports. Fire-and-forget:
 * a failure to post an error must never cascade into another error.
 *
 * Called from:
 *   • ErrorBoundary.componentDidCatch (React tree errors)
 *   • The route-level guards installed in Milestone 1.1
 *   • Any explicit try/catch in feature code (`import { reportError }`)
 *
 * Payload deliberately excludes PII beyond a sha256(email).substring(0,8)
 * hash which the backend computes from the auth token.
 */
import Constants from "expo-constants";
import { Platform } from "react-native";

import { api } from "@/src/lib/api";

const APP_VERSION =
  (Constants.expoConfig?.version as string) ||
  (Constants.manifest as any)?.version ||
  "unknown";

let _lastMessage = "";
let _lastTs = 0;

export async function reportError(
  err: unknown,
  meta?: { component?: string; urlPath?: string; extra?: Record<string, unknown> },
): Promise<void> {
  try {
    const message = String((err as any)?.message || err || "unknown").slice(0, 1000);
    // Dedup identical errors within 5 seconds to avoid loops
    const now = Date.now();
    if (message === _lastMessage && now - _lastTs < 5000) return;
    _lastMessage = message;
    _lastTs = now;

    const stack = (err as any)?.stack ? String((err as any).stack).slice(0, 8000) : undefined;
    const device = {
      os:         Platform.OS,
      os_version: Platform.Version,
      app_version: APP_VERSION,
      // Model/deviceName are unavailable without expo-device — skip.
    };
    await (api as any).request("/telemetry/error", {
      method: "POST",
      body: {
        message,
        stack,
        url_path: meta?.urlPath,
        component: meta?.component,
        device,
        extra: meta?.extra || null,
      },
      // Auth is optional — the endpoint accepts anonymous errors.
      auth: false,
    }).catch(() => {
      /* swallow — never let telemetry throw */
    });
  } catch {
    /* never rethrow from telemetry */
  }
}
