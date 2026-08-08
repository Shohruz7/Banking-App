/**
 * Where the two tokens live (ADR-0030).
 *
 * The access token is a module variable and is **never persisted**. The refresh token goes in
 * `localStorage`, so a page reload does not become a logout.
 *
 * The tradeoff is stated rather than hidden: script running on this origin can read the refresh
 * token. What makes that survivable is the backend, not this file — refresh tokens rotate on every
 * use, a replayed one revokes its whole family (ADR-0013), and the window is one day. An HttpOnly
 * cookie would close the hole properly and is deliberately deferred; it needs the backend to set
 * and clear the cookie across four endpoints plus CSRF on the refresh POST, which is a week's
 * decision and not this week's.
 *
 * Keeping the *access* token out of storage is the part that is free, and it is the one an attacker
 * would rather have: it is the credential that authenticates every request and the socket, and
 * nothing on a reload needs it — the refresh token mints a new one.
 */

import type { TokenPair } from "../api/types";

const REFRESH_KEY = "banking.refresh";

let accessToken: string | null = null;

export const getAccessToken = (): string | null => accessToken;

export function getRefreshToken(): string | null {
  try {
    return localStorage.getItem(REFRESH_KEY);
  } catch {
    // Safari in private mode throws on localStorage access. A session that cannot survive a reload
    // is worse than no session, so this degrades to memory-only rather than failing the login.
    return null;
  }
}

export function setTokens(pair: TokenPair): void {
  accessToken = pair.access;
  try {
    localStorage.setItem(REFRESH_KEY, pair.refresh);
  } catch {
    /* memory-only, as above */
  }
  notify();
}

export function clearSession(): void {
  accessToken = null;
  try {
    localStorage.removeItem(REFRESH_KEY);
  } catch {
    /* nothing to clear */
  }
  notify();
}

/** True when a reload might be able to resume — i.e. there is a refresh token to spend. */
export const hasStoredSession = (): boolean => getRefreshToken() !== null;

// ---------------------------------------------------------------------------------------------
// Change notification
// ---------------------------------------------------------------------------------------------

type Listener = () => void;
const listeners = new Set<Listener>();

/**
 * Subscribe to token changes.
 *
 * The socket is the reason this exists: it authenticates with the access token in its first frame
 * and has to re-authenticate with the *new* one after a refresh, or the consumer closes it at the
 * old token's expiry.
 */
export function onTokensChanged(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function notify(): void {
  for (const listener of listeners) listener();
}
