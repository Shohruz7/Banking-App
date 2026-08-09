/**
 * Create an account.
 *
 * Registration is not a login — the endpoint returns the new user and no tokens, deliberately. So
 * this signs in afterwards with the credentials just submitted, which is also the first proof that
 * they work.
 *
 * As of Week 8 the new user lands with a funded Checking and an empty Savings, opened in the same
 * transaction that created them (`ledger/onboarding.py`). Before that this screen led straight to
 * an empty dashboard with nothing to do on it.
 */

import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";

import { apiFetch, ApiError } from "../api/client";
import type { User } from "../api/types";
import { useAuth } from "../auth/AuthProvider";
import { Button, Card, ErrorNote, Field } from "../components/ui";
import { usePageTitle } from "../usePageTitle";

export default function Register() {
  usePageTitle("Create an account");
  const { signIn } = useAuth();
  const navigate = useNavigate();

  const [form, setForm] = useState({ username: "", email: "", password: "" });
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const update = (key: keyof typeof form) => (event: { target: { value: string } }) =>
    setForm((current) => ({ ...current, [key]: event.target.value }));

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setFieldErrors({});
    setBusy(true);

    try {
      await apiFetch<User>("/auth/register/", { method: "POST", body: form, anonymous: true });
      await signIn(form.username, form.password);
      navigate("/", { replace: true });
    } catch (caught) {
      if (caught instanceof ApiError && caught.code === "validation_error") {
        // Django's password validators return a list of messages per field; the envelope keeps them
        // as `details`, so they can be shown against the field that caused them.
        const errors: Record<string, string> = {};
        for (const field of ["username", "email", "password"]) {
          const message = caught.fieldError(field);
          if (message) errors[field] = message;
        }
        setFieldErrors(errors);
      } else {
        setError(
          caught instanceof ApiError ? caught.message : "Could not create the account just now.",
        );
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="mx-auto flex min-h-dvh max-w-md items-center px-4">
      <Card className="w-full" title="Create an account">
        {error && <div className="mb-4"><ErrorNote>{error}</ErrorNote></div>}

        <form onSubmit={onSubmit} className="flex flex-col gap-4">
          <Field
            label="Username"
            value={form.username}
            onChange={update("username")}
            autoComplete="username"
            error={fieldErrors["username"]}
            required
            autoFocus
          />
          <Field
            label="Email"
            type="email"
            value={form.email}
            onChange={update("email")}
            autoComplete="email"
            error={fieldErrors["email"]}
            required
          />
          <Field
            label="Password"
            type="password"
            value={form.password}
            onChange={update("password")}
            autoComplete="new-password"
            error={fieldErrors["password"]}
            required
          />
          <Button type="submit" loading={busy}>
            Create account
          </Button>
          <p className="text-center text-sm text-ink-muted">
            Already registered?{" "}
            <Link to="/login" className="text-brand underline">
              Sign in
            </Link>
          </p>
        </form>
      </Card>
    </main>
  );
}
