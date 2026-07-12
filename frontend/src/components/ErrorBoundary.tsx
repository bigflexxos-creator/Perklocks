/**
 * ErrorBoundary — root-level crash catcher for the PerksLocks app.
 *
 * Wraps the entire render tree. If any descendant component throws during
 * render or in a lifecycle method, this catches the error, logs it
 * internally (console.error visible in Metro/dev), and renders a friendly
 * "Something went wrong — Tap to retry" card instead of the React Native
 * white screen.
 *
 * Does NOT catch:
 *  - Errors in event handlers (those are caught locally with try/catch)
 *  - Async errors (those are caught by the API client's retry layer)
 *  - SSR errors (we don't SSR)
 */
import React from "react";
import { View, Text, Pressable, StyleSheet, ScrollView } from "react-native";
import { reportError } from "@/src/lib/telemetry";

type Props = {
  children: React.ReactNode;
  /** Human-readable name used in telemetry (e.g. "HomeTab", "LabTab"). */
  boundary?: string;
  /** Path snapshot at render time (routes register this). */
  urlPath?: string;
  /** Optional custom fallback UI instead of the default retry card. */
  fallback?: (err: Error, retry: () => void) => React.ReactNode;
};
type State = { hasError: boolean; error: Error | null; errorInfo: string };

export class ErrorBoundary extends React.Component<Props, State> {
  state: State = { hasError: false, error: null, errorInfo: "" };

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error, errorInfo: "" };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error("[ErrorBoundary] caught:", error);
    console.error("[ErrorBoundary] componentStack:", info.componentStack);
    this.setState({ errorInfo: info.componentStack ?? "" });
    // Fire-and-forget telemetry so we can see what's crashing in the wild
    // without waiting for user reports. Never let telemetry itself throw.
    void reportError(error, {
      component: this.props.boundary || "ErrorBoundary",
      urlPath:   this.props.urlPath,
      extra:     { componentStack: (info.componentStack || "").slice(0, 3000) },
    });
  }

  handleRetry = () => {
    this.setState({ hasError: false, error: null, errorInfo: "" });
  };

  render() {
    if (this.state.hasError) {
      if (this.props.fallback && this.state.error) {
        return this.props.fallback(this.state.error, this.handleRetry);
      }
      return (
        <View style={styles.root}>
          <View style={styles.card}>
            <Text style={styles.title}>Something went wrong</Text>
            <Text style={styles.msg}>
              The app hit an unexpected error. Tap retry below — if it keeps
              happening, force-close the app and reopen.
            </Text>
            {!!this.state.error?.message && (
              <ScrollView style={styles.errBox}>
                <Text style={styles.errTxt} selectable>
                  {this.state.error.message}
                </Text>
              </ScrollView>
            )}
            <Pressable
              onPress={this.handleRetry}
              testID="error-boundary-retry"
              style={({ pressed }) => [
                styles.retryBtn,
                pressed && { opacity: 0.7 },
              ]}
            >
              <Text style={styles.retryTxt}>TAP TO RETRY</Text>
            </Pressable>
          </View>
        </View>
      );
    }
    return this.props.children;
  }
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: "#0A0A0A", padding: 24,
    justifyContent: "center", alignItems: "center" },
  card: { width: "100%", maxWidth: 420, backgroundColor: "#15151A",
    borderWidth: 1, borderColor: "#22232A", borderRadius: 16, padding: 24 },
  title: { color: "#FFFFFF", fontSize: 20, fontWeight: "900",
    letterSpacing: 0.5, marginBottom: 8 },
  msg: { color: "#A0A4AE", fontSize: 14, lineHeight: 20, marginBottom: 16 },
  errBox: { maxHeight: 140, backgroundColor: "#0A0A0A",
    borderRadius: 10, padding: 10, marginBottom: 16 },
  errTxt: { color: "#FF6B7E", fontSize: 11, fontFamily: "monospace" },
  retryBtn: { backgroundColor: "#FFD700", borderRadius: 12, paddingVertical: 14,
    alignItems: "center" },
  retryTxt: { color: "#0A0A0A", fontWeight: "900", letterSpacing: 1.5,
    fontSize: 13 },
});
