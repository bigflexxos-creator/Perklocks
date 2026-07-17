import { storage } from "@/src/utils/storage";
import { Platform } from "react-native";

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

  // ── Native (Expo Go / built app): always use EXPO_PUBLIC_BACKEND_URL. ──
  // On native, `window.location.origin` is polyfilled by RN to the Metro
  // dev-server URL (e.g. "http://192.168.x.x:8081"), which DOES NOT serve
  // `/api/*`. The web-only previewMismatch branch below would incorrectly
  // return that Metro URL and every request would 404 — verified in Expo
  // Go 2026-07-15 when picks showed skeleton loaders and "GAME · 0".
  // Platform.OS is imported synchronously so it's safe to gate on here.
  if (Platform.OS !== "web") {
    return (envUrl && envUrl.trim().length > 0) ? envUrl : PINNED_PREVIEW_URL;
  }

  // ── Web: keep the existing dev/prod-origin swap logic ──────────────
  if (envUrl && envUrl.trim().length > 0) {
    // Avoid pinning the bundle to the dev preview URL in production builds.
    if (typeof window !== "undefined" && window.location && window.location.origin) {
      const origin = window.location.origin;
      const isLocalhost = /^https?:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(origin);
      const previewMismatch =
        envUrl.includes(".preview.emergentagent.com") &&
        !origin.includes(".preview.emergentagent.com") &&
        !isLocalhost; // localhost dev preview intentionally hits the remote preview backend
      if (previewMismatch) return origin;
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
  // ─── ESPN-backed team meta (logos + colors, injuries) ──────────────
  // Injected by backend `_decorate_with_espn_meta` on `/api/picks/today`
  // and detail endpoints. Available for MLB, NFL, NBA, CFB, NHL, WNBA,
  // NCAAB, and every ESPN-covered soccer league.
  home_meta?: {
    logo?: string;
    color?: string;
    alt_color?: string;
    abbrev?: string;
  };
  away_meta?: {
    logo?: string;
    color?: string;
    alt_color?: string;
    abbrev?: string;
  };
  injury_chip?: {
    home: { out: number; doubtful: number; questionable: number };
    away: { out: number; doubtful: number; questionable: number };
    worst_side: "home" | "away" | null;
    home_key_injuries?: Array<{ athlete: string; position?: string; status: string; description?: string }>;
    away_key_injuries?: Array<{ athlete: string; position?: string; status: string; description?: string }>;
  };

  // ─── ESPN Signal Engine (analysis layer, not display) ──────────────
  // Applied server-side by `services/espn_signal_engine.apply_signals`.
  // Adjusts `win_probability` and `lock_score` in a bounded ±6pt window
  // based on injuries + recent team form. Stores auditable reasoning
  // so the "Why This Pick" panel can show what moved the number.
  espn_signals?: {
    applied: boolean;
    delta: number;              // +/- percentage points added to win_probability
    base_prob: number;
    final_prob?: number;
    side?: "home" | "away";
    items: Array<
      | { kind: "injury"; side: "pick" | "opponent"; tier: "out" | "doubtful" | "questionable"; count: number; delta: number }
      | { kind: "form"; pick_form: string; opp_form: string; diff: number; delta: number }
    >;
  };
  pre_espn_win_probability?: number;

  // ─── PerksLocks Signal Engine (Phase A, universal signals) ──────────
  // Computed server-side by `services/signal_engine`. Six independent
  // signals (Form/Matchup/Volume/Injury/Market/Value) combined into a
  // 0-100 Signal Score. `signal_score` survives the lite payload for
  // the card chip; the full block is detail-endpoint only.
  signal_score?: number;
  signal_engine?: {
    version: number;
    score: number;
    grade: "Elite" | "Strong" | "Moderate" | "Weak" | "Fade";
    breakdown: string;          // "Value +6 · Form +4.5 · Market -2"
    components: Array<{
      key: "form" | "matchup" | "volume" | "injury" | "market" | "value";
      label: string;
      points: number;           // signed contribution
      max: number;              // absolute cap for this component
      details: string[];        // real-number evidence lines
      found: boolean;           // whether any underlying data existed
    }>;
    why: string[];              // signal-driven "Why This Pick" bullets
    computed_at?: string;
  };

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

  // ─── Goalscorer Matchup Engine v3 (soccer goalscorer picks only) ────
  // Populated by /app/backend/goalscorer_matchup.py at API read time
  // for any Soccer pick whose market is anytime/first/last goal scorer
  // or "to score or assist". Renders inside the "Why this pick?" panel.
  matchup_score?: number;          // 0..100 final score (post-penalty)
  matchup_raw_score?: number;      // 0..100 pre-penalty
  matchup_confidence?: number;     // 0..1
  matchup_grade?: string;          // "A+" .. "F"
  matchup_subscore?: number;
  opportunity_subscore?: number;
  form_subscore?: number;
  historical_subscore?: number;
  starter_probability?: number;    // 0..1
  expected_minutes?: number;       // 0..90
  role?: string | null;            // "ST" / "FW" / "CAM" etc.
  penalty_taker?: boolean;
  xG_form?: number;                // xG per 90
  market_rank?: number | null;
  why_this_pick?: string[];
  why_not_this_pick?: string[];
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
  minSignal?: number;
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
//   1. 20-second per-request timeout via AbortController (bumped from 10s
//      2026-07-13 after user reported "Connection hiccup" on Expo Go cold
//      boots — the backend can take 6-8s to warm up after a supervisor
//      restart, and 10s wasn't enough headroom when mobile network adds
//      2-3s of TLS handshake overhead. Retry works after that because the
//      backend is warm; giving the first attempt more time avoids the
//      failed-banner-then-retry UX entirely).
//   2. Up to 2 retries on network errors / 5xx (exponential 300ms → 900ms)
//   3. In-flight GET deduplication — identical concurrent GETs share one
//      network call. Prevents accidental thundering-herd from React
//      double-renders. Mutations (POST/PUT/DELETE) NEVER dedupe.
const REQUEST_TIMEOUT_MS = 20_000;
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
  opts: { method?: string; body?: any; auth?: boolean; timeoutMs?: number } = {},
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
        const res = await _fetchWithTimeout(finalUrl, init, opts.timeoutMs ?? REQUEST_TIMEOUT_MS);
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

// ── MLB HR Slate types — mirror of GameHRSlate / HRHitter in
//    /app/backend/services/mlb_hr_intel.py. Keep in-step with backend.
export type HRHitter = {
  batter_id: number;
  batter_name: string;
  team: string;
  opponent: string;
  is_home: boolean;
  hr_probability: number;
  hr_score: number;
  grade: string;
  park_mult: number;     park_label: string;
  pitcher_mult: number;  pitcher_label: string;
  batter_power_mult: number; batter_power_label: string;
  recent_form_mult: number;  recent_form_label: string;
  weather_mult: number;  weather_label: string;
  temp_mult: number;     temp_label: string;
  platoon_mult: number;  platoon_label: string;
  h2h_mult: number;      h2h_label: string;
  season_hr?: number;
  iso?: number;
  last_15_hrs?: number;
  last_15_games?: number;
  h2h_hr?: number;
  h2h_pa?: number;
  batter_hand?: string;
  why_this_pick?: string[];
  book_hr_odds?: number | null;
  book_hr_implied_pct?: number | null;
};
export type GameHRSlate = {
  game_id: string;
  home_team: string;
  away_team: string;
  venue: string;
  commence_time: string;
  pitcher_home_name: string;
  pitcher_home_id: number | null;
  pitcher_home_hr9: number | null;
  pitcher_away_name: string;
  pitcher_away_id: number | null;
  pitcher_away_hr9: number | null;
  temp_f: number | null;
  wind_mph: number | null;
  wind_deg: number | null;
  wind_blowing_label: string;
  roof_status: string;
  park_hr_factor: number;
  park_hr_label: string;
  picks: HRHitter[];
};
export type HRSlateResponse = {
  date: string;
  as_of: string;
  games: GameHRSlate[];
  total_picks: number;
};

export const api = {
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

  // ── LAB endpoints (Session 2+3) ─────────────────────────────────
  // Cheatsheets — real streak facts from settled-pick history.
  labCheatsheets: (opts?: { sport?: string; min_lock?: number; min_streak_hits?: number; limit?: number }) => {
    const params = new URLSearchParams();
    if (opts?.sport) params.set("sport", opts.sport);
    if (opts?.min_lock != null) params.set("min_lock", String(opts.min_lock));
    if (opts?.min_streak_hits != null) params.set("min_streak_hits", String(opts.min_streak_hits));
    if (opts?.limit != null) params.set("limit", String(opts.limit));
    const qs = params.toString();
    return request<{
      generated_at: string;
      sport_filter: string | null;
      count: number;
      cards: any[];
      groups: {
        title: string;
        icon: string;
        entries: {
          pick_id: string;
          player_display: string;
          market_clean: string;
          sport: string;
          opponent: string | null;
          hits: number; n: number; pct: number;
          fact_text: string;
        }[];
      }[];
    }>(`/lab/cheatsheets${qs ? "?" + qs : ""}`);
  },
  labCheatsheetDetail: (pick_id: string) =>
    request<{
      pick_id: string;
      player: string; player_display: string;
      sport: string; market: string;
      opponent: string | null; book_odds: number | null;
      recent_form: { hits: number; n: number; pct: number };
      head_to_head: { hits: number; n: number; pct: number; opponent: string | null };
      venue_split: { hits: number; n: number; pct: number; venue: string } | null;
      games: { date: string; opponent: string; hit: boolean; status: string }[];
    }>(`/lab/cheatsheet-detail/${encodeURIComponent(pick_id)}`),

  // Hot Hitters — stats-driven best-bets discovery, independent of book odds.
  // Ranks every active MLB hitter by composite heat score (L15 avg + OBP +
  // OPS + current hit streak).  Surfaces niche players (Otto Lopez et al.)
  // that sportsbooks skip.
  labHotHitters: (opts?: { limit?: number }) => {
    const params = new URLSearchParams();
    if (opts?.limit != null) params.set("limit", String(opts.limit));
    const qs = params.toString();
    return request<{
      generated_at: string;
      window_days: number;
      total_ranked: number;
      hitters: {
        player_id: number;
        player_name: string;
        team: string;
        team_abbr: string;
        position: string | null;
        heat_score: number;
        l15_avg: number;
        l15_ops: number;
        l15_obp: number;
        l15_games: number;
        hit_streak: number;
        playing_today: boolean;
        next_opponent: string | null;
        next_opponent_abbr: string | null;
        next_pitcher: string | null;
        reasons: string[];
      }[];
    }>(`/lab/hot-hitters${qs ? "?" + qs : ""}`);
  },

  // Correlation Lab — historical parlay leg co-occurrence hit rates.
  labCorrelations: (opts?: { sport?: string; min_pairs?: number; limit?: number }) => {
    const params = new URLSearchParams();
    if (opts?.sport) params.set("sport", opts.sport);
    if (opts?.min_pairs != null) params.set("min_pairs", String(opts.min_pairs));
    if (opts?.limit != null) params.set("limit", String(opts.limit));
    const qs = params.toString();
    return request<{
      rows: {
        family_a: string; family_b: string;
        sample_size: number;
        both_hit_rate: number;
        leg_a_hit_rate: number; leg_b_hit_rate: number;
        lift: number | null;
        verdict: string;
      }[];
      total_pairs_seen: number;
    }>(`/lab/correlations${qs ? "?" + qs : ""}`);
  },
  // Bet Backtester — strategy filter → win rate, ROI, sample.
  labBacktest: (opts?: {
    sport?: string; market_family?: string;
    odds_min?: number; odds_max?: number;
    edge_min?: number; lock_min?: number; lock_max?: number;
    limit_sample?: number;
  }) => {
    const params = new URLSearchParams();
    Object.entries(opts || {}).forEach(([k, v]) => {
      if (v != null && v !== "") params.set(k, String(v));
    });
    const qs = params.toString();
    return request<{
      filters: Record<string, unknown>;
      sample_size: number;
      won: number; lost: number; push: number;
      hit_rate: number;
      units_profit: number; units_risked: number;
      roi: number;
      best_day: { date: string; units: number } | null;
      worst_day: { date: string; units: number } | null;
      days_traded: number;
      verdict: string;
      family_breakdown: {
        family: string; n: number; hit_rate: number;
        units_profit: number; roi: number;
      }[];
    }>(`/lab/backtest${qs ? "?" + qs : ""}`);
  },
  // Pattern Finder — auto-mined profitable buckets, Wilson-ranked.
  labPatterns: (opts?: { sport?: string; axis?: string; min_n?: number; limit?: number }) => {
    const params = new URLSearchParams();
    if (opts?.sport) params.set("sport", opts.sport);
    if (opts?.axis) params.set("axis", opts.axis);
    if (opts?.min_n != null) params.set("min_n", String(opts.min_n));
    if (opts?.limit != null) params.set("limit", String(opts.limit));
    const qs = params.toString();
    return request<{
      axis: string; min_n: number; buckets_considered: number;
      rows: {
        bucket: string; n: number; hit_rate: number;
        wilson_lower: number; roi: number; units_profit: number;
      }[];
    }>(`/lab/patterns${qs ? "?" + qs : ""}`);
  },
  // Matchup DNA — deep player profile with vs-opponent breakdown.
  labMatchupDNA: (sport: string, subject: string, opponent?: string) => {
    const params = new URLSearchParams();
    if (opponent) params.set("opponent", opponent);
    const qs = params.toString();
    return request<{
      subject: string; sport: string;
      overall: {
        n: number; won: number; lost: number; push: number;
        hit_rate: number; units_profit: number; roi: number;
      };
      vs_opponent: (Record<string, unknown> & { opponent: string; n: number }) | null;
      by_market: {
        family: string; n: number; won: number; lost: number;
        push: number; hit_rate: number; units_profit: number; roi: number;
      }[];
      home_away: Record<string, {
        n: number; won: number; lost: number; push: number;
        hit_rate: number; units_profit: number; roi: number;
      }>;
      recent_form: { date: string; market: string; status: string; units: number }[];
      hot_cold: string;
    }>(`/lab/matchup-dna/${encodeURIComponent(sport)}/${encodeURIComponent(subject)}${qs ? "?" + qs : ""}`);
  },
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
    if (filters?.minSignal != null && filters.minSignal > 0) qs.set("min_signal", String(filters.minSignal));
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
    // 2026-07-13: Response now includes `alt_availability` diagnostic
    // when the ALT tab is empty for a sport whose tournaments aren't
    // covered by the book (e.g. tennis 250s). Frontend renders a
    // friendly explanation instead of a bare "no picks" empty state.
    return request<{
      picks: Pick[];
      alt_availability?: {
        supported:  boolean;
        reason:     string;
        message:    string;
        suggestion?: string;
      } | null;
    }>(`/picks/today${q ? `?${q}` : ""}`);
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
        cashout_estimate?: number | null;
        last_resettled_at?: string | null;
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
  resettleParlay: (id: string) => request<{
    id: string; status: string; legs_won: number; legs_lost: number;
    legs_pending: number; settled_at: string | null; payout: number | null;
    last_resettled_at?: string | null;
  }>(`/parlay/${id}/resettle`, { method: "POST" }),
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
  // MLB HR slate (added 2026-06-30) — backs the new "HR" tab.
  // Each game returns its weather/park/pitcher context plus up to 5
  // top batter HR projections with full explainability bullets.
  hrSlate: (opts?: { date?: string; refresh?: boolean }) => {
    const q: string[] = [];
    if (opts?.date) q.push(`date=${encodeURIComponent(opts.date)}`);
    if (opts?.refresh) q.push("refresh=true");
    // Cold slate builds take ~9-12s (Statcast + MLB Stats API + Open-Meteo
    // fan-outs) so we lengthen the client timeout here from the default
    // 10s to 30s. Warm cache hits return in <100ms so the 30s ceiling
    // is basically only for the first request of the day.
    return request<HRSlateResponse>(
      `/mlb/hr-slate${q.length ? "?" + q.join("&") : ""}`,
      { timeoutMs: 30_000 } as any,
    );
  },
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

  // ── Phase 0 — CLV report ──────────────────────────────────────────
  clvReport: (days: number = 30) => request<{
    since: string;
    days: number;
    overall: { label: string; n: number; won: number; win_pct: number;
      roi_per_100u: number; avg_clv_pp: number | null;
      beat_close_pct: number | null };
    bands: Array<{ label: string; n: number; won: number; win_pct: number;
      roi_per_100u: number; avg_clv_pp: number | null;
      beat_close_pct: number | null }>;
    snapshot_coverage: { real_close_snapshots: number;
      sharp_book_snapshots: number; note: string };
    notes: string;
  }>(`/analytics/clv?days=${days}`),

  // ── Phase 5a — Kelly staking calculator ───────────────────────────
  kelly: (params: {
    win_probability: number; american_odds: number;
    bankroll?: number; fraction?: number; max_stake_pct?: number;
  }) => {
    const q = new URLSearchParams({
      win_probability: String(params.win_probability),
      american_odds:   String(params.american_odds),
      bankroll:        String(params.bankroll ?? 100),
      fraction:        String(params.fraction ?? 0.25),
      max_stake_pct:   String(params.max_stake_pct ?? 0.05),
    }).toString();
    return request<{
      stake: number; stake_pct: number;
      kelly_f: number; fractional_kelly: number;
      expected_value: number; edge_pp: number; note: string;
    }>(`/analytics/kelly?${q}`);
  },

  kellyForPick: (pickId: string, bankroll: number = 100, fraction: number = 0.25) =>
    request<{
      pick_id: string; market: string; selection: string; sport: string;
      book_odds: number; prob_source: string; prob_used: number;
      stake: number; stake_pct: number; kelly_f: number;
      fractional_kelly: number; expected_value: number;
      edge_pp: number; note: string;
    }>(
      `/analytics/kelly/for-pick?pick_id=${encodeURIComponent(pickId)}` +
      `&bankroll=${bankroll}&fraction=${fraction}`,
    ),

  // ── Phase 5c — Steam detection ────────────────────────────────────
  steamPicks: (hours: number = 6, direction?: "toward" | "away", limit: number = 50) => {
    const q = new URLSearchParams({
      hours: String(hours), limit: String(limit),
    });
    if (direction) q.append("direction", direction);
    return request<{
      count: number; hours: number; direction_filter: string;
      picks: Array<{
        id: string; sport: string; market: string; selection: string;
        event: string; event_time: string; book_odds: number;
        lock_score: number;
        steam: {
          direction: "toward" | "away"; magnitude_pp: number;
          american_delta: number; american_start: number;
          american_end: number; observed_at: string;
          window_minutes: number; observations: number;
        };
      }>;
    }>(`/analytics/steam?${q.toString()}`);
  },

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
