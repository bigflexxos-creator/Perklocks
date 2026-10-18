/**
 * P0.10 — ONE global native connectivity authority.
 * ────────────────────────────────────────────────────────────────────
 * Tracks online/offline, reachability, app active/background,
 * last online, connection type (wifi/cellular/none).  Coordinates
 * foreground refresh so screens do not each create their own
 * AppState listeners and fire request storms: subscribers get ONE
 * `foreground` signal per active transition (debounced 5 s), after a
 * cheap reachability probe against the canonical API origin.
 *
 * 2026-10-18 Perklocks Defect 3 — Native connectivity authority:
 *   Previously this module relied on AppState + navigator.onLine
 *   only.  On real devices (Expo Go, dev clients) `navigator.onLine`
 *   is often undefined / unreliable, so the device-online flag was
 *   effectively a constant `true` and transport-state transitions
 *   had to be inferred from request outcomes.  Hooking into
 *   `@react-native-community/netinfo` gives us the AUTHORITATIVE
 *   native connectivity source (Wi-Fi / cellular / none / unknown)
 *   on iOS + Android and a true Wi-Fi↔cellular handoff signal that
 *   drives the Network Change Contract.
 */
import { AppState, AppStateStatus, Platform } from "react-native";
import { buildApiUrl } from "@/src/lib/api";

// NetInfo is a lightweight native dependency.  We load it lazily so
// test environments / web preview that don't ship the native module
// never throw — `require` is wrapped in try/catch and we gracefully
// degrade to AppState + navigator.onLine when it's unavailable.
let _NetInfo: any = null;
try {
  // eslint-disable-next-line @typescript-eslint/no-var-requires, global-require
  _NetInfo = require("@react-native-community/netinfo").default
          || require("@react-native-community/netinfo");
} catch { _NetInfo = null; }

export type ConnectivityState = {
  online: boolean;
  reachable: boolean | null;   // null = unknown / not probed yet
  appState: AppStateStatus | "unknown";
  lastOnlineAt: number | null;
  lastProbeAt: number | null;
  // 2026-10-18 Perklocks Defect 3 — authoritative via NetInfo on
  // native; "unknown" without NetInfo.  Values: "wifi" | "cellular" |
  // "ethernet" | "vpn" | "bluetooth" | "wimax" | "other" | "none" |
  // "unknown".
  connectionType: string;
};

export type ConnectivityEvent =
  | "state"             // generic internal state change
  | "foreground"        // app returned to foreground (debounced)
  | "network_change";   // 2026-10-18 Perklocks Defect 3 — Wi-Fi ↔ cellular transition

type Listener = (s: ConnectivityState, event: ConnectivityEvent) => void;

const state: ConnectivityState = {
  online: true,
  reachable: null,
  appState: (AppState.currentState as AppStateStatus) || "unknown",
  lastOnlineAt: Date.now(),
  lastProbeAt: null,
  connectionType: "unknown",
};
const listeners = new Set<Listener>();
let started = false;
let lastForegroundAt = 0;
let probeInflight: Promise<boolean> | null = null;
// 2026-10-18 Perklocks Defect 3 — remember the previous connection
// type so an actual transition (wifi → cellular) emits exactly one
// `network_change` event and the probe can confirm reachability on
// the new interface.
let lastConnectionType = "unknown";

function emit(event: ConnectivityEvent) {
  listeners.forEach((l) => { try { l({ ...state }, event); } catch {} });
}

export function getConnectivity(): ConnectivityState {
  return { ...state };
}

/** Single-flight reachability probe.  Never throws.
 *  2026-10-18 Perklocks Defect 4 — carries its OWN timeout policy
 *  (6 s, no retries) and routes the outcome through the authoritative
 *  `recordOriginProbe()` on transportState.  It is isolated from the
 *  normal request pipeline: it does NOT share the inflight-dedupe map,
 *  does NOT touch the ETag store, and does NOT bump the request-level
 *  consecutive-failure streak.  A single probe timeout therefore
 *  cannot alone decide "backend_unreachable" — only 2+ consecutive
 *  probes may. */
const PROBE_TIMEOUT_MS = 6000;
export function probeReachability(): Promise<boolean> {
  if (probeInflight) return probeInflight;
  probeInflight = (async () => {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), PROBE_TIMEOUT_MS);
    let ok = false;
    try {
      const res = await fetch(buildApiUrl("/health"), {
        signal: ctrl.signal,
        // Probe MUST bypass every HTTP cache — a 200 from a stale
        // SW/edge cache would falsely claim the origin is reachable
        // during a true outage.
        cache: "no-store",
        headers: {
          "Cache-Control": "no-cache, no-store, must-revalidate",
          "Pragma": "no-cache",
          "Accept": "application/json",
        },
      });
      ok = res.status < 500;
      state.reachable = ok;
      state.online = ok || state.online;
      if (ok) state.lastOnlineAt = Date.now();
      return ok;
    } catch {
      state.reachable = false;
      return false;
    } finally {
      clearTimeout(t);
      state.lastProbeAt = Date.now();
      probeInflight = null;
      // Report authoritative probe outcome to transport state
      // (dynamic import to avoid cycle with transportState.ts).
      try {
        // eslint-disable-next-line @typescript-eslint/no-var-requires
        const ts = require("@/src/lib/transportState");
        if (ts && typeof ts.recordOriginProbe === "function") {
          ts.recordOriginProbe(ok);
        }
      } catch { /* non-fatal */ }
      emit("state");
    }
  })();
  return probeInflight;
}

export function markOnline(): void {
  if (!state.online) { state.online = true; emit("state"); }
  state.lastOnlineAt = Date.now();
  state.reachable = true;
}

export function markOffline(): void {
  if (state.online) { state.online = false; state.reachable = false; emit("state"); }
}

/** 2026-10-18 Perklocks Defect 3 — translate NetInfo state into our
 *  connectivity snapshot.  Called by the subscriber below and the
 *  initial fetch() on start.  Pure state mutation + emit; never throws. */
function _applyNetInfoState(ni: any): void {
  if (!ni || typeof ni !== "object") return;
  // NetInfo values: isConnected (bool|null), isInternetReachable
  // (bool|null), type ("wifi"|"cellular"|"none"|"unknown"|...).
  const type = typeof ni.type === "string" ? String(ni.type).toLowerCase() : "unknown";
  const prevType = lastConnectionType;
  const connected = ni.isConnected === true;
  const internetReachable = ni.isInternetReachable;
  // Device-online truth: NetInfo's `isConnected` is the LINK-LAYER
  // signal.  We trust it as authoritative — if the OS reports no
  // interface at all we are offline.  `isInternetReachable` is a
  // DNS/ping result and may be `null` on some Android builds; when
  // null we fall back to `isConnected`.
  const nextOnline = connected && (internetReachable === null || internetReachable === true);
  const prevOnline = state.online;
  state.connectionType = type;
  state.online = nextOnline;
  if (nextOnline) {
    state.lastOnlineAt = Date.now();
  }
  // On transition from offline → online, clear `reachable` so the
  // next request / probe decides fresh.  Do NOT fabricate a
  // positive here — the probe is the only authority for backend
  // reachability.
  if (!prevOnline && nextOnline) {
    state.reachable = null;
  }
  if (prevOnline && !nextOnline) {
    state.reachable = false;
  }
  // Did the link-layer change (Wi-Fi ↔ cellular)?  Emit a network
  // change event so visible subscribers can run the Network Change
  // Contract (reachability probe, then at most one coalesced
  // refresh — see consumers for the policy).
  const linkChanged = type !== prevType && prevType !== "unknown" && type !== "unknown";
  lastConnectionType = type;
  emit("state");
  if (linkChanged && nextOnline) {
    // Fire a probe to confirm the new interface actually reaches
    // the backend before notifying consumers.  Probe success is
    // routed through the transportState authority so
    // `backend_unreachable` can't be inferred from one bad hop.
    probeReachability().finally(() => { emit("network_change"); });
  }
}

export function startConnectivity(): void {
  if (started) return;
  started = true;
  // 1. AppState listener — unchanged foreground policy.
  AppState.addEventListener("change", async (next) => {
    state.appState = next;
    emit("state");
    if (next !== "active") return;
    const now = Date.now();
    if (now - lastForegroundAt < 5000) return;   // debounce rapid toggles
    lastForegroundAt = now;
    await probeReachability();
    // ONE coordinated foreground signal; screens refresh visible stale
    // data once and optional data gradually.
    emit("foreground");
  });
  // 2. Web — hook the native `online`/`offline` events for parity.
  if (Platform.OS === "web" && typeof window !== "undefined" && window.addEventListener) {
    const nav: any = (globalThis as any).navigator;
    if (nav && typeof nav.onLine === "boolean") state.online = nav.onLine;
    window.addEventListener("online", () => { markOnline(); emit("foreground"); });
    window.addEventListener("offline", () => markOffline());
  }
  // 3. 2026-10-18 Perklocks Defect 3 — NetInfo on native.  Lazily
  //    required at module load; if the dependency is missing (web
  //    preview, test env) we degrade silently.
  if (_NetInfo && typeof _NetInfo.addEventListener === "function") {
    try {
      // Seed with current state so the first subscriber sees reality
      // instead of the default `online=true`.
      if (typeof _NetInfo.fetch === "function") {
        _NetInfo.fetch()
          .then((s: any) => { try { _applyNetInfoState(s); } catch {} })
          .catch(() => undefined);
      }
      _NetInfo.addEventListener((s: any) => {
        try { _applyNetInfoState(s); } catch {}
      });
    } catch { /* native module absent — ignore */ }
  }
}

export function subscribeConnectivity(l: Listener): () => void {
  startConnectivity();
  listeners.add(l);
  return () => { listeners.delete(l); };
}

/** TEST-ONLY — reset module-scope state so unit tests start clean. */
export function _resetConnectivityForTests(): void {
  state.online = true;
  state.reachable = null;
  state.appState = (AppState.currentState as AppStateStatus) || "unknown";
  state.lastOnlineAt = Date.now();
  state.lastProbeAt = null;
  state.connectionType = "unknown";
  lastConnectionType = "unknown";
  lastForegroundAt = 0;
  probeInflight = null;
  started = false;
  listeners.clear();
}

/** TEST-ONLY — inject a synthetic NetInfo-shaped payload so unit
 *  tests can verify the Wi-Fi ↔ cellular transition contract without
 *  booting a real device. */
export function _applyNetInfoStateForTests(ni: any): void {
  _applyNetInfoState(ni);
}
