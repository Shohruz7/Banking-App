/**
 * The session, as React sees it.
 *
 * The login is two-step and the shape of it is the only thing here worth reading twice.
 * `POST /auth/token/` answers **200** either way: with a token pair, or with
 * `{mfa_required: true, mfa_token}` — because the password *was* right, the flow simply is not
 * finished. So `signIn` returns a discriminated result and the caller routes on it; nothing
 * inspects a status code to decide whether a login worked.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useQueryClient } from "@tanstack/react-query";

import { SessionExpired, apiFetch, onSessionExpired } from "../api/client";
import { isMfaChallenge, type LoginResponse, type TokenPair, type User } from "../api/types";
import { clearSession, getRefreshToken, hasStoredSession, setTokens } from "./store";

export type SignInResult = { status: "signed-in" } | { status: "mfa-required"; token: string };

interface AuthValue {
  user: User | null;
  /** True until the stored refresh token has been spent (or found absent) on first load. */
  loading: boolean;
  signIn: (username: string, password: string) => Promise<SignInResult>;
  completeMfa: (mfaToken: string, code: string) => Promise<void>;
  signOut: () => Promise<void>;
  refreshUser: () => Promise<void>;
}

const AuthContext = createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(hasStoredSession());
  const queryClient = useQueryClient();
  // Guards React 18's double-invoked effects in development: two bootstraps would spend the
  // refresh token twice, and the second spend is a replay of a rotated token — which the backend
  // correctly treats as theft and answers by revoking the whole family.
  const bootstrapped = useRef(false);

  const loadUser = useCallback(async () => {
    setUser(await apiFetch<User>("/auth/me/"));
  }, []);

  /** Resume a session on first load: the access token is gone, the refresh token may not be. */
  useEffect(() => {
    if (bootstrapped.current) return;
    bootstrapped.current = true;

    if (!hasStoredSession()) {
      setLoading(false);
      return;
    }

    // `apiFetch` sees no access token, gets a 401, and refreshes — the ordinary path, reused rather
    // than a second bootstrap-only implementation of it.
    loadUser()
      .catch((error: unknown) => {
        // Only an actually-rejected session clears the token. This used to catch everything, which
        // meant any failure at all deleted the refresh token from localStorage — so a transient 502
        // while the backend restarted, or a page loaded on a flaky connection, silently logged the
        // user out for good. They would reopen the tab and find themselves signed out by a deploy.
        //
        // `SessionExpired` is the only failure that means the credential is dead: `apiFetch` throws
        // it exactly when the refresh was attempted and refused. A TypeError from an unreachable
        // server, or a 5xx from a proxy, says nothing about whether the token is still good — so
        // keep it and let the next navigation try again.
        if (error instanceof SessionExpired) clearSession();
      })
      .finally(() => setLoading(false));
  }, [loadUser]);

  /** A failed refresh anywhere ends the session everywhere. */
  useEffect(
    () =>
      onSessionExpired(() => {
        setUser(null);
        queryClient.clear();
      }),
    [queryClient],
  );

  const signIn = useCallback(
    async (username: string, password: string): Promise<SignInResult> => {
      const body = await apiFetch<LoginResponse>("/auth/token/", {
        method: "POST",
        body: { username, password },
        anonymous: true,
      });

      if (isMfaChallenge(body)) return { status: "mfa-required", token: body.mfa_token };

      setTokens(body);
      await loadUser();
      return { status: "signed-in" };
    },
    [loadUser],
  );

  const completeMfa = useCallback(
    async (mfaToken: string, code: string) => {
      const pair = await apiFetch<TokenPair>("/auth/token/mfa/", {
        method: "POST",
        body: { mfa_token: mfaToken, code },
        anonymous: true,
      });
      setTokens(pair);
      await loadUser();
    },
    [loadUser],
  );

  const signOut = useCallback(async () => {
    const refresh = getRefreshToken();
    if (refresh) {
      // Best effort. The server revoking the session is what makes logout real, but a network
      // failure must not strand the user in a logged-in-looking UI — the local clear happens
      // either way.
      await apiFetch("/auth/logout/", { method: "POST", body: { refresh } }).catch(() => undefined);
    }
    clearSession();
    setUser(null);
    queryClient.clear();
  }, [queryClient]);

  const value = useMemo(
    () => ({ user, loading, signIn, completeMfa, signOut, refreshUser: loadUser }),
    [user, loading, signIn, completeMfa, signOut, loadUser],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside an AuthProvider");
  return value;
}
