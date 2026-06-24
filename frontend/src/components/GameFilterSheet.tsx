/**
 * GameFilterSheet — Modal sheet listing every unique game on today's
 * board so the user can narrow the Locks feed to a single match.
 *
 * UX:
 *   • Opens via a "Filter by Game" button on the home tab.
 *   • Lists every (event, pick_count) in the currently-loaded slate,
 *     sorted by pick_count desc → highest-action games first.
 *   • Search box at the top filters the list as the user types
 *     (matches team names case-insensitively).
 *   • "All Games" row at the top resets the filter.
 *   • Selected row gets a gold check + accent border.
 *   • Apply commits `filters.event` back to the parent and closes.
 *
 * Cross-platform: pure RN primitives (View / Pressable / TextInput).
 * No web-only libraries.
 */
import React, { useMemo, useState } from "react";
import {
  Modal, View, Text, TextInput, Pressable, ScrollView,
  StyleSheet, Platform,
} from "react-native";
import { Ionicons } from "@expo/vector-icons";
import { COLORS } from "@/src/theme";
import type { Pick } from "@/src/lib/api";

type Props = {
  visible:    boolean;
  picks:      Pick[];            // currently-loaded slate (any sport)
  activeEvent?: string;          // currently-applied event filter
  onClose:    () => void;
  onApply:    (event: string | undefined) => void;
};

type Row = { event: string; sport: string; count: number };

function _groupByEvent(picks: Pick[]): Row[] {
  const map = new Map<string, Row>();
  for (const p of picks) {
    const ev = (p.event || "").trim();
    if (!ev) continue;
    const row = map.get(ev) || { event: ev, sport: p.sport || "", count: 0 };
    row.count += 1;
    map.set(ev, row);
  }
  return Array.from(map.values()).sort((a, b) => b.count - a.count);
}

export function GameFilterSheet({
  visible, picks, activeEvent, onClose, onApply,
}: Props) {
  const [query, setQuery] = useState("");
  const [picked, setPicked] = useState<string | undefined>(activeEvent);

  // Re-sync local state when the sheet opens with the parent's filter.
  React.useEffect(() => {
    if (visible) {
      setPicked(activeEvent);
      setQuery("");
    }
  }, [visible, activeEvent]);

  const rows = useMemo(() => _groupByEvent(picks), [picks]);
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return rows;
    return rows.filter(r => r.event.toLowerCase().includes(q));
  }, [rows, query]);

  const apply = () => {
    onApply(picked);
    onClose();
  };

  const reset = () => setPicked(undefined);

  return (
    <Modal
      visible={visible}
      transparent
      animationType="slide"
      onRequestClose={onClose}
    >
      <Pressable style={styles.backdrop} onPress={onClose}>
        <Pressable style={styles.sheet} onPress={(e) => e.stopPropagation()}>
          {/* Grab handle */}
          <View style={styles.handle} />

          {/* Title */}
          <View style={styles.titleRow}>
            <Text style={styles.title}>FILTER BY GAME</Text>
            <Text style={styles.subtitle}>
              {rows.length} {rows.length === 1 ? "game" : "games"} on the slate
            </Text>
          </View>

          {/* Search */}
          <View style={styles.searchRow}>
            <Ionicons name="search" size={16} color={COLORS.textMuted} />
            <TextInput
              style={styles.searchInput}
              placeholder="Search team or matchup…"
              placeholderTextColor={COLORS.textMuted}
              value={query}
              onChangeText={setQuery}
              autoCorrect={false}
              autoCapitalize="none"
              testID="game-filter-search"
            />
            {query.length > 0 && (
              <Pressable hitSlop={6} onPress={() => setQuery("")}>
                <Ionicons name="close-circle" size={18} color={COLORS.textMuted} />
              </Pressable>
            )}
          </View>

          {/* List */}
          <ScrollView
            style={styles.list}
            keyboardShouldPersistTaps="handled"
            showsVerticalScrollIndicator={false}
          >
            {/* "All Games" reset row */}
            <Pressable
              onPress={() => setPicked(undefined)}
              style={[styles.row, !picked && styles.rowActive]}
              testID="game-filter-row-all"
            >
              <View style={{ flex: 1 }}>
                <Text style={styles.rowTitle}>All Games</Text>
                <Text style={styles.rowSub}>Show every match on the board</Text>
              </View>
              {!picked && (
                <Ionicons name="checkmark-circle" size={20} color={COLORS.goldElite} />
              )}
            </Pressable>

            {filtered.map((r) => {
              const active = picked === r.event;
              return (
                <Pressable
                  key={r.event}
                  onPress={() => setPicked(r.event)}
                  style={[styles.row, active && styles.rowActive]}
                  testID={`game-filter-row-${r.event.replace(/\s+/g, "_")}`}
                >
                  <View style={{ flex: 1, minWidth: 0 }}>
                    <Text style={styles.rowTitle} numberOfLines={1}>
                      {r.event}
                    </Text>
                    <Text style={styles.rowSub}>
                      {r.sport} · {r.count} {r.count === 1 ? "pick" : "picks"}
                    </Text>
                  </View>
                  {active && (
                    <Ionicons name="checkmark-circle" size={20} color={COLORS.goldElite} />
                  )}
                </Pressable>
              );
            })}

            {filtered.length === 0 && (
              <View style={styles.empty}>
                <Text style={styles.emptyText}>
                  No games match &quot;{query}&quot;
                </Text>
              </View>
            )}
          </ScrollView>

          {/* Actions */}
          <View style={styles.actions}>
            <Pressable
              onPress={reset}
              style={[styles.btn, styles.btnGhost]}
              testID="game-filter-reset"
            >
              <Text style={styles.btnGhostText}>RESET</Text>
            </Pressable>
            <Pressable
              onPress={apply}
              style={[styles.btn, styles.btnPrimary]}
              testID="game-filter-apply"
            >
              <Text style={styles.btnPrimaryText}>
                {picked ? "APPLY" : "CLEAR FILTER"}
              </Text>
            </Pressable>
          </View>
        </Pressable>
      </Pressable>
    </Modal>
  );
}

const styles = StyleSheet.create({
  backdrop: {
    flex: 1,
    backgroundColor: "rgba(0,0,0,0.65)",
    justifyContent: "flex-end",
  },
  sheet: {
    backgroundColor: COLORS.surface,
    borderTopLeftRadius: 20,
    borderTopRightRadius: 20,
    paddingBottom: Platform.OS === "ios" ? 32 : 20,
    maxHeight: "85%",
    minHeight: 360,
  },
  handle: {
    width: 38,
    height: 4,
    borderRadius: 2,
    backgroundColor: COLORS.borderDefault,
    alignSelf: "center",
    marginTop: 8,
    marginBottom: 4,
  },
  titleRow: {
    paddingHorizontal: 18,
    paddingTop: 10,
    paddingBottom: 6,
  },
  title: {
    color: COLORS.textPrimary,
    fontSize: 16,
    fontWeight: "900",
    letterSpacing: 1.2,
  },
  subtitle: {
    color: COLORS.textMuted,
    fontSize: 11.5,
    marginTop: 2,
    fontWeight: "700",
    letterSpacing: 0.4,
  },
  searchRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    marginHorizontal: 18,
    marginVertical: 8,
    paddingHorizontal: 12,
    paddingVertical: 10,
    borderRadius: 10,
    borderWidth: 1,
    borderColor: COLORS.borderDefault,
    backgroundColor: "rgba(255,255,255,0.03)",
  },
  searchInput: {
    flex: 1,
    color: COLORS.textPrimary,
    fontSize: 14,
    paddingVertical: 0,
    ...Platform.select({
      web: { outlineWidth: 0 } as any,
      default: {},
    }),
  },
  list: {
    paddingHorizontal: 12,
    flexGrow: 0,
  },
  row: {
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
    paddingVertical: 12,
    paddingHorizontal: 12,
    borderRadius: 10,
    marginVertical: 3,
    borderWidth: 1,
    borderColor: "transparent",
    backgroundColor: "rgba(255,255,255,0.02)",
  },
  rowActive: {
    borderColor: COLORS.goldElite,
    backgroundColor: "rgba(255,215,0,0.10)",
  },
  rowTitle: {
    color: COLORS.textPrimary,
    fontSize: 14,
    fontWeight: "800",
    letterSpacing: -0.2,
  },
  rowSub: {
    color: COLORS.textMuted,
    fontSize: 11.5,
    fontWeight: "700",
    letterSpacing: 0.3,
    marginTop: 2,
  },
  empty: {
    paddingVertical: 30,
    alignItems: "center",
  },
  emptyText: {
    color: COLORS.textMuted,
    fontSize: 13,
    fontWeight: "700",
  },
  actions: {
    flexDirection: "row",
    gap: 10,
    paddingHorizontal: 16,
    paddingTop: 12,
    paddingBottom: 4,
    borderTopWidth: StyleSheet.hairlineWidth,
    borderTopColor: COLORS.borderDefault,
  },
  btn: {
    flex: 1,
    height: 44,
    borderRadius: 10,
    alignItems: "center",
    justifyContent: "center",
  },
  btnGhost: {
    borderWidth: 1,
    borderColor: COLORS.borderDefault,
    backgroundColor: "transparent",
  },
  btnGhostText: {
    color: COLORS.textSecondary,
    fontSize: 12,
    fontWeight: "900",
    letterSpacing: 1.0,
  },
  btnPrimary: {
    backgroundColor: COLORS.goldElite,
  },
  btnPrimaryText: {
    color: "#000",
    fontSize: 12,
    fontWeight: "900",
    letterSpacing: 1.0,
  },
});

/** Trigger button — paired with the FilterButton on the home tab. */
export function GameFilterButton({
  onPress, activeEvent, totalGames,
}: { onPress: () => void; activeEvent?: string; totalGames: number }) {
  return (
    <Pressable
      onPress={onPress}
      style={[styles_btn.wrap, !!activeEvent && styles_btn.wrapActive]}
      hitSlop={8}
      testID="game-filter-button"
    >
      <Ionicons
        name={activeEvent ? "football" : "football-outline"}
        size={14}
        color={activeEvent ? COLORS.goldElite : COLORS.textSecondary}
      />
      <Text style={[styles_btn.text, !!activeEvent && styles_btn.textActive]} numberOfLines={1}>
        {activeEvent ? _shortenEvent(activeEvent) : `GAME · ${totalGames}`}
      </Text>
    </Pressable>
  );
}

function _shortenEvent(ev: string): string {
  // "Real Madrid @ Manchester City" → "RMA @ MCI"
  // Fallback to first 18 chars when team names are short.
  const parts = ev.split(" @ ");
  if (parts.length !== 2) return ev.slice(0, 24);
  const tla = (t: string) => {
    const tokens = t.split(/\s+/).filter(Boolean);
    if (tokens.length === 1) return tokens[0].slice(0, 3).toUpperCase();
    return tokens.map(s => s[0]?.toUpperCase()).join("").slice(0, 4);
  };
  return `${tla(parts[0])} @ ${tla(parts[1])}`;
}

const styles_btn = StyleSheet.create({
  wrap: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    paddingHorizontal: 12,
    paddingVertical: 8,
    borderRadius: 10,
    borderWidth: 1,
    borderColor: COLORS.borderDefault,
    backgroundColor: "rgba(255,255,255,0.03)",
    minHeight: 36,
  },
  wrapActive: {
    borderColor: COLORS.goldElite,
    backgroundColor: "rgba(255,215,0,0.12)",
  },
  text: {
    color: COLORS.textSecondary,
    fontSize: 11,
    fontWeight: "900",
    letterSpacing: 0.8,
  },
  textActive: {
    color: COLORS.goldElite,
  },
});
