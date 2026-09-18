/**
 * FEATURED HERO STORE — isolates the 7-second hero rotation from the Locks
 * list dataset.  Before: `featuredIdx` sat in the `listRows` useMemo deps,
 * so every rotation rebuilt the rows array and FlatList reconciled the whole
 * board.  Now `listRows` is independent of the rotation; each card
 * subscribes (keyed) and only the previously-featured and newly-featured
 * cards re-render.
 */
import { useCallback, useSyncExternalStore } from "react";

let _featuredId: string | null = null;
const _listeners = new Set<() => void>();

export function setFeaturedPickId(id: string | null): void {
  if (id === _featuredId) return;
  _featuredId = id;
  _listeners.forEach((l) => { try { l(); } catch { /* ignore */ } });
}

export function getFeaturedPickId(): string | null {
  return _featuredId;
}

function _subscribe(l: () => void): () => void {
  _listeners.add(l);
  return () => { _listeners.delete(l); };
}

/** True only for the currently featured pick; boolean snapshot → a card
 *  re-renders solely when ITS featured state flips. */
export function useIsFeatured(pickId: string | undefined | null): boolean {
  const get = useCallback(() => !!pickId && _featuredId === pickId, [pickId]);
  return useSyncExternalStore(_subscribe, get, get);
}
