/**
 * PERKLOCKS · Frontend Error Taxonomy
 * ──────────────────────────────────────────────────────────────────
 * Single source of truth for classifying every failure that can occur
 * at the client request boundary.  Rendered UI text is chosen from
 * this classification — critically, an ``ABORTED_SUPERSEDED`` error
 * (a request that was cancelled because a newer user intent replaced
 * it) MUST NEVER surface as "Connection Hiccup".  It is not a network
 * failure, it is expected control flow.
 *
 * Producers:
 *   - src/lib/api.ts request() rejects with an Error whose `kind`
 *     property is one of these values.  Callers should read `.kind`
 *     rather than substring-match message text.
 *
 * Consumers:
 *   - app/(tabs)/index.tsx Locks screen — decides whether to show a
 *     retry banner, keep the last-good slate, or silently ignore.
 *   - Any future screen that fetches paged/versioned data.
 */
export const ErrorKind = {
  NETWORK_OFFLINE:      "NETWORK_OFFLINE",       // navigator.onLine === false OR bare TypeError with "network"
  DNS_TLS:              "DNS_TLS",               // TLS/DNS failure at connect time
  TIMEOUT:              "TIMEOUT",               // AbortError from the internal timeout guard
  HTTP_5XX:             "HTTP_5XX",              // 5xx from server after retries
  HTTP_429:             "HTTP_429",              // rate limited after retries
  AUTH_FAILURE:         "AUTH_FAILURE",          // 401 / 403
  JSON_PARSE:           "JSON_PARSE",            // body received but not valid JSON
  ABORTED_SUPERSEDED:   "ABORTED_SUPERSEDED",    // caller-provided AbortSignal fired (newer request took over)
  STALE_FALLBACK:       "STALE_FALLBACK",        // backend returned empty and we kept the last-good slate
  EMPTY_CANONICAL:      "EMPTY_CANONICAL",       // backend returned empty and there is no fallback
  RENDER_FAILURE:       "RENDER_FAILURE",        // JS threw during commit/paint
  UNKNOWN:              "UNKNOWN",
} as const;

export type ErrorKindT = typeof ErrorKind[keyof typeof ErrorKind];

/** Human-readable copy for the retry banner — never conflates the
 *  ABORTED_SUPERSEDED "not an error" with actual transport failure. */
export const ERROR_COPY: Record<ErrorKindT, { title: string; message: string; showRetry: boolean }> = {
  NETWORK_OFFLINE:     { title: "You're offline",       message: "Reconnect to refresh. Showing your last good slate.", showRetry: true  },
  DNS_TLS:             { title: "Can't reach server",   message: "DNS or TLS failure. Showing your last good slate.",   showRetry: true  },
  TIMEOUT:             { title: "Server slow to respond", message: "Request timed out. Showing your last good slate.", showRetry: true  },
  HTTP_5XX:            { title: "Server hiccup",        message: "Backend is bouncing. Showing your last good slate.", showRetry: true  },
  HTTP_429:            { title: "Slow down",            message: "Too many requests. Try again in a moment.",           showRetry: true  },
  AUTH_FAILURE:        { title: "Session expired",      message: "Please sign in again.",                                showRetry: false },
  JSON_PARSE:          { title: "Corrupt response",     message: "We couldn't read the server's reply. Please retry.",  showRetry: true  },
  ABORTED_SUPERSEDED:  { title: "",                     message: "",                                                    showRetry: false },
  STALE_FALLBACK:      { title: "Slate refreshing",     message: "Showing your cached picks while we refresh.",         showRetry: false },
  EMPTY_CANONICAL:     { title: "No locks yet",         message: "The slate is being built. Refresh in a moment.",      showRetry: true  },
  RENDER_FAILURE:      { title: "Display error",        message: "We hit a rendering issue. Please retry.",              showRetry: true  },
  UNKNOWN:             { title: "Something went wrong", message: "Please retry.",                                        showRetry: true  },
};

/** Classify an arbitrary caught error into an ErrorKind.  Reads
 *  properties in priority order so a specific classification wins
 *  over the fallback UNKNOWN.
 *
 *  Special contract: `ABORTED_SUPERSEDED` is a HAPPY-PATH signal
 *  that the caller cancelled the request because a newer request
 *  is now in-flight.  Callers should short-circuit and do NOTHING
 *  (no banner, no toast, no state clear) when they see this.
 */
export function classifyError(err: any): ErrorKindT {
  if (!err) return ErrorKind.UNKNOWN;
  // Explicit tag wins (throwing side already knew what happened).
  if (err.kind && typeof err.kind === "string") return err.kind as ErrorKindT;
  // Caller-provided AbortSignal fired.  We tag this ourselves in
  // request() so `errFromCallerAbort=true` distinguishes it from
  // the internal timeout guard (which also uses AbortController).
  if (err.errFromCallerAbort === true) return ErrorKind.ABORTED_SUPERSEDED;
  // Internal timeout guard fired.
  if (err.name === "AbortError" || err.errFromTimeout === true) {
    return ErrorKind.TIMEOUT;
  }
  // HTTP status classification (set by request() for 4xx/5xx).
  const status = typeof err.status === "number" ? err.status : 0;
  if (status === 401 || status === 403) return ErrorKind.AUTH_FAILURE;
  if (status === 429)                    return ErrorKind.HTTP_429;
  if (status >= 500)                     return ErrorKind.HTTP_5XX;
  // JSON parse failure — surfaced by request() catch-around-JSON.parse.
  if (err.errFromParse === true)         return ErrorKind.JSON_PARSE;
  // Network-level failure hints.
  const msg = (err.message || err.toString() || "").toLowerCase();
  if (msg.includes("cloudflare") || msg.includes("520") || msg.includes("origin web server")) {
    return ErrorKind.HTTP_5XX;
  }
  if (msg.includes("network request failed") || msg.includes("failed to fetch")) {
    // If browser reports offline, tag as OFFLINE explicitly.
    if (typeof navigator !== "undefined" && navigator && (navigator as any).onLine === false) {
      return ErrorKind.NETWORK_OFFLINE;
    }
    return ErrorKind.NETWORK_OFFLINE;
  }
  if (msg.includes("dns") || msg.includes("tls") || msg.includes("ssl")) {
    return ErrorKind.DNS_TLS;
  }
  return ErrorKind.UNKNOWN;
}

/** Convenience — is this error "user visible"?  ABORTED_SUPERSEDED
 *  and STALE_FALLBACK are silent by contract. */
export function isSilentError(k: ErrorKindT): boolean {
  return k === ErrorKind.ABORTED_SUPERSEDED || k === ErrorKind.STALE_FALLBACK;
}
