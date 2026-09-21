/**
 * Universal AppState → Canonical Freshness Reconciliation
 * ═══════════════════════════════════════════════════════════════════
 * Every time the app returns from background, we fire ONE cheap
 * ``GET /api/version`` and let the ``BoardVersionHeaderMiddleware``
 * response header flow through ``noteBoardVersion`` → cache sweeper.
 *
 * This means:
 *   · If the backend committed a NEWER canonical_version while the
 *     app was in the background, the very next screen render will
 *     force-refetch every cached detail / HI / rollover / parlay row.
 *   · If nothing changed, the response is a 100 B hit and the
 *     freshness observer no-ops.
 *
 * Runs ONCE per app boot from the root ``_layout.tsx``.  Debounces to
 * at most one call every 5 s so a rapid background/foreground toggle
 * (iOS's aggressive suspend/resume) doesn't spam the backend.
 */
import { AppState, AppStateStatus } from "react-native";

let _installed = false;
let _lastPingAt = 0;

async function _pingCanonicalVersion(): Promise<void> {
  const now = Date.now();
  if (now - _lastPingAt < 5000) return; // debounce
  _lastPingAt = now;
  try {
    // Use the shared ``api.request`` path so the response flows
    // through ``noteBoardVersion`` at the transport layer — no code
    // duplication.  ``/api/version`` is a tiny always-fresh endpoint
    // that already participates in the middleware.
    const { api } = await import("@/src/lib/api");
    await api.version();
  } catch {
    // Offline / auth-less version endpoint failure is a no-op.  The
    // next screen that fetches will still detect the version change
    // via the in-transport ``noteBoardVersion`` hook.
  }
}

export function installAppStateFreshnessReconciler(): () => void {
  if (_installed) return () => undefined;
  _installed = true;
  const listener = (state: AppStateStatus) => {
    if (state === "active") {
      void _pingCanonicalVersion();
    }
  };
  const sub = AppState.addEventListener("change", listener);
  // Fire once at install so a cold boot reconciles too.
  void _pingCanonicalVersion();
  return () => {
    try { sub.remove(); } catch { /* older RN */ }
    _installed = false;
  };
}
