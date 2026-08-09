/**
 * The order ticket's contracts, which are the transfer form's contracts wearing a different hat.
 *
 * One idempotency key per attempt, for the same reason: the client cannot know whether a request
 * that timed out actually placed the order, so the retry has to be able to say "this one, again"
 * rather than "another one".
 *
 * And the status is not what says whether anything happened. **Both** outcomes are 201 — a market
 * order comes back `filled`, a limit order comes back `open` and waits. Reading the status instead
 * of the body would tell somebody their resting order had executed.
 */

import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { Order } from "../api/types";
import { setTokens } from "../auth/store";
import { asMoney, asQuantity } from "../money";
import { BASE, aUser, accountsPage, errorBody, http, json, server } from "../test/server";
import { renderWithProviders } from "../test/render";
import OrderTicket from "./OrderTicket";

const filledOrder: Order = {
  id: "44444444-4444-7444-8444-444444444444",
  symbol: "AAPL",
  side: "buy",
  order_type: "market",
  status: "filled",
  quantity: asQuantity("6.50000000"),
  filled_quantity: asQuantity("6.50000000"),
  filled_price: asMoney("153.8500"),
  limit_price: null,
  created_at: "2026-08-09T12:00:00Z",
  resolved_at: "2026-08-09T12:00:01Z",
  entry_id: "55555555-5555-7555-8555-555555555555",
  idempotency_key: null,
  reject_reason: "",
};

function arrange(handler: Parameters<typeof http.post>[1]) {
  setTokens({ access: "access-1", refresh: "refresh-1" });
  server.use(
    http.get(`${BASE}/accounts/`, () => json(accountsPage)),
    // Setting tokens makes `hasStoredSession()` true, so AuthProvider resumes the session on mount
    // and asks who it is talking to. Unstubbed, that request is what `onUnhandledRequest: "error"`
    // shouts about.
    http.get(`${BASE}/auth/me/`, () => json(aUser)),
    http.post(`${BASE}/orders/`, handler),
  );
}

async function placeMarketBuy(quantity = "6.5") {
  const user = userEvent.setup();
  // Behind a skeleton until the accounts query resolves — the branch that did not exist before
  // Week 9 and rendered an empty select instead.
  const account = await screen.findByLabelText("Cash account");
  await waitFor(() => expect(within(account).getAllByRole("option")).toHaveLength(2));

  await user.selectOptions(account, accountsPage.results[0]!.id);
  await user.type(screen.getByLabelText("Quantity"), quantity);
  await user.click(screen.getByRole("button", { name: /buy AAPL/i }));
  return user;
}

describe("OrderTicket", () => {
  it("reuses one idempotency key across a retry, and mints a new one for the next order", async () => {
    const keys: (string | undefined)[] = [];
    let attempt = 0;

    arrange(async ({ request }) => {
      const body = (await request.json()) as { idempotency_key?: string };
      keys.push(body.idempotency_key);
      attempt += 1;
      // First attempt fails in a way that says nothing about whether it posted — the exact case
      // the key exists for.
      return attempt === 1 ? json(errorBody("server_error", "Upstream."), 503) : json(filledOrder, 201);
    });

    renderWithProviders(<OrderTicket symbol="AAPL" />);
    const user = await placeMarketBuy();

    await screen.findByText(/upstream/i);
    await user.click(screen.getByRole("button", { name: /buy AAPL/i }));
    await screen.findByText(/filled/i);

    // Same attempt, same key: the server can recognise the retry rather than opening a second
    // position.
    expect(keys[0]).toBeDefined();
    expect(keys[1]).toBe(keys[0]);

    // A new order is a new attempt, and reusing a key for different details is a 409 by ADR-0024.
    await user.type(screen.getByLabelText("Quantity"), "1");
    await user.click(screen.getByRole("button", { name: /buy AAPL/i }));
    await waitFor(() => expect(keys).toHaveLength(3));
    expect(keys[2]).not.toBe(keys[0]);
  });

  it("formats the fill rather than echoing the wire strings", async () => {
    arrange(() => json(filledOrder, 201));

    renderWithProviders(<OrderTicket symbol="AAPL" />);
    await placeMarketBuy();

    // "6.5 AAPL at $153.85", not "6.50000000 AAPL at 153.8500". The wire carries full precision
    // because the ledger needs it; a person reading a confirmation does not.
    const confirmation = await screen.findByRole("status");
    expect(confirmation).toHaveTextContent("6.5");
    expect(confirmation).toHaveTextContent("$153.85");
    expect(confirmation).not.toHaveTextContent("6.50000000");
    expect(confirmation).not.toHaveTextContent("153.8500");
  });

  it("says a limit order is resting rather than done", async () => {
    // Both outcomes are 201. Only the body distinguishes them, and telling somebody their order
    // filled when it is sitting in the book is the worst available wording.
    arrange(() => json({ ...filledOrder, status: "open", order_type: "limit", filled_quantity: asQuantity("0.00000000"), filled_price: null, limit_price: asMoney("100.0000"), resolved_at: null }, 201));

    renderWithProviders(<OrderTicket symbol="AAPL" />);
    const user = userEvent.setup();

    const account = await screen.findByLabelText("Cash account");
    await waitFor(() => expect(within(account).getAllByRole("option")).toHaveLength(2));
    await user.selectOptions(account, accountsPage.results[0]!.id);
    await user.selectOptions(screen.getByLabelText("Type"), "limit");
    await user.type(screen.getByLabelText("Quantity"), "1");
    await user.type(screen.getByLabelText("Limit price"), "100.00");
    await user.click(screen.getByRole("button", { name: /buy AAPL/i }));

    expect(await screen.findByRole("status")).toHaveTextContent(/resting/i);
  });

  it("never sends a limit order without a limit price", async () => {
    let requests = 0;
    arrange(() => {
      requests += 1;
      return json(filledOrder, 201);
    });

    renderWithProviders(<OrderTicket symbol="AAPL" />);
    const user = userEvent.setup();

    const account = await screen.findByLabelText("Cash account");
    await waitFor(() => expect(within(account).getAllByRole("option")).toHaveLength(2));
    await user.selectOptions(account, accountsPage.results[0]!.id);
    await user.selectOptions(screen.getByLabelText("Type"), "limit");
    await user.type(screen.getByLabelText("Quantity"), "1");
    await user.click(screen.getByRole("button", { name: /buy AAPL/i }));

    // Two guards agree here, which is why the assertion is about the *request* rather than about
    // a particular message: the field is `required`, so the browser refuses to submit at all, and
    // behind that `onSubmit` checks again. The serializer is the third and only real one. What
    // matters is that a malformed order never leaves the tab.
    await waitFor(() => expect(requests).toBe(0));
  });

  it("shows an error rather than a dead form when the accounts request fails", async () => {
    setTokens({ access: "access-1", refresh: "refresh-1" });
    server.use(
      http.get(`${BASE}/accounts/`, () => json(errorBody("server_error", "Nope."), 500)),
      http.get(`${BASE}/auth/me/`, () => json(aUser)),
    );

    renderWithProviders(<OrderTicket symbol="AAPL" />);

    // Before Week 9 this rendered the full ticket with an empty select and no explanation.
    expect(await screen.findByRole("alert")).toHaveTextContent(/could not load your accounts/i);
    expect(screen.queryByRole("button", { name: /buy AAPL/i })).not.toBeInTheDocument();
  });
});
