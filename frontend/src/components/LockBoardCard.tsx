/**
 * LockBoardCard — SLICE 1.6 alias (2026-09-02)
 * =============================================
 *
 * Formalized name for the lightweight board-list row.  Alias for
 * `LockPickCard` today; the split from `PickBreakdown` (Slice 8) will
 * peel additional expensive tree branches off this file over time.
 *
 * Slice 1.6 board-row perf contract (enforced by
 * `frontend/__tests__/slice_1_6_board_row.spec.ts`):
 *
 *   • No component in the board row makes a per-card API call at mount
 *     when the required data is already carried by the Lightweight
 *     Board DTO (Slice 1.2B).  MatchupGradeBadge honours this via its
 *     `preloaded` prop.
 *   • Every `<Modal>` inside a board row is lazy-mounted
 *     (`{visibleState && <Modal>}`).  A 100-card slate must not sit on
 *     200+ idle Modal instances.
 *   • Deep breakdown JSX (splits, matchup, distribution, model
 *     provenance, risks) MUST live on the Pick Breakdown screen
 *     (`/pick/[id]`), not on the collapsed board row.
 *
 * Consumers should import `LockBoardCard` for board/list rendering and
 * reserve direct `LockPickCard` imports for detail contexts where the
 * expanded breakdown is intended.
 */
export { LockPickCard as LockBoardCard } from "./LockPickCard";
