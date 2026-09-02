/**
 * Perklocks Premium Design System 2.0 — layered dark sportsbook aesthetic.
 *
 * Design direction: premium sports tech + modern sportsbook polish.
 *  - Layered dark navy surfaces (bg → surface → elevated → glossy)
 *  - Restrained metallic gold identity (Perklocks brand)
 *  - Controlled top-edge highlights + soft depth shadows
 *  - Tier-driven visual intensity (STANDARD → APEX)
 *
 * This is PRESENTATION only. Backend truth
 * (published_lock_score / grade / probability / edge) is not touched.
 */
export const COLORS = {
  // ── Layered dark surfaces (UI 3.0 — GOLD IMPACT micro-pass) ──────
  //  2026-06 μ-pass "Gold Impact Option 1": deeper near-black navy so
  //  the gold-on-dark contrast reads bold and premium instead of
  //  washed.  Card surface, borders, text, and gold tokens all
  //  brightened in lockstep.  All layout / component structure
  //  untouched — only color values.
  bg: "#0B0F1A",              // slightly lifted from #070B14
  surface: "#0E1320",         // lifted from #090E18
  surfaceElevated: "#151B2B",
  surfaceRaised: "#1D2438",
  surfaceGloss: "rgba(255,221,92,0.12)",
  surfaceInset: "rgba(255,255,255,0.08)",

  // ── Text ─────────────────────────────────────────────────────────
  //  Brightness lift 2026-08-22: primary stays pure white; secondary
  //  and muted brightened so labels/dates/counts pop cleanly.
  textPrimary: "#FFFFFF",
  textSecondary: "#E6EAF2",   // lifted from #DDE2EC
  textMuted: "#AAB3C4",       // lifted from #9AA3B4
  textDim: "#7A8194",

  // ── Brand accents ────────────────────────────────────────────────
  //  Volt blue and neon green nudged toward luminous premium tones;
  //  Gold Impact: brighter luminous core so LOCK numbers/premium
  //  labels read as GOLD, not brown.
  //  Locks Mockup Match (2026-08-22): further luminized gold identity
  //  to match the attached mockup's metallic-gold treatment (PERKLOCKS
  //  wordmark, LOCK box, active LOCKS tab, featured card border).
  voltBlue: "#4C9BFF",
  electricBlaze: "#FF5F5C",
  neonGreen: "#4DE68A",
  neonLime:  "#B6FF3D",   // confidence-bar mid-stop (lime)
  // Perklocks gold identity — luminous metallic tones.
  //  Phase 17 PVS 2.0 (2026-06): gold is now RESERVED for TRUE 100
  //  APEX only (per master directive).  99 PEAK moves to
  //  perklocksPurple below.  Wordmark / active tab may still use
  //  gold as brand identity, but tier surfaces gate strictly.
  goldElite: "#FFD700",    // true luminous gold core — APEX only
  goldRich:  "#FFE066",    // bright metallic gold highlight — APEX only
  goldGlow:  "#FFC933",    // saturated gold used for outer-glow shadow tint
  goldDeep:  "#B98A17",    // deep metallic base
  goldGloss: "rgba(255,215,0,0.28)",

  // ── Perklocks Intelligence / Premium (Phase 17 PVS 2.0) ─────────
  //  Purple identifies non-APEX Perklocks intelligence moments:
  //   * 99 PEAK Lock
  //   * "Why This Pick" evidence panels
  //   * Model provenance / AI-generated content markers
  //   * Elite research / Lab premium features
  //  Chosen for luminous depth against layered black surfaces without
  //  competing with the gold APEX identity or the sport neon accents.
  perklocksPurple:      "#B98CFF",    // luminous premium purple core
  perklocksPurpleRich:  "#D5B4FF",    // brighter highlight
  perklocksPurpleDeep:  "#7A4DFF",    // deep base
  perklocksPurpleSoft:  "rgba(185,140,255,0.20)",
  perklocksPurpleBorder:"rgba(185,140,255,0.68)",
  perklocksPurpleGlow:  "rgba(185,140,255,0.35)",

  // ── Borders ──────────────────────────────────────────────────────
  //  Brightness lift 2026-08-22: bumped so cards separate more clearly.
  borderDefault: "rgba(255,255,255,0.22)",
  borderStrong:  "rgba(255,255,255,0.32)",
  borderActive:  "rgba(255,255,255,0.48)",
  borderGold:    "rgba(255,215,0,0.82)",

  // ── State colors ─────────────────────────────────────────────────
  dangerBg: "#1B0708",
  dangerBorder: "rgba(255,95,92,0.42)",
  dangerSurface: "rgba(255,95,92,0.12)",
  successBg: "rgba(77,230,138,0.14)",
  successBorder: "rgba(77,230,138,0.42)",

  // ── History result surfaces ──────────────────────────────────────
  winSurface: "rgba(77,230,138,0.14)",
  winBorder:  "rgba(77,230,138,0.52)",
  lossSurface: "rgba(255,95,92,0.14)",
  lossBorder:  "rgba(255,95,92,0.46)",
  pushSurface: "rgba(255,255,255,0.09)",
  pushBorder:  "rgba(255,255,255,0.26)",
};

// ─────────────────────────────────────────────────────────────────────
// Lock Tier visual system (VISUAL ONLY — does not alter Lock Score
// calculation, grade, Apex qualification, or Board eligibility).
// ─────────────────────────────────────────────────────────────────────
export type LockTierKey =
  | "STANDARD" | "ELITE" | "STRONG" | "RARE" | "PEAK" | "APEX";

export interface LockTierVisual {
  key: LockTierKey;
  label: string;              // Human label surfaced on the badge
  accent: string;             // Primary accent color for borders/text
  accentSoft: string;         // Semi-transparent accent for tinted surfaces
  borderColor: string;        // Card border color
  borderWidth: number;
  surfaceBg: string;          // Card background
  surfaceGlossTop: string;    // Top gloss highlight tone
  glowColor: string;          // Shadow color for premium depth
  glowOpacity: number;
  glowRadius: number;
  chipBg: string;             // Tier chip background
  chipTextColor: string;
  chipBorderColor: string;
  icon?: string;              // e.g. "⚡" for APEX
}

/** Resolve the visual tier from a canonical Lock Score (85-100).
 *  DO NOT use for eligibility — that is decided by is_main_board_eligible.
 */
export function getLockTierVisual(lockScore: number): LockTierVisual {
  const s = Math.round(lockScore);
  if (s >= 100) {
    return {
      key: "APEX",
      label: "APEX LOCK",
      accent: COLORS.goldElite,
      accentSoft: "rgba(255,221,92,0.26)",
      borderColor: "rgba(255,221,92,0.90)",
      borderWidth: 2,
      surfaceBg: "#2A2210",
      surfaceGlossTop: "rgba(255,221,92,0.22)",
      glowColor: COLORS.goldElite,
      glowOpacity: 0.60,
      glowRadius: 22,
      chipBg: "rgba(255,221,92,0.28)",
      chipTextColor: COLORS.goldElite,
      chipBorderColor: "rgba(255,221,92,0.95)",
      icon: "⚡",
    };
  }
  if (s === 99) {
    // Phase 17 PVS 2.0 — 99 PEAK now uses Perklocks Intelligence
    // Purple.  Gold is RESERVED strictly for TRUE 100 APEX.  Peak
    // remains distinct from Rare Lock (green) via the purple accent.
    return {
      key: "PEAK",
      label: "99 LOCK",
      accent: COLORS.perklocksPurple,
      accentSoft: COLORS.perklocksPurpleSoft,
      borderColor: COLORS.perklocksPurpleBorder,
      borderWidth: 1.75,
      surfaceBg: "#1A162B",
      surfaceGlossTop: "rgba(185,140,255,0.15)",
      glowColor: COLORS.perklocksPurple,
      glowOpacity: 0.44,
      glowRadius: 14,
      chipBg: COLORS.perklocksPurpleSoft,
      chipTextColor: COLORS.perklocksPurpleRich,
      chipBorderColor: COLORS.perklocksPurpleBorder,
    };
  }
  if (s >= 96) {
    return {
      key: "RARE",
      label: "RARE LOCK",
      accent: COLORS.neonGreen,
      accentSoft: "rgba(77,230,138,0.22)",
      borderColor: "rgba(77,230,138,0.72)",
      borderWidth: 1.6,
      surfaceBg: "#132420",
      surfaceGlossTop: "rgba(77,230,138,0.14)",
      glowColor: COLORS.neonGreen,
      glowOpacity: 0.34,
      glowRadius: 12,
      chipBg: "rgba(77,230,138,0.22)",
      chipTextColor: COLORS.neonGreen,
      chipBorderColor: "rgba(77,230,138,0.72)",
    };
  }
  if (s >= 93) {
    return {
      key: "STRONG",
      label: "STRONG LOCK",
      accent: COLORS.voltBlue,
      accentSoft: "rgba(76,155,255,0.22)",
      borderColor: "rgba(76,155,255,0.62)",
      borderWidth: 1.5,
      surfaceBg: "#131C33",
      surfaceGlossTop: "rgba(76,155,255,0.12)",
      glowColor: COLORS.voltBlue,
      glowOpacity: 0.26,
      glowRadius: 10,
      chipBg: "rgba(76,155,255,0.22)",
      chipTextColor: COLORS.voltBlue,
      chipBorderColor: "rgba(76,155,255,0.66)",
    };
  }
  if (s >= 90) {
    return {
      key: "ELITE",
      label: "ELITE SETUP",
      accent: "#B8C6FF",
      accentSoft: "rgba(184,198,255,0.20)",
      borderColor: "rgba(184,198,255,0.44)",
      borderWidth: 1.35,
      surfaceBg: COLORS.surfaceElevated,
      surfaceGlossTop: "rgba(255,255,255,0.09)",
      glowColor: "#000000",
      glowOpacity: 0.42,
      glowRadius: 10,
      chipBg: "rgba(184,198,255,0.20)",
      chipTextColor: "#B8C6FF",
      chipBorderColor: "rgba(184,198,255,0.44)",
    };
  }
  // 85–89 STANDARD (clean premium baseline — brightened surface + border)
  return {
    key: "STANDARD",
    label: "LOCK",
    accent: COLORS.textSecondary,
    accentSoft: "rgba(255,255,255,0.10)",
    borderColor: COLORS.borderStrong,
    borderWidth: 1.2,
    surfaceBg: COLORS.surface,
    surfaceGlossTop: COLORS.surfaceGloss,
    glowColor: "#000000",
    glowOpacity: 0.40,
    glowRadius: 10,
    chipBg: "rgba(255,255,255,0.10)",
    chipTextColor: COLORS.textPrimary,
    chipBorderColor: COLORS.borderActive,
  };
}

export const GRADE_COLORS = {
  "APEX Lock":  COLORS.goldElite,          // 100 APEX only (gold reserved)
  "Elite Lock": COLORS.perklocksPurple,    // 98-99 non-APEX premium (purple)
  "Strong Lock": COLORS.neonGreen,
  "Lock":       COLORS.neonGreen,
  "Playable":   COLORS.voltBlue,
  "Good Bet":   COLORS.voltBlue,
  Pass:         COLORS.textMuted,
} as const;

export const SPORT_ICONS: Record<string, string> = {
  MLB: "baseball",
  NBA: "basketball",
  NFL: "football",
  CFB: "american-football",
  NHL: "snow",
  Soccer: "soccer",
  Tennis: "tennis",
  UFC: "barbell",
  KBO: "baseball",
};

export const SPORTS = ["All", "MLB", "NBA", "NFL", "CFB", "NHL", "Soccer", "Tennis", "UFC"] as const;

// ── Sport-color neon accents (Locks Mockup 2026-08-22) ───────────────
// Controlled neon accents applied to sport chips, thin card edges,
// icons, and glows. Used ONLY as accents — never as a full flood.
// "All" defaults to luminous gold (the Perklocks brand identity).
export const SPORT_COLORS: Record<string, {
  accent: string;      // Full-strength neon color
  soft: string;        // Semi-transparent surface tint (~15%)
  border: string;      // Semi-transparent border (~60%)
  glow: string;        // Shadow tint (matches accent)
}> = {
  All:    { accent: "#FFD700", soft: "rgba(255,215,0,0.14)", border: "rgba(255,215,0,0.68)", glow: "#FFD700" },
  MLB:    { accent: "#4C9BFF", soft: "rgba(76,155,255,0.14)", border: "rgba(76,155,255,0.62)", glow: "#4C9BFF" },
  NBA:    { accent: "#B98CFF", soft: "rgba(185,140,255,0.14)", border: "rgba(185,140,255,0.62)", glow: "#B98CFF" },
  NFL:    { accent: "#4DE68A", soft: "rgba(77,230,138,0.14)", border: "rgba(77,230,138,0.60)", glow: "#4DE68A" },
  CFB:    { accent: "#FF9548", soft: "rgba(255,149,72,0.14)", border: "rgba(255,149,72,0.62)", glow: "#FF9548" },
  NHL:    { accent: "#5EE3FF", soft: "rgba(94,227,255,0.14)", border: "rgba(94,227,255,0.62)", glow: "#5EE3FF" },
  Tennis: { accent: "#D4FF3D", soft: "rgba(212,255,61,0.14)", border: "rgba(212,255,61,0.62)", glow: "#D4FF3D" },
  Soccer: { accent: "#5EE3FF", soft: "rgba(94,227,255,0.14)", border: "rgba(94,227,255,0.62)", glow: "#5EE3FF" },
  UFC:    { accent: "#FF5F5C", soft: "rgba(255,95,92,0.14)",  border: "rgba(255,95,92,0.60)",  glow: "#FF5F5C" },
};

// Convenience — safe lookup with All-gold fallback.
export function getSportColor(sport?: string) {
  if (!sport) return SPORT_COLORS.All;
  return SPORT_COLORS[sport] || SPORT_COLORS.All;
}

// Confidence-bar gradient stops (green → lime → gold → orange → red).
// Used by LockPickCard's progress bar per mockup spec §8.
export const CONFIDENCE_GRADIENT = [
  "#4DE68A",   // green (low-lock end)
  "#B6FF3D",   // lime
  "#FFD700",   // gold
  "#FF9548",   // orange
  "#FF5F5C",   // red
];

// ── Depth / shadow scale ────────────────────────────────────────────
export const SHADOW = {
  shadowColor: "#000",
  shadowOpacity: 0.4,
  shadowRadius: 12,
  shadowOffset: { width: 0, height: 4 },
  elevation: 6,
};

export const SHADOW_SM = {
  shadowColor: "#000",
  shadowOpacity: 0.28,
  shadowRadius: 6,
  shadowOffset: { width: 0, height: 2 },
  elevation: 3,
};

export const SHADOW_LG = {
  shadowColor: "#000",
  shadowOpacity: 0.55,
  shadowRadius: 22,
  shadowOffset: { width: 0, height: 10 },
  elevation: 10,
};

// ── Radius scale ────────────────────────────────────────────────────
export const RADIUS = {
  xs: 4,
  sm: 6,
  md: 10,
  lg: 14,
  xl: 18,
  pill: 999,
};

// ── Spacing scale (8pt grid) ────────────────────────────────────────
export const SPACING = { xs: 4, sm: 8, md: 12, lg: 16, xl: 24, xxl: 32 };
