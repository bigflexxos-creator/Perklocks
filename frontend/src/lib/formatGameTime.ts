/**
 * Single source of truth for rendering game start times in the UI.
 *
 * Accepts an ISO-8601 string (UTC) and returns a localized, human-friendly
 * label:
 *   • "Today · 7:30 PM"
 *   • "Tomorrow · 1:00 PM"
 *   • "Sat, Jun 21 · 9:00 PM"  (anything ≥ 2 days out)
 *
 * Returns `""` for null/undefined/invalid inputs so callers can safely
 * gate rendering with `{label && <Text>{label}</Text>}`.
 */
export function formatGameTime(iso: string | null | undefined): string {
  if (!iso) return "";
  try {
    const dt = new Date(iso);
    if (Number.isNaN(dt.getTime())) return "";
    const now = new Date();
    const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const tomorrowStart = new Date(todayStart);
    tomorrowStart.setDate(todayStart.getDate() + 1);
    const dayAfter = new Date(todayStart);
    dayAfter.setDate(todayStart.getDate() + 2);
    const time = dt.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
    if (dt >= todayStart && dt < tomorrowStart) return `Today · ${time}`;
    if (dt >= tomorrowStart && dt < dayAfter) return `Tomorrow · ${time}`;
    const date = dt.toLocaleDateString(undefined, {
      weekday: "short", month: "short", day: "numeric",
    });
    return `${date} · ${time}`;
  } catch {
    return "";
  }
}

/**
 * Compact variant for tight UI like the bet-slip leg row.
 * Examples: "Tonight 7:30p", "Tmrw 1:00p", "Sat 9:00p".
 */
export function formatGameTimeShort(iso: string | null | undefined): string {
  if (!iso) return "";
  try {
    const dt = new Date(iso);
    if (Number.isNaN(dt.getTime())) return "";
    const now = new Date();
    const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const tomorrowStart = new Date(todayStart);
    tomorrowStart.setDate(todayStart.getDate() + 1);
    const dayAfter = new Date(todayStart);
    dayAfter.setDate(todayStart.getDate() + 2);
    const time = dt
      .toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })
      .replace(" AM", "a").replace(" PM", "p")
      .replace(" am", "a").replace(" pm", "p");
    if (dt >= todayStart && dt < tomorrowStart) return `Today ${time}`;
    if (dt >= tomorrowStart && dt < dayAfter) return `Tmrw ${time}`;
    const day = dt.toLocaleDateString(undefined, { weekday: "short" });
    return `${day} ${time}`;
  } catch {
    return "";
  }
}
