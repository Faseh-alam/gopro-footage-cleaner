/** Local session storage for Supabase Auth tokens issued by Flask. */

export type AuthUser = {
  id: string;
  email: string;
  full_name: string;
};

export type AuthSession = {
  access_token: string;
  refresh_token: string;
  expires_in?: number | null;
  token_type?: string;
};

export type TodayMetrics = {
  work_date?: string;
  footage_hours?: number;
  footage_label?: string;
  metrics?: {
    start_time?: string | null;
    end_time?: string | null;
    sd_cards_connected?: number;
    footage_seconds_processed?: number;
  };
  cards?: Array<{ card_id: string; camera_serial?: string | null; footage_seconds?: number }>;
};

export type AuthState = {
  user: AuthUser;
  session: AuthSession;
  today?: TodayMetrics | null;
};

const STORAGE_KEY = "gopro_cleaner_auth";

export function loadAuth(): AuthState | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed?.session?.access_token || !parsed?.user?.id) return null;
    return parsed as AuthState;
  } catch {
    return null;
  }
}

export function saveAuth(state: AuthState | null) {
  if (typeof window === "undefined") return;
  if (!state) {
    window.localStorage.removeItem(STORAGE_KEY);
    return;
  }
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
}

export function getAccessToken(): string {
  return loadAuth()?.session?.access_token || "";
}
