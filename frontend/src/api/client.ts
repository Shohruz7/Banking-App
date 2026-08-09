/**
 * The one way this app talks to the API.
 *
 * Three jobs, and the middle one is the only interesting one:
 *
 * 1. Attach the bearer token and parse the single error envelope (ADR-0006) into an `ApiError`
 *    carrying `.code` and `.details`, so a caller branches on `"insufficient_funds"` rather than
 *    on a message string that will be reworded one day.
 *
 * 2. **Refresh exactly once.** When a 15-minute access token expires, a dashboard typically has
 *    four or five queries in flight, and every one of them gets a 401 at the same instant. The
 *    naive interceptor fires five refreshes; four of them present a token that the first one has
 *    already rotated away, and rotation with reuse detection (ADR-0013) reads four replays of a
 *    rotated token as a stolen-token incident and revokes the entire family. The user is logged
 *    out *because* their session was healthy. So a refresh in progress is a promise every other
 *    401 awaits.
 *
 * 3. Give up cleanly. A failed refresh clears the session and hands control to the auth layer,
 *    which is also what closes the socket.
 */

import { clearSession, getAccessToken, getRefreshToken, setTokens } from "../auth/store";
import type { ErrorEnvelope, TokenPair } from "./types";

/** Same-origin by design — Vite proxies in dev, nginx in prod (ADR-0029). */
const BASE = "/api/v1";

/** One non-2xx response, unwrapped. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown>;

  constructor(status: number, code: string, message: string, details: Record<string, unknown>) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }

  /** The first message for a field, if the server rejected one. */
  fieldError(field: string): string | undefined {
    const value = this.details[field];
    if (Array.isArray(value)) return typeof value[0] === "string" ? value[0] : undefined;
    return typeof value === "string" ? value : undefined;
  }
}

/** Raised when a refresh fails and the session is over. The auth layer listens for this. */
export class SessionExpired extends Error {
  constructor() {
    super("Session expired");
    this.name = "SessionExpired";
  }
}

type Listener = () => void;
const expiryListeners = new Set<Listener>();

/** Notified when a refresh fails — one subscriber (the auth provider) in practice. */
export function onSessionExpired(listener: Listener): () => void {
  expiryListeners.add(listener);
  return () => expiryListeners.delete(listener);
}

function announceExpiry(): void {
  clearSession();
  for (const listener of expiryListeners) listener();
}

// ---------------------------------------------------------------------------------------------
// The refresh, deduped
// ---------------------------------------------------------------------------------------------

/** The in-flight refresh, or null. Every concurrent 401 awaits this exact promise. */
let refreshInFlight: Promise<string> | null = null;

async function refreshAccessToken(): Promise<string> {
  const refresh = getRefreshToken();
  if (!refresh) throw new SessionExpired();

  const response = await fetch(`${BASE}/auth/token/refresh/`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh }),
  });

  if (!response.ok) throw new SessionExpired();

  const pair = (await response.json()) as TokenPair;
  // The backend rotates: the response carries a *new* refresh token and the old one is blacklisted.
  // Storing only the access token here would leave the next refresh presenting a rotated token —
  // which reuse detection correctly treats as theft.
  setTokens(pair);
  return pair.access;
}

function refreshOnce(): Promise<string> {
  refreshInFlight ??= refreshAccessToken().finally(() => {
    refreshInFlight = null;
  });
  return refreshInFlight;
}

// ---------------------------------------------------------------------------------------------
// The request
// ---------------------------------------------------------------------------------------------

export interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  body?: unknown;
  /** Skip the bearer token and the refresh dance — login, register, refresh itself. */
  anonymous?: boolean;
  signal?: AbortSignal;
}

async function toApiError(response: Response): Promise<ApiError> {
  let code = "error";
  let message = response.statusText || "Request failed";
  let details: Record<string, unknown> = {};

  try {
    const body = (await response.json()) as Partial<ErrorEnvelope>;
    if (body.error) {
      code = body.error.code ?? code;
      message = body.error.message ?? message;
      details = (body.error.details ?? {}) as Record<string, unknown>;
    }
  } catch {
    // A response with no JSON body at all — a proxy error page, say. The status is all we have,
    // and inventing a code would be worse than admitting that.
  }

  return new ApiError(response.status, code, message, details);
}

async function send(path: string, options: RequestOptions, token: string | null): Promise<Response> {
  const headers: Record<string, string> = {};
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (token) headers["Authorization"] = `Bearer ${token}`;

  return fetch(`${BASE}${path}`, {
    method: options.method ?? "GET",
    headers,
    ...(options.body !== undefined ? { body: JSON.stringify(options.body) } : {}),
    ...(options.signal ? { signal: options.signal } : {}),
  });
}

/**
 * Send, and on a 401 refresh once and send again.
 *
 * The one place that knows how a request survives an expired access token. It used to be inlined
 * in `apiFetch` and `apiFetchWithStatus`, and `downloadFile` was written without it — which is
 * precisely the bug that costs you every statement download on a tab left open past the token
 * lifetime. Three callers, one rule.
 */
async function sendWithRefresh(path: string, options: RequestOptions): Promise<Response> {
  const response = await send(path, options, options.anonymous ? null : getAccessToken());
  if (response.status !== 401 || options.anonymous) return response;

  try {
    const token = await refreshOnce();
    return await send(path, options, token);
  } catch {
    announceExpiry();
    throw new SessionExpired();
  }
}

/**
 * Make a request, refreshing once on a 401 and retrying.
 *
 * `TResponse` is asserted, not validated: the generated types are the contract and CI proves they
 * match the server, so a runtime schema check here would be a second implementation of a promise
 * already kept somewhere better.
 */
export async function apiFetch<TResponse>(
  path: string,
  options: RequestOptions = {},
): Promise<TResponse> {
  const response = await sendWithRefresh(path, options);

  if (!response.ok) throw await toApiError(response);
  if (response.status === 204) return undefined as TResponse;
  return (await response.json()) as TResponse;
}

/**
 * Like {@link apiFetch}, but hands back the status too.
 *
 * Exists for exactly one endpoint. `POST /transfers/` answers **201** when it posted and **200**
 * when an idempotency key replayed an entry that had already posted, and the difference is the
 * whole point of ADR-0010 — "I made this happen" versus "this had already happened". A client that
 * only reads the body cannot tell them apart and will cheerfully tell the user they sent the money
 * twice.
 */
export async function apiFetchWithStatus<TResponse>(
  path: string,
  options: RequestOptions = {},
): Promise<{ status: number; data: TResponse }> {
  const response = await sendWithRefresh(path, options);

  if (!response.ok) throw await toApiError(response);
  if (response.status === 204) return { status: 204, data: undefined as TResponse };
  return { status: response.status, data: (await response.json()) as TResponse };
}

/**
 * Follow a pagination cursor. The API returns absolute URLs; strip the prefix and reuse the client
 * rather than calling `fetch` directly, so a page-two request still refreshes its token.
 */
export function fetchCursor<TResponse>(url: string): Promise<TResponse> {
  const path = url.replace(/^https?:\/\/[^/]+/, "").replace(BASE, "");
  return apiFetch<TResponse>(path);
}

/**
 * Download a file through the API.
 *
 * A statement PDF needs an `Authorization` header, so a plain `<a href>` cannot fetch it — the
 * browser sends no bearer token and gets a 401. Fetch it, turn it into an object URL, click it,
 * and revoke.
 *
 * Through `sendWithRefresh` like everything else. Calling `send` directly here meant the download
 * was the one request in the app that could not survive an expired access token: leave the
 * statements page open for the fifteen minutes the token lasts and every download failed with
 * "Could not download that statement", which is true and completely misleading.
 */
export async function downloadFile(path: string, filename: string): Promise<void> {
  const response = await sendWithRefresh(path, {});
  if (!response.ok) throw await toApiError(response);

  const url = URL.createObjectURL(await response.blob());
  try {
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    anchor.click();
  } finally {
    URL.revokeObjectURL(url);
  }
}

/** Test seam: forget any in-flight refresh between tests. */
export function __resetRefreshState(): void {
  refreshInFlight = null;
}
