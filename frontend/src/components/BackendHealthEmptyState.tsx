/**
 * Commit C — Backend-health-aware EmptyState wrapper.
 *
 * Observes the live TransportState authority (which bridges the
 * global connectivity module) and renders a HONEST empty state when
 * the backend is unreachable / degraded / the device is offline.
 *
 * Rendering contract:
 *   • Returns `null` while `stage === "online"` or `"idle"` so the
 *     surrounding screen is free to render its real content / its
 *     own domain-specific "no picks" state.
 *   • Returns a themed EmptyState card for every other stage, with
 *     an optional `onRetry` passed through.
 *
 * Pure presentation — never mutates connectivity / transport / cache.
 */
import React, { useEffect, useState } from "react";
import {
  TransportState,
  getTransportState,
  subscribeTransport,
} from "@/src/lib/transportState";
import { probeReachability } from "@/src/lib/connectivity";
import { EmptyState } from "@/src/components/EmptyState";

type Props = {
  /** Optional retry handler.  Invoked AFTER a reachability probe so
   *  one retry attempt refreshes both the connectivity state and the
   *  caller's data source. */
  onRetry?: () => void;
  /** When the backend is auth-locked, render a dedicated "sign in"
   *  CTA instead of the retry button.  Caller is responsible for
   *  routing to /login. */
  onSignIn?: () => void;
  /** When provided, shown as the italic hint at the bottom of the
   *  card ("Last good slate from 12:04pm" / "7 picks cached"). */
  secondaryHint?: string;
  /** When TRUE, suppresses the "degraded" variant so a transient
   *  blip (2 consecutive 5xx on one slow endpoint) does not steal
   *  the whole screen.  Default FALSE. */
  degradedSilent?: boolean;
  testID?: string;
};

const _label: Record<TransportState["stage"], {
  title: string;
  message: string;
  icon: Parameters<typeof EmptyState>[0]["icon"];
  variant: "empty" | "error" | "info";
}> = {
  idle: {
    // Should never render — the component short-circuits at idle.
    title: "", message: "",
    icon: undefined, variant: "info",
  },
  online: {
    title: "", message: "",
    icon: undefined, variant: "info",
  },
  offline: {
    title: "You're offline",
    message: "Reconnect to refresh. Showing your last good slate when available.",
    icon: "cloud-offline-outline",
    variant: "error",
  },
  backend_unreachable: {
    title: "Can't reach the backend",
    message: "The device is online but the server isn't answering. We'll keep trying in the background.",
    icon: "warning-outline",
    variant: "error",
  },
  degraded: {
    title: "Backend is bouncing",
    message: "The server is responding slowly or with errors. Showing your last good slate.",
    icon: "pulse-outline",
    variant: "info",
  },
  auth_locked: {
    title: "Session expired",
    message: "Please sign in again to continue.",
    icon: "lock-closed-outline",
    variant: "error",
  },
};

export const BackendHealthEmptyState: React.FC<Props> = ({
  onRetry,
  onSignIn,
  secondaryHint,
  degradedSilent = false,
  testID,
}) => {
  const [snap, setSnap] = useState<TransportState>(getTransportState);
  useEffect(() => subscribeTransport(setSnap), []);

  if (snap.stage === "online" || snap.stage === "idle") return null;
  if (snap.stage === "degraded" && degradedSilent) return null;

  const cfg = _label[snap.stage];
  const isAuth = snap.stage === "auth_locked";

  const handleRetry = isAuth
    ? undefined
    : onRetry
      ? async () => {
          try { await probeReachability(); } catch {}
          try { onRetry(); } catch {}
        }
      : undefined;

  return (
    <EmptyState
      icon={cfg.icon}
      title={cfg.title}
      message={cfg.message}
      variant={cfg.variant}
      onRetry={handleRetry}
      onCtaPress={isAuth ? onSignIn : undefined}
      ctaLabel={isAuth ? "SIGN IN" : undefined}
      secondaryHint={secondaryHint}
      testID={testID || `backend-health-${snap.stage}`}
    />
  );
};

export default BackendHealthEmptyState;
