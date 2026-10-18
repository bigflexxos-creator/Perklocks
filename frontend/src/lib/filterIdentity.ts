/**
 * PERKLOCKS · Deterministic Filter Identity
 * ──────────────────────────────────────────────────────────────────
 * One canonical function that converts the FULL picks-board request
 * identity (sport + lineType + sortKey + sortDir + PickFilters +
 * multi-select arrays) into a short, stable, order-independent string.
 *
 * Why a dedicated module?
 *   The persisted picks cache was previously keyed on `sport` ONLY.
 *   That meant Yankees-only-filtered MLB and league-wide MLB shared
 *   a single disk entry — the LAST write won.  On resume you could
 *   see one sport's "last-good" slate painted under a totally
 *   different filter preset.  Defects 7 & 8 in the Connectivity +
 *   Board Stability closure require that the cache key reflect the
 *   full request identity so each filter preset keeps its own
 *   independent last-good slate.
 *
 * Contract:
 *   • Pure function. No I/O. No module state.
 *   • Sport normalized to UPPERCASE and trimmed.
 *   • Arrays sorted + de-duped before hashing so [A,B] ≡ [B,A].
 *   • `undefined` / `null` / empty-string values are omitted entirely
 *     — they MUST NOT change the hash so an "empty filter" today
 *     matches an "empty filter" tomorrow.
 *   • NEVER include ephemeral junk (cache-buster, timestamps,
 *     signal references, etc.).
 */

/** FNV-1a 32-bit non-cryptographic hash — matches the digest used
 *  by requestCacheIdentity so logs can be correlated at a glance. */
function _fnv1a(input: string): string {
  let h = 0x811c9dc5;
  for (let i = 0; i < input.length; i++) {
    h ^= input.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return (h >>> 0).toString(16).padStart(8, "0");
}

export type FilterIdentityInput = {
  sport?:    string | null;
  lineType?: string | null;
  sortKey?:  string | null;
  sortDir?:  string | null;
  // Local PickFilters shape — kept loose so callers don't need the
  // full type import.
  filters?: Record<string, unknown> | null;
  // Multi-select store arrays.
  sports?:   string[] | null;
  leagues?:  string[] | null;
  markets?:  string[] | null;
  gameIds?:  string[] | null;
  events?:   string[] | null;
  search?:   string | null;
};

/** Normalize an array: sort + de-dupe + drop blanks. */
function _normArray(a: unknown): string[] {
  if (!Array.isArray(a)) return [];
  const seen = new Set<string>();
  const out: string[] = [];
  for (const v of a) {
    if (v == null) continue;
    const s = String(v).trim();
    if (!s) continue;
    if (seen.has(s)) continue;
    seen.add(s);
    out.push(s);
  }
  out.sort();
  return out;
}

/** Normalize a PickFilters-shaped object: alpha-sort keys + drop
 *  undefined / null / empty-string / 0-equivalent values. */
function _normFilters(f: Record<string, unknown> | null | undefined): string {
  if (!f || typeof f !== "object") return "";
  const parts: string[] = [];
  const keys = Object.keys(f).sort();
  for (const k of keys) {
    const v = (f as Record<string, unknown>)[k];
    if (v === undefined || v === null) continue;
    if (typeof v === "string" && v.trim() === "") continue;
    if (typeof v === "boolean" && !v) continue;        // false ≡ absent
    if (typeof v === "number" && v === 0) continue;    // 0 ≡ no floor
    if (Array.isArray(v) && v.length === 0) continue;
    const vs = Array.isArray(v)
      ? JSON.stringify(_normArray(v))
      : JSON.stringify(v);
    parts.push(`${k}=${vs}`);
  }
  return parts.join("&");
}

/** Build a human-readable canonical identity string.  Used as the
 *  input to `filterIdentityHash` and also surfaced in logs so a
 *  mismatch is diagnosable without decoding the hex digest. */
export function filterIdentityString(i: FilterIdentityInput): string {
  const sport = ((i.sport ?? "ALL") + "").toUpperCase().trim() || "ALL";
  const lt    = ((i.lineType ?? "") + "").toLowerCase().trim();
  const sk    = ((i.sortKey ?? "") + "").toLowerCase().trim();
  const sd    = ((i.sortDir ?? "") + "").toLowerCase().trim();
  const f     = _normFilters(i.filters as Record<string, unknown>);
  const sS    = _normArray(i.sports).join(",");
  const lS    = _normArray(i.leagues).join(",");
  const mS    = _normArray(i.markets).join(",");
  const gS    = _normArray(i.gameIds).join(",");
  const eS    = _normArray(i.events).join(",");
  const q     = ((i.search ?? "") + "").trim();
  // Field delimiters chosen so a value containing one of them cannot
  // forge a boundary with a later value.
  return [
    `sp=${sport}`,
    lt ? `lt=${lt}` : "",
    sk ? `sk=${sk}` : "",
    sd ? `sd=${sd}` : "",
    f  ? `f=${f}`   : "",
    sS ? `sports=${sS}`  : "",
    lS ? `leagues=${lS}` : "",
    mS ? `markets=${mS}` : "",
    gS ? `games=${gS}`   : "",
    eS ? `events=${eS}`  : "",
    q  ? `q=${q}`        : "",
  ].filter(Boolean).join("|");
}

/** Short, order-independent, stable hex digest of the FULL request
 *  identity.  Use this as a persisted-cache suffix / dedup key. */
export function filterIdentityHash(i: FilterIdentityInput): string {
  return _fnv1a(filterIdentityString(i));
}
