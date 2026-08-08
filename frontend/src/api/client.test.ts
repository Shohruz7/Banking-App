/**
 * The client's two load-bearing behaviours: one refresh, and one error shape.
 */

import { describe, expect, it, vi } from "vitest";

import { apiFetch, ApiError, SessionExpired, onSessionExpired } from "./client";
import { setTokens } from "../auth/store";
import { BASE, errorBody, http, json, server } from "../test/server";

const signedIn = () => setTokens({ access: "access-1", refresh: "refresh-1" });

describe("apiFetch", () => {
  it("unwraps the error envelope into a code and per-field details", async () => {
    signedIn();
    server.use(
      http.post(`${BASE}/transfers/`, () =>
        json(errorBody("validation_error", "Invalid input.", { amount: ["Too small."] }), 400),
      ),
    );

    const error = await apiFetch("/transfers/", { method: "POST", body: {} }).catch((e) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).code).toBe("validation_error");
    expect((error as ApiError).fieldError("amount")).toBe("Too small.");
  });

  it("refreshes once for concurrent 401s, not once per request", async () => {
    // The property that matters. Five simultaneous 401s firing five refreshes means four of them
    // present a token the first has already rotated away — and reuse detection (ADR-0013) reads
    // four replays as theft and revokes the whole family. The user is logged out *because* their
    // session was healthy.
    signedIn();
    const refreshes = vi.fn();
    let accessValid = false;

    server.use(
      http.post(`${BASE}/auth/token/refresh/`, () => {
        refreshes();
        accessValid = true;
        return json({ access: "access-2", refresh: "refresh-2" });
      }),
      http.get(`${BASE}/accounts/`, ({ request }) => {
        const token = request.headers.get("Authorization");
        if (!accessValid || token !== "Bearer access-2") {
          return json(errorBody("not_authenticated", "Token is invalid."), 401);
        }
        return json({ next: null, previous: null, results: [] });
      }),
    );

    const results = await Promise.all([
      apiFetch("/accounts/"),
      apiFetch("/accounts/"),
      apiFetch("/accounts/"),
      apiFetch("/accounts/"),
      apiFetch("/accounts/"),
    ]);

    expect(refreshes).toHaveBeenCalledTimes(1);
    expect(results).toHaveLength(5);
  });

  it("stores the rotated refresh token, not just the new access token", async () => {
    // Keeping the old refresh token would make the *next* refresh a replay of a rotated one, which
    // the backend correctly treats as theft. The bug would surface a quarter of an hour later.
    signedIn();
    let presented: string | null = null;
    let accessValid = false;

    server.use(
      http.post(`${BASE}/auth/token/refresh/`, async ({ request }) => {
        presented = ((await request.json()) as { refresh: string }).refresh;
        accessValid = true;
        return json({ access: "access-2", refresh: "refresh-2" });
      }),
      http.get(`${BASE}/auth/me/`, () =>
        accessValid ? json({}) : json(errorBody("not_authenticated", "nope"), 401),
      ),
    );

    await apiFetch("/auth/me/");
    expect(presented).toBe("refresh-1");
    expect(localStorage.getItem("banking.refresh")).toBe("refresh-2");
  });

  it("ends the session when the refresh itself fails", async () => {
    signedIn();
    const expired = vi.fn();
    const stop = onSessionExpired(expired);

    server.use(
      http.get(`${BASE}/accounts/`, () => json(errorBody("not_authenticated", "nope"), 401)),
      http.post(`${BASE}/auth/token/refresh/`, () =>
        json(errorBody("token_not_valid", "Session has been revoked."), 401),
      ),
    );

    await expect(apiFetch("/accounts/")).rejects.toBeInstanceOf(SessionExpired);
    expect(expired).toHaveBeenCalledOnce();
    expect(localStorage.getItem("banking.refresh")).toBeNull();
    stop();
  });

  it("never refreshes for an anonymous request", async () => {
    // A wrong password is a 401 on the login endpoint. Treating it as an expired token would fire a
    // pointless refresh and report "session expired" to somebody who simply mistyped.
    const refreshes = vi.fn();
    server.use(
      http.post(`${BASE}/auth/token/refresh/`, () => {
        refreshes();
        return json({});
      }),
      http.post(`${BASE}/auth/token/`, () =>
        json(errorBody("no_active_account", "No active account found."), 401),
      ),
    );

    await expect(
      apiFetch("/auth/token/", { method: "POST", body: {}, anonymous: true }),
    ).rejects.toBeInstanceOf(ApiError);
    expect(refreshes).not.toHaveBeenCalled();
  });
});
