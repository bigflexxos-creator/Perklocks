import React, { useEffect } from "react";
import { View, StyleSheet } from "react-native";
import { Tabs, useRouter } from "expo-router";
import { Ionicons } from "@expo/vector-icons";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { COLORS } from "@/src/theme";
import { useAuth } from "@/src/contexts/AuthContext";
import { BetSlipFab } from "@/src/components/BetSlipFab";
import { preloadPrimaryTabs } from "@/src/lib/preloadPrimaryTabs";

/**
 * Locks-Mockup 2026-08-22 §13: active tab (LOCKS) renders inside a
 * luminous gold container so the current section is unmistakably
 * highlighted. Inactive tabs render the plain icon in muted gray.
 * Icon set / labels / routes are unchanged.
 */
function TabIcon({
  name, color, focused,
}: {
  name: React.ComponentProps<typeof Ionicons>["name"];
  color: string;
  focused: boolean;
}) {
  return (
    <View style={[styles.iconWrap, focused && styles.iconWrapActive]}>
      <Ionicons name={name} color={color} size={focused ? 22 : 20} />
    </View>
  );
}

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
          backgroundColor: "#08090f",
          borderTopColor: "rgba(255,255,255,0.08)",
          borderTopWidth: 1,
          height: 68 + insets.bottom,
          paddingBottom: insets.bottom + 6,
          paddingTop: 8,
        },
        tabBarActiveTintColor: COLORS.goldElite,
        tabBarInactiveTintColor: COLORS.textMuted,
        tabBarLabelStyle: { fontSize: 10, fontWeight: "900", letterSpacing: 1.4, marginTop: 2 },
      }}
    >
      <Tabs.Screen
        name="index"
        options={{
          title: "LOCKS",
          tabBarIcon: ({ color, focused }) => (
            <TabIcon name="lock-closed" color={color} focused={focused} />
          ),
          tabBarButtonTestID: "tab-locks",
        }}
      />
      <Tabs.Screen
        name="rollover"
        options={{
          title: "ROLLOVER",
          tabBarIcon: ({ color, focused }) => (
            <TabIcon name="flash" color={color} focused={focused} />
          ),
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
          tabBarIcon: ({ color, focused }) => (
            <TabIcon name="layers" color={color} focused={focused} />
          ),
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
          tabBarIcon: ({ color, focused }) => (
            <TabIcon name="wallet" color={color} focused={focused} />
          ),
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
          title: "LAB",
          tabBarIcon: ({ color, focused }) => (
            <TabIcon name="flask" color={color} focused={focused} />
          ),
          tabBarButtonTestID: "tab-lab",
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
          tabBarIcon: ({ color, focused }) => (
            <TabIcon name="person-circle" color={color} focused={focused} />
          ),
          tabBarButtonTestID: "tab-profile",
        }}
      />
      {/* ── ADMIN tab — PERMANENTLY HIDDEN from bottom tab bar for
          all users, including admins (per user directive 2026-08).
          Admin access is available via Profile → Admin Dashboard for
          authorized users only. Route/screen preserved; only the tab
          bar entry is hidden with href: null. Server-side auth
          (routes/analytics_routes.py) still enforces admin role. */}
      <Tabs.Screen
        name="admin"
        options={{
          href: null,
          title: "ADMIN",
          tabBarButtonTestID: "tab-admin",
        }}
      />
    </Tabs>
    <BetSlipFab />
    </View>
  );
}

const styles = StyleSheet.create({
  // Locks-Mockup 2026-08-22 correction §13: BLACK elevated container
  // with a luminous gold border + soft outer glow. Interior stays true
  // black so the icon sits inside a dark "lantern", not a gold wash.
  iconWrap: {
    width: 42, height: 32, borderRadius: 12,
    alignItems: "center", justifyContent: "center",
  },
  iconWrapActive: {
    backgroundColor: "#000000",
    borderWidth: 1.4,
    borderColor: "rgba(255,215,0,0.85)",
    shadowColor: "#FFD700",
    shadowOpacity: 0.75,
    shadowRadius: 10,
    shadowOffset: { width: 0, height: 0 },
  },
});
