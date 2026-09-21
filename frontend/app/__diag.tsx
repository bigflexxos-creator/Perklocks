/**
 * /__diag — Physical Expo Go boundary diagnostic
 * ═══════════════════════════════════════════════════════════════════
 * Renders EVERYTHING the phone actually sees so we can prove which
 * boundary between (frontend bundle) → (resolved origin) → (HTTP
 * response) → (React render) is the first to diverge from Preview.
 *
 * ALL data is captured from the LIVE session — no static import
 * shortcut, no cached values, no assumptions.
 *
 * Route accessible at /__diag (or /(no-tabs)/__diag in nested layouts).
 */
import React, { useEffect, useState } from "react";
import { SafeAreaView, ScrollView, StyleSheet, Text, View, Pressable, Platform } from "react-native";
import Constants from "expo-constants";

import { FRONTEND_BUILD_REVISION, FRONTEND_BUILD_MANIFEST } from "@/src/lib/buildRevision";
import { getCurrentEpoch, hydrateCanonicalEpoch } from "@/src/lib/canonicalEpoch";
import { api, getBackendUrl } from "@/src/lib/api";

const CANARY_PICK_ID = "3d1b1fcf-be50-5f10-9de8-c03d83cf7d41"; // Northwestern @ Indiana O47.5

type Diag = {
  frontendRevision: string;
  frontendManifest: any;
  platform: string;
  runtimeVersion: any;
  expoConfigExtra: any;
  resolvedApiOrigin: string;
  currentEpoch: any;
  picksTodayRevision: string | number | null;
  picksTodayFirstIds: string[];
  canary: {
    pickId: string;
    httpStatus: number | null;
    xCanonicalRevision: string | null;
    xCanonicalVersion: string | null;
    xCanonicalGenerationId: string | null;
    event: string | null;
    market: string | null;
    line: number | null;
    book_odds: number | string | null;
    win_probability: number | null;
    implied_probability: number | string | null;
    edge_percent: number | null;
    lock_score: number | null;
    grade: string | null;
    cfb_engine_version: string | null;
    error: string | null;
  };
};

export default function DiagnosticScreen() {
  const [diag, setDiag] = useState<Diag | null>(null);
  const [refreshTick, setRefreshTick] = useState(0);

  useEffect(() => {
    let alive = true;
    (async () => {
      await hydrateCanonicalEpoch().catch(() => {});
      // Resolve API origin the same way api.ts does.
      let apiOrigin = "";
      try { apiOrigin = getBackendUrl(); } catch (e) { apiOrigin = `error: ${String(e)}`; }

      // Fetch /api/picks/today and capture headers + top revision.
      let picksTodayRevision: string | number | null = null;
      let picksTodayFirstIds: string[] = [];
      try {
        const list = await api.picksToday("All", "std", "edge", {} as any, "desc");
        picksTodayRevision =
          (list as any).canonical_revision ??
          (list as any).board_version ??
          null;
        picksTodayFirstIds = ((list.picks || []) as any[]).slice(0, 5).map((p) => p.id);
      } catch (e) {
        picksTodayFirstIds = [`error: ${String(e)}`];
      }

      // Direct raw fetch of the canary pick so we can inspect headers.
      const url = `${apiOrigin}/api/picks/${CANARY_PICK_ID}`;
      const canary: Diag["canary"] = {
        pickId: CANARY_PICK_ID,
        httpStatus: null,
        xCanonicalRevision: null,
        xCanonicalVersion: null,
        xCanonicalGenerationId: null,
        event: null, market: null, line: null, book_odds: null,
        win_probability: null, implied_probability: null,
        edge_percent: null, lock_score: null, grade: null,
        cfb_engine_version: null,
        error: null,
      };
      try {
        // Best-effort: pull the auth token from AsyncStorage so a
        // signed-in session can hit /api/picks/{id}.  On public
        // preview the endpoint is 200-open; on strict-auth it returns
        // 401 but we still surface headers.
        let token: string | null = null;
        try {
          const { storage } = await import("@/src/utils/storage");
          token = await storage.getItem<string>("lockscore_token", "");
        } catch {}
        const r = await fetch(url, {
          headers: token ? { Authorization: `Bearer ${token}` } : {},
        });
        canary.httpStatus = r.status;
        canary.xCanonicalRevision  = r.headers.get("x-canonical-revision")
                                   || r.headers.get("X-Canonical-Revision");
        canary.xCanonicalVersion   = r.headers.get("x-canonical-version")
                                   || r.headers.get("X-Canonical-Version");
        canary.xCanonicalGenerationId = r.headers.get("x-canonical-generation-id")
                                   || r.headers.get("X-Canonical-Generation-Id");
        if (r.ok) {
          const body: any = await r.json();
          canary.event = body.event;
          canary.market = body.market;
          canary.line = body.line;
          canary.book_odds = body.book_odds;
          canary.win_probability = body.win_probability;
          canary.implied_probability = body.implied_probability;
          canary.edge_percent = body.edge_percent;
          canary.lock_score = body.lock_score;
          canary.grade = body.grade;
          canary.cfb_engine_version = body.cfb_engine_version;
        } else {
          canary.error = `${r.status} ${r.statusText}`;
        }
      } catch (e) {
        canary.error = String(e);
      }

      if (!alive) return;
      setDiag({
        frontendRevision: FRONTEND_BUILD_REVISION,
        frontendManifest: FRONTEND_BUILD_MANIFEST,
        platform: Platform.OS + " " + Platform.Version,
        runtimeVersion: (Constants as any).expoConfig?.runtimeVersion || (Constants as any).manifest?.runtimeVersion || "unknown",
        expoConfigExtra: (Constants as any).expoConfig?.extra || null,
        resolvedApiOrigin: apiOrigin,
        currentEpoch: getCurrentEpoch(),
        picksTodayRevision,
        picksTodayFirstIds,
        canary,
      });
    })();
    return () => { alive = false; };
  }, [refreshTick]);

  return (
    <SafeAreaView style={styles.wrap}>
      <ScrollView contentContainerStyle={styles.content}>
        <Text style={styles.title}>PHYSICAL EXPO DIAGNOSTIC</Text>
        <Text style={styles.sub}>Boundary probe — read every value below aloud.</Text>
        <Pressable onPress={() => setRefreshTick((n) => n + 1)} style={styles.btn}>
          <Text style={styles.btnTxt}>RE-RUN</Text>
        </Pressable>
        {diag === null ? (
          <Text style={styles.pending}>Collecting live data…</Text>
        ) : (
          <>
            <Section title="1  FRONTEND BUILD REVISION" value={diag.frontendRevision} highlight />
            <Section title="   PLATFORM" value={diag.platform} />
            <Section title="   Expo runtime version" value={String(diag.runtimeVersion)} />

            <Section title="2  RESOLVED API ORIGIN" value={diag.resolvedApiOrigin} highlight />

            <Section title="3  CURRENT CANONICAL EPOCH (in-memory)"
                     value={JSON.stringify(diag.currentEpoch, null, 2)} />

            <Section title="4  /api/picks/today  ·  canonical_revision"
                     value={String(diag.picksTodayRevision)}
                     highlight />
            <Section title="   first 5 pick IDs"
                     value={diag.picksTodayFirstIds.join("\n") || "—"} />

            <Section title="5  CANARY — Northwestern @ Indiana O47.5"
                     value={"pick_id: " + CANARY_PICK_ID} highlight />
            <Section title="   HTTP" value={
              "status " + String(diag.canary.httpStatus) +
              (diag.canary.error ? " · error " + diag.canary.error : "")
            } />
            <Section title="   X-Canonical-Revision" value={String(diag.canary.xCanonicalRevision)} highlight />
            <Section title="   X-Canonical-Version"  value={String(diag.canary.xCanonicalVersion)}  />
            <Section title="   X-Canonical-Generation-Id" value={String(diag.canary.xCanonicalGenerationId)} />
            <Section title="   event"           value={String(diag.canary.event)} />
            <Section title="   market · line"   value={String(diag.canary.market) + "  ·  " + String(diag.canary.line) + "  ·  odds " + String(diag.canary.book_odds)} />
            <Section title="   WIN PROBABILITY" value={String(diag.canary.win_probability) + "%"} highlight />
            <Section title="   IMPLIED PROBABILITY" value={String(diag.canary.implied_probability) + "%"} highlight />
            <Section title="   EDGE"            value={String(diag.canary.edge_percent) + " pp"} />
            <Section title="   LOCK SCORE"      value={String(diag.canary.lock_score)} highlight />
            <Section title="   GRADE"           value={String(diag.canary.grade)} />
            <Section title="   cfb_engine_version" value={String(diag.canary.cfb_engine_version)} highlight />

            <Section title="MANIFEST FEATURES" value={diag.frontendManifest.features.join("\n")} />
          </>
        )}
      </ScrollView>
    </SafeAreaView>
  );
}

function Section({ title, value, highlight }: { title: string; value: string; highlight?: boolean }) {
  return (
    <View style={[styles.row, highlight && styles.rowHi]}>
      <Text style={styles.rowTitle}>{title}</Text>
      <Text style={styles.rowValue}>{value}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  wrap:   { flex: 1, backgroundColor: "#000" },
  content:{ padding: 16, paddingBottom: 60 },
  title:  { color: "#fff", fontSize: 20, fontWeight: "900" },
  sub:    { color: "#888", fontSize: 12, marginTop: 4, marginBottom: 12 },
  pending:{ color: "#aaa", fontSize: 14, marginTop: 16 },
  btn:    { backgroundColor: "#1f6feb", padding: 10, borderRadius: 8, alignSelf: "flex-start", marginBottom: 12 },
  btnTxt: { color: "#fff", fontWeight: "800", letterSpacing: 1 },
  row:    { paddingVertical: 8, borderBottomWidth: StyleSheet.hairlineWidth, borderBottomColor: "#222" },
  rowHi:  { backgroundColor: "#0b1e12", paddingHorizontal: 8, borderRadius: 6, marginVertical: 4, borderBottomWidth: 0 },
  rowTitle:{ color: "#aaa", fontSize: 11, letterSpacing: 1, fontWeight: "700" },
  rowValue:{ color: "#fff", fontSize: 14, fontFamily: Platform.select({ ios: "Menlo", android: "monospace", default: "monospace" }), marginTop: 2 },
});
