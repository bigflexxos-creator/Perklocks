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
  // ── Layered dark surfaces ─────────────────────────────────────────
  //  bg          — app background (deepest)
  //  surface     — default card surface
  //  surfaceRaised — elevated card / grouped section
  //  surfaceGloss  — top-of-card highlight tone
  //  surfaceInset  — inset chip / progress-track base
  bg: "#07080C",
  surface: "#111420",
  surfaceElevated: "#171A28",
  surfaceRaised: "#1B1F30",
  surfaceGloss: "rgba(255,255,255,0.045)",
  surfaceInset: "rgba(255,255,255,0.04)",

  // Back-compat aliases (existing screens read these).
  // Point at slightly warmer/richer navy versions.
  //   → produces immediately-perceptible depth without touching call sites.

  // ── Text ─────────────────────────────────────────────────────────
  textPrimary: "#FFFFFF",
  textSecondary: "#C4C7D4",
  textMuted: "#8A8FA3",
  textDim: "#5F6478",

  // ── Brand accents ────────────────────────────────────────────────
  voltBlue: "#2F84FF",     // brighter, more premium than the old #007AFF
  electricBlaze: "#FF4D4A",
  neonGreen: "#3DDC77",
  // Perklocks gold identity — richer metallic tones.
  goldElite: "#FFD24A",
  goldRich: "#F5B417",
  goldDeep: "#B87C00",
  goldGloss: "rgba(255,210,74,0.14)",

  // ── Borders ──────────────────────────────────────────────────────
  borderDefault: "rgba(255,255,255,0.08)",
  borderStrong:  "rgba(255,255,255,0.14)",
  borderActive:  "rgba(255,255,255,0.28)",
  borderGold:    "rgba(255,210,74,0.55)",

  // ── State colors ─────────────────────────────────────────────────
  dangerBg: "#1B0708",
  dangerBorder: "rgba(255,77,74,0.30)",
  dangerSurface: "rgba(255,77,74,0.08)",
  successBg: "rgba(61,220,119,0.10)",
  successBorder: "rgba(61,220,119,0.32)",

  // ── History result surfaces ──────────────────────────────────────
  winSurface: "rgba(61,220,119,0.10)",
  winBorder:  "rgba(61,220,119,0.40)",
  lossSurface: "rgba(255,77,74,0.10)",
  lossBorder:  "rgba(255,77,74,0.36)",
  pushSurface: "rgba(255,255,255,0.06)",
  pushBorder:  "rgba(255,255,255,0.20)",
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
      accentSoft: "rgba(255,210,74,0.18)",
      borderColor: "rgba(255,210,74,0.75)",
      borderWidth: 1.75,
      surfaceBg: "#1A1608",
      surfaceGlossTop: "rgba(255,210,74,0.12)",
      glowColor: COLORS.goldElite,
      glowOpacity: 0.45,
      glowRadius: 18,
      chipBg: "rgba(255,210,74,0.20)",
      chipTextColor: COLORS.goldElite,
      chipBorderColor: "rgba(255,210,74,0.80)",
      icon: "⚡",
    };
  }
  if (s === 99) {
    return {
      key: "PEAK",
      label: "99 LOCK",
      accent: COLORS.goldRich,
      accentSoft: "rgba(245,180,23,0.16)",
      borderColor: "rgba(245,180,23,0.55)",
      borderWidth: 1.5,
      surfaceBg: "#161422",
      surfaceGlossTop: "rgba(245,180,23,0.09)",
      glowColor: COLORS.goldRich,
      glowOpacity: 0.30,
      glowRadius: 12,
      chipBg: "rgba(245,180,23,0.16)",
      chipTextColor: COLORS.goldRich,
      chipBorderColor: "rgba(245,180,23,0.55)",
    };
  }
  if (s >= 96) {
    return {
      key: "RARE",
      label: "RARE LOCK",
      accent: COLORS.neonGreen,
      accentSoft: "rgba(61,220,119,0.14)",
      borderColor: "rgba(61,220,119,0.55)",
      borderWidth: 1.35,
      surfaceBg: "#0F1A18",
      surfaceGlossTop: "rgba(61,220,119,0.08)",
      glowColor: COLORS.neonGreen,
      glowOpacity: 0.22,
      glowRadius: 10,
      chipBg: "rgba(61,220,119,0.14)",
      chipTextColor: COLORS.neonGreen,
      chipBorderColor: "rgba(61,220,119,0.55)",
    };
  }
  if (s >= 93) {
    return {
      key: "STRONG",
      label: "STRONG LOCK",
      accent: COLORS.voltBlue,
      accentSoft: "rgba(47,132,255,0.14)",
      borderColor: "rgba(47,132,255,0.45)",
      borderWidth: 1.25,
      surfaceBg: "#101528",
      surfaceGlossTop: "rgba(47,132,255,0.06)",
      glowColor: COLORS.voltBlue,
      glowOpacity: 0.16,
      glowRadius: 8,
      chipBg: "rgba(47,132,255,0.14)",
      chipTextColor: COLORS.voltBlue,
      chipBorderColor: "rgba(47,132,255,0.50)",
    };
  }
  if (s >= 90) {
    return {
      key: "ELITE",
      label: "ELITE SETUP",
      accent: "#9BB0FF",
      accentSoft: "rgba(155,176,255,0.12)",
      borderColor: "rgba(155,176,255,0.28)",
      borderWidth: 1.15,
      surfaceBg: COLORS.surfaceElevated,
      surfaceGlossTop: "rgba(255,255,255,0.05)",
      glowColor: "#000000",
      glowOpacity: 0.28,
      glowRadius: 8,
      chipBg: "rgba(155,176,255,0.12)",
      chipTextColor: "#9BB0FF",
      chipBorderColor: "rgba(155,176,255,0.28)",
    };
  }
  // 85–89 STANDARD (clean premium baseline)
  return {
    key: "STANDARD",
    label: "LOCK",
    accent: COLORS.textSecondary,
    accentSoft: "rgba(255,255,255,0.06)",
    borderColor: COLORS.borderDefault,
    borderWidth: 1,
    surfaceBg: COLORS.surface,
    surfaceGlossTop: COLORS.surfaceGloss,
    glowColor: "#000000",
    glowOpacity: 0.28,
    glowRadius: 8,
    chipBg: "rgba(255,255,255,0.06)",
    chipTextColor: COLORS.textSecondary,
    chipBorderColor: COLORS.borderStrong,
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
