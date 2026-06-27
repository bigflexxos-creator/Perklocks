/**
 * GameFilterSheet — Modal sheet listing every unique game on today's
 * board so the user can narrow the Locks feed to one OR MORE matches.
 *
 * Multi-select (2026-06-27 upgrade):
 *   • User can tap multiple games — each toggles inclusion in the
 *     persistent `events: string[]` array on the global filter store.
 *   • Selected count is shown in the title and on the trigger pill.
 *   • Apply commits the chosen events back through `onApply(events[])`.
 *   • "Reset" clears the selection inside the sheet (does NOT touch
 *     other filters — there's a master "RESET ALL FILTERS" elsewhere
 *     on the home tab for that).
 *
 * Backward-compat: prior single-select API (`activeEvent: string`,
 *   `onApply: (event?: string) => void`) is preserved on the component
 *   under `legacy*` props so any caller that hasn't migrated still
 *   works (mapped to a 1-element selection internally).
 *
 * Cross-platform: pure RN primitives. No web-only libraries.
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
  picks:      Pick[];             // currently-loaded slate (any sport)
  /** Multi-select: events currently active in the filter store. */
  activeEvents?: string[];
  /** Legacy: single active event. Used if `activeEvents` not provided. */
  activeEvent?: string;
  onClose:    () => void;
  /** Multi-select callback — receives the full new array. Preferred. */
  onApplyEvents?: (events: string[]) => void;
  /** Legacy single-select callback — emits the FIRST selected event or
   *  undefined when cleared. Kept for older home-tab call sites. */
  onApply?:   (event: string | undefined) => void;
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
  visible, picks, activeEvents, activeEvent, onClose, onApplyEvents, onApply,
}: Props) {
  const [query, setQuery] = useState("");
  // Local multi-select state. Seeded from either the new `activeEvents`
  // array OR the legacy single `activeEvent` prop (which we wrap into
  // a 1-element array).
  const initial = useMemo<Set<string>>(() => {
    if (activeEvents && activeEvents.length) return new Set(activeEvents);
    if (activeEvent) return new Set([activeEvent]);
    return new Set();
  }, [activeEvents, activeEvent]);
  const [picked, setPicked] = useState<Set<string>>(initial);

  // Re-sync local state when the sheet opens with the parent's filter.
  React.useEffect(() => {
    if (visible) {
      setPicked(new Set(initial));
      setQuery("");
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visible]);

  const rows = useMemo(() => _groupByEvent(picks), [picks]);
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return rows;
    return rows.filter(r => r.event.toLowerCase().includes(q));
  }, [rows, query]);

  const toggle = (event: string) => {
    setPicked(prev => {
      const next = new Set(prev);
      if (next.has(event)) next.delete(event);
      else next.add(event);
      return next;
    });
  };

  const apply = () => {
    const arr = Array.from(picked);
    if (onApplyEvents) onApplyEvents(arr);
    if (onApply) onApply(arr.length ? arr[0] : undefined);
    onClose();
  };

  const reset = () => setPicked(new Set());
  const pickedCount = picked.size;

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
            <View style={styles.titleHeaderRow}>
              <Text style={styles.title}>FILTER BY GAME</Text>
              {pickedCount > 0 && (
                <View style={styles.countBadge}>
                  <Text style={styles.countBadgeText}>{pickedCount}</Text>
                </View>
              )}
            </View>
            <Text style={styles.subtitle}>
              {pickedCount > 0
                ? `${pickedCount} of ${rows.length} game${rows.length === 1 ? "" : "s"} selected`
                : `${rows.length} ${rows.length === 1 ? "game" : "games"} on the slate · tap to select multiple`}
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
            {/* "All Games" row — clears the selection. Active state =
                user has nothing picked, which means "show everything". */}
            <Pressable
              onPress={() => setPicked(new Set())}
              style={[styles.row, pickedCount === 0 && styles.rowActive]}
              testID="game-filter-row-all"
            >
              <View style={{ flex: 1 }}>
                <Text style={styles.rowTitle}>All Games</Text>
                <Text style={styles.rowSub}>Show every match on the board</Text>
              </View>
              {pickedCount === 0 && (
                <Ionicons name="checkmark-circle" size={20} color={COLORS.goldElite} />
              )}
            </Pressable>

            {filtered.map((r) => {
              const active = picked.has(r.event);
              return (
                <Pressable
                  key={r.event}
                  onPress={() => toggle(r.event)}
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
                  {/* Multi-select checkbox indicator. Empty box when
                      inactive, gold check-circle when picked. */}
                  {active ? (
                    <Ionicons name="checkmark-circle" size={20} color={COLORS.goldElite} />
                  ) : (
                    <Ionicons name="ellipse-outline" size={20} color={COLORS.borderDefault} />
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
              <Text style={styles.btnGhostText}>CLEAR</Text>
            </Pressable>
            <Pressable
              onPress={apply}
              style={[styles.btn, styles.btnPrimary]}
              testID="game-filter-apply"
            >
              <Text style={styles.btnPrimaryText}>
                {pickedCount > 0
                  ? `APPLY · ${pickedCount}`
                  : "SHOW ALL GAMES"}
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
  titleHeaderRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
  },
  title: {
    color: COLORS.textPrimary,
    fontSize: 16,
    fontWeight: "900",
    letterSpacing: 1.2,
  },
  countBadge: {
    backgroundColor: COLORS.goldElite,
    minWidth: 22,
    height: 22,
    paddingHorizontal: 6,
    borderRadius: 11,
    alignItems: "center",
    justifyContent: "center",
  },
  countBadgeText: {
    color: "#000",
    fontSize: 12,
    fontWeight: "900",
    letterSpacing: 0.4,
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
  onPress, activeEvent, activeEventsCount = 0, totalGames,
}: {
  onPress: () => void;
  /** Legacy: single active event label (still rendered when count<=1). */
  activeEvent?: string;
  /** Multi-select count from the global filter store. */
  activeEventsCount?: number;
  totalGames: number;
}) {
  const isActive = activeEventsCount > 0 || !!activeEvent;
  let label: string;
  if (activeEventsCount > 1) {
    label = `${activeEventsCount} GAMES`;
  } else if (activeEventsCount === 1) {
    // Legacy single-event display preserved for the 1-event case.
    label = activeEvent ? _shortenEvent(activeEvent) : "1 GAME";
  } else if (activeEvent) {
    label = _shortenEvent(activeEvent);
  } else {
    label = `GAME · ${totalGames}`;
  }
  return (
    <Pressable
      onPress={onPress}
      style={[styles_btn.wrap, isActive && styles_btn.wrapActive]}
      hitSlop={8}
      testID="game-filter-button"
    >
      <Ionicons
        name={isActive ? "football" : "football-outline"}
        size={14}
        color={isActive ? COLORS.goldElite : COLORS.textSecondary}
      />
      <Text style={[styles_btn.text, isActive && styles_btn.textActive]} numberOfLines={1}>
        {label}
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
