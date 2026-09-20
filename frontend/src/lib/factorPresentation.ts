/**
 * FactorPresentation — Gate 3 P1 (2026-06)
 *
 * Typed consumer-presentation boundary for backend factor / evidence
 * metadata rendered on Pick Breakdown.  Previously the UI iterated
 * ``Object.entries(pick.factors)`` and coerced every value with
 * ``Number(v) || 0`` + a `%` glyph, which:
 *
 *   • leaked internal ``__*`` metadata to consumers;
 *   • fabricated 0 % values when the underlying evidence was
 *     categorical or a count;
 *   • rendered raw snake_case backend variable names ("model_pct",
 *     "l3_edge_norm") in the UI.
 *
 * This module maps each known backend factor key to an allowlisted
 * presentation object.  Unknown / internal keys resolve to
 * ``userVisible = false`` so consumer UI never renders them; Developer
 * Diagnostics remains free to display the raw payload.
 *
 * No model math changes — this is a display projection.
 */
export type FactorDirection = "positive" | "negative" | "neutral";
export type FactorUnit =
  | "percent"       // 0-100 display
  | "probability"   // 0-1 probability
  | "score"         // score / 100 style number
  | "yards"
  | "count"
  | "minutes"
  | "label"         // categorical
  | "unitless";

export interface FactorPresentation {
  key: string;
  label: string;
  displayValue: string;
  numericValue?: number;
  unit?: FactorUnit;
  range?: [number, number];
  direction?: FactorDirection;
  description?: string;
  userVisible: boolean;
}

interface FactorSpec {
  label: string;
  unit: FactorUnit;
  range?: [number, number];
  description?: string;
}

// Allowlist of user-visible factor keys.  Anything not listed here is
// treated as internal / dev-diag only.
const _SPECS: Record<string, FactorSpec> = {
  // Model-side
  model_pct:         { label: "Model win probability",     unit: "percent",     range: [0, 100],
                       description: "Model probability the selection wins." },
  model_edge:        { label: "Model edge vs. line",        unit: "percent",     range: [-30, 30],
                       description: "Model probability minus the implied price of the line." },
  probability:       { label: "Blended probability",        unit: "probability", range: [0, 1],
                       description: "Unified probability engine output." },
  edge:              { label: "Edge",                        unit: "percent",     range: [-30, 30] },
  lock_score:        { label: "Lock Score",                  unit: "score",       range: [0, 100],
                       description: "Canonical published Perklocks Lock Score." },
  confidence:        { label: "Confidence",                  unit: "score",       range: [0, 100] },
  signal_score:      { label: "Signal Score",                unit: "score",       range: [0, 100] },
  matchup_score:     { label: "Matchup Score",               unit: "score",       range: [0, 100] },
  win_probability:   { label: "Win Expected",                unit: "probability", range: [0, 1] },
  matchup_grade:     { label: "Matchup grade",               unit: "label" },
  tier_v2:           { label: "Tier",                        unit: "label" },
  grade:             { label: "Grade",                       unit: "label" },
  // Player form
  n_picks:           { label: "Recent sample size",          unit: "count" },
  games_logged:      { label: "Games logged",                unit: "count" },
  current_streak:    { label: "Current streak",              unit: "count" },
  consistency:       { label: "Consistency",                 unit: "probability", range: [0, 1] },
  // Sport-specific counts
  pass_yds:          { label: "Passing yards",               unit: "yards" },
  rush_yds:          { label: "Rushing yards",               unit: "yards" },
  rec_yds:           { label: "Receiving yards",             unit: "yards" },
  receptions:        { label: "Receptions",                  unit: "count" },
  hits:              { label: "Hits",                        unit: "count" },
  outs_recorded:     { label: "Outs recorded",               unit: "count" },
  minutes:           { label: "Minutes",                     unit: "minutes" },
  points:            { label: "Points",                      unit: "count" },
  rebounds:          { label: "Rebounds",                    unit: "count" },
  assists:           { label: "Assists",                     unit: "count" },
  threes:            { label: "3-pointers",                  unit: "count" },
  goals:             { label: "Goals",                       unit: "count" },
  sot:               { label: "Shots on target",             unit: "count" },
  shots:             { label: "Shots",                       unit: "count" },
  xg:                { label: "Expected goals (xG)",         unit: "probability", range: [0, 3] },
};

function _formatValue(raw: unknown, spec: FactorSpec): { display: string; numeric?: number } {
  if (raw == null || raw === "") {
    return { display: "—" };
  }
  if (spec.unit === "label") {
    return { display: String(raw) };
  }
  const n = typeof raw === "number" ? raw : Number(raw);
  if (!Number.isFinite(n)) {
    // Categorical / string data on a numeric spec — surface as label.
    return { display: String(raw) };
  }
  switch (spec.unit) {
    case "percent":
      // Assume 0-100 already unless range says 0-1
      if (spec.range && spec.range[1] <= 1) return { display: `${(n * 100).toFixed(1)}%`, numeric: n };
      return { display: `${n.toFixed(1)}%`, numeric: n };
    case "probability":
      return { display: `${(n * 100).toFixed(1)}%`, numeric: n };
    case "score":
      return { display: n.toFixed(1), numeric: n };
    case "yards":
      return { display: `${Math.round(n)} yd`, numeric: n };
    case "count":
      return { display: Number.isInteger(n) ? String(n) : n.toFixed(1), numeric: n };
    case "minutes":
      return { display: `${Math.round(n)} min`, numeric: n };
    default:
      return { display: n.toString(), numeric: n };
  }
}

function _direction(spec: FactorSpec, num?: number): FactorDirection {
  if (num == null) return "neutral";
  if (spec.unit === "score" || spec.unit === "percent" || spec.unit === "probability") {
    const midpoint = spec.range ? (spec.range[0] + spec.range[1]) / 2 : 0;
    return num > midpoint ? "positive" : num < midpoint ? "negative" : "neutral";
  }
  return "neutral";
}

/** Return a user-safe presentation for a single factor entry.  When
 *  ``userVisible`` is ``false`` the caller MUST NOT render it in the
 *  normal consumer UI. */
export function factorPresentation(key: string, value: unknown): FactorPresentation {
  // Internal metadata prefix — never expose to consumers.
  if (typeof key === "string" && key.startsWith("__")) {
    return {
      key,
      label: key,
      displayValue: String(value),
      userVisible: false,
    };
  }
  const spec = _SPECS[key];
  if (!spec) {
    // Not in the allowlist — fail closed to consumer UI.
    return {
      key,
      label: key,
      displayValue: String(value),
      userVisible: false,
    };
  }
  const { display, numeric } = _formatValue(value, spec);
  return {
    key,
    label: spec.label,
    displayValue: display,
    numericValue: numeric,
    unit: spec.unit,
    range: spec.range,
    direction: _direction(spec, numeric),
    description: spec.description,
    userVisible: true,
  };
}

/** Project a full ``factors`` object into an ordered array of
 *  consumer-safe presentations, dropping internal / unknown keys. */
export function factorPresentationList(
  factors: Record<string, unknown> | null | undefined,
): FactorPresentation[] {
  if (!factors || typeof factors !== "object") return [];
  const out: FactorPresentation[] = [];
  for (const [k, v] of Object.entries(factors)) {
    const fp = factorPresentation(k, v);
    if (fp.userVisible) out.push(fp);
  }
  return out;
}
