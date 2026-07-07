/**
 * Dynamic Expo config — extends app.json with a build-time timestamp so the
 * StaleBuildBanner can accurately detect an out-of-date deployment without
 * anyone having to remember to bump a constant.
 *
 * How it works
 * ------------
 * `app.config.js` is re-evaluated by Expo/Metro every time the bundle is
 * produced (dev server start, `expo export`, Emergent Publish). We snapshot
 * `new Date().toISOString()` at that exact moment and expose it via
 * `expo.extra.buildTime`. The banner reads it through `expo-constants` and
 * compares against the current server day. A fresh deploy → diff ≈ 0 →
 * banner disappears automatically.
 *
 * The rest of the config still lives in app.json so we have a single source
 * of truth for static fields (name, ios/android, plugins, etc.).
 */
const appJson = require("./app.json");

const now = new Date();
// ISO for high-fidelity timestamp; YYYY-MM-DD for cheap day-diff on device.
const buildTime = now.toISOString();
const buildDate = buildTime.slice(0, 10); // "2026-07-07"

module.exports = ({ config }) => {
  const merged = {
    ...appJson.expo,
    ...(config || {}),
    extra: {
      ...(appJson.expo && appJson.expo.extra ? appJson.expo.extra : {}),
      ...((config && config.extra) || {}),
      buildTime,
      buildDate,
    },
  };
  return merged;
};
