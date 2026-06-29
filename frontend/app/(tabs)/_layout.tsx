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
        freezeOnBlur: true,
        lazy: true,
        tabBarStyle: {
          backgroundColor: "rgba(10,10,10,0.96)",
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
