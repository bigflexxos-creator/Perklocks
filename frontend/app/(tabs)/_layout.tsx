import React, { useEffect } from "react";
import { View, Platform } from "react-native";
import { Tabs, useRouter } from "expo-router";
import { Ionicons } from "@expo/vector-icons";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { COLORS } from "@/src/theme";
import { useAuth } from "@/src/contexts/AuthContext";
import { BetSlipFab } from "@/src/components/BetSlipFab";

export default function TabsLayout() {
  const insets = useSafeAreaInsets();
  const { user, loading } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!loading && !user) router.replace("/(auth)/login");
  }, [loading, user]);

  // CRITICAL (2026-06-29 v24): on react-native-web, ALL tab screens
  // render into the DOM simultaneously with `display:block` and the
  // active one painted on top. `freezeOnBlur` only freezes the React
  // render tree, not the DOM — so the inactive tabs are still visible
  // through any non-100%-opaque pixel. Even with our solid backgrounds,
  // sub-pixel anti-aliasing + the Emergent preview chrome was letting
  // multiple tabs (Locks + Rollover + Parlay) all show through at once.
  // The bulletproof fix on web is `unmountOnBlur: true` — inactive tabs
  // are physically REMOVED from the DOM tree, so they literally cannot
  // bleed through anything. We keep this off on native (iOS/Android)
  // because there freezeOnBlur is enough AND we want to preserve scroll
  // position / form state across tab switches.
  const isWeb = Platform.OS === "web";

  return (
    <View style={{ flex: 1, backgroundColor: "transparent" }}>
    <Tabs
      screenOptions={{
        headerShown: false,
        // CRITICAL (2026-06-29 v19): give every tab scene a SOLID dark
        // background. We previously used `transparent` to let the
        // global PerkLocks branded ImageBackground (in app/_layout.tsx)
        // show through every tab, but on the web build this caused
        // every inactive tab to paint through the active one — user
        // saw Profile content stacked on top of Locks etc.
        // The 96 % opacity here:
        //   • Keeps the brand bg visible at the screen edges & in
        //     between cards (cards themselves are rgba surfaces),
        //   • But makes the SCENE itself opaque enough that no
        //     inactive tab can bleed through.
        // freezeOnBlur + lazy stay on for native (RN bottom-tabs),
        // and the solid scene bg covers the web build where freeze
        // semantics don't apply the same way.
        sceneStyle: { backgroundColor: "rgba(10,10,10,0.94)" },
        // freezeOnBlur is enough on native; on web we need to actually
        // unmount inactive tabs (see comment above on `isWeb`).
        freezeOnBlur: true,
        unmountOnBlur: isWeb,
        lazy: true,
        tabBarStyle: {
          // Solid (no alpha) so inactive-tab content behind the bar can't
          // bleed through the bottom strip. User report 2026-06-29:
          // "bottom row making bleed" — the 4% alpha at 0.96 was enough
          // to silhouette the Locks slate under the tab bar when Profile
          // was active.
          backgroundColor: "#0a0a0a",
          borderTopColor: COLORS.borderDefault,
          borderTopWidth: 1,
          height: 64 + insets.bottom,
          paddingBottom: insets.bottom + 6,
          paddingTop: 8,
        },
        tabBarActiveTintColor: COLORS.textPrimary,
        tabBarInactiveTintColor: COLORS.textMuted,
        tabBarLabelStyle: { fontSize: 10, fontWeight: "800", letterSpacing: 1.2 },
      }}
    >
      <Tabs.Screen
        name="index"
        options={{
          title: "LOCKS",
          tabBarIcon: ({ color, size }) => <Ionicons name="lock-closed" color={color} size={size} />,
          tabBarButtonTestID: "tab-locks",
        }}
      />
      <Tabs.Screen
        name="rollover"
        options={{
          title: "ROLLOVER",
          tabBarIcon: ({ color, size }) => <Ionicons name="flash" color={color} size={size} />,
          tabBarButtonTestID: "tab-rollover",
        }}
      />
      <Tabs.Screen
        name="parlay"
        options={{
          title: "PARLAY",
          tabBarIcon: ({ color, size }) => <Ionicons name="layers" color={color} size={size} />,
          tabBarButtonTestID: "tab-parlay",
        }}
      />
      <Tabs.Screen
        name="under"
        options={{
          title: "UNDER LOCK",
          tabBarIcon: ({ color, size }) => <Ionicons name="trending-down" color={color} size={size} />,
          tabBarButtonTestID: "tab-under-of-day",
        }}
      />
      <Tabs.Screen
        name="profile"
        options={{
          title: "PROFILE",
          tabBarIcon: ({ color, size }) => <Ionicons name="person-circle" color={color} size={size} />,
          tabBarButtonTestID: "tab-profile",
        }}
      />
    </Tabs>
    <BetSlipFab />
    </View>
  );
}
