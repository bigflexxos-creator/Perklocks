/**
 * Stale-version banner (Layer 2 of the 3-layer cache-bust system).
 *
 * Sits at the top of the Locks tab. On mount + on every screen focus it
 * fetches `/api/version`. If the backend's `data_version` doesn't match the
 * one we last persisted, a yellow banner appears prompting the user to tap
 * to refresh. Tapping wipes the known AsyncStorage caches and reloads.
 *
 * On launch the same check runs invisibly in `_layout.tsx` (so the wipe
 * happens before any provider hydrates). This banner exists for the case
 * where the user has the app OPEN when a backend deploy ships — we want
 * them to see a clear, dismissible signal instead of mystery stale data.
 */
import React, { useCallback, useEffect, useRef, useState } from "react";
import { Platform, Pressable, StyleSheet, Text, View } from "react-native";
import { Ionicons } from "@expo/vector-icons";
import { COLORS } from "@/src/theme";
import { api } from "@/src/lib/api";
import {
  forceClearAllCaches,
  getStoredBackendVersion,
} from "@/src/lib/cachebust";

interface Props {
  /** Called after the user taps "Refresh now" — caller should re-hydrate
   *  any in-memory pick lists. */
  onRefresh?: () => void;
}

export function StaleVersionBanner({ onRefresh }: Props) {
  const [staleBackendVersion, setStaleBackendVersion] = useState<string | null>(null);
  const [dismissed, setDismissed] = useState(false);
  const lastCheckRef = useRef<number>(0);

  const check = useCallback(async () => {
    // Throttle: don't hit /api/version more than once every 20 s.
    const now = Date.now();
    if (now - lastCheckRef.current < 20_000) return;
    lastCheckRef.current = now;
    try {
      const [serverVersion, storedVersion] = await Promise.all([
        api.version().then((r) => r.data_version).catch(() => null),
        getStoredBackendVersion(),
      ]);
      if (!serverVersion) return;
      if (storedVersion && serverVersion !== storedVersion) {
        setStaleBackendVersion(serverVersion);
      } else {
        setStaleBackendVersion(null);
      }
    } catch {
      // Silent — banner is informational, not critical.
    }
  }, []);

  useEffect(() => { check(); }, [check]);

  // Re-check whenever this component is re-mounted (i.e. tab refocused).

  const onTapRefresh = useCallback(async () => {
    await forceClearAllCaches();
    setStaleBackendVersion(null);
    onRefresh?.();
    // Web: full reload yanks fresh JS too. RN: AsyncStorage wipe + in-memory
    // refetch is enough; the bundle itself is rebundled by Publish.
    if (Platform.OS === "web") {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const w: any = (typeof window !== "undefined" ? window : null);
      if (w?.location?.reload) w.location.reload();
    }
  }, [onRefresh]);

  if (!staleBackendVersion || dismissed) return null;

  return (
    <View style={styles.bar} testID="stale-version-banner">
      <Ionicons name="alert-circle" size={18} color="#FFB300" />
      <View style={{ flex: 1 }}>
        <Text style={styles.title}>NEW PICKS DATA AVAILABLE</Text>
        <Text style={styles.sub}>Your app is showing cached data — tap to refresh</Text>
      </View>
      <Pressable
        onPress={onTapRefresh}
        style={styles.refreshBtn}
        testID="stale-version-refresh-btn"
        hitSlop={8}
      >
        <Text style={styles.refreshTxt}>REFRESH</Text>
      </Pressable>
      <Pressable
        onPress={() => setDismissed(true)}
        style={styles.closeBtn}
        testID="stale-version-dismiss-btn"
        hitSlop={8}
      >
        <Ionicons name="close" size={16} color={COLORS.textMuted} />
      </Pressable>
    </View>
  );
}

const styles = StyleSheet.create({
  bar: {
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
    paddingHorizontal: 14,
    paddingVertical: 10,
    marginHorizontal: 14,
    marginTop: 8,
    borderRadius: 12,
    borderWidth: 1,
    borderColor: "rgba(255,179,0,0.45)",
    backgroundColor: "rgba(255,179,0,0.08)",
  },
  title: {
    color: "#FFB300",
    fontSize: 11,
    fontWeight: "900",
    letterSpacing: 1.2,
  },
  sub: {
    color: COLORS.textSecondary,
    fontSize: 11,
    marginTop: 2,
  },
  refreshBtn: {
    paddingHorizontal: 12,
    paddingVertical: 6,
    borderRadius: 8,
    backgroundColor: "#FFB300",
  },
  refreshTxt: {
    color: "#0A0A0A",
    fontSize: 11,
    fontWeight: "900",
    letterSpacing: 1,
  },
  closeBtn: {
    padding: 4,
  },
});
