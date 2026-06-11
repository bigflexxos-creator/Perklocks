import React, { createContext, useContext, useEffect, useState, ReactNode } from "react";
import { api, getToken, setToken, User } from "@/src/lib/api";

type AuthState = {
  user: User | null;
  loading: boolean;
  signIn: (email: string, password: string) => Promise<void>;
  signUp: (email: string, password: string, name?: string) => Promise<void>;
  signOut: () => Promise<void>;
};

const AuthCtx = createContext<AuthState | undefined>(undefined);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    (async () => {
      const tok = await getToken();
      if (tok) {
        try {
          const u = await api.me();
          setUser(u);
        } catch {
          await setToken(null);
        }
      }
      setLoading(false);
    })();
  }, []);

  const signIn = async (email: string, password: string) => {
    const res = await api.login(email, password);
    await setToken(res.access_token);
    setUser(res.user);
  };

  const signUp = async (email: string, password: string, name?: string) => {
    const res = await api.register(email, password, name);
    await setToken(res.access_token);
    setUser(res.user);
  };

  const signOut = async () => {
    await setToken(null);
    setUser(null);
  };

  return (
    <AuthCtx.Provider value={{ user, loading, signIn, signUp, signOut }}>
      {children}
    </AuthCtx.Provider>
  );
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthCtx);
  if (!ctx) throw new Error("useAuth must be inside AuthProvider");
  return ctx;
}
