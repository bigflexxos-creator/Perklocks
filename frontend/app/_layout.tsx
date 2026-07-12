import { Stack } from "expo-router";
import * as SplashScreen from "expo-splash-screen";
import { useEffect, useState } from "react";
import { Image, Platform, StyleSheet, View } from "react-native";
import { Asset } from "expo-asset";
import { StatusBar } from "expo-status-bar";
import { SafeAreaProvider } from "react-native-safe-area-context";
import { GestureHandlerRootView } from "react-native-gesture-handler";
import { ThemeProvider, DefaultTheme } from "@react-navigation/native";

// React Navigation theme that paints all card backgrounds transparent so
// the global PerkLocks backdrop (set on <body> on web, rendered as <Image>
// on native) shows through every Stack & Tab screen.
const TransparentTheme = {
  ...DefaultTheme,
  colors: {
    ...DefaultTheme.colors,
    background: "transparent",
    card: "transparent",
  },
};

import { useIconFonts } from "@/src/hooks/use-icon-fonts";
import { AuthProvider } from "@/src/contexts/AuthContext";
import { BetSlipProvider } from "@/src/contexts/BetSlipContext";
import { MLBLiveProvider } from "@/src/contexts/MLBLiveContext";
import { FiltersProvider } from "@/src/stores/useFilters";
import { ErrorBoundary } from "@/src/components/ErrorBoundary";
import { reportError } from "@/src/lib/telemetry";
import {
  runCacheBustIfNeeded,
  runBackendCacheBustIfNeeded,
} from "@/src/lib/cachebust";
import { api } from "@/src/lib/api";

// Keep the native splash visible from cold start until icon fonts register.
// Required because @expo/vector-icons' componentDidMount fallback fires
// Font.loadAsync against a broken vendor path if any <Icon> mounts before
// the family is registered — which throws on Android Expo Go.
SplashScreen.preventAutoHideAsync();

export default function RootLayout() {
  const [loaded, error] = useIconFonts();
  const [cacheBustDone, setCacheBustDone] = useState(false);
  const [bgUri, setBgUri] = useState<string | null>(null);

  // Resolve the brand background to a URI we can use as a CSS background
  // on web (RN-Web's <Image> mis-sets opacity:0 inside React Navigation's
  // nested stacking contexts and the bg never appears).
  useEffect(() => {
    (async () => {
      try {
        const asset = Asset.fromModule(require("@/assets/images/brand-bg-v7.jpg"));
        await asset.downloadAsync();
        setBgUri(asset.localUri || asset.uri);
      } catch (e) {
        console.warn("[bg] failed to resolve asset", e);
      }
    })();
  }, []);

  // On web, paint the bg on <html> (not body) so it doesn't repaint on every
  // scroll inside the app's ScrollViews. We deliberately use `scroll`
  // (default) instead of `fixed` — `fixed` on mobile Safari forces a full
  // viewport repaint per scroll frame and tanks scroll perf catastrophically.
  // `<html>` is large enough to not need parallax fixing.
  useEffect(() => {
    if (Platform.OS !== "web" || !bgUri) return;
    const html = document.documentElement;
    const prev = html.style.cssText;
    html.style.backgroundImage = `url("${bgUri}")`;
    html.style.backgroundSize = "cover";
    html.style.backgroundPosition = "center top";
    html.style.backgroundRepeat = "no-repeat";
    html.style.backgroundColor = "#08090f";
    // Keep body fully transparent so the html paint shows through.
    document.body.style.backgroundColor = "transparent";
    return () => {
      html.style.cssText = prev;
    };
  }, [bgUri]);

  // CRITICAL (2026-06-29 v26): React Navigation v7's bottom-tab navigator on
  // web marks INACTIVE tab scenes with `aria-hidden="true"` but does NOT
  // visually hide them — they stay `display: flex` and `visibility: visible`,
  // so they paint THROUGH the active scene (user reported "screens jumble
  // together" / "bleeding"). `freezeOnBlur`, `unmountOnBlur` (removed in v7),
  // and opaque sceneStyle backgrounds all failed because RN-Web's stacking
  // contexts let translucent rgba pixels bleed regardless.
  //
  // Surgical fix: inject ONE CSS rule that hides any element with
  // `aria-hidden="true"` ONLY inside the tabs navigator (scoped via the
  // `#perklocks-tabs-root` nativeID set on the wrapping View in
  // `app/(tabs)/_layout.tsx`). We deliberately do NOT touch aria-hidden
  // elsewhere, because the Stack navigator also marks the inactive
  // `(tabs)` route group as aria-hidden during `(auth)/login →
  // (tabs)/index` transitions — a broader rule would keep the tabs DOM
  // permanently hidden after login (regression observed 2026-06-29).
  useEffect(() => {
    if (Platform.OS !== "web") return;
    const styleEl = document.createElement("style");
    styleEl.setAttribute("data-perklocks-tab-fix", "v26");
    styleEl.innerHTML = `
      /* Hide inactive tab scenes (react-navigation v7 web) so they cannot
         bleed through the active scene's transparent layers. Scoped to
         the tabs root so we don't break Stack-level aria-hidden during
         auth → tabs transitions. */
      #perklocks-tabs-root [aria-hidden="true"] {
        display: none !important;
      }
    `;
    document.head.appendChild(styleEl);
    return () => {
      styleEl.remove();
    };
  }, []);

  // Run the cache buster BEFORE any provider mounts so the BetSlipContext
  // hydrates from a clean slate when a data-version bump occurs. This is
  // what kills the "Marozsan 41-6" / other stale-pick artifacts that get
  // pinned into AsyncStorage when picks shipped with bad explanations.
  //
  // Layer 3: client-baked version check (`APP_DATA_VERSION`).
  // Layer 2: backend-version check via `/api/version` — phones auto-wipe
  // whenever the server ships a new DATA_VERSION.
  useEffect(() => {
    (async () => {
      const clientResult = await runCacheBustIfNeeded();
      if (clientResult.wiped) {
        // eslint-disable-next-line no-console
        console.log("[cachebust] L3 wiped AsyncStorage —", clientResult.reason);
      }
      const backendResult = await runBackendCacheBustIfNeeded(
        () => api.version().then((r) => r.data_version).catch(() => null),
      );
      if (backendResult.wiped) {
        // eslint-disable-next-line no-console
        console.log("[cachebust] L2 wiped AsyncStorage —", backendResult.reason);
      }
      setCacheBustDone(true);
    })();
  }, []);

  useEffect(() => {
    if ((loaded || error) && cacheBustDone) {
      SplashScreen.hideAsync();
    }
  }, [loaded, error, cacheBustDone]);

  // Global unhandled-error trap — captures errors that escape React's
  // render tree (async setTimeout callbacks, unhandled promise rejects).
  // Milestone 1.1 stability layer.
  useEffect(() => {
    if (typeof globalThis === "undefined") return;
    const g = globalThis as any;
    const orig = g.ErrorUtils?.getGlobalHandler?.();
    g.ErrorUtils?.setGlobalHandler?.((err: Error, isFatal?: boolean) => {
      try {
        void reportError(err, {
          component: "GlobalHandler",
          extra: { isFatal: !!isFatal, kind: "js-error" },
        });
      } catch { /* never rethrow */ }
      if (orig) orig(err, isFatal);
    });
    const onUnhandled = (ev: any) => {
      try {
        const reason = ev?.reason || ev;
        void reportError(reason instanceof Error ? reason : new Error(String(reason)), {
          component: "unhandledrejection",
          extra: { kind: "promise-rejection" },
        });
      } catch { /* never rethrow */ }
    };
    if (typeof (globalThis as any).addEventListener === "function") {
      (globalThis as any).addEventListener("unhandledrejection", onUnhandled);
    }
    return () => {
      if (typeof (globalThis as any).removeEventListener === "function") {
        (globalThis as any).removeEventListener("unhandledrejection", onUnhandled);
      }
    };
  }, []);

  // If the CDN is unreachable we fall through on error rather than wedging
  // the app — icons will tofu, but the app still boots.
  if ((!loaded && !error) || !cacheBustDone) return null;

  return (
    <GestureHandlerRootView style={{ flex: 1 }}>
      <ErrorBoundary boundary="RootLayout">
        <SafeAreaProvider style={{ backgroundColor: "transparent" }}>
          {/* ── Global branded backdrop ──
              On web the bg is painted on <body> via useEffect (see above)
              so it can never be covered by React Navigation's nested
              stacking contexts. On native we render a fixed Image. */}
          {Platform.OS !== "web" && bgUri && (
            <Image
              source={{ uri: bgUri }}
              resizeMode="cover"
              style={styles.bgImage}
              fadeDuration={0}
            />
          )}
          <View style={styles.brandScrim} pointerEvents="none" />
          <AuthProvider>
            <FiltersProvider>
              <BetSlipProvider>
                <MLBLiveProvider>
                  <StatusBar style="light" />
                  <ThemeProvider value={TransparentTheme}>
                    <Stack
                      screenOptions={{
                        headerShown: false,
                        contentStyle: { backgroundColor: "transparent" },
                      }}
                    />
                  </ThemeProvider>
                </MLBLiveProvider>
              </BetSlipProvider>
            </FiltersProvider>
          </AuthProvider>
        </SafeAreaProvider>
      </ErrorBoundary>
    </GestureHandlerRootView>
  );
}

const styles = StyleSheet.create({
  bgImage: {
    position: "absolute",
    top: 0, left: 0, right: 0, bottom: 0,
    width: "100%",
    height: "100%",
    zIndex: 0,
    opacity: 1,
  },
  brandScrim: {
    ...StyleSheet.absoluteFillObject,
    backgroundColor: "rgba(0,0,0,0.30)",
    zIndex: 1,
  },
});
