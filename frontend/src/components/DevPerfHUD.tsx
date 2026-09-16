/**
 * DEV-only Performance HUD overlay.
 *
 * Gated by two conditions BOTH being true:
 *   1. __DEV__ === true (Metro dev build)
 *   2. EXPO_PUBLIC_PERF_HUD === "1"
 *
 * When either fails, the component returns null — production bundles
 * pay literally nothing.  Never rendered on real user devices.
 */
import React, { useCallback, useEffect, useRef, useState } from "react";
import { View, Text, StyleSheet, Pressable } from "react-native";
import { getPerfSnapshot, startFpsMeter, stopFpsMeter, resetPerfSnapshot } from "@/src/lib/perfHUD";

const HUD_ENABLED =
  (typeof __DEV__ !== "undefined" && __DEV__) &&
  (process.env.EXPO_PUBLIC_PERF_HUD === "1");

export function DevPerfHUD(): React.ReactElement | null {
  const [collapsed, setCollapsed] = useState(false);
  const [, setTick] = useState(0);
  const tRef = useRef<any>(null);

  useEffect(() => {
    if (!HUD_ENABLED) return;
    startFpsMeter();
    tRef.current = setInterval(() => setTick((n) => n + 1), 1000);
    return () => {
      stopFpsMeter();
      if (tRef.current) clearInterval(tRef.current);
    };
  }, []);

  const onReset = useCallback(() => {
    resetPerfSnapshot();
    setTick((n) => n + 1);
  }, []);

  if (!HUD_ENABLED) return null;

  const snap = getPerfSnapshot();
  const priority = [
    "locks.load", "locks.load.response", "locks.load.commit",
    "locks.load.paint", "locks.tap_to_paint", "perf.fps",
    "list.mounted_rows",
  ];
  const priorityRows = priority
    .map((lbl) => snap.rows.find((r) => r.label === lbl))
    .filter(Boolean) as any[];
  const other = snap.rows.filter((r) => !priority.includes(r.label));

  return (
    <View style={styles.overlay} pointerEvents="box-none">
      <Pressable onPress={() => setCollapsed((c) => !c)} style={styles.header} testID="perf-hud-toggle">
        <Text style={styles.title}>⚡ PERF HUD {collapsed ? "▸" : "▾"}</Text>
        <Pressable onPress={onReset} hitSlop={8}><Text style={styles.reset}>reset</Text></Pressable>
      </Pressable>
      {!collapsed && (
        <View style={styles.body}>
          {priorityRows.length === 0 && (
            <Text style={styles.empty}>(no samples yet — interact with the app)</Text>
          )}
          {priorityRows.map((r) => (
            <Row key={r.label} r={r} />
          ))}
          {other.length > 0 && (
            <>
              <Text style={styles.section}>other</Text>
              {other.map((r) => <Row key={r.label} r={r} />)}
            </>
          )}
        </View>
      )}
    </View>
  );
}

function Row({ r }: { r: { label: string; n: number; p50: number | null; p95: number | null; last: number | null } }) {
  const isFps = r.label === "perf.fps";
  const isCount = r.label === "list.mounted_rows";
  const suffix = isFps || isCount ? "" : "ms";
  const p50 = r.p50 != null ? Math.round(r.p50) : "—";
  const p95 = r.p95 != null ? Math.round(r.p95) : "—";
  const last = r.last != null ? Math.round(r.last) : "—";
  return (
    <View style={styles.row}>
      <Text style={styles.label} numberOfLines={1}>{r.label}</Text>
      <Text style={styles.val}>n={r.n}</Text>
      <Text style={styles.val}>p50={p50}{suffix}</Text>
      <Text style={styles.val}>p95={p95}{suffix}</Text>
      <Text style={styles.val}>last={last}{suffix}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  overlay: {
    position: "absolute",
    top: 60,
    right: 8,
    minWidth: 300,
    maxWidth: 380,
    backgroundColor: "rgba(0,0,0,0.85)",
    borderColor: "rgba(255, 200, 0, 0.5)",
    borderWidth: 1,
    borderRadius: 8,
    padding: 6,
    zIndex: 9999,
  },
  header: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center",
    paddingHorizontal: 4,
    paddingVertical: 2,
  },
  title:   { color: "#ffd75f", fontWeight: "800", fontSize: 12 },
  reset:   { color: "rgba(255,255,255,0.6)", fontSize: 10, textDecorationLine: "underline" },
  body:    { marginTop: 4 },
  row:     { flexDirection: "row", alignItems: "center", justifyContent: "space-between", paddingVertical: 1 },
  label:   { flex: 1, color: "#fff", fontSize: 10, fontFamily: "monospace" as any },
  val:     { color: "#9dfd9d", fontSize: 10, fontFamily: "monospace" as any, marginLeft: 4 },
  section: { color: "rgba(255,255,255,0.5)", fontSize: 10, marginTop: 4, marginBottom: 2 },
  empty:   { color: "rgba(255,255,255,0.5)", fontSize: 10, fontStyle: "italic" },
});

export default DevPerfHUD;
