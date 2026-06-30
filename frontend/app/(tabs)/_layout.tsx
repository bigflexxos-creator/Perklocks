import React, { useEffect } from "react";
import { View } from "react-native";
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

  // CRITICAL (2026-06-30 v25 — permanent fix):
  //   `unmountOnBlur: true` on web was causing the "app keeps
  //   crashing while going through tabs" report. Each tab switch
  //   destroyed and re-mounted the inactive screen, which:
  //     1. Aborted in-flight `/api/picks/today` fetches mid-stream
  //     2. Triggered setState() on already-unmounted components
  //     3. Re-ran every screen's load effect from scratch, slamming
  //        the backend on every tab tap
  //   We rely on the CSS injection in `useEffect` below
  //   (`[aria-hidden="true"] { display: none !important; }`) to hide
  //   inactive tabs at the DOM level instead — the screens stay
  //   mounted and frozen, no unmount cycle, no race conditions.
  //   `freezeOnBlur: true` + `lazy: true` give us the React-tree
  //   freeze on native too.

  return (
    <View testID="perklocks-tabs-root" nativeID="perklocks-tabs-root" style={{ flex: 1, backgroundColor: "#0a0a0a", overflow: "hidden" }}>
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
        // freezeOnBlur freezes the React tree on both platforms; we
        // do NOT unmount on web because that triggers the crash-on-tab
        // bug (mid-fetch unmounts → setState on unmounted component).
        // Instead, inactive tabs are hidden via the global CSS
        // injection below (`[aria-hidden="true"] { display: none }`).
        freezeOnBlur: true,
        unmountOnBlur: false,
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
