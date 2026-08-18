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
  // ── Layered dark surfaces (UI 3.0 environment lift) ──────────────
  //  Boosted 2026-08 — user reported the app environment still felt
  //  crushed/dull.  New palette: luminous deep-navy with a
  //  perceptible electric-blue ambient tint so the app reads as
  //  "dark luxury sports intelligence" rather than flat black.
  bg: "#0B1226",              // luminous deep navy (was #050710)
  surface: "#1A2340",         // richer elevated navy (was #141A2B)
  surfaceElevated: "#242E4F",
  surfaceRaised: "#2E3960",
  surfaceGloss: "rgba(120,180,255,0.10)",   // subtle blue ambient
  surfaceInset: "rgba(255,255,255,0.06)",

  // Back-compat aliases (existing screens read these).
  // Point at slightly warmer/richer navy versions.
  //   → produces immediately-perceptible depth without touching call sites.

  // ── Text ─────────────────────────────────────────────────────────
  //  Secondary/muted brightened for crisp readability on OLED.
  textPrimary: "#FFFFFF",
  textSecondary: "#D6D9E4",
  textMuted: "#9CA1B5",
  textDim: "#6B7186",

  // ── Brand accents ────────────────────────────────────────────────
  //  Volt blue and neon green nudged toward luminous premium tones;
  //  gold moved from muddy amber to bright metallic yellow.
  voltBlue: "#4C9BFF",     // luminous premium blue (was #2F84FF)
  electricBlaze: "#FF5F5C",
  neonGreen: "#4DE68A",    // richer emerald (was #3DDC77)
  // Perklocks gold identity — richer metallic tones.
  goldElite: "#FFDD5C",    // brighter metallic gold (was #FFD24A)
  goldRich:  "#FFC736",    // luminous rich gold (was #F5B417)
  goldDeep:  "#D69100",    // deep metallic base (was #B87C00)
  goldGloss: "rgba(255,221,92,0.22)",

  // ── Borders ──────────────────────────────────────────────────────
  //  Stronger baseline so cards visibly separate from the background
  //  on mobile.
  borderDefault: "rgba(255,255,255,0.14)",
  borderStrong:  "rgba(255,255,255,0.22)",
  borderActive:  "rgba(255,255,255,0.38)",
  borderGold:    "rgba(255,221,92,0.70)",

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
    return {
      key: "PEAK",
      label: "99 LOCK",
      accent: COLORS.goldRich,
      accentSoft: "rgba(255,199,54,0.22)",
      borderColor: "rgba(255,199,54,0.72)",
      borderWidth: 1.75,
      surfaceBg: "#1F1B2D",
      surfaceGlossTop: "rgba(255,199,54,0.15)",
      glowColor: COLORS.goldRich,
      glowOpacity: 0.42,
      glowRadius: 14,
      chipBg: "rgba(255,199,54,0.22)",
      chipTextColor: COLORS.goldRich,
      chipBorderColor: "rgba(255,199,54,0.72)",
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
  "Elite Lock": COLORS.goldElite,
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
