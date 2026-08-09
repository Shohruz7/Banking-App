/**
 * The profile, and turning MFA on or off.
 *
 * Enrolment returns the secret exactly once — it is envelope-encrypted at rest (ADR-0027) and this
 * response is the only moment it crosses the wire, because the user has to scan it. The `qr_svg`
 * field arrives as a `data:` URI, so it renders straight into an `<img>` with no QR library on this
 * side.
 *
 * Disabling revokes **every** session, including this one. So the UI signs itself out afterwards
 * rather than leaving a page whose next request will 401.
 */

import { useState, type FormEvent } from "react";

import { apiFetch, ApiError } from "../api/client";
import type { MfaEnrollment } from "../api/types";
import { useAuth } from "../auth/AuthProvider";
import { useToast } from "../components/Toaster";
import { Button, Card, ErrorNote, Field } from "../components/ui";
import { usePageTitle } from "../usePageTitle";

export default function Settings() {
  usePageTitle("Settings");
  const { user, signOut, refreshUser } = useAuth();
  const toast = useToast();

  const [enrollment, setEnrollment] = useState<MfaEnrollment | null>(null);
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function beginEnrollment() {
    setError(null);
    setBusy(true);
    try {
      setEnrollment(await apiFetch<MfaEnrollment>("/auth/mfa/enroll/", { method: "POST" }));
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not start enrolment.");
    } finally {
      setBusy(false);
    }
  }

  async function confirmEnrollment(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await apiFetch("/auth/mfa/confirm/", { method: "POST", body: { code } });
      setEnrollment(null);
      setCode("");
      await refreshUser();
      toast("Two-factor authentication is on.", "good");
    } catch (caught) {
      setError(
        caught instanceof ApiError && caught.code === "invalid_mfa_code"
          ? "That code is not right. Codes change every 30 seconds."
          : "Could not confirm the device.",
      );
    } finally {
      setBusy(false);
    }
  }

  async function disable(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await apiFetch("/auth/mfa/disable/", { method: "POST", body: { code } });
      toast("Two-factor authentication is off. Signing you out.", "info");
      // Every session was just revoked, this one included. Staying put would mean the next request
      // 401s and the refresh fails — a worse version of the same outcome.
      await signOut();
    } catch (caught) {
      setError(
        caught instanceof ApiError && caught.code === "invalid_mfa_code"
          ? "That code is not right."
          : "Could not disable two-factor authentication.",
      );
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <Card title="Profile">
        <dl className="grid gap-4 sm:grid-cols-3">
          <div>
            <dt className="text-xs uppercase tracking-wide text-ink-muted">Username</dt>
            <dd className="mt-1 font-medium">{user?.username}</dd>
          </div>
          <div>
            <dt className="text-xs uppercase tracking-wide text-ink-muted">Email</dt>
            <dd className="mt-1 font-medium">{user?.email}</dd>
          </div>
          <div>
            <dt className="text-xs uppercase tracking-wide text-ink-muted">Two-factor</dt>
            <dd className="mt-1 font-medium">{user?.mfa_enabled ? "On" : "Off"}</dd>
          </div>
        </dl>
      </Card>

      <Card title="Two-factor authentication">
        {error && <div className="mb-4"><ErrorNote>{error}</ErrorNote></div>}

        {user?.mfa_enabled ? (
          <form onSubmit={disable} className="flex max-w-sm flex-col gap-4">
            <p className="text-sm text-ink-muted">
              Enter a current code to turn it off. Every signed-in device will be signed out.
            </p>
            <Field
              label="Code"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              inputMode="numeric"
              maxLength={6}
              required
            />
            <Button type="submit" variant="danger" loading={busy}>
              Turn off
            </Button>
          </form>
        ) : enrollment ? (
          <form onSubmit={confirmEnrollment} className="flex max-w-sm flex-col gap-4">
            <p className="text-sm text-ink-muted">
              Scan this with your authenticator app, then enter the code it shows.
            </p>
            <img
              src={enrollment.qr_svg}
              alt="QR code for enrolling this account in your authenticator app"
              className="size-48 rounded-lg border border-border bg-white p-2"
            />
            <p className="text-xs text-ink-muted">
              Can't scan? Enter this key manually:{" "}
              <code className="rounded bg-surface-sunken px-1 py-0.5">{enrollment.secret}</code>
            </p>
            <Field
              label="Code"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              inputMode="numeric"
              maxLength={6}
              required
            />
            <Button type="submit" loading={busy}>
              Confirm
            </Button>
          </form>
        ) : (
          <div className="flex flex-col items-start gap-4">
            <p className="text-sm text-ink-muted">
              Require a six-digit code from your authenticator app when signing in.
            </p>
            <Button onClick={() => void beginEnrollment()} loading={busy}>
              Set up
            </Button>
          </div>
        )}
      </Card>
    </div>
  );
}
