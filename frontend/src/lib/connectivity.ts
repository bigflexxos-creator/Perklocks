/**
 * P0.10 — ONE global native connectivity authority.
 *
 * Tracks online/offline, reachability, app active/background, last online.
 * Coordinates foreground refresh so screens do not each create their own
 * AppState listeners and fire request storms: subscribers get ONE
 * `foreground` signal per active transition (debounced 5 s), after a cheap
 * reachability probe against the canonical API origin.
 *
 * No new native dependency: uses AppState (RN core) + `navigator.onLine`
 * where available + a tiny HEAD/GET probe to `/api/health`.
 */
import { AppState, AppStateStatus, Platform } from "react-native";
import { buildApiUrl } from "@/src/lib/api";

export type ConnectivityState = {
  online: boolean;
  reachable: boolean | null;   // null = unknown / not probed yet
  appState: AppStateStatus | "unknown";
  lastOnlineAt: number | null;
  lastProbeAt: number | null;
  connectionType: string;      // "unknown" without NetInfo; kept for parity
};

type Listener = (s: ConnectivityState, event: "state" | "foreground") => void;

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

function emit(event: "state" | "foreground") {
  listeners.forEach((l) => { try { l({ ...state }, event); } catch {} });
}

export function getConnectivity(): ConnectivityState {
  return { ...state };
}

/** Single-flight reachability probe. Never throws. */
export function probeReachability(): Promise<boolean> {
  if (probeInflight) return probeInflight;
  probeInflight = (async () => {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 6000);
    try {
      const res = await fetch(buildApiUrl("/health"), { signal: ctrl.signal });
      const ok = res.status < 500;
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

export function startConnectivity(): void {
  if (started) return;
  started = true;
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
  if (Platform.OS === "web" && typeof window !== "undefined" && window.addEventListener) {
    const nav: any = (globalThis as any).navigator;
    if (nav && typeof nav.onLine === "boolean") state.online = nav.onLine;
    window.addEventListener("online", () => { markOnline(); emit("foreground"); });
    window.addEventListener("offline", () => markOffline());
  }
}

export function subscribeConnectivity(l: Listener): () => void {
  startConnectivity();
  listeners.add(l);
  return () => { listeners.delete(l); };
}
