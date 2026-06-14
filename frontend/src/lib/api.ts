import { storage } from "@/src/utils/storage";

// Backend URL resolution:
//   1. Prefer EXPO_PUBLIC_BACKEND_URL if set at build time (dev preview / native builds)
//   2. Fall back to the app's own origin in the browser so the published web app
//      automatically uses its own production domain (Emergent serves /api/* from
//      the same origin)
//   3. Empty string as last resort (relative URL)
function resolveBaseUrl(): string {
  const envUrl = process.env.EXPO_PUBLIC_BACKEND_URL;
  if (envUrl && envUrl.trim().length > 0) {
    // Avoid pinning the bundle to the dev preview URL in production builds.
    if (typeof window !== "undefined" && window.location && window.location.origin) {
      const previewMismatch =
        envUrl.includes(".preview.emergentagent.com") &&
        !window.location.origin.includes(".preview.emergentagent.com");
      if (previewMismatch) return window.location.origin;
    }
    return envUrl;
  }
  if (typeof window !== "undefined" && window.location && window.location.origin) {
    return window.location.origin;
  }
  return "";
}
const BASE_URL = resolveBaseUrl();

export type Pick = {
  id: string;
  sport: string;
  league: string;
  event: string;
  event_time?: string | null;
  market: string;
  selection: string;
  win_probability: number;
  book_odds: number;
  implied_probability: number;
  edge_percent: number;
  lock_score: number;
  grade: "Elite Lock" | "Strong Lock" | "Good Bet" | "Pass";
  confidence: string;
  factors: Record<string, number>;
  key_insights: string[];
  explanation?: string;
  pick_date: string;
};

export type User = { id: string; email: string; name?: string };
export type LineType = "both" | "main" | "alt";
export type SortKey = "lock" | "time" | "edge" | "implied";

export type PickFilters = {
  minLock?: number;
  minImplied?: number;
  maxImplied?: number;
};

const TOKEN_KEY = "lockscore_token";

export async function getToken(): Promise<string | null> {
  return await storage.secureGet(TOKEN_KEY, "");
}

export async function setToken(t: string | null): Promise<void> {
  if (!t) await storage.secureRemove(TOKEN_KEY);
  else await storage.secureSet(TOKEN_KEY, t);
}

async function request<T>(
  path: string,
  opts: { method?: string; body?: any; auth?: boolean } = {},
): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "application/json",
  };
  if (opts.auth !== false) {
    const tok = await getToken();
    if (tok) headers.Authorization = `Bearer ${tok}`;
  }
  const res = await fetch(`${BASE_URL}/api${path}`, {
    method: opts.method || "GET",
    headers,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const text = await res.text();
  let data: any = {};
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { detail: text };
  }
  if (!res.ok) {
    const msg = data?.detail || `Request failed (${res.status})`;
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return data as T;
}

export const api = {
  register: (email: string, password: string, name?: string) =>
    request<{ access_token: string; user: User }>("/auth/register", {
      method: "POST",
      body: { email, password, name },
      auth: false,
    }),
  login: (email: string, password: string) =>
    request<{ access_token: string; user: User }>("/auth/login", {
      method: "POST",
      body: { email, password },
      auth: false,
    }),
  me: () => request<User>("/auth/me"),
  picksToday: (sport?: string, lineType?: LineType, sortKey?: SortKey, filters?: PickFilters) => {
    const qs = new URLSearchParams();
    if (sport && sport !== "All") qs.set("sport", sport);
    if (lineType && lineType !== "both") qs.set("line_type", lineType);
    if (sortKey && sortKey !== "lock") qs.set("sort", sortKey);
    if (filters?.minLock != null && filters.minLock > 85) qs.set("min_lock", String(filters.minLock));
    if (filters?.minImplied != null) qs.set("min_implied", String(filters.minImplied));
    if (filters?.maxImplied != null) qs.set("max_implied", String(filters.maxImplied));
    const q = qs.toString();
    return request<{ picks: Pick[] }>(`/picks/today${q ? `?${q}` : ""}`);
  },
  picksAll: (sport?: string) =>
    request<{ picks: Pick[] }>(`/picks/all${sport && sport !== "All" ? `?sport=${sport}` : ""}`),
  betKiller: (sport?: string) =>
    request<{ picks: Pick[] }>(`/picks/bet-killer${sport && sport !== "All" ? `?sport=${sport}` : ""}`),
  rollover: (lineType?: LineType) =>
    request<{ picks: Pick[]; pick: Pick | null; composite_rank?: number; total_evaluated?: number }>(
      `/picks/rollover${lineType && lineType !== "both" ? `?line_type=${lineType}` : ""}`,
    ),
  underOfTheDay: (lineType?: LineType, sortKey?: SortKey) => {
    const qs = new URLSearchParams();
    if (lineType && lineType !== "both") qs.set("line_type", lineType);
    if (sortKey && sortKey !== "lock") qs.set("sort", sortKey);
    const q = qs.toString();
    return request<{ pick: Pick | null; alternates: Pick[]; total_evaluated: number; scoped_to_today?: boolean }>(
      `/picks/under-of-the-day${q ? `?${q}` : ""}`,
    );
  },
  parlay: (legs: number = 3, mode: "standard" | "high_risk" = "standard", sport?: string, lineType?: LineType) => {
    const qs = new URLSearchParams({ legs: String(legs), mode });
    if (sport && sport !== "mix") qs.set("sport", sport);
    if (lineType && lineType !== "both") qs.set("line_type", lineType);
    return request<{ parlay: null | {
      legs: Pick[]; leg_count: number;
      combined_decimal_odds: number; combined_american_odds: string;
      combined_win_probability: number; payout_on_100: number; profit_on_100: number;
    }; reason?: string }>(`/picks/parlay?${qs.toString()}`);
  },
  pickDetail: (id: string) => request<Pick & { ai_pending?: boolean }>(`/picks/${id}`),
  pickAiExplain: (id: string) => request<{ explanation: string; source: string }>(`/picks/${id}/ai-explain`, { method: "POST" }),
  refresh: () => request<{ refreshed: boolean; count: number; date: string }>("/picks/refresh", { method: "POST" }),
  history: (days: number = 30, rolloverOnly = false) =>
    request<{
      picks: (Pick & { status?: string; settled_at?: string; final_score?: Record<string, number>; loss_analysis?: string })[];
      stats: { total: number; won: number; lost: number; push: number; hit_rate: number; rollover_hit_rate: number; rollover_decided: number };
    }>(`/picks/history?days=${days}${rolloverOnly ? "&rollover_only=true" : ""}`),
  lossAnalysis: (id: string) =>
    request<{ analysis: string; source: string }>(`/picks/${id}/loss-analysis`, { method: "POST" }),
  triggerSettle: () =>
    request<{ settled: number; won: number; lost: number; push: number; skipped: number; props_pending: number }>("/picks/settle", { method: "POST" }),
  stats: () => request<{
    date: string; total_picks: number; elite_count: number;
    avg_edge_percent: number;
    by_sport: { sport: string; count: number; avg_lock: number; avg_edge: number; elite_count: number }[];
  }>("/stats/summary"),
};
