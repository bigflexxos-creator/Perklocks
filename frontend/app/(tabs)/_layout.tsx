import React, { useEffect } from "react";
import { View } from "react-native";
import { Tabs, useRouter } from "expo-router";
import { Ionicons } from "@expo/vector-icons";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { COLORS } from "@/src/theme";
import { useAuth } from "@/src/contexts/AuthContext";
import { BetSlipFab } from "@/src/components/BetSlipFab";
import { preloadPrimaryTabs } from "@/src/lib/preloadPrimaryTabs";

export default function TabsLayout() {
  const insets = useSafeAreaInsets();
  const { user, loading } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!loading && !user) router.replace("/(auth)/login");
  }, [loading, user]);

  // μ-closure P3 (2026-06): once authenticated, silently preload the
  // primary tabs' data into the SWR cache so first visits paint
  // instantly instead of showing the cold skeleton.
  useEffect(() => {
    if (!loading && user) {
      void preloadPrimaryTabs();
    }
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
    <View testID="perklocks-tabs-root" nativeID="perklocks-tabs-root" style={{ flex: 1, backgroundColor: "transparent", overflow: "hidden" }}>
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
        tabBarActiveTintColor: COLORS.goldElite,
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
      {/* HR slate now surfaced INSIDE the MLB filter view (see
          components/MLBHRBanner.tsx + app/hr.tsx full-screen detail).
          Tab is hidden from the tab bar via href:null. */}
      <Tabs.Screen
        name="hr"
        options={{
          href: null,
          title: "HR",
        }}
      />
      {/* NFL ATD slate — hidden tab route accessed via the 🏈 ATD chip
          in SportFilterBar when sport === "NFL". See app/(tabs)/atd.tsx. */}
      <Tabs.Screen
        name="atd"
        options={{
          href: null,
          title: "ATD",
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
      {/* ── My Bets (personal tracked bets + ROI) ─ 2026-07-21 ──────
          Available to every logged-in user. Server enforces
          `user_id == current_user.id` on every read via
          routes/user_bets_routes.py so users only see their own bets. */}
      <Tabs.Screen
        name="my-bets"
        options={{
          title: "MY BETS",
          tabBarIcon: ({ color, size }) => <Ionicons name="wallet" color={color} size={size} />,
          tabBarButtonTestID: "tab-my-bets",
        }}
      />
      {/* ── LAB moved off the bottom bar (2026-07-21) ──────────────
          User: "Still don't see admin user section where I see my
          personal bets" — with 8 tabs (LOCKS · ROLLOVER · PARLAY ·
          MY BETS · LAB · UNDER · PROFILE · ADMIN) the tab bar was
          overflowing on 390px devices and MY BETS was getting pushed
          off-screen. LAB and UNDER moved to href:null (accessed via
          Profile → shortcuts) so the tab bar stays clean at 6 tabs
          for admin (5 for regular user). */}
      <Tabs.Screen
        name="lab"
        options={{
          href: null,
          title: "LAB",
        }}
      />
      <Tabs.Screen
        name="under"
        options={{
          href: null,
          title: "UNDER",
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
      {/* ── ADMIN tab — visible ONLY to users with role === "admin"
          (see routes/analytics_routes.py — server enforces 403 on
          non-admin regardless of UI visibility). Regular users don't
          see this tab in the bar at all; admins see it as the last
          entry so they can jump into model/ROI analytics without
          leaving the tab layout.  2026-07-21. */}
      <Tabs.Screen
        name="admin"
        options={{
          href: (user?.role === "admin") ? "/(tabs)/admin" : null,
          title: "ADMIN",
          tabBarIcon: ({ color, size }) => <Ionicons name="shield-checkmark" color={color} size={size} />,
          tabBarButtonTestID: "tab-admin",
        }}
      />
    </Tabs>
    <BetSlipFab />
    </View>
  );
}
