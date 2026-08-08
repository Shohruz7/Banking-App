/**
 * The WebSocket client for `ws/v1/stream/` (ADR-0022).
 *
 * The protocol is accept-then-authenticate, and every one of the client's obligations exists
 * because the consumer will otherwise close the socket:
 *
 * - The **first frame** must be `{type: "auth", token}`, sent inside `WS_AUTH_DEADLINE_SECONDS`
 *   (5s on the server) or the socket closes 4401.
 * - `auth.ok` carries `expires_at`. The server closes the socket the moment that access token
 *   expires, so a fresh `auth` frame has to arrive *before* it — re-authenticating keeps the same
 *   connection, its group memberships and its subscriptions. Without this the socket would silently
 *   die every fifteen minutes and the UI would just stop updating.
 * - **4403** means the session was revoked (a `session.revoked` message precedes it). That is not a
 *   transport failure and must not be retried: the credentials are gone. Reconnecting would spin.
 * - **4429** means too many symbol subscriptions; drop them and reconnect rather than hammering.
 *
 * A socket is a courtesy, never a source of truth. Every event it carries describes something that
 * has already committed (ADR-0023), and every screen still works with the socket closed — it just
 * updates on refetch instead.
 */

export const CLOSE_UNAUTHENTICATED = 4401;
export const CLOSE_SESSION_REVOKED = 4403;
export const CLOSE_TOO_MANY_SUBSCRIPTIONS = 4429;

/** Server's ceiling is 25; staying under it client-side turns a disconnect into a no-op. */
export const MAX_SUBSCRIPTIONS = 25;

/** Re-authenticate this long before the access token expires. */
const REAUTH_MARGIN_MS = 30_000;

export type StreamEvent =
  | { type: "auth.ok"; user_id: number; username: string; expires_at: string }
  | { type: "subscribed"; symbols: string[]; unknown?: string[] }
  | { type: "session.revoked" }
  | { type: "pong"; at: string }
  | { type: "error"; code: string; message: string }
  | { type: "balance.updated"; account_id: string; balance: string; at: string }
  | { type: "transfer.posted"; entry_id: string; amount: string; description: string; at: string }
  | { type: "price.tick"; symbol: string; price: string; at: string }
  | { type: "order.filled"; at: string; [key: string]: unknown }
  | { type: "order.rejected"; at: string; [key: string]: unknown }
  | { type: "order.cancelled"; at: string; [key: string]: unknown };

export interface StreamHandlers {
  onEvent: (event: StreamEvent) => void;
  /** The session is over — a revoked session or a token that cannot be renewed. */
  onSessionLost: () => void;
  /** Current access token, read at connect time and at every re-auth. */
  getToken: () => string | null;
}

export class StreamClient {
  private socket: WebSocket | null = null;
  private handlers: StreamHandlers;
  private subscriptions = new Set<string>();
  private reauthTimer: ReturnType<typeof setTimeout> | null = null;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private attempt = 0;
  /** Set when the caller closed it deliberately, so `onclose` does not reconnect. */
  private closing = false;

  constructor(handlers: StreamHandlers) {
    this.handlers = handlers;
  }

  connect(): void {
    if (this.socket || this.closing) return;
    const token = this.handlers.getToken();
    if (!token) return;

    const scheme = window.location.protocol === "https:" ? "wss" : "ws";
    const socket = new WebSocket(`${scheme}://${window.location.host}/ws/v1/stream/`);
    this.socket = socket;

    socket.onopen = () => {
      // First frame, immediately. Anything else here and the deadline task closes us.
      this.send({ type: "auth", token: this.handlers.getToken() ?? "" });
    };

    socket.onmessage = (raw) => {
      let event: StreamEvent;
      try {
        event = JSON.parse(raw.data as string) as StreamEvent;
      } catch {
        return;
      }

      if (event.type === "auth.ok") {
        this.attempt = 0;
        this.scheduleReauth(event.expires_at);
        // Re-subscribing on every auth.ok, not just the first: after a reconnect the server has no
        // memory of what this client was watching.
        if (this.subscriptions.size > 0) {
          this.send({ type: "subscribe", symbols: [...this.subscriptions] });
        }
      }

      this.handlers.onEvent(event);
    };

    socket.onclose = (event) => {
      this.socket = null;
      this.clearReauth();

      if (this.closing) return;

      if (event.code === CLOSE_SESSION_REVOKED) {
        this.handlers.onSessionLost();
        return;
      }

      if (event.code === CLOSE_TOO_MANY_SUBSCRIPTIONS) {
        this.subscriptions.clear();
      }

      this.scheduleReconnect();
    };
  }

  /** Re-authenticate now — call after a token refresh so the socket outlives the old token. */
  reauthenticate(): void {
    const token = this.handlers.getToken();
    if (token && this.socket?.readyState === WebSocket.OPEN) {
      this.send({ type: "auth", token });
    }
  }

  subscribe(symbols: string[]): void {
    const added = symbols
      .map((s) => s.toUpperCase())
      .filter((s) => !this.subscriptions.has(s))
      .slice(0, Math.max(0, MAX_SUBSCRIPTIONS - this.subscriptions.size));
    if (added.length === 0) return;

    for (const symbol of added) this.subscriptions.add(symbol);
    this.send({ type: "subscribe", symbols: added });
  }

  unsubscribe(symbols: string[]): void {
    const removed = symbols.map((s) => s.toUpperCase()).filter((s) => this.subscriptions.has(s));
    if (removed.length === 0) return;

    for (const symbol of removed) this.subscriptions.delete(symbol);
    this.send({ type: "unsubscribe", symbols: removed });
  }

  close(): void {
    this.closing = true;
    this.clearReauth();
    if (this.retryTimer) clearTimeout(this.retryTimer);
    this.socket?.close();
    this.socket = null;
    this.subscriptions.clear();
  }

  // -------------------------------------------------------------------------------------------

  private send(payload: Record<string, unknown>): void {
    if (this.socket?.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify(payload));
    }
  }

  private scheduleReauth(expiresAt: string): void {
    this.clearReauth();
    const remaining = new Date(expiresAt).getTime() - Date.now() - REAUTH_MARGIN_MS;
    this.reauthTimer = setTimeout(() => this.reauthenticate(), Math.max(remaining, 1000));
  }

  private clearReauth(): void {
    if (this.reauthTimer) clearTimeout(this.reauthTimer);
    this.reauthTimer = null;
  }

  /** Exponential backoff with jitter, capped — a server that is down should not be stampeded. */
  private scheduleReconnect(): void {
    const base = Math.min(1000 * 2 ** this.attempt, 30_000);
    const delay = base * (0.5 + Math.random() / 2);
    this.attempt += 1;
    this.retryTimer = setTimeout(() => this.connect(), delay);
  }
}
