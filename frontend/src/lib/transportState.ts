/**
 * Commit C — PERKLOCKS Transport State Authority (frontend).
 *
 * ONE observable surface for the live health of outbound API requests.
 * Combines native connectivity (online / reachable) with request-level
 * telemetry (inflight count, last outcome, consecutive-failure count,
 * degraded flag) so screens can render honest "Backend Unreachable" or
 * "Backend Degraded" empty-states instead of blank "No locks" cards.
 *
 * Design contract (surgical, additive):
 *   • NEVER synthesizes outcomes.  Every `recordRequestOk / Fail`
 *     call is driven by a real `_fetchWithTimeout` resolution in
 *     `src/lib/api.ts`.  No fake telemetry — mirrors backend HONEST
 *     ZERO policy.
 *   • NEVER mutates Lock Score, canonical epoch, auth, ETag, or
 *     publication state.  Pure observability + presentation hint.
 *   • Degraded ≠ Offline: a backend returning 5xx when the network
 *     is fine is "backend_unreachable" / "backend_degraded"; we do
 *     NOT falsely claim the device is offline.
 *   • Subscribers get a single state snapshot per real change (no
 *     tight-loop emits).  Debounced listener delivery is unnecessary
 *     because the only producers are request lifecycle hooks.
 */
import { useEffect, useState } from "react";
import { ErrorKind, ErrorKindT } from "@/src/lib/errorTaxonomy";
import {
  getConnectivity,
  subscribeConnectivity,
} from "@/src/lib/connectivity";

export type TransportOutcome =
  | "ok"
  | "timeout"
  | "http_5xx"
  | "http_429"
  | "offline"
  | "auth"
  | "parse"
  | "aborted"
  | "unknown";

export type TransportStage =
  | "online"              // reachable + recent ok, zero consecutive fails
  | "degraded"            // reachable but ≥2 consecutive non-ok (5xx/timeout)
  | "backend_unreachable" // online, probe failed OR ≥3 consecutive 5xx/timeout
  | "offline"             // device-level offline (navigator / probe)
  | "auth_locked"         // last outcome was definitive auth failure
  | "idle";               // no requests observed yet (cold boot)

export type TransportState = {
  stage: TransportStage;
  online: boolean;
  reachable: boolean | null;
  // ── 2026-10-18 Perklocks Defect 1 — separate concepts ──────────────
  // `deviceOnline` is written ONLY by the native connectivity
  // authority (NetInfo / navigator).  A bare request failure may NOT
  // flip it.  Mirrors `online` for legacy readers but is explicitly
  // authoritative.
  deviceOnline: boolean;
  // `originReachable` is written ONLY by `probeReachability()`
  // (health probe) or a succesful request.  Individual request
  // timeouts do NOT flip it.
  originReachable: boolean | null;
  inflight: number;
  lastOutcome: TransportOutcome;
  lastOutcomeAt: number | null;
  lastOkAt: number | null;
  // Separate failure streaks per source so a slow optional endpoint
  // cannot inflate the probe streak and vice-versa.
  consecutiveFailures: number;              // legacy aggregate (requests)
  consecutiveRequestFailures: number;       // requests only
  consecutiveOriginProbeFailures: number;   // probes only
  degraded: boolean;
  // Monotone counter — bumps on EVERY recorded transition so React
  // consumers using `useSyncExternalStore` style memoization have a
  // stable "revision" for cache busting.
  revision: number;
};

type Listener = (s: TransportState) => void;

const state: TransportState = {
  stage: "idle",
  online: true,
  reachable: null,
  deviceOnline: true,
  originReachable: null,
  inflight: 0,
  lastOutcome: "unknown",
  lastOutcomeAt: null,
  lastOkAt: null,
  consecutiveFailures: 0,
  consecutiveRequestFailures: 0,
  consecutiveOriginProbeFailures: 0,
  degraded: false,
  revision: 0,
};
const listeners = new Set<Listener>();

function _snapshot(): TransportState {
  return { ...state };
}

function _emit(): void {
  state.revision += 1;
  const snap = _snapshot();
  listeners.forEach((l) => {
    try { l(snap); } catch { /* observer failures are non-fatal */ }
  });
}

function _computeStage(): TransportStage {
  if (state.lastOutcome === "auth")                       return "auth_locked";
  // Device-offline requires authoritative connectivity signal.
  if (state.deviceOnline === false || state.online === false)
                                                           return "offline";
  // Backend-unreachable requires EITHER two consecutive health
  // probes failing OR 3+ consecutive request failures (defect 1 fix:
  // single timeout cannot flip this any more).
  if (state.originReachable === false)                    return "backend_unreachable";
  if (state.consecutiveRequestFailures >= 3 &&
      (state.lastOutcome === "http_5xx" ||
       state.lastOutcome === "timeout"))                  return "backend_unreachable";
  if (state.consecutiveRequestFailures >= 2)              return "degraded";
  if (state.lastOkAt !== null)                            return "online";
  return "idle";
}

/** Public read-only accessor. */
export function getTransportState(): TransportState {
  return _snapshot();
}

/** Subscribe to transitions.  Returns an unsubscribe fn. */
export function subscribeTransport(l: Listener): () => void {
  listeners.add(l);
  // Deliver current snapshot immediately so late subscribers do not
  // have to wait for the next producer event to render.
  try { l(_snapshot()); } catch { /* non-fatal */ }
  return () => { listeners.delete(l); };
}

/** React hook — returns the live TransportState snapshot and
 *  re-renders the caller on every transition.  Pure observability;
 *  mirrors the plain subscribeTransport contract. */
export function useTransportState(): TransportState {
  const [snap, setSnap] = useState<TransportState>(getTransportState);
  useEffect(() => subscribeTransport(setSnap), []);
  return snap;
}

/** Convenience predicate — TRUE when the backend / network is in a
 *  state that should preempt a domain "no results" render with the
 *  honest BackendHealthEmptyState card. */
export function isTransportUnhealthy(s: TransportState): boolean {
  return s.stage !== "online" && s.stage !== "idle";
}

/** Called by `_fetchWithTimeout` immediately before issuing fetch. */
export function recordRequestStart(): void {
  state.inflight += 1;
  _emit();
}

/** Called when a request completes successfully (any 2xx/3xx + a
 *  304-cached result).  Clears the consecutive-failure counter and
 *  advances the "last-good" marker. */
export function recordRequestOk(): void {
  if (state.inflight > 0) state.inflight -= 1;
  const now = Date.now();
  state.lastOutcome = "ok";
  state.lastOutcomeAt = now;
  state.lastOkAt = now;
  state.consecutiveFailures = 0;
  state.consecutiveRequestFailures = 0;
  state.consecutiveOriginProbeFailures = 0;
  state.degraded = false;
  state.online = true;
  state.deviceOnline = true;
  state.reachable = true;
  state.originReachable = true;
  state.stage = _computeStage();
  _emit();
}

/** Called when a request fails (any 4xx/5xx retry-exhausted, or
 *  network/timeout).  `kind` is the ErrorTaxonomy classification.
 *
 *  2026-10-18 Perklocks Defects 1 & 2 fix:
 *  INDIVIDUAL request failures NEVER flip `deviceOnline` or
 *  `originReachable` — those are owned by the native connectivity
 *  authority and the single-flight `probeReachability()` respectively.
 *  A single slow endpoint can therefore NO LONGER mark the whole
 *  backend unreachable or the device offline. */
export function recordRequestFail(kind: ErrorKindT | string): void {
  if (state.inflight > 0) state.inflight -= 1;
  // Caller-cancelled requests are NOT real failures — they must
  // never regress stage or increment the consecutive counter.
  if (kind === ErrorKind.ABORTED_SUPERSEDED) {
    state.lastOutcome = "aborted";
    state.lastOutcomeAt = Date.now();
    state.stage = _computeStage();
    _emit();
    return;
  }
  let outcome: TransportOutcome = "unknown";
  switch (kind) {
    case ErrorKind.NETWORK_OFFLINE:
      // Only trust the authority — if the taxonomy upgraded this
      // from navigator.onLine === false we honour it; otherwise
      // (we already classify bare fetch failures as
      // NETWORK_TRANSPORT_FAILURE) the fail is a request-level event.
      outcome = "offline";
      // Deliberately NOT flipping state.online here — NetInfo /
      // connectivity authority owns the device-online flag.
      break;
    case ErrorKind.NETWORK_TRANSPORT_FAILURE:
    case ErrorKind.DNS_TLS:
      // Request-level transport failure.  Count toward the request
      // streak but DO NOT mark origin unreachable from a single hit —
      // only the authoritative health probe may do that.
      outcome = "unknown";
      break;
    case ErrorKind.TIMEOUT:
      outcome = "timeout";
      // Deliberately NOT setting reachable=false — one slow endpoint
      // is not grounds for "backend_unreachable".
      break;
    case ErrorKind.HTTP_5XX:
      outcome = "http_5xx";
      state.reachable = true;  // backend answered, just badly
      state.originReachable = true;
      break;
    case ErrorKind.HTTP_429:
      outcome = "http_429";
      state.reachable = true;
      state.originReachable = true;
      break;
    case ErrorKind.AUTH_FAILURE:
      outcome = "auth";
      break;
    case ErrorKind.JSON_PARSE:
      outcome = "parse";
      state.reachable = true;
      state.originReachable = true;
      break;
    default:
      outcome = "unknown";
      break;
  }
  // AUTH_FAILURE and 429 are transport-neutral — don't inflate the
  // consecutive-fail streak used to decide "degraded".
  const bumpStreak = outcome === "offline" || outcome === "timeout" ||
                     outcome === "http_5xx" || outcome === "parse" ||
                     outcome === "unknown";
  if (bumpStreak) {
    state.consecutiveFailures += 1;
    state.consecutiveRequestFailures += 1;
  }
  state.lastOutcome = outcome;
  state.lastOutcomeAt = Date.now();
  state.degraded = state.consecutiveRequestFailures >= 2;
  state.stage = _computeStage();
  _emit();
}

/** 2026-10-18 Perklocks Defect 4 — authoritative probe result.
 *  Called by `probeReachability()` with the true outcome.  This is
 *  the ONLY path that may transition `originReachable` to false
 *  (and therefore the stage to `backend_unreachable`).  Normal
 *  request failures route through `recordRequestFail` and never
 *  touch origin reachability. */
export function recordOriginProbe(ok: boolean): void {
  if (ok) {
    state.originReachable = true;
    state.reachable = true;
    state.consecutiveOriginProbeFailures = 0;
  } else {
    state.consecutiveOriginProbeFailures += 1;
    // Only mark origin unreachable after two consecutive probe
    // failures — one 6s probe hiccup is not enough to blank screens.
    if (state.consecutiveOriginProbeFailures >= 2) {
      state.originReachable = false;
      state.reachable = false;
    }
  }
  state.stage = _computeStage();
  _emit();
}

/** Resets consecutive-failure state (used by connectivity's
 *  foreground probe when the device returns to active). */
export function recordExternalRecovery(): void {
  if (state.consecutiveFailures === 0 && !state.degraded) return;
  state.consecutiveFailures = 0;
  state.degraded = false;
  state.stage = _computeStage();
  _emit();
}

/** TEST ONLY — reset the module-scope state so unit tests start clean. */
export function _resetTransportStateForTests(): void {
  state.stage = "idle";
  state.online = true;
  state.deviceOnline = true;
  state.reachable = null;
  state.originReachable = null;
  state.inflight = 0;
  state.lastOutcome = "unknown";
  state.lastOutcomeAt = null;
  state.lastOkAt = null;
  state.consecutiveFailures = 0;
  state.consecutiveRequestFailures = 0;
  state.consecutiveOriginProbeFailures = 0;
  state.degraded = false;
  state.revision = 0;
}

// ── Connectivity bridge ────────────────────────────────────────────
// Mirror the ONE global connectivity authority: when the device goes
// offline/online or the foreground probe lands, propagate into the
// transport state's `online`/`reachable` fields WITHOUT fabricating
// request outcomes.  We do NOT emit synthetic ok/fail — those still
// come exclusively from real request lifecycle events.
let _bridgeInstalled = false;
function _installConnectivityBridge(): void {
  if (_bridgeInstalled) return;
  _bridgeInstalled = true;
  try {
    subscribeConnectivity((c, event) => {
      const prev = { online: state.online, reachable: state.reachable };
      // Only the connectivity authority may flip deviceOnline.
      state.online = c.online;
      state.deviceOnline = c.online;
      if (c.reachable !== null) {
        state.reachable = c.reachable;
        state.originReachable = c.reachable;
      }
      if (event === "foreground" && c.reachable === true) {
        // Fresh probe success → clear streaks so the next real request
        // starts from a clean slate.
        state.consecutiveFailures = 0;
        state.consecutiveRequestFailures = 0;
        state.consecutiveOriginProbeFailures = 0;
        state.degraded = false;
      }
      if (prev.online !== state.online ||
          prev.reachable !== state.reachable ||
          event === "foreground") {
        state.stage = _computeStage();
        _emit();
      }
    });
    // Seed from current connectivity snapshot without emitting a
    // producer event (initial state already captures defaults).
    const c = getConnectivity();
    state.online = c.online;
    state.reachable = c.reachable;
    state.stage = _computeStage();
  } catch { /* native-only APIs may be absent in some test envs */ }
}
_installConnectivityBridge();
