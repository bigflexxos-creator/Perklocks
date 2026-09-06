// MAIN 40 · Closure item #9 (2026-06-05) — safe back navigation.
//
// `router.back()` raises `GO_BACK was not handled by any navigator` when
// the app enters a route directly (deep link, Expo Go bundle reload,
// share-open) so the navigation stack has no parent to pop.  This
// helper guards every back button with `canGoBack()` and falls back to
// a deterministic parent tab so the user is never dead-ended.
//
// Usage: `onPress={() => safeBack("/(tabs)/")}`
import { router } from "expo-router";

export function safeBack(fallback: string = "/(tabs)/"): void {
  try {
    // expo-router exposes `canGoBack()` on the imperative router since
    // v3; if it returns true a back pop is safe.
    // @ts-expect-error — canGoBack is available at runtime.
    if (typeof router.canGoBack === "function" && router.canGoBack()) {
      router.back();
      return;
    }
  } catch {
    /* fall through to replace */
  }
  try {
    router.replace(fallback as never);
  } catch {
    // Absolute last resort — no-op.  Never throw from a back handler.
  }
}
