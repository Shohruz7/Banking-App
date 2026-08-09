/**
 * Sign in, and the MFA challenge that may follow it.
 *
 * One component for both steps, because they are one flow and the challenge token is state that
 * belongs to it. Routing to a second page would mean either putting a pre-auth credential in a URL
 * or in storage, and it is neither of those things — it is a five-minute, single-use token that
 * exists only between these two requests.
 */

import { useState, type FormEvent } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";

import { ApiError } from "../api/client";
import { useAuth } from "../auth/AuthProvider";
import { Button, Card, ErrorNote, Field } from "../components/ui";
import { usePageTitle } from "../usePageTitle";

export default function Login() {
  usePageTitle("Sign in");
  const { signIn, completeMfa } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();

  // `RequireAuth` records where the user was headed before it bounced them here. Reading it is what
  // makes a deep link survive a sign-in — without this the state was written on every redirect and
  // never looked at, so everybody landed on the dashboard and the link they followed was lost.
  //
  // Only same-origin paths are honoured. `state` comes off the history entry, which a page on
  // another site can populate before navigating here, so treating it as a bare redirect target
  // would be an open redirect: a phishing link that lands on the real login form and then hands the
  // freshly-signed-in user to an attacker's page.
  const from = (location.state as { from?: { pathname?: string } } | null)?.from?.pathname;
  const destination = from?.startsWith("/") && !from.startsWith("//") ? from : "/";

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [mfaToken, setMfaToken] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmitPassword(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const result = await signIn(username, password);
      // A 200 carrying `mfa_required` is *not* a failure — the password was right. Branching on the
      // body rather than the status is the whole reason the backend returns 200 here.
      if (result.status === "mfa-required") setMfaToken(result.token);
      else navigate(destination, { replace: true });
    } catch (caught) {
      setError(
        caught instanceof ApiError ? caught.message : "Could not sign in. Please try again.",
      );
    } finally {
      setBusy(false);
    }
  }

  async function onSubmitCode(event: FormEvent) {
    event.preventDefault();
    if (!mfaToken) return;
    setError(null);
    setBusy(true);
    try {
      await completeMfa(mfaToken, code);
      navigate(destination, { replace: true });
    } catch (caught) {
      const message =
        caught instanceof ApiError && caught.code === "invalid_mfa_code"
          ? "That code is not right. Codes change every 30 seconds."
          : "That challenge is no longer valid. Please sign in again.";
      setError(message);
      // A spent or expired challenge cannot be retried — the backend burns the pre-auth token on
      // first use, so offering the code field again would only produce the same error.
      if (caught instanceof ApiError && caught.code !== "invalid_mfa_code") setMfaToken(null);
      setCode("");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="mx-auto flex min-h-dvh max-w-md items-center px-4">
      <Card className="w-full" title={mfaToken ? "Two-factor code" : "Sign in"}>
        {error && <div className="mb-4"><ErrorNote>{error}</ErrorNote></div>}

        {mfaToken ? (
          <form onSubmit={onSubmitCode} className="flex flex-col gap-4">
            <p className="text-sm text-ink-muted">
              Enter the six-digit code from your authenticator app.
            </p>
            <Field
              label="Code"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              inputMode="numeric"
              autoComplete="one-time-code"
              maxLength={6}
              required
              autoFocus
            />
            <Button type="submit" loading={busy}>
              Verify
            </Button>
          </form>
        ) : (
          <form onSubmit={onSubmitPassword} className="flex flex-col gap-4">
            <Field
              label="Username or email"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="username"
              required
              autoFocus
            />
            <Field
              label="Password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
              required
            />
            <Button type="submit" loading={busy}>
              Sign in
            </Button>
            <p className="text-center text-sm text-ink-muted">
              No account?{" "}
              <Link to="/register" className="text-brand underline">
                Create one
              </Link>
            </p>
          </form>
        )}
      </Card>
    </main>
  );
}
