import { Stack } from "expo-router";
import * as SplashScreen from "expo-splash-screen";
import { useEffect, useState } from "react";
import { StatusBar } from "expo-status-bar";
import { SafeAreaProvider } from "react-native-safe-area-context";
import { GestureHandlerRootView } from "react-native-gesture-handler";

import { useIconFonts } from "@/src/hooks/use-icon-fonts";
import { AuthProvider } from "@/src/contexts/AuthContext";
import { BetSlipProvider } from "@/src/contexts/BetSlipContext";
import { MLBLiveProvider } from "@/src/contexts/MLBLiveContext";
import { ErrorBoundary } from "@/src/components/ErrorBoundary";
import { runCacheBustIfNeeded } from "@/src/lib/cachebust";

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
  useEffect(() => {
    (async () => {
      const result = await runCacheBustIfNeeded();
      if (result.wiped) {
        // eslint-disable-next-line no-console
        console.log("[cachebust] wiped AsyncStorage —", result.reason);
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
    <GestureHandlerRootView style={{ flex: 1, backgroundColor: "#0A0A0A" }}>
      <ErrorBoundary>
        <SafeAreaProvider>
          <AuthProvider>
            <BetSlipProvider>
              <MLBLiveProvider>
                <StatusBar style="light" />
                <Stack screenOptions={{ headerShown: false, contentStyle: { backgroundColor: "#0A0A0A" } }} />
              </MLBLiveProvider>
            </BetSlipProvider>
          </AuthProvider>
        </SafeAreaProvider>
      </ErrorBoundary>
    </GestureHandlerRootView>
  );
}
