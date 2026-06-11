/**
 * LockScore AI theme — dark performance-pro aesthetic.
 */
export const COLORS = {
  bg: "#0A0A0A",
  surface: "#141414",
  surfaceElevated: "#1E1E1E",
  textPrimary: "#FFFFFF",
  textSecondary: "#A1A1AA",
  textMuted: "#71717A",
  voltBlue: "#007AFF",
  electricBlaze: "#FF3B30",
  neonGreen: "#32D74B",
  goldElite: "#FFD700",
  borderDefault: "rgba(255,255,255,0.10)",
  borderActive: "rgba(255,255,255,0.30)",
  killerBg: "#180505",
  killerBorder: "rgba(255,59,48,0.30)",
  killerSurface: "rgba(255,59,48,0.08)",
};

export const GRADE_COLORS = {
  "Elite Lock": COLORS.goldElite,
  "Strong Lock": COLORS.neonGreen,
  "Good Bet": COLORS.voltBlue,
  Pass: COLORS.textMuted,
} as const;

export const SPORT_ICONS: Record<string, string> = {
  MLB: "baseball",
  NBA: "basketball",
  NFL: "football",
  Soccer: "soccer",
  Tennis: "tennis",
};

export const SPORTS = ["All", "MLB", "NBA", "WNBA", "NFL", "Soccer", "Tennis"] as const;

export const SHADOW = {
  shadowColor: "#000",
  shadowOpacity: 0.4,
  shadowRadius: 12,
  shadowOffset: { width: 0, height: 4 },
  elevation: 6,
};
