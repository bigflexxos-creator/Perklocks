import { Stack } from "expo-router";
import * as SplashScreen from "expo-splash-screen";
import { useEffect, useState } from "react";
import { ImageBackground, StyleSheet, View } from "react-native";
import { StatusBar } from "expo-status-bar";
import { SafeAreaProvider } from "react-native-safe-area-context";
import { GestureHandlerRootView } from "react-native-gesture-handler";

import { useIconFonts } from "@/src/hooks/use-icon-fonts";
import { AuthProvider } from "@/src/contexts/AuthContext";
import { BetSlipProvider } from "@/src/contexts/BetSlipContext";
import { MLBLiveProvider } from "@/src/contexts/MLBLiveContext";
import { FiltersProvider } from "@/src/stores/useFilters";
import { ErrorBoundary } from "@/src/components/ErrorBoundary";
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

  // If the CDN is unreachable we fall through on error rather than wedging
  // the app — icons will tofu, but the app still boots.
  if ((!loaded && !error) || !cacheBustDone) return null;

  return (
    <GestureHandlerRootView style={{ flex: 1, backgroundColor: "#000" }}>
      <ErrorBoundary>
        <SafeAreaProvider>
          <AuthProvider>
            <FiltersProvider>
              <BetSlipProvider>
                <MLBLiveProvider>
                  <StatusBar style="light" />
                  {/* ── Global branded backdrop ──
                      Renders the stadium / phone-mockup composite behind every
                      route. Stack `contentStyle` is transparent so each
                      screen's content sits on top of this single shared image,
                      avoiding bundle-size duplication and giving a continuous
                      visual identity from splash → login → tabs.
                  */}
                  <ImageBackground
                    source={require("@/assets/images/brand-bg-v5.png")}
                    resizeMode="cover"
                    style={StyleSheet.absoluteFillObject}
                  >
                    <View style={styles.brandScrim} />
                  </ImageBackground>
                  <Stack
                    screenOptions={{
                      headerShown: false,
                      // Transparent so the global ImageBackground above shows
                      // through. Each screen still paints its own translucent
                      // dark scrim via its `safe` style.
                      contentStyle: { backgroundColor: "transparent" },
                    }}
                  />
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
  // Light scrim on top of brand-bg-v5.png (custom designed dark backdrop
  // with gold lock motif). Just a 10% tint to push slight depth.
  brandScrim: {
    ...StyleSheet.absoluteFillObject,
    backgroundColor: "rgba(0,0,0,0.10)",
  },
});
