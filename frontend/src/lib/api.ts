import { storage } from "@/src/utils/storage";

// Backend URL resolution:
//   1. Prefer EXPO_PUBLIC_BACKEND_URL if set at build time (dev preview / native builds)
//   2. Fall back to the app's own origin in the browser so the published web app
//      automatically uses its own production domain (Emergent serves /api/* from
//      the same origin)
//   3. Empty string as last resort (relative URL)
//
// ── PUBLISHED-APP PIN OVERRIDE (2026-06-29) ──
// Earlier today the production deployed backend at emergent.host was
// 29x slower than the dev preview and returning Cloudflare 520s, so we
// temporarily shipped the published bundle pinned to the healthy
// preview URL. Emergent support fixed the production backend
// (maxPoolSize → 20, Grow resource tier, SPORTDB_API_KEY corrected,
// deferred startup deployed). Production now responds in <500ms.
// FORCE_PREVIEW_BACKEND is back to FALSE so the published app routes
// to its proper production backend.
const FORCE_PREVIEW_BACKEND = false;
const PINNED_PREVIEW_URL = "https://bet-edge-ai-1.preview.emergentagent.com";

function resolveBaseUrl(): string {
  if (FORCE_PREVIEW_BACKEND) return PINNED_PREVIEW_URL;
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
  grade: "Elite Lock" | "Strong Lock" | "Lock" | "Playable" | "Pass";
  confidence: string;
  factors: Record<string, number>;
  key_insights: string[];
  top_reasons?: string[];
  edge_score?: number;
  confidence_score?: number;
  risk_score?: number;
  explanation?: string;
  elite_player?: boolean;
  elite_player_name?: string;
  pick_date: string;

  // ─── Lock Engine V2 (shadow mode) — populated when ENABLE_COUNTER_ENGINE=true
  lock_score_v2?: number;
  tier_v2?: "Apex Lock" | "Rare Lock" | "Strong Lock" | "Elite Setup" | string;
  is_apex?: boolean;
  apex_blockers?: string[];
  counter_score?: number;
  survival_score?: number;
  evidence_score?: number;
  conviction_score?: number;
  // STICKY 95+ pin — picks that ever crossed 95 lock_score get pinned
  // to the board across refresh cycles so users who saw a 99-lock can
  // always find it. `lock_score_peak` carries the all-time-high.
  pinned?: boolean;
  lock_score_peak?: number;
  lock_score_raw?: number;

  // ─── Sportsbook Mapping Engine (book-agnostic + per-book deep links) ──
  selection_v2?: {
    league: string;
    league_label: string;
    sport: string;
    event: {
      home: string;
      away: string;
      kickoff: string | null;
      date: string;
      slug: string;
    };
    market: { family: string; subtype: string; label: string };
    selection: {
      side: string | null;
      team: string | null;
      player: string | null;
      line: number | null;
      label: string;
    };
  };
  sportsbook_mapping?: Record<string, {
    supports_deep_link: boolean;
    deep_link: string | null;
    event_id?: string | null;
    market_id?: string | null;
    selection_id?: string | null;
    event_url: string | null;
    search_url: string | null;
    league_url: string | null;
    home_url: string;
    best_link: string;
    best_depth: "selection" | "event" | "search" | "league" | "home";
    search_query: string;
  }>;

  // ─── Monte Carlo Simulator (Phase A — MLB only) ─────────────────────
  sim_win_probability?: number;        // 0–100, P(win) from 10k MC runs
  sim_ci_lower?: number;               // 95% Wilson CI lower bound (0–100)
  sim_ci_upper?: number;               // 95% Wilson CI upper bound (0–100)
  sim_runs?: number;                   // # of Monte Carlo iterations
  sim_threshold?: number;              // over/under line being simulated
  sim_is_under?: boolean;              // true if Under bet
  sim_disagreement_with_model?: number;// sim_wp − blended model wp
  sim_signal?: "stronger" | "weaker" | "neutral";
  sim_lock_lift?: number;              // ± points applied to lock_score
  // Soccer goal scorer specifics
  sim_player_xg?: number;              // Player's expected goals this match
  sim_expected_goals?: number;         // Sim mean over RUNS
  sim_p_score_2plus?: number;          // P(scores 2+ goals) %
  sim_p_hattrick?: number;             // P(scores 3+ goals) %
  sim_shots_per_game?: number;         // Parsed from key_insights
  sim_recent_goal_rate?: number;       // Recent 'scored in N of last M' rate (%)
  sim_opp_concedes?: number;           // Opponent goals conceded / match
  sim_player_xg_per_game?: number;     // Player career xG/match (parsed)
  // ─── Player Form (from live learning store) ─────────────────────────
  player_form?: {
    name: string;
    n_picks: number;
    hit_rate?: number;
    last5_hit?: number;
    last10_hit?: number;
    current_streak?: number;            // +N consecutive wins, -N consecutive losses
  };

  // ─── "Why this pick?" rationale (built by backend/pick_enrichment.py)
  // Universal shape across all sports — fields populated vary per source.
  // Rendered as the collapsible audit panel on LockPickCard.
  pick_rationale?: PickRationale;
};

export type PickRationale = {
  summary?: string;
  data_source?: string;
  engine?: string;               // e.g. "mlb_hitter_intel" when MLB intel ran
  evidence?: string[];           // ✅ "Why we like it" bullets
  concerns?: string[];           // ⚠️ "Watch-outs" bullets
  espn_rank?: number | null;     // soccer-scorer leaderboard rank
  stats_this_season?: Record<string, any> | null;
  model_win_prob_pct?: number;
  edge_percent?: number;
  lock_score?: number;
  // MLB hitter-intel only
  matchup?: {
    batter?: string;
    batter_hand?: string;
    pitcher?: string;
    pitcher_hand?: string;
    ballpark?: string | null;
    is_home?: boolean;
    batting_order?: number | null;
  };
  splits?: Record<string, number | null>;
  pitcher_quality?: Record<string, number | null>;
  recent_form?: Record<string, number | null>;
  multipliers?: Record<string, number>;
  base_form_pct?: number;
  final_hit_prob_pct?: number;
  confidence_score?: number;     // 0–100, MLB intel
  lean?: "OVER" | "UNDER" | "PASS" | string;
  edge_pct_points?: number;
  model_prob?: number;
};

export type User = { id: string; email: string; name?: string };
export type LineType = "both" | "main" | "alt";
export type SortKey = "lock" | "time" | "edge" | "win" | "implied";
export type SortDirection = "desc" | "asc";

export type PickFilters = {
  minLock?: number;
  minImplied?: number;
  maxImplied?: number;
  /** Market filter token — matched against /picks/markets/{sport} tokens. */
  market?: string;
  /** League substring match. */
  league?: string;
  /** Specific event/game filter (e.g. "PSG @ Arsenal"). Applied
   *  client-side so it instantly narrows the visible board to a single
   *  match without a backend round-trip. */
  event?: string;
  /** When TRUE, show only picks where the Monte Carlo simulator hit ≥85%
   *  AND agrees with the model by ≥5pp. Applied client-side.
   *  DEPRECATED in favour of `simEdgeFloor` (2026-06-24) — kept for
   *  backward compat with old persisted filter state. */
  simEdgeOnly?: boolean;
  /** Sim Edge floor (0–100). When > 0, hide picks below this Monte
   *  Carlo win-probability threshold. 0 / undefined = no filter. */
  simEdgeFloor?: number;
};

export type SportMarket = { token: string; label: string };
export type SportLeague = { name: string; count: number };

// xG Form A/B shadow analytics — one bucket per HOT/COLD/NEUTRAL form label.
// Returned by `GET /api/analytics/xg-form-shadow`. Rendered on the
// Analytics screen so the user can monitor whether the ±6pp form lift
// would have improved hit rate before promoting it from shadow → live.
export type XGFormBucket = {
  n:             number;
  won:           number;
  lost:          number;
  hit_rate:      number | null;   // % (won / n × 100)
  avg_lock:      number | null;   // mean displayed lock_score
  avg_shadow:    number | null;   // mean shadow lock_score (with lift)
  brier_live:    number | null;   // mean squared error of live win prob
  brier_shadow:  number | null;   // mean squared error of shadow win prob
  delta_hit_pp:  number | null;   // hit_rate − NEUTRAL.hit_rate (pp)
};

// Parlay Optimizer V1 — Top 3 cards with health grade + reasoning
export type ParlayCard = {
  label: "SAFE" | "BALANCED" | "AGGRESSIVE";
  grade: "A" | "B" | "C" | "D" | "F";
  strength_score: number;            // 0-100 health composite
  leg_count: number;
  legs: Pick[];
  survival_pct: number;              // estimated hit rate
  avg_edge_pct: number;
  avg_roi_pct: number;
  avg_win_prob: number;
  diversification_pct: number;       // 100 - max-sport-concentration
  correlation_score: number;
  stability_score: number;
  combined_decimal_odds: number;
  combined_american_odds: string;
  payout_on_100: number;
  profit_on_100: number;
  reasons: string[];                 // Why-this-parlay bullets
};

const TOKEN_KEY = "lockscore_token";

export async function getToken(): Promise<string | null> {
  return await storage.secureGet(TOKEN_KEY, "");
}

export async function setToken(t: string | null): Promise<void> {
  if (!t) await storage.secureRemove(TOKEN_KEY);
  else await storage.secureSet(TOKEN_KEY, t);
}

// ── Reliability layer: retries + timeout + in-flight dedupe ─────────────
// Surgical hardening that doesn't change response shapes. Adds:
//   1. 10-second per-request timeout via AbortController
//   2. Up to 2 retries on network errors / 5xx (exponential 300ms → 900ms)
//   3. In-flight GET deduplication — identical concurrent GETs share one
//      network call. Prevents accidental thundering-herd from React
//      double-renders. Mutations (POST/PUT/DELETE) NEVER dedupe.
const REQUEST_TIMEOUT_MS = 10_000;
const MAX_RETRIES = 2;
const _inflight = new Map<string, Promise<any>>();

async function _fetchWithTimeout(url: string, init: RequestInit, timeoutMs: number) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    return await fetch(url, { ...init, signal: ctrl.signal });
  } finally {
    clearTimeout(timer);
  }
}

async function request<T>(
  path: string,
  opts: { method?: string; body?: any; auth?: boolean } = {},
): Promise<T> {
  const method = (opts.method || "GET").toUpperCase();
  const url = `${BASE_URL}/api${path}`;

  // In-flight dedupe — GETs only (mutations must NEVER be deduped).
  const dedupeKey = method === "GET" ? `${method}:${url}` : null;
  if (dedupeKey && _inflight.has(dedupeKey)) {
    return _inflight.get(dedupeKey) as Promise<T>;
  }

  const exec = async (): Promise<T> => {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      Accept: "application/json",
      // Force every layer (browser HTTP cache, iOS NSURLCache, any
      // CDN/edge proxy in front of the backend) to bypass cached
      // responses and hit our origin. Without these, iOS in particular
      // will happily serve a hours-old `/picks/today` payload to the
      // app even though the same URL in Safari shows fresh data.
      "Cache-Control": "no-cache, no-store, must-revalidate",
      "Pragma": "no-cache",
    };
    if (opts.auth !== false) {
      const tok = await getToken();
      if (tok) headers.Authorization = `Bearer ${tok}`;
    }
    // Cache-buster query param on GETs ensures any intermediate cache that
    // ignores headers (some CDNs do) can't serve a stale entry — the URL
    // itself is unique per request.
    let finalUrl = url;
    if (method === "GET") {
      const sep = url.includes("?") ? "&" : "?";
      finalUrl = `${url}${sep}_=${Date.now()}`;
    }
    const init: RequestInit = {
      method,
      headers,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
      // RN's fetch ignores this on iOS but it's the canonical way to
      // disable HTTP caching on web/Hermes for parity.
      cache: "no-store",
    };

    let lastErr: any = null;
    for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
      try {
        const res = await _fetchWithTimeout(finalUrl, init, REQUEST_TIMEOUT_MS);
        const text = await res.text();
        let data: any = {};
        try { data = text ? JSON.parse(text) : {}; }
        catch { data = { detail: text }; }
        if (!res.ok) {
          // ── 401 auto-recover (2026-06-26) ───────────────────────
          // A 401 means our stored token is invalid — either the
          // session expired, OR (more commonly) the backend rotated
          // JWT_SECRET so every token signed with the old secret is
          // now dead. Without this branch, the request handler
          // throws → home tab catches it silently → user sees an
          // empty "no locks on board" with no path forward, since
          // their token stays in storage and every refresh hits
          // 401 again. Fix: drop the dead token + fire a global
          // event the AuthContext listens to so the app bounces
          // back to /login. Skips on the auth endpoints themselves
          // (a bad login attempt is a legitimate 401 we want the
          // caller to handle, not redirect).
          if (res.status === 401 && !path.startsWith("/auth/") && opts.auth !== false) {
            try { await setToken(null); } catch {}
            try {
              // Fire a custom global event for the AuthContext to
              // pick up. Wrapped in try/catch so it's a no-op in
              // any runtime that lacks `EventTarget`.
              if (typeof globalThis !== "undefined" && (globalThis as any).dispatchEvent) {
                (globalThis as any).dispatchEvent(
                  new CustomEvent("perkslocks:auth-expired", { detail: { path } }),
                );
              }
            } catch {}
          }
          // 4xx errors are intentional — don't retry (e.g. 401 means bad
          // creds, retrying won't help). Only retry 5xx + 408 + 429.
          const shouldRetry = res.status >= 500 || res.status === 408 || res.status === 429;
          if (!shouldRetry || attempt === MAX_RETRIES) {
            const msg = data?.detail || data?.error || `Request failed (${res.status})`;
            throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
          }
          lastErr = new Error(`HTTP ${res.status}`);
        } else {
          return data as T;
        }
      } catch (err: any) {
        // Network error / timeout / abort → retry
        lastErr = err;
        if (attempt === MAX_RETRIES) break;
      }
      // Exponential backoff: 300ms → 900ms
      await new Promise((r) => setTimeout(r, 300 * Math.pow(3, attempt)));
    }
    throw lastErr || new Error("Request failed");
  };

  if (dedupeKey) {
    const promise = exec().finally(() => _inflight.delete(dedupeKey));
    _inflight.set(dedupeKey, promise);
    return promise;
  }
  return exec();
}

export const api = {
  // Generic typed request helper — exposed so feature screens (e.g.
  // admin dashboard) can call new endpoints without us having to add
  // a wrapper for every single one.
  request,
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
  playerForm: (pickId: string) =>
    request<{
      player_name: string;
      team: string;
      league: string;
      season: string;
      position: string;
      games: number;
      minutes: number;
      goals: number;
      xg: number;
      npxg: number;
      assists: number;
      xa: number;
      shots: number;
      key_passes: number;
      xg_per_90: number;
      npxg_per_90: number;
      goals_per_90: number;
      shots_per_90: number;
      goals_over_xg: number;
      form_label: "HOT" | "COLD" | "NEUTRAL";
      form_score: number;
      form_lift: number;
      updated_at: string | null;
      source: string;
    }>(`/picks/${pickId}/player-form`),
  pickProbability: (pickId: string) =>
    request<{
      p_v1: number;
      p_v2: number;
      sim_probability: number | null;
      p_final: number;
      p_calibrated: number;
      edge: number;
      classification: string;
      simulator_variance: number | null;
      sim_ran?: boolean;
      stability_score: number | null;
      implied_probability: number;
      weights: { v1: number; v2: number; sim: number };
      effective_weights?: { v1: number; v2: number; sim: number };
      calibration: { fit_sample_size: number; last_fit_at: string | null };
    }>(`/picks/${pickId}/probability`),
  version: () =>
    request<{ data_version: string; server_time: string; server_started_at: string }>(
      "/version",
      { auth: false },
    ),
  xgFormShadow: () =>
    request<{
      buckets: {
        HOT:     XGFormBucket;
        COLD:    XGFormBucket;
        NEUTRAL: XGFormBucket;
      };
      promote_ready:   boolean;
      promotion_rule:  string;
      shadow_mode:     boolean;
      generated_at:    string;
    }>("/analytics/xg-form-shadow"),
  picksToday: (sport?: string, lineType?: LineType, sortKey?: SortKey, filters?: PickFilters, direction?: SortDirection, extra?: { sports?: string[]; leagues?: string[]; markets?: string[]; gameIds?: string[]; search?: string }) => {
    const qs = new URLSearchParams();
    if (sport && sport !== "All") qs.set("sport", sport);
    if (lineType && lineType !== "both") qs.set("line_type", lineType);
    // Always emit `sort` — the backend default is `time`, not `lock`, so
    // omitting `sort=lock` silently flips us to chronological order and
    // the "Sort: Lock High→Low" pill stops actually applying.
    // (Bug 2026-06-26: "filter by lock doesn't work, time/win pct do".)
    if (sortKey) qs.set("sort", sortKey);
    if (direction) qs.set("direction", direction);
    // ── New unified multi-select params (CSV) ───────────────────
    // Forwarded from the global `useFilters` store. Empty arrays
    // are no-ops; backend treats absence as "all".
    if (extra?.sports?.length)   qs.set("sports",   extra.sports.join(","));
    if (extra?.leagues?.length)  qs.set("leagues",  extra.leagues.join(","));
    if (extra?.markets?.length)  qs.set("markets",  extra.markets.join(","));
    if (extra?.gameIds?.length)  qs.set("game_ids", extra.gameIds.join(","));
    if (extra?.search)           qs.set("search",   extra.search);
    if (filters?.minLock != null && filters.minLock > 85) qs.set("min_lock", String(filters.minLock));
    if (filters?.minImplied != null) qs.set("min_implied", String(filters.minImplied));
    if (filters?.maxImplied != null) qs.set("max_implied", String(filters.maxImplied));
    if (filters?.market) qs.set("market", filters.market);
    if (filters?.league) qs.set("league", filters.league);
    // Lite payload — strip detail-only fields (sportsbook_mapping,
    // evidence_breakdown, probability, etc). 5x smaller payload
    // (~1.5MB → ~300KB) for a much snappier home tab. The pick-detail
    // screen calls /api/picks/{id} separately and still gets the full
    // document, so no UX regression. (Perf, 2026-06-25.)
    qs.set("lite", "true");
    const q = qs.toString();
    return request<{ picks: Pick[] }>(`/picks/today${q ? `?${q}` : ""}`);
  },
  sportMarkets: (sport: string) =>
    request<{ sport: string; markets: SportMarket[]; leagues: SportLeague[] }>(
      `/picks/markets/${encodeURIComponent(sport)}`,
    ),
  picksAll: (sport?: string) =>
    request<{ picks: Pick[] }>(`/picks/all${sport && sport !== "All" ? `?sport=${sport}` : ""}`),
  // (Bet Killer endpoint removed — superseded by Under-of-the-Day.)
  rollover: (lineType?: LineType, filters?: PickFilters, sport?: string) => {
    const qs = new URLSearchParams();
    if (lineType && lineType !== "both") qs.set("line_type", lineType);
    if (sport && sport !== "All") qs.set("sport", sport);
    if (filters?.market) qs.set("market", filters.market);
    if (filters?.league) qs.set("league", filters.league);
    const q = qs.toString();
    return request<{ picks: Pick[]; pick: Pick | null; composite_rank?: number; total_evaluated?: number }>(
      `/picks/rollover${q ? `?${q}` : ""}`,
    );
  },
  underOfTheDay: (lineType?: LineType, sortKey?: SortKey, filters?: PickFilters, sport?: string) => {
    const qs = new URLSearchParams();
    if (lineType && lineType !== "both") qs.set("line_type", lineType);
    if (sortKey && sortKey !== "lock") qs.set("sort", sortKey);
    if (sport && sport !== "All") qs.set("sport", sport);
    if (filters?.market) qs.set("market", filters.market);
    if (filters?.league) qs.set("league", filters.league);
    const q = qs.toString();
    return request<{
      pick: Pick | null;
      alternates: Pick[];
      total_evaluated?: number;
      scoped_to_today?: boolean;
    }>(`/picks/under-of-the-day${q ? `?${q}` : ""}`);
  },
  parlay: (legs: number = 3, mode: "standard" | "high_risk" | "today_window" | "advanced" = "standard",
           sport?: string, lineType?: LineType,
           includeSports: string[] = [],
           filters?: PickFilters, rank: number = 1, lockedIds: string[] = [],
           sportMode: "auto" | "custom" | "single" = "auto",
           windowHours: number = 24,
           excludeSports: string[] = [],
           refreshNonce: number = 0,
           advancedSub?: "safer" | "ev") => {
    const qs = new URLSearchParams({
      legs: String(legs), mode, rank: String(rank),
      sport_mode: sportMode, window_hours: String(windowHours),
    });
    if (sportMode === "single" && sport && sport !== "mix") qs.set("sport", sport);
    if (sportMode === "custom" && includeSports.length > 0) {
      qs.set("include_sports", includeSports.join(","));
    }
    if (sportMode === "auto" && excludeSports.length > 0) {
      qs.set("exclude_sports", excludeSports.join(","));
    }
    if (lineType && lineType !== "both") qs.set("line_type", lineType);
    // Market filter only makes sense when the parlay is locked to one sport
    // (sport_mode=single). For mixed/auto/custom modes, sending market=
    // "pitcher_strikeouts" would filter every Soccer/Tennis/UFC pick out and
    // leave the optimizer with only MLB — which is how the user's parlay
    // tab ended up showing only one sport after a stale filter leaked in.
    if (sportMode === "single" && filters?.market) qs.set("market", filters.market);
    if (sportMode === "single" && filters?.league) qs.set("league", filters.league);
    if (lockedIds.length > 0) qs.set("locked_ids", lockedIds.join(","));
    if (refreshNonce > 0) qs.set("refresh_nonce", String(refreshNonce));
    if (mode === "advanced" && advancedSub) qs.set("advanced_sub", advancedSub);
    return request<{
      parlay: null | {
        legs: Pick[]; leg_count: number;
        combined_decimal_odds: number; combined_american_odds: string;
        combined_win_probability: number; payout_on_100: number; profit_on_100: number;
      };
      parlays?: ParlayCard[];
      rank?: number;
      locked_ids?: string[];
      window_hours?: number;
      sport_mode?: string;
      reason?: string;
    }>(`/picks/parlay?${qs.toString()}`);
  },
  pickDetail: (id: string) => request<Pick & { ai_pending?: boolean }>(`/picks/${id}`),
  saveParlay: (legs: Pick[], mode: string = "standard", stake: number = 1.0) =>
    request<{ id: string; status: string; combined_odds: number; legs_pending: number; legs_won: number; legs_lost: number; payout: number | null }>(
      "/parlay/save", { method: "POST", body: { legs, mode, stake } }
    ),
  parlayHistory: (filter?: "won" | "live" | "lost" | "all") => {
    const qs = filter && filter !== "all" ? `?filter=${filter}` : "";
    return request<{
      parlays: Array<{
        id: string; created_at: string; mode: string; combined_odds: number;
        stake: number; status: "live" | "won" | "lost";
        legs_won: number; legs_lost: number; legs_pending: number;
        settled_at: string | null; payout: number | null;
        legs: Array<{
          pick_id: string; sport: string; league: string; event: string;
          market: string; selection: string; book_odds: number;
          event_time: string; lock_score: number;
          status: "pending" | "won" | "lost" | "void";
        }>;
      }>;
      count: number;
    }>(`/parlay/history${qs}`);
  },
  deleteParlay: (id: string) => request<{ deleted: boolean }>(`/parlay/${id}`, { method: "DELETE" }),
  pickAiExplain: (id: string) => request<{ explanation: string; source: string }>(`/picks/${id}/ai-explain`, { method: "POST" }),
  pickSimulation: (id: string) =>
    request<{
      sim_win_probability: number;
      sim_ci_lower: number;
      sim_ci_upper: number;
      sim_runs: number;
      sim_threshold: number;
      sim_is_under: boolean;
      sim_disagreement_with_model: number;
      sim_signal: "stronger" | "weaker" | "neutral";
      // Risk Meter — five-number summary of the projected stat
      // distribution. Optional because pure ML picks have no
      // meaningful integer distribution and skip these fields.
      sim_pctl_p10?: number;
      sim_pctl_p25?: number;
      sim_pctl_p50?: number;
      sim_pctl_p75?: number;
      sim_pctl_p90?: number;
      sim_pctl_min?: number;
      sim_pctl_max?: number;
      sim_pctl_line?: number;
      sim_pctl_line_quantile_pct?: number;
      sim_pctl_n?: number;
    }>(`/picks/${id}/simulation`),
  pickEvidenceInspector: (id: string) =>
    request<{
      pick_id: string;
      sport: string;
      market: string;
      player_name: string | null;
      event: string;
      probability_pct: number | null;
      edge_pct: number | null;
      evidence_score: number | null;
      lock_score: number | null;
      lock_score_raw: number | null;
      evidence_breakdown: Record<string, any>;
      key_insights: string[];
      status: string;
    }>(`/admin/pick-evidence/${id}`),
  pitcherH2H: (id: string) =>
    request<{
      pitcher: string;
      opp_team: string;
      ok: boolean;
      error?: string;
      season_starts?: number;
      season_avg_k?: number;
      vs_team_starts?: number;
      vs_team_avg_k?: number;
      vs_team_recent?: Array<{
        date: string;
        opp: string;
        k: number;
        ip: string;
      }>;
    }>(`/picks/${id}/pitcher-h2h`),
  simBacktest: (days: number = 30, sport?: string) =>
    request<{
      n: number;
      days: number;
      sport?: string | null;
      message?: string;
      brier?: number;
      log_loss?: number;
      brier_skill_score?: number;
      calibration?: Array<{
        bucket: string;
        n: number;
        expected_pct: number;
        observed_pct: number;
        delta: number;
      }>;
      strategies?: Record<string, { bets: number; units: number; roi_pct: number }>;
      by_sport?: Record<string, any>;
    }>(`/analytics/sim-backtest?days=${days}${sport ? `&sport=${encodeURIComponent(sport)}` : ""}`),
  pickCoverage: (id: string, cohort: "teammates" | "league" = "teammates") =>
    request<{
      pick_id?: string;
      primary?: { name?: string; id?: number; miss_games?: number };
      reliability: "High Sample" | "Medium Sample" | "Low Sample";
      survival_index: number;
      candidates: Array<{
        id: number;
        name: string;
        position?: string;
        score: number;
        streak: string;
        last10: { hit: number; n: number; rate: number };
        last30: { hit: number; n: number; rate: number };
        season: { hit: number; n: number; rate: number };
        label: string;
      }>;
      cohort_size: number;
      computed_at: string;
      note?: string;
    }>(`/picks/${id}/coverage?cohort=${cohort}`),
  pickLockBreakdown: (id: string) =>
    request<{
      pick_id: string;
      v2_enabled: boolean;
      live_computed?: boolean;
      shadow: {
        evidence_score?: number;
        conviction_score?: number;
        counter_score?: number;
        survival_score?: number;
        variance_score?: number;
        simulation_pass?: number;
        agreement_score?: number;
        lock_score_v2?: number;
        tier_v2?: string;
        is_apex?: boolean;
        apex_blockers?: string[];
        v2_reasons?: {
          evidence: Array<[string, string, string]>;
          counter:  Array<[string, string, string]>;
          survival: Array<[string, string, string]>;
        };
      };
    }>(`/picks/${id}/lock-breakdown`),
  pickMarketRank: (id: string) =>
    request<{
      pick_id: string;
      event: string;
      sport: string;
      total: number;
      ranked: Array<{
        id: string;
        market: string;
        short_market: string;
        selection?: string;
        win_probability?: number;
        edge_percent?: number;
        book_odds?: number;
        lock_score?: number;
        lock_score_v2?: number;
        tier_v2?: string;
        is_apex?: boolean;
        market_score: number;
        is_current: boolean;
      }>;
      best?: any;
      alternatives: any[];
      rule: "single" | "co_best" | "best_with_alts" | "dominant" | "no_candidates";
    }>(`/picks/${id}/market-rank`),
  pickScorerBundles: (id: string) =>
    request<{
      pick_id: string;
      eligible: boolean;
      synthesizable?: boolean;
      note?: string;
      player?: string;
      primary_market?: string;
      primary_odds?: number;
      primary_implied_pct?: number;
      "expected_goals_\u03bb"?: number;
      bundles?: Array<{
        name: string;
        type: "primary" | "synthesized";
        probability: number;
        fair_american: string;
        decimal: number;
      }>;
      method?: string;
    }>(`/picks/${id}/scorer-bundles`),
  soccerLabLeagues: (refresh = false) =>
    request<{
      leagues: Array<{ key: string; title: string; group: string; description: string; has_outrights: boolean }>;
      count: number;
      age_sec: number;
      source: string;
      fetched_at: number;
    }>(`/soccer-lab/leagues${refresh ? "?refresh=true" : ""}`),
  soccerLabFeed: (limit = 50, min_lock = 78, sport = "Soccer") =>
    request<{
      count: number;
      total_returned: number;
      min_lock: number;
      picks: Array<{
        id: string;
        league: string;
        event: string;
        event_time: string;
        market: string;
        selection?: string;
        book_odds?: number;
        win_probability?: number;
        edge_percent?: number;
        lock_score?: number;
        lock_score_v2?: number;
        tier_v2?: string;
        is_apex?: boolean;
        grade?: string;
        confidence: number;
        implied_probability?: number;
      }>;
      league_distribution: Array<{ league: string; count: number }>;
    }>(`/soccer-lab/feed?limit=${limit}&min_lock=${min_lock}&sport=${encodeURIComponent(sport)}`),
  nrfiYrfi: () =>
    request<{
      count: number;
      category: string;
      picks: Array<{
        id: string;
        sport: string;
        market: string;
        side: "NRFI" | "YRFI";
        lock_score: number;
        grade: string;
        win_probability: number;
        edge_percent: number;
        match: string;
        home_team: string;
        away_team: string;
        event_time: string;
        key_insights: string[];
        model_inputs: {
          league_base: number;
          pitcher_factor: number;
          lineup_top_factor: number;
          park_factor: number;
        };
        model_output: {
          expected_runs_1st_inning: number;
          nrfi_prob: number;
          yrfi_prob: number;
        };
      }>;
    }>("/picks/nrfi-yrfi"),
  refresh: () => request<{
    refreshed: boolean;
    count: number;
    date: string;
    cooldown_seconds?: number;
    next_refresh_at?: string | null;
    last_refresh_at?: string | null;
    rate_limited?: boolean;
    retry_after_minutes?: number;
    message?: string;
  }>("/picks/refresh", { method: "POST" }),
  refreshStatus: () => request<{
    can_refresh: boolean;
    cooldown_seconds: number;
    next_refresh_at: string | null;
    last_refresh_at: string | null;
  }>("/picks/refresh-status"),
  mlbLive: () => request<{
    games: Record<string, {
      home: string;
      away: string;
      home_score: number | null;
      away_score: number | null;
      status: string;
      abstract_status: string;
      is_live: boolean;
      is_final: boolean;
    }>;
    as_of: string;
  }>("/mlb/live"),
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
  modelPerformance: () => request<{
    as_of: string;
    totals: {
      picks: number; wins: number; losses: number; pushes: number; decisive: number;
      hit_rate: number; units_risked: number; units_won: number; roi_pct: number;
      units_profit_7d: number; units_profit_30d: number;
      avg_edge_pct: number; avg_clv: number; positive_clv_pct: number;
    };
    by_sport: AnalyticsRow[];
    by_market: AnalyticsRow[];
    by_confidence: AnalyticsRow[];
    calibration: { band: string; count: number; avg_lock_score: number; actual_hit_rate: number; delta: number }[];
    highlights: {
      best_sport: AnalyticsRow | null;
      best_market: AnalyticsRow | null;
      worst_market: AnalyticsRow | null;
    };
  }>("/analytics/model-performance"),

  learnedWeights: () => request<{
    sample_size: number;
    updated_at: string | null;
    buckets: { sport: string; market_label: string; n: number; wins: number; losses: number;
               hit_rate: number; expected_wp: number; roi: number; weight: number; active: boolean }[];
    calibration: { band: string; n: number; actual: number; expected: number; delta: number;
                   adjustment: number; active: boolean }[];
    settings?: { min_samples: number; max_wp_delta: number; max_cal_delta: number };
  }>("/analytics/learned-weights"),

  learnNow: () => request<{ active_buckets: number; picks_adjusted: number; sample_size: number }>(
    "/analytics/learn", { method: "POST" }),
  analyticsV2: () => request<{
    as_of?: string;
    total_settled?: number;
    market_rows?: Array<{
      sport: string; market: string; n: number; won: number; lost: number;
      units_risked: number; units_profit: number; roi: number; hit_rate: number;
      clv_avg: number; calibration_err: number; composite_weight?: number;
    }>;
    market_weights?: Record<string, number>;
    band_calibration?: Array<{ band: string; n: number; expected: number; actual: number; gap: number; needs_gate_raise: boolean }>;
    band_raises?: Record<string, number>;
    profit_by_sport?: Array<{
      sport: string; n: number; won: number; lost: number;
      units_risked: number; units_profit: number; roi_pct: number;
      hit_rate_pct: number; clv_avg: number;
    }>;
    changes_log?: Array<{ ts: string; type: string; reason?: string; sport?: string; market?: string; band?: string; from?: number; to?: number; raise_by?: number }>;
  }>("/analytics/v2"),
  analyticsV2Recompute: () => request<{ gated: boolean; total_settled: number; rows: number }>(
    "/analytics/v2/recompute", { method: "POST" }),

  // ── Phase 3 — Multi-Armed Bandit (Thompson sampling) ───────────────
  bandit: () => request<{
    n_arms: number;
    arms: Array<{
      arm: string;
      description: string;
      n: number;
      wins: number;
      losses: number;
      push: number;
      units_risked: number;
      units_profit: number;
      roi: number;
      alpha: number;
      beta: number;
      posterior_mean: number;
      posterior_thompson: number;
      last_updated: string;
    }>;
  }>("/analytics/bandit"),

  backtest: (days: number = 30) => request<{
    window_days: number;
    n_picks: number;
    ranked: string[];
    arms: Record<string, {
      description: string;
      n: number; wins: number; losses: number; push: number;
      hit_rate: number; units_risked: number; units_profit: number;
      roi: number; max_drawdown: number; sharpe: number;
      curve: Array<[string, number]>;
    }>;
  }>(`/analytics/backtest?days=${days}`),

  // ─────────────────────────── NFL Intelligence ───────────────────────────
  // Three engines, all read-only, all return TRUE probability (no edge).
  //   • nflSafeBets         — top-N player-prop locks ranked by hit rate
  //   • nflAtdLeaderboard   — ranked P(TD ≥ 1) for every eligible player
  //   • nflGameSafeBets     — best ML / Spread / Total locks across all
  //                            upcoming NFL matchups in one shot
  // Surface these in the home tab as "NFL Intelligence" feature cards.
  nflSafeBets: (limit: number = 10, minProbability: number = 0.78) =>
    request<NFLSafeBetsResponse>(
      `/nfl/safe-bets?limit=${limit}&min_probability=${minProbability}`,
    ),
  nflAtdLeaderboard: (
    limit: number = 20,
    minProbability: number = 0.30,
    minOpportunityRating: "low" | "med" | "high" = "med",
  ) =>
    request<NFLAtdLeaderboardResponse>(
      `/nfl/atd/leaderboard?limit=${limit}&min_probability=${minProbability}&min_opportunity_rating=${minOpportunityRating}`,
    ),
  nflGameSafeBets: (limit: number = 10, minProbability: number = 0.78) =>
    request<NFLGameSafeBetsResponse>(
      `/nfl/games/safe-bets?limit=${limit}&min_probability=${minProbability}`,
    ),
  nflTeamLeaderboard: (limit: number = 32) =>
    request<{
      league_ppg: number;
      n_teams: number;
      teams: Array<{
        team: string; rating: number; ppg: number; opp_ppg: number;
        win_rate: number; n_games: number;
      }>;
    }>(`/nfl/games/teams?limit=${limit}`),
};

// ─── NFL response types (shared with screens that render the cards) ───
export type NFLSafePick = {
  probability: number;
  probability_empirical?: number;
  confidence: number;
  median: number;
  mean: number;
  floor_p10: number;
  std: number;
  volatility_cv?: number;
  sample_size: number;
  hits: number;
  total_attempts?: number;
  prop: string;
  line: number;
  player_id: string;
  player_name: string;
  team: string;
  market: string;
  reason: string;
  /** Compact rationale chip rendered under the card. v2 (2026-06-29). */
  why?: string;
  /** "target" = -200..-456 sweet-spot band, "acceptable" = -163..-200 (mild stretch). */
  band?: "target" | "acceptable" | string;
  implied_american_odds?: number;
};
export type NFLSafeBetsResponse = {
  total_candidates: number;
  passed_filters: number;
  rejected: Record<string, number>;
  rules: Record<string, unknown>;
  picks: NFLSafePick[];
};

export type NFLAtdPick = {
  player_id: string;
  player_name: string;
  team: string;
  opponent?: string | null;
  td_probability: number;
  confidence: number;
  opportunity_rating: "low" | "med" | "high" | string;
  weighted_touches_recent: number;
  weighted_tds_recent: number;
  is_rb_archetype?: boolean;
  sample_games: number;
  reasons: string[];
};
export type NFLAtdLeaderboardResponse = {
  total_candidates: number;
  passed_filters: number;
  rejected: Record<string, number>;
  rules: Record<string, unknown>;
  league_means?: Record<string, number>;
  picks: NFLAtdPick[];
};

export type NFLGamePick = {
  matchup: string;
  market: "moneyline" | "spread" | "total" | string;
  favored?: string;
  expected_margin?: number;
  expected_total?: number;
  true_probability: number;
  pick: {
    market: string;
    team?: string;
    opponent?: string;
    spread?: number;
    total?: number;
    side?: string;
    true_probability: number;
  };
};
export type NFLGameSafeBetsResponse = {
  count: number;
  min_probability: number;
  matchups_evaluated: number;
  bets: NFLGamePick[];
};

export type AnalyticsRow = {
  key: string; count: number; wins: number; losses: number; pushes: number;
  hit_rate: number; units: number; roi: number; avg_edge: number; avg_clv: number;
};
