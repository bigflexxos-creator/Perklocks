/**
 * PERKLOCKS · Board Version Client Integration Hook
 * ──────────────────────────────────────────────────────────────────
 * Enforces the Session 8 immutable-board contract on the client:
 *
 *   • Page 1 arrives with `board_version` (stable hash of today's
 *     canonical population).
 *   • We RETAIN that version and pass it back on every subsequent
 *     `/api/picks/all?cursor=...&board_version=...` call so the
 *     backend serves the same immutable slate.
 *   • If the backend publishes a NEWER version mid-scroll, its
 *     response carries `newer_version_available: true`.  We DO NOT
 *     merge that page into our current V1 state — the current view
 *     stays consistent with page 1's version.  A banner is exposed
 *     via `newerVersionAvailable` so the UI can offer "refresh".
 *   • `refresh()` atomically resets to page 1 of the current
 *     backend version — this is the ONLY path that switches board
 *     versions.  No incremental fetch ever cross-contaminates.
 *
 * Every fetch is wired to an AbortController that is cancelled on
 * unmount and on any dependency change (sport, refresh), so a stale
 * response can NEVER commit state.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { swrCacheRead, swrCacheWrite } from "@/src/lib/useSWR";
import { picksLiteKey } from "@/src/lib/serverStateKeys";
import { api, Pick } from "@/src/lib/api";
import { classifyError, ErrorKind, ErrorKindT, isSilentError } from "@/src/lib/errorTaxonomy";
import perf from "@/src/lib/perfHUD";

export type BoardCursorState = {
  picks:                    Pick[];
  boardVersion:             string | null;
  currentBackendVersion:    string | null;
  newerVersionAvailable:    boolean;
  hasMore:                  boolean;
  nextCursor:               string | null;
  total:                    number;
  loading:                  boolean;
  fetchingMore:             boolean;
  errorKind:                ErrorKindT | null;
};

const initialState: BoardCursorState = {
  picks:                    [],
  boardVersion:             null,
  currentBackendVersion:    null,
  newerVersionAvailable:    false,
  hasMore:                  false,
  nextCursor:               null,
  total:                    0,
  loading:                  false,
  fetchingMore:             false,
  errorKind:                null,
};

export function useBoardCursor(sport?: string, limit: number = 200) {
  // Seed page 1 from the canonical board cache (warm → immediate paint).
  const _seed = swrCacheRead<{ picks: any[]; ts: number }>(picksLiteKey(sport || "All"));
  const [state, setState] = useState<BoardCursorState>(
    _seed?.picks?.length ? { ...initialState, picks: _seed.picks as any, loading: false } : initialState,
  );

  // Per-generation AbortControllers.  Each new load()/loadMore()
  // aborts the previous.  React-strict double-mount and rapid
  // dependency flips are handled cleanly.
  const inflightRef = useRef<AbortController | null>(null);
  const stateRef    = useRef(state); stateRef.current = state;

  const cancelInflight = useCallback(() => {
    if (inflightRef.current) {
      try { inflightRef.current.abort(); } catch {}
      inflightRef.current = null;
    }
  }, []);

  const fetchPage = useCallback(async (opts: {
    cursor:        string | null;
    board_version: string | null;
    append:        boolean;
    reason:        string;
  }) => {
    cancelInflight();
    const ctrl = new AbortController();
    inflightRef.current = ctrl;
    setState((s) => opts.append ? { ...s, fetchingMore: true, errorKind: null }
                                 : { ...s, loading: true, errorKind: null });
    const t = perf.startMark(`board.${opts.reason}`);
    try {
      const r = await api.picksBoard({
        sport, limit,
        cursor:         opts.cursor,
        board_version:  opts.board_version,
        signal:         ctrl.signal,
      });
      t.step("response");
      // Ignore if superseded — the AbortController above will already
      // have flipped `ctrl.signal.aborted`, and request() will have
      // rejected with ABORTED_SUPERSEDED.  This branch just double-
      // checks in case the response arrives before the abort event
      // fires on some slow platforms.
      if (ctrl.signal.aborted) return;
      // ── Contract enforcement ──
      // If we're APPENDING, the backend MUST return the same
      // board_version we pinned to.  Anything else = protocol
      // violation; drop the page rather than mixing versions.
      if (opts.append) {
        const pinned = stateRef.current.boardVersion;
        if (pinned && r.board_version !== pinned) {
          setState((s) => ({
            ...s, fetchingMore: false,
            newerVersionAvailable: true,
            currentBackendVersion: r.current_board_version,
            errorKind: null,
          }));
          t.end({ dropped: "version_mismatch" });
          return;
        }
      }
      // Page 1 writes through the SAME canonical cache authority the Locks
      // board reads (`picks|lite|{sport}`) only when the page is the
      // complete population (no further pages) — a partial page never
      // masquerades as the full board.
      if (!opts.append && !r.has_more && r.picks.length > 0) {
        try { swrCacheWrite(picksLiteKey(sport || "All"), { picks: r.picks.slice(), ts: Date.now() }); } catch {}
      }
      setState((s) => {
        // Atomic swap for page 1, append for subsequent pages.
        const combined = opts.append
          ? _mergePreservingOrder(s.picks, r.picks)
          : r.picks.slice();
        return {
          picks:                 combined,
          boardVersion:          r.board_version,
          currentBackendVersion: r.current_board_version,
          newerVersionAvailable: r.newer_version_available,
          hasMore:               r.has_more,
          nextCursor:            r.next_cursor,
          total:                 r.total,
          loading:               false,
          fetchingMore:          false,
          errorKind:             null,
        };
      });
      t.end({ n: r.picks.length, page: opts.append ? "append" : "first" });
    } catch (err: any) {
      const kind = classifyError(err);
      if (isSilentError(kind)) {
        // ABORTED_SUPERSEDED — nothing to do, a newer request replaced us.
        t.end({ superseded: true });
        return;
      }
      setState((s) => ({
        ...s,
        loading:      false,
        fetchingMore: false,
        errorKind:    kind,
      }));
      t.end({ errorKind: kind });
    }
  }, [sport, limit, cancelInflight]);

  /** Load page 1 fresh — abandons any current version pin. */
  const refresh = useCallback(() => {
    cancelInflight();
    setState({ ...initialState, loading: true });
    return fetchPage({ cursor: null, board_version: null, append: false, reason: "refresh" });
  }, [cancelInflight, fetchPage]);

  /** Advance one page, PINNED to the current board_version. */
  const loadMore = useCallback(() => {
    const s = stateRef.current;
    if (!s.hasMore || s.fetchingMore || !s.nextCursor) return Promise.resolve();
    return fetchPage({
      cursor:         s.nextCursor,
      board_version:  s.boardVersion,   // ← pin
      append:         true,
      reason:         "load_more",
    });
  }, [fetchPage]);

  /** Explicit opt-in to switch to the newest backend board version. */
  const acceptNewerVersion = useCallback(() => refresh(), [refresh]);

  // Kick off page 1 on mount / sport change.  Any prior in-flight
  // request is cancelled by fetchPage() itself.
  useEffect(() => {
    refresh();
    return cancelInflight;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sport]);

  return { ...state, refresh, loadMore, acceptNewerVersion };
}

function _mergePreservingOrder(base: Pick[], next: Pick[]): Pick[] {
  // Backend already sorts deterministically (lock_score DESC,
  // event_time ASC, canonical id ASC) and paginates without gaps
  // when board_version is pinned.  Guard against duplicate IDs
  // arising from a mis-tagged retry.
  if (!next || next.length === 0) return base;
  const seen = new Set<string>(base.map((p) => String(p.id)));
  const out  = base.slice();
  for (const p of next) {
    const id = String(p.id || "");
    if (!id || seen.has(id)) continue;
    out.push(p);
    seen.add(id);
  }
  return out;
}
