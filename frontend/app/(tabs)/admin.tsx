/**
 * Admin tab entry point — visible ONLY to `role === "admin"` users via
 * the `href: null` conditional in (tabs)/_layout.tsx. This screen is a
 * thin passthrough that re-renders the existing `/analytics` screen so
 * admins can get to model performance / bandit / bucket / CLV analytics
 * inside the tab layout instead of a modal push. Regular users never
 * see this tab (href is null for them) AND if they somehow reach it,
 * the analytics screen has its own useAuth redirect to /my-bets.
 *
 * Data comes from admin-only endpoints (all guarded by
 * `Depends(current_admin)` in routes/analytics_routes.py). Server 403s
 * a non-admin token regardless of what the UI does.
 */
import AnalyticsScreen from "../analytics";

export default function AdminTab() {
  return <AnalyticsScreen />;
}
