/**
 * CANONICAL SERVER-STATE KEYS — shared by destination screens AND the
 * primary-tab prefetch so both read/write the SAME useSWR cache entry.
 */
export const parlayKey = (
  n: number, m: string, s: string, lt: string,
  incl: string[], excl: string[], f: any, r: number,
  locked: string[], sMode: string, wHours: number, nonce: number,
  advSub?: string,
) =>
  `parlay|${n}|${m}|${s}|${lt}|${incl.join(",")}|${excl.join(",")}|`
  + `${JSON.stringify(f || {})}|${r}|${locked.join(",")}|`
  + `${sMode}|${wHours}|${nonce}|${advSub || ""}`;

export const rolloverKey = (lt: string, sp: string, f: any) => `rollover|${lt}|${sp}|${JSON.stringify(f || {})}`;
export const MY_BETS_KEY = "my-bets|default";
export const PROFILE_STATS_KEY = "profile|stats";
export const picksLiteKey = (sport: string) => `picks|lite|${sport}`;
export const pickDetailKey = (id: string) => `pick-detail|${id}`;
export const historicalIntelligenceKey = (id: string, sample: string, venue: string) =>
  `historical-intelligence|${id}|${sample}|${venue}`;
