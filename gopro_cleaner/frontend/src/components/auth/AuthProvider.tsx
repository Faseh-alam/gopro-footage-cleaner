import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { useNavigate, useRouterState } from "@tanstack/react-router";
import { toast } from "sonner";
import { api } from "@/lib/api";
import {
  loadAuth,
  saveAuth,
  type AuthState,
  type AuthUser,
  type TodayMetrics,
} from "@/lib/auth";

type AuthContextValue = {
  ready: boolean;
  user: AuthUser | null;
  today: TodayMetrics | null;
  login: (email: string, password: string) => Promise<void>;
  signup: (fullName: string, email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  refreshToday: () => Promise<void>;
};

const AuthContext = createContext<AuthContextValue | null>(null);

/** ScaleAI / local stations: no Supabase login gate. Re-enable when cloud auth is back. */
const AUTH_DISABLED = true;

const PUBLIC_PATHS = new Set(["/login"]);

function formatClock(iso?: string | null) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  } catch {
    return "—";
  }
}

export { formatClock };

export function formatMetricsSummary(today: TodayMetrics | null) {
  if (!today?.metrics) return "";
  const cards = today.metrics.sd_cards_connected ?? 0;
  const footage = today.footage_label || "0h 0m";
  const start = formatClock(today.metrics.start_time);
  return `${start} · ${cards} cards · ${footage}`;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const navigate = useNavigate();
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  const [ready, setReady] = useState(AUTH_DISABLED);
  const [auth, setAuth] = useState<AuthState | null>(null);

  const applyAuth = useCallback((next: AuthState | null) => {
    setAuth(next);
    saveAuth(next);
  }, []);

  const refreshToday = useCallback(async () => {
    if (AUTH_DISABLED) return;
    if (!loadAuth()?.session?.access_token) return;
    try {
      const data = await api("/api/auth/metrics/today");
      setAuth((prev) => {
        if (!prev) return prev;
        const next = { ...prev, today: data };
        saveAuth(next);
        return next;
      });
    } catch {
      /* ignore — may be offline mid-restart */
    }
  }, []);

  useEffect(() => {
    if (AUTH_DISABLED) {
      applyAuth(null);
      setReady(true);
      return;
    }
    let cancelled = false;
    (async () => {
      const stored = loadAuth();
      if (!stored) {
        if (!cancelled) {
          setAuth(null);
          setReady(true);
        }
        return;
      }
      try {
        const data = await api("/api/auth/me");
        if (cancelled) return;
        applyAuth({
          user: data.user,
          session: stored.session,
          today: data.today,
        });
      } catch {
        if (cancelled) return;
        try {
          const refreshed = await api("/api/auth/refresh", {
            method: "POST",
            body: JSON.stringify({ refresh_token: stored.session.refresh_token }),
          });
          if (cancelled) return;
          applyAuth({
            user: refreshed.user,
            session: refreshed.session,
            today: stored.today || null,
          });
          await refreshToday();
        } catch {
          if (!cancelled) applyAuth(null);
        }
      } finally {
        if (!cancelled) setReady(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [applyAuth, refreshToday]);

  useEffect(() => {
    if (!ready) return;
    if (AUTH_DISABLED) {
      if (pathname === "/login") navigate({ to: "/review" });
      return;
    }
    const isPublic = PUBLIC_PATHS.has(pathname);
    if (!auth?.user && !isPublic) {
      navigate({ to: "/login" });
    } else if (auth?.user && isPublic) {
      navigate({ to: "/review" });
    }
  }, [ready, auth?.user, pathname, navigate]);

  const login = useCallback(
    async (email: string, password: string) => {
      if (AUTH_DISABLED) {
        navigate({ to: "/review" });
        return;
      }
      const data = await api("/api/auth/login", {
        method: "POST",
        body: JSON.stringify({ email, password }),
      });
      applyAuth({
        user: data.user,
        session: data.session,
        today: data.today,
      });
      toast.success(`Welcome back, ${data.user.full_name || data.user.email}`);
      navigate({ to: "/review" });
    },
    [applyAuth, navigate],
  );

  const signup = useCallback(
    async (fullName: string, email: string, password: string) => {
      if (AUTH_DISABLED) {
        navigate({ to: "/review" });
        return;
      }
      const data = await api("/api/auth/signup", {
        method: "POST",
        body: JSON.stringify({ full_name: fullName, email, password }),
      });
      applyAuth({
        user: data.user,
        session: data.session,
        today: data.today,
      });
      toast.success("Account created — you're signed in");
      navigate({ to: "/review" });
    },
    [applyAuth, navigate],
  );

  const logout = useCallback(async () => {
    if (AUTH_DISABLED) {
      navigate({ to: "/review" });
      return;
    }
    try {
      await api("/api/auth/logout", { method: "POST" });
    } catch {
      /* still clear local session */
    }
    applyAuth(null);
    toast.success("Logged out — end time saved for today");
    navigate({ to: "/login" });
  }, [applyAuth, navigate]);

  const value = useMemo<AuthContextValue>(
    () => ({
      ready,
      user: auth?.user || null,
      today: auth?.today || null,
      login,
      signup,
      logout,
      refreshToday,
    }),
    [ready, auth, login, signup, logout, refreshToday],
  );

  if (!ready) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-background text-sm text-muted-foreground">
        Loading…
      </div>
    );
  }

  if (!AUTH_DISABLED && !auth?.user && !PUBLIC_PATHS.has(pathname)) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-background text-sm text-muted-foreground">
        Redirecting to login…
      </div>
    );
  }

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
