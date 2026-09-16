/**
 * Session 9 · Board Version Client Integration — DEV proof screen.
 * ──────────────────────────────────────────────────────────────────
 * A dedicated screen that exercises the version-pinned cursor
 * pagination end-to-end.  Renders live:
 *   • board_version this page is pinned to
 *   • backend's current board_version
 *   • newer_version_available banner
 *   • picks count / total / has_more
 *   • [LOAD NEXT PAGE] button (pinned to pinned version)
 *   • [ACCEPT NEWER VERSION] button (atomic reset)
 *
 * Runtime scenario the user should exercise:
 *   1. Load page 1  → note V1, N picks
 *   2. Wait for backend publish (or run refresh in an admin tool)
 *   3. Tap LOAD NEXT PAGE → response comes back with V1 rows (not V2)
 *      and `newer_version_available=true` — banner appears, list
 *      still consistent with V1
 *   4. Tap ACCEPT NEWER VERSION → atomically resets to V2 page 1
 *
 * Location: /app/board-version-test (via expo-router file convention).
 */
import React from "react";
import { View, Text, StyleSheet, ScrollView, TouchableOpacity } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { useBoardCursor } from "@/src/lib/useBoardCursor";
import { COLORS } from "@/src/theme";
import { ERROR_COPY } from "@/src/lib/errorTaxonomy";

export default function BoardVersionTestScreen() {
  const {
    picks, boardVersion, currentBackendVersion, newerVersionAvailable,
    hasMore, nextCursor, total, loading, fetchingMore, errorKind,
    refresh, loadMore, acceptNewerVersion,
  } = useBoardCursor("All", 50);

  return (
    <SafeAreaView style={styles.safe}>
      <ScrollView contentContainerStyle={styles.content}>
        <Text style={styles.title}>Board Version — Client Integration</Text>
        <Text style={styles.subtitle}>
          Session 9 proof: cursor pagination pinned to an immutable
          board_version.  Pages never mix across versions.
        </Text>

        <View style={styles.card}>
          <Row k="board_version (pinned)"   v={boardVersion ?? "—"} />
          <Row k="current backend version" v={currentBackendVersion ?? "—"} />
          <Row k="newer available"         v={String(newerVersionAvailable)} highlight={newerVersionAvailable} />
          <Row k="picks in view"            v={String(picks.length)} />
          <Row k="total"                    v={String(total)} />
          <Row k="has_more"                 v={String(hasMore)} />
          <Row k="next_cursor"              v={nextCursor ? nextCursor.slice(0, 24) + "…" : "—"} />
          <Row k="loading / more"           v={`${loading} / ${fetchingMore}`} />
          <Row k="last error"               v={errorKind ? `${errorKind}: ${ERROR_COPY[errorKind]?.title || ""}` : "—"} />
        </View>

        {newerVersionAvailable && (
          <View style={styles.banner}>
            <Text style={styles.bannerText}>
              A newer board version is available.  Current view stays pinned
              to the frozen slate — tap ACCEPT NEWER VERSION to reset.
            </Text>
          </View>
        )}

        <View style={styles.actionsRow}>
          <TouchableOpacity onPress={refresh} style={[styles.btn, styles.btnPrimary]}>
            <Text style={styles.btnText}>REFRESH (page 1)</Text>
          </TouchableOpacity>
          <TouchableOpacity
            onPress={loadMore}
            disabled={!hasMore || fetchingMore}
            style={[styles.btn, styles.btnSecondary, (!hasMore || fetchingMore) && styles.btnDisabled]}
          >
            <Text style={styles.btnText}>LOAD NEXT PAGE</Text>
          </TouchableOpacity>
        </View>
        {newerVersionAvailable && (
          <TouchableOpacity onPress={acceptNewerVersion} style={[styles.btn, styles.btnAccept]}>
            <Text style={styles.btnText}>ACCEPT NEWER VERSION</Text>
          </TouchableOpacity>
        )}

        <Text style={styles.section}>First 20 picks in view</Text>
        {picks.slice(0, 20).map((p, i) => (
          <View key={p.id ?? i} style={styles.pickRow}>
            <Text style={styles.pickIdx}>{i + 1}.</Text>
            <View style={{ flex: 1 }}>
              <Text style={styles.pickTitle} numberOfLines={1}>
                {p.market} · {p.selection}
              </Text>
              <Text style={styles.pickMeta} numberOfLines={1}>
                {p.sport} · lock {p.lock_score} · {p.event}
              </Text>
            </View>
          </View>
        ))}
      </ScrollView>
    </SafeAreaView>
  );
}

function Row({ k, v, highlight }: { k: string; v: string; highlight?: boolean }) {
  return (
    <View style={styles.row}>
      <Text style={styles.key}>{k}</Text>
      <Text style={[styles.val, highlight && styles.valHi]} numberOfLines={2}>{v}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  safe:      { flex: 1, backgroundColor: COLORS.bg },
  content:   { padding: 16, paddingBottom: 60 },
  title:     { color: COLORS.textPrimary, fontSize: 22, fontWeight: "800", marginBottom: 4 },
  subtitle:  { color: COLORS.textMuted, fontSize: 13, marginBottom: 14 },
  card:      { backgroundColor: COLORS.surface, borderRadius: 12, padding: 12, borderWidth: 1, borderColor: COLORS.borderDefault },
  row:       { flexDirection: "row", justifyContent: "space-between", paddingVertical: 4 },
  key:       { color: COLORS.textMuted, fontSize: 12, fontFamily: "monospace" as any },
  val:       { color: COLORS.textPrimary, fontSize: 12, fontFamily: "monospace" as any, maxWidth: "60%" as any, textAlign: "right" },
  valHi:     { color: COLORS.goldElite, fontWeight: "800" },
  banner:    { backgroundColor: "rgba(255,215,95,0.10)", borderColor: COLORS.goldElite, borderWidth: 1, borderRadius: 10, padding: 12, marginTop: 12 },
  bannerText:{ color: COLORS.goldElite, fontSize: 12, fontWeight: "600" },
  actionsRow:{ flexDirection: "row", marginTop: 14, gap: 10 },
  btn:       { flex: 1, borderRadius: 10, paddingVertical: 12, alignItems: "center" },
  btnPrimary:{ backgroundColor: COLORS.voltBlue },
  btnSecondary:{ backgroundColor: COLORS.surface, borderWidth: 1, borderColor: COLORS.borderDefault },
  btnAccept: { backgroundColor: COLORS.goldElite, marginTop: 10 },
  btnDisabled:{ opacity: 0.4 },
  btnText:   { color: COLORS.bg, fontWeight: "800", fontSize: 12 },
  section:   { color: COLORS.textPrimary, fontSize: 14, fontWeight: "700", marginTop: 20, marginBottom: 6 },
  pickRow:   { flexDirection: "row", paddingVertical: 6, borderTopWidth: 1, borderTopColor: COLORS.borderDefault },
  pickIdx:   { color: COLORS.textMuted, width: 28, fontSize: 12 },
  pickTitle: { color: COLORS.textPrimary, fontSize: 13, fontWeight: "600" },
  pickMeta:  { color: COLORS.textMuted, fontSize: 11, marginTop: 2 },
});
