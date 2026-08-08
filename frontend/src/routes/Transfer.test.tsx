/**
 * The transfer form's two contracts with the backend: one idempotency key per attempt, and 200
 * meaning "already sent" rather than "sent".
 */

import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { setTokens } from "../auth/store";
import { BASE, accountsPage, anEntry, errorBody, http, json, server } from "../test/server";
import { renderWithProviders } from "../test/render";
import Transfer from "./Transfer";

const secondAccount = {
  ...accountsPage.results[0]!,
  id: "33333333-3333-7333-8333-333333333333",
  name: "Savings",
};

const twoAccounts = { ...accountsPage, results: [accountsPage.results[0]!, secondAccount] };

function arrange(handler: Parameters<typeof http.post>[1]) {
  setTokens({ access: "access-1", refresh: "refresh-1" });
  server.use(
    http.get(`${BASE}/accounts/`, () => json(twoAccounts)),
    http.get(`${BASE}/auth/me/`, () => json({})),
    http.post(`${BASE}/transfers/`, handler),
  );
}

async function fillAndSend(amount = "25.00") {
  const user = userEvent.setup();

  // The form is behind a skeleton until the accounts query resolves, hence `find` not `get`.
  // Scoped to the "From" select once it exists: both dropdowns list Checking, so a bare
  // `findByRole("option")` matches twice and throws.
  const from = await screen.findByLabelText("From");
  await waitFor(() => expect(within(from).getAllByRole("option")).toHaveLength(3));

  await user.selectOptions(from, accountsPage.results[0]!.id);
  await user.selectOptions(screen.getByLabelText("To"), secondAccount.id);
  await user.type(screen.getByLabelText("Amount"), amount);
  await user.click(screen.getByRole("button", { name: "Send" }));
  return user;
}

describe("Transfer", () => {
  it("reuses one idempotency key across a retried attempt, and mints a new one for the next", async () => {
    const keys: (string | undefined)[] = [];
    let failFirst = true;

    arrange(async ({ request }) => {
      const body = (await request.json()) as { idempotency_key?: string };
      keys.push(body.idempotency_key);
      if (failFirst) {
        failFirst = false;
        return json(errorBody("service_unavailable", "Try again."), 503);
      }
      return json(anEntry, 201);
    });

    renderWithProviders(<Transfer />);
    const user = await fillAndSend();

    await screen.findByRole("alert");

    // Retry the same attempt: the key must not change, or a request that actually posted before
    // timing out would post a second time.
    await user.click(screen.getByRole("button", { name: "Send" }));
    await screen.findByText("Transfer sent.");

    expect(keys).toHaveLength(2);
    expect(keys[0]).toBe(keys[1]);

    // A new transfer is a new attempt, and reusing the old key would be a 409 by design.
    await user.type(screen.getByLabelText("Amount"), "10.00");
    await user.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(keys).toHaveLength(3));
    expect(keys[2]).not.toBe(keys[0]);
  });

  it("says 'already sent' on a 200 replay rather than reporting a second transfer", async () => {
    arrange(() => json(anEntry, 200));

    renderWithProviders(<Transfer />);
    await fillAndSend();

    expect(await screen.findByText(/Already sent/)).toBeInTheDocument();
    expect(screen.queryByText("Transfer sent.")).not.toBeInTheDocument();
  });

  it("puts insufficient funds against the amount field", async () => {
    arrange(() => json(errorBody("insufficient_funds", "Not enough funds."), 400));

    renderWithProviders(<Transfer />);
    await fillAndSend("999999.00");

    expect(await screen.findByText("There isn't enough in that account.")).toBeInTheDocument();
  });

  it("discards the key after a 409, because retrying with it can never succeed", async () => {
    const keys: (string | undefined)[] = [];
    let conflict = true;

    arrange(async ({ request }) => {
      const body = (await request.json()) as { idempotency_key?: string };
      keys.push(body.idempotency_key);
      if (conflict) {
        conflict = false;
        return json(errorBody("idempotency_key_conflict", "Key already used."), 409);
      }
      return json(anEntry, 201);
    });

    renderWithProviders(<Transfer />);
    const user = await fillAndSend();

    await screen.findByText(/conflicted with an earlier one/);

    await user.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(keys).toHaveLength(2));
    expect(keys[1]).not.toBe(keys[0]);
  });
});
