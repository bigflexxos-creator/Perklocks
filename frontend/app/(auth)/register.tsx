import React, { useState } from "react";
import {
  View, Text, TextInput, Pressable, StyleSheet,
  KeyboardAvoidingView, Platform, ScrollView, ActivityIndicator,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { useRouter } from "expo-router";
import { COLORS } from "@/src/theme";
import { useAuth } from "@/src/contexts/AuthContext";

export default function Register() {
  const router = useRouter();
  const { signUp } = useAuth();
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const onSubmit = async () => {
    setError(null);
    if (password.length < 6) {
      setError("Password must be at least 6 characters");
      return;
    }
    setLoading(true);
    try {
      await signUp(email.trim().toLowerCase(), password, name.trim() || undefined);
      router.replace("/(tabs)");
    } catch (e: any) {
      setError(e?.message || "Sign up failed");
    } finally {
      setLoading(false);
    }
  };

  return (
    <SafeAreaView style={styles.safe} edges={["top", "bottom"]}>
      <KeyboardAvoidingView behavior={Platform.OS === "ios" ? "padding" : undefined} style={{ flex: 1 }}>
        <ScrollView contentContainerStyle={styles.scroll} keyboardShouldPersistTaps="handled" showsVerticalScrollIndicator={false}>
          <Text style={styles.title}>Create account</Text>
          <Text style={styles.subtitle}>Start tracking the day&apos;s elite locks</Text>

          <View style={styles.field}>
            <Text style={styles.label}>NAME (OPTIONAL)</Text>
            <TextInput
              testID="register-name-input"
              value={name} onChangeText={setName}
              placeholder="Your name"
              placeholderTextColor={COLORS.textMuted}
              style={styles.input}
            />
          </View>
          <View style={styles.field}>
            <Text style={styles.label}>EMAIL</Text>
            <TextInput
              testID="register-email-input"
              value={email} onChangeText={setEmail}
              autoCapitalize="none" autoCorrect={false} keyboardType="email-address"
              placeholder="you@example.com"
              placeholderTextColor={COLORS.textMuted}
              style={styles.input}
            />
          </View>
          <View style={styles.field}>
            <Text style={styles.label}>PASSWORD</Text>
            <TextInput
              testID="register-password-input"
              value={password} onChangeText={setPassword} secureTextEntry
              placeholder="At least 6 characters"
              placeholderTextColor={COLORS.textMuted}
              style={styles.input}
            />
          </View>

          {error && <Text testID="register-error" style={styles.error}>{error}</Text>}

          <Pressable
            testID="register-submit-button"
            disabled={loading || !email || !password}
            onPress={onSubmit}
            style={({ pressed }) => [
              styles.cta,
              (loading || !email || !password) && { opacity: 0.5 },
              pressed && { transform: [{ scale: 0.98 }] },
            ]}
          >
            {loading ? <ActivityIndicator color={COLORS.bg} /> : <Text style={styles.ctaText}>CREATE ACCOUNT</Text>}
          </Pressable>

          <Pressable testID="go-to-login-button" onPress={() => router.replace("/(auth)/login")} style={styles.linkBtn}>
            <Text style={styles.linkText}>
              Already have an account? <Text style={{ color: COLORS.voltBlue, fontWeight: "800" }}>Sign in</Text>
            </Text>
          </Pressable>
        </ScrollView>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: "transparent" },
  scroll: { padding: 24, paddingTop: 60, paddingBottom: 60 },
  title: { fontSize: 32, fontWeight: "900", color: COLORS.textPrimary, letterSpacing: -1, marginBottom: 6 },
  subtitle: { fontSize: 14, color: COLORS.textSecondary, marginBottom: 30 },
  field: { marginBottom: 18 },
  label: { fontSize: 10, color: COLORS.textMuted, fontWeight: "800", letterSpacing: 1.5, marginBottom: 8 },
  input: {
    backgroundColor: COLORS.surface, borderWidth: 1, borderColor: COLORS.borderDefault,
    borderRadius: 12, paddingHorizontal: 16, paddingVertical: 14,
    color: COLORS.textPrimary, fontSize: 15, fontWeight: "600",
  },
  error: { color: COLORS.electricBlaze, fontSize: 13, fontWeight: "600", marginTop: 4, marginBottom: 8 },
  cta: { backgroundColor: COLORS.textPrimary, borderRadius: 12, paddingVertical: 16, alignItems: "center", marginTop: 10 },
  ctaText: { color: COLORS.bg, fontSize: 14, fontWeight: "900", letterSpacing: 2 },
  linkBtn: { marginTop: 24, alignItems: "center" },
  linkText: { color: COLORS.textSecondary, fontSize: 14 },
});
