/**
 * Commit C — PERKLOCKS Exact Request Cache Identity (frontend).
 *
 * ONE canonical identity function that is used by:
 *   • `_inflight` dedupe map in src/lib/api.ts
 *   • `_etagStore` snapshot cache in src/lib/api.ts
 *   • `useSWR` cache key documentation / parity checks
 *
 * Why a dedicated module?
 *   Previous ad-hoc keying was close-but-not-identical between the
 *   two maps.  `_etagKey(url)` sorted query params; `_inflight` used
 *   `${method}:${url}` with cache-buster still present.  That
 *   divergence let the same conceptual request produce DIFFERENT
 *   keys in the two maps — benign in practice, but it meant a 304
 *   fast-path could race against a dedupe miss.  Commit C locks the
 *   identity down to one deterministic function.
 *
 * Contract:
 *   • Method is upper-cased.
 *   • Path is normalized to the canonical `/api/...` form the caller
 *     already uses (buildApiUrl does the slash hygiene; we treat the
 *     returned URL as the source of truth).
 *   • Query string: parsed, cache-buster `_=…` stripped, remaining
 *     params alpha-sorted so `?a=1&b=2` and `?b=2&a=1` match.
 *   • Auth salt: when a request carries `Authorization`, we hash the
 *     bearer-token suffix into a short digest and append — this
 *     prevents the ETag store (and thus the 304 body-reuse path)
 *     from ever serving one user's body to another on token
 *     rotation / impersonation cases.  Unauthenticated requests
 *     stamp `pub` so the public cache is shared across sessions.
 *   • Pure function — no I/O, no module state.  Safe to call per
 *     request without perf cost.
 */

/** Short, non-cryptographic 32-bit FNV-1a hash of a string.  We only
 *  need a stable digest to key the cache; collision safety matters
 *  per-user not globally. */
function _fnv1a(input: string): string {
  let h = 0x811c9dc5;
  for (let i = 0; i < input.length; i++) {
    h ^= input.charCodeAt(i);
    // Multiply with 32-bit wrap via Math.imul.
    h = Math.imul(h, 0x01000193);
  }
  // Positive hex, 8 chars max — short enough for log readability.
  return (h >>> 0).toString(16).padStart(8, "0");
}

/** Extract an auth-scope salt from a Bearer token.  Returns `"pub"`
 *  for unauthenticated requests.  The last 24 chars of a JWT are
 *  the signature tail — hashing them is enough to partition two
 *  users' caches without exposing the token in logs. */
export function _authScopeFromToken(tok?: string | null): string {
  if (!tok || typeof tok !== "string" || tok.length === 0) return "pub";
  // Grab the tail so hashing input is bounded.
  const tail = tok.length > 24 ? tok.slice(-24) : tok;
  return "u" + _fnv1a(tail);
}

/** Canonicalize a URL by sorting its query parameters and dropping
 *  the cache-buster.  Returns `{ base, qs }`. */
export function _canonicalizeUrl(url: string): { base: string; qs: string } {
  const [base, rawQs] = (url || "").split("?");
  if (!rawQs) return { base, qs: "" };
  const parts = rawQs
    .split("&")
    .filter((p) => p && !p.startsWith("_="));  // drop cache-buster
  parts.sort();
  return { base, qs: parts.join("&") };
}

/** The single canonical identity used by every request-level cache
 *  surface in the frontend. */
export function requestCacheIdentity(opts: {
  method: string;
  url: string;
  authToken?: string | null;
}): string {
  const method = (opts.method || "GET").toUpperCase();
  const { base, qs } = _canonicalizeUrl(opts.url);
  const scope = _authScopeFromToken(opts.authToken);
  return qs
    ? `${method}|${scope}|${base}?${qs}`
    : `${method}|${scope}|${base}`;
}

/** Narrower identity used by the ETag store — same as the full
 *  identity but strips the method (ETag is a GET-only cache).  Kept
 *  as a dedicated helper so callers signal intent. */
export function etagCacheIdentity(opts: {
  url: string;
  authToken?: string | null;
}): string {
  const { base, qs } = _canonicalizeUrl(opts.url);
  const scope = _authScopeFromToken(opts.authToken);
  return qs ? `${scope}|${base}?${qs}` : `${scope}|${base}`;
}
