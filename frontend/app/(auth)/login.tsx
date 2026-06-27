import React, { useState } from "react";
import {
  View, Text, TextInput, Pressable, StyleSheet,
  KeyboardAvoidingView, Platform, ScrollView, ActivityIndicator,
  ImageBackground,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { useRouter } from "expo-router";
import { Ionicons } from "@expo/vector-icons";
import { COLORS } from "@/src/theme";
import { useAuth } from "@/src/contexts/AuthContext";

// Stadium / PL composite — same artwork used by the Expo splash screen
// so launch → login feels like one continuous brand moment.
// eslint-disable-next-line @typescript-eslint/no-var-requires
const BRAND_BG = require("@/assets/images/splash-bg.png");

export default function Login() {
  const router = useRouter();
  const { signIn } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const onSubmit = async () => {
    setError(null);
    setLoading(true);
    try {
      await signIn(email.trim().toLowerCase(), password);
      router.replace("/(tabs)");
    } catch (e: any) {
      setError(e?.message || "Sign in failed");
    } finally {
      setLoading(false);
    }
  };

  return (
    <ImageBackground source={BRAND_BG} resizeMode="cover" style={styles.safe}>
      {/* Subtle dark scrim so the form fields stay legible on top of the
          stadium background. */}
      <View style={styles.scrim} />
      <SafeAreaView style={{ flex: 1 }} edges={["top", "bottom"]}>
      <KeyboardAvoidingView
        behavior={Platform.OS === "ios" ? "padding" : undefined}
        style={{ flex: 1 }}
      >
        <ScrollView
          contentContainerStyle={styles.scroll}
          keyboardShouldPersistTaps="handled"
          showsVerticalScrollIndicator={false}
        >
          <View style={styles.brand}>
            <View style={styles.logoBox}>
              <Ionicons name="flash" size={32} color={COLORS.goldElite} />
            </View>
            <Text style={styles.brandName}>PERKLOCKS</Text>
            <Text style={styles.brandTag}>AI BETTING INTELLIGENCE</Text>
          </View>

          <Text style={styles.title}>Welcome back</Text>
          <Text style={styles.subtitle}>Sign in to access today&apos;s lock picks</Text>

          <View style={styles.field}>
            <Text style={styles.label}>EMAIL</Text>
            <TextInput
              testID="login-email-input"
              value={email}
              onChangeText={setEmail}
              autoCapitalize="none"
              autoCorrect={false}
              keyboardType="email-address"
              placeholder="you@example.com"
              placeholderTextColor={COLORS.textMuted}
              style={styles.input}
            />
          </View>

          <View style={styles.field}>
            <Text style={styles.label}>PASSWORD</Text>
            <TextInput
              testID="login-password-input"
              value={password}
              onChangeText={setPassword}
              secureTextEntry
              placeholder="••••••••"
              placeholderTextColor={COLORS.textMuted}
              style={styles.input}
            />
          </View>

          {error && (
            <Text testID="login-error" style={styles.error}>
              {error}
            </Text>
          )}

          <Pressable
            testID="login-submit-button"
            disabled={loading || !email || !password}
            onPress={onSubmit}
            style={({ pressed }) => [
              styles.cta,
              (loading || !email || !password) && { opacity: 0.5 },
              pressed && { transform: [{ scale: 0.98 }] },
            ]}
          >
            {loading ? (
              <ActivityIndicator color={COLORS.bg} />
            ) : (
              <Text style={styles.ctaText}>SIGN IN</Text>
            )}
          </Pressable>

          <Pressable
            testID="go-to-register-button"
            onPress={() => router.push("/(auth)/register")}
            style={styles.linkBtn}
          >
            <Text style={styles.linkText}>
              New to PerkLocks?{" "}
              <Text style={{ color: COLORS.voltBlue, fontWeight: "800" }}>Create account</Text>
            </Text>
          </Pressable>
        </ScrollView>
      </KeyboardAvoidingView>
      </SafeAreaView>
    </ImageBackground>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: COLORS.bg },
  scrim: {
    // Subtle dark scrim over the brand background so the form fields
    // remain legible. 55% opacity reads as "stadium glow" rather than
    // washing out the artwork entirely.
    ...StyleSheet.absoluteFillObject,
    backgroundColor: "rgba(0,0,0,0.55)",
  },
  scroll: { padding: 24, paddingTop: 40, paddingBottom: 60 },
  brand: { alignItems: "center", marginBottom: 40 },
  logoBox: {
    width: 64, height: 64, borderRadius: 16,
    backgroundColor: COLORS.surface,
    borderWidth: 1, borderColor: COLORS.borderDefault,
    alignItems: "center", justifyContent: "center", marginBottom: 12,
  },
  brandName: {
    fontSize: 28, fontWeight: "900", color: COLORS.textPrimary,
    letterSpacing: 4,
  },
  brandTag: {
    fontSize: 10, color: COLORS.textMuted, fontWeight: "700",
    letterSpacing: 2, marginTop: 4,
  },
  title: { fontSize: 32, fontWeight: "900", color: COLORS.textPrimary, letterSpacing: -1, marginBottom: 6 },
  subtitle: { fontSize: 14, color: COLORS.textSecondary, marginBottom: 30 },
  field: { marginBottom: 18 },
  label: { fontSize: 10, color: COLORS.textMuted, fontWeight: "800", letterSpacing: 1.5, marginBottom: 8 },
  input: {
    backgroundColor: COLORS.surface,
    borderWidth: 1, borderColor: COLORS.borderDefault,
    borderRadius: 12, paddingHorizontal: 16, paddingVertical: 14,
    color: COLORS.textPrimary, fontSize: 15, fontWeight: "600",
  },
  error: { color: COLORS.electricBlaze, fontSize: 13, fontWeight: "600", marginTop: 4, marginBottom: 8 },
  cta: {
    backgroundColor: COLORS.textPrimary,
    borderRadius: 12, paddingVertical: 16, alignItems: "center", marginTop: 10,
  },
  ctaText: { color: COLORS.bg, fontSize: 14, fontWeight: "900", letterSpacing: 2 },
  linkBtn: { marginTop: 24, alignItems: "center" },
  linkText: { color: COLORS.textSecondary, fontSize: 14 },
});
