/**
 * The two-step sign-in, and the two places it is easy to get backwards.
 *
 * **A 200 is not a success.** The backend answers a correct password with 200 and a body saying
 * `mfa_required`, because the password *was* right — the request did not fail. A client that
 * branches on the status lets somebody into the app on the strength of their first factor.
 *
 * **A spent challenge cannot be retried.** The pre-auth token is burned on first use, so anything
 * other than a wrong-code error means the challenge is gone and the code field has to disappear
 * with it. Leaving it up offers the user a form that can only ever produce the same error.
 */

import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { getRefreshToken } from "../auth/store";
import { BASE, aUser, errorBody, http, json, server } from "../test/server";
import { renderWithProviders } from "../test/render";
import Login from "./Login";

const TOKENS = { access: "access-1", refresh: "refresh-1" };

// No `/auth/me/` stub by default, and no bootstrap request to stub: `hasStoredSession()` is false
// with storage cleared, so `AuthProvider` skips the resume entirely. Only the test that actually
// signs in registers one — and it must be the *only* `/auth/me/` handler, because MSW takes the
// first match and a 401 stub listed ahead of it would send the client down the refresh path
// instead. That is not hypothetical: it is what the first draft of this file did, and
// `onUnhandledRequest: "error"` is what surfaced it.

async function signIn() {
  const user = userEvent.setup();
  await user.type(await screen.findByLabelText("Username or email"), "alice");
  await user.type(screen.getByLabelText("Password"), "hunter2hunter2");
  await user.click(screen.getByRole("button", { name: "Sign in" }));
  return user;
}

describe("Login", () => {
  it("treats a 200 carrying mfa_required as a challenge, not a sign-in", async () => {
    server.use(
      http.post(`${BASE}/auth/token/`, () =>
        json({ mfa_required: true, mfa_token: "pre-auth-1" }),
      ),
    );

    renderWithProviders(<Login />);
    await signIn();

    // The second factor, not the dashboard.
    expect(await screen.findByLabelText("Code")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Verify" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Password")).not.toBeInTheDocument();
  });

  it("signs straight in when no second factor is enrolled", async () => {
    server.use(
      http.post(`${BASE}/auth/token/`, () => json(TOKENS)),
      http.get(`${BASE}/auth/me/`, () => json(aUser)),
    );

    renderWithProviders(<Login />);
    await signIn();

    // Observable outcome, not the absence of a field: the token pair reached storage, which is what
    // "signed in" means here. Login renders standalone, so there is no dashboard to navigate to.
    await waitFor(() => expect(getRefreshToken()).toBe("refresh-1"));
    expect(screen.queryByLabelText("Code")).not.toBeInTheDocument();
  });

  it("keeps the code field up when the code was merely wrong", async () => {
    server.use(
      http.post(`${BASE}/auth/token/`, () => json({ mfa_required: true, mfa_token: "pre-auth-1" })),
      http.post(`${BASE}/auth/token/mfa/`, () =>
        json(errorBody("invalid_mfa_code", "Wrong code."), 400),
      ),
    );

    renderWithProviders(<Login />);
    const user = await signIn();

    const code = await screen.findByLabelText("Code");
    await user.type(code, "000000");
    await user.click(screen.getByRole("button", { name: "Verify" }));

    // A mistyped digit is retryable — the challenge is still alive, so the field stays and the
    // message says codes rotate.
    expect(await screen.findByText(/codes change every 30 seconds/i)).toBeInTheDocument();
    expect(screen.getByLabelText("Code")).toBeInTheDocument();
    expect(screen.getByLabelText("Code")).toHaveValue("");
  });

  it("takes the code field away when the challenge itself is spent", async () => {
    server.use(
      http.post(`${BASE}/auth/token/`, () => json({ mfa_required: true, mfa_token: "pre-auth-1" })),
      http.post(`${BASE}/auth/token/mfa/`, () =>
        json(errorBody("invalid_mfa_token", "Expired."), 400),
      ),
    );

    renderWithProviders(<Login />);
    const user = await signIn();

    await user.type(await screen.findByLabelText("Code"), "123456");
    await user.click(screen.getByRole("button", { name: "Verify" }));

    // The token was burned on first use, so offering the field again could only reproduce this
    // error. Back to the password form.
    expect(await screen.findByText(/no longer valid/i)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByLabelText("Code")).not.toBeInTheDocument());
    expect(screen.getByLabelText("Password")).toBeInTheDocument();
  });

  it("reports a wrong password without claiming the session expired", async () => {
    server.use(
      http.post(`${BASE}/auth/token/`, () =>
        json(errorBody("invalid_credentials", "No account matches those details."), 401),
      ),
    );

    renderWithProviders(<Login />);
    await signIn();

    // The login request is `anonymous`, so a 401 here must not trigger a refresh or a
    // "session expired" path — a mistyped password is not an expired session.
    expect(
      await screen.findByText(/no account matches those details/i),
    ).toBeInTheDocument();
  });
});
