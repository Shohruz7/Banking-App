/**
 * Move money.
 *
 * **The idempotency key is the interesting part of this screen.** It is generated once per
 * *attempt* and held in a ref, so:
 *
 * - the user double-clicking Send sends the same key twice, and the second request replays the
 *   first rather than posting a second transfer;
 * - a retry after a network timeout carries the same key, which is the case the whole mechanism
 *   exists for — the client cannot know whether the request that timed out actually posted;
 * - starting a *new* transfer mints a new key, because reusing one for different details is a
 *   409 (ADR-0024), and correctly so.
 *
 * The other half is reading the status. **201** means this request moved the money; **200** means
 * one already had and this replayed it. Saying "Sent" to both is how a double-click turns into a
 * support ticket about a second transfer that never existed.
 */

import { useRef, useState, type FormEvent } from "react";

import { ApiError } from "../api/client";
import { useAccounts, useTransfer } from "../api/hooks";
import { Amount } from "../components/Amount";
import { Button, Card, Empty, ErrorNote, Field, Select, Skeleton } from "../components/ui";
import { usePageTitle } from "../usePageTitle";

export default function Transfer() {
  usePageTitle("Transfer");
  const accounts = useAccounts();
  const transfer = useTransfer();

  const [source, setSource] = useState("");
  const [destination, setDestination] = useState("");
  const [amount, setAmount] = useState("");
  const [description, setDescription] = useState("");
  const [notice, setNotice] = useState<{ tone: "good" | "info"; text: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});

  /** The key for the attempt in progress. Null between attempts. */
  const attemptKey = useRef<string | null>(null);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setFieldErrors({});
    setNotice(null);

    // Minted here, not in the mutation: a React Query retry re-runs the mutation function, and a
    // key generated inside it would be a *different* key on the retry — which is precisely the
    // double-post this is meant to prevent.
    attemptKey.current ??= crypto.randomUUID();

    try {
      const result = await transfer.mutateAsync({
        source_account: source,
        destination_account: destination,
        amount,
        description: description || "Transfer",
        idempotency_key: attemptKey.current,
      });

      setNotice(
        result.posted
          ? { tone: "good", text: "Transfer sent." }
          : { tone: "info", text: "Already sent — this transfer had posted, so nothing changed." },
      );

      // The attempt is over either way, so the next transfer gets a fresh key.
      attemptKey.current = null;
      setAmount("");
      setDescription("");
    } catch (caught) {
      if (caught instanceof ApiError) {
        if (caught.code === "validation_error") {
          const errors: Record<string, string> = {};
          for (const field of ["amount", "source_account", "destination_account"]) {
            const message = caught.fieldError(field);
            if (message) errors[field] = message;
          }
          setFieldErrors(errors);
        } else if (caught.code === "insufficient_funds") {
          setFieldErrors({ amount: "There isn't enough in that account." });
        } else if (caught.code === "idempotency_key_conflict") {
          // The key was bound to a *different* request. Retrying with it can never succeed.
          attemptKey.current = null;
          setError("That transfer conflicted with an earlier one. Please try again.");
        } else {
          setError(caught.message);
        }
      } else {
        setError("Could not reach the server. Your transfer was not sent.");
      }
    }
  }

  const options = accounts.data?.results ?? [];

  return (
    <Card title="Transfer">
      {accounts.isPending ? (
        <Skeleton className="h-48" />
      ) : accounts.isError ? (
        // Without this branch the form renders anyway, with two dropdowns holding nothing but
        // "Choose an account" and a Send button disabled forever — a screen that looks functional,
        // says nothing, and cannot work. On the one screen that moves money, say what happened.
        <ErrorNote>Could not load your accounts, so a transfer cannot be started.</ErrorNote>
      ) : options.length < 2 ? (
        // The "To" list filters out whatever is selected as the source, so a single-account holder
        // sees an empty destination dropdown and no reason for it.
        <Empty
          title="A transfer needs two accounts"
          hint={
            options.length === 1
              ? "You have one account, so there is nowhere to move money to yet."
              : "No accounts were found on this profile."
          }
        />
      ) : (
        <form onSubmit={onSubmit} className="flex max-w-lg flex-col gap-4">
          {error && <ErrorNote>{error}</ErrorNote>}
          {notice && (
            <p
              role="status"
              className={
                notice.tone === "good"
                  ? "rounded-lg bg-credit/10 px-3 py-2 text-sm text-credit"
                  : "rounded-lg bg-surface-sunken px-3 py-2 text-sm text-ink-muted"
              }
            >
              {notice.text}
            </p>
          )}

          <Select
            label="From"
            value={source}
            onChange={(e) => setSource(e.target.value)}
            error={fieldErrors["source_account"]}
            required
          >
            <option value="">Choose an account</option>
            {options.map((account) => (
              <option key={account.id} value={account.id}>
                {account.name} — {account.number}
              </option>
            ))}
          </Select>

          <Select
            label="To"
            value={destination}
            onChange={(e) => setDestination(e.target.value)}
            error={fieldErrors["destination_account"]}
            required
          >
            <option value="">Choose an account</option>
            {options
              .filter((account) => account.id !== source)
              .map((account) => (
                <option key={account.id} value={account.id}>
                  {account.name} — {account.number}
                </option>
              ))}
          </Select>

          <Field
            label="Amount"
            value={amount}
            onChange={(e) => setAmount(e.target.value)}
            inputMode="decimal"
            placeholder="0.00"
            error={fieldErrors["amount"]}
            required
          />

          <Field
            label="Description"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="Transfer"
            maxLength={255}
          />

          {source && (
            <p className="text-sm text-ink-muted">
              Available:{" "}
              <Amount value={options.find((a) => a.id === source)?.balance ?? "0.0000"} />
            </p>
          )}

          <Button type="submit" loading={transfer.isPending} disabled={!source || !destination}>
            Send
          </Button>
        </form>
      )}
    </Card>
  );
}
