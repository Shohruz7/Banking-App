/**
 * The bridge between the socket and the query cache — the mechanism ADR-0032 is actually about.
 *
 * `socket.test.ts` covers the protocol: what frames go out and when. This covers what happens to
 * the cache when a frame comes *in*, which is a different contract and the one that decides whether
 * a screen is correct after a fill lands.
 *
 * The rule being enforced is "events invalidate, they never write". The tempting alternative —
 * splice `balance.updated`'s `balance` field straight into the accounts list — is one fewer request
 * and a second copy of the ledger's arithmetic living in the client. The negative assertions below
 * are the interesting half: `order.rejected` must not invalidate anything money-shaped, because a
 * rejected order rolled back and there is nothing new to fetch.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, act } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { keys } from "../api/hooks";
import { Toaster } from "../components/Toaster";
import { StreamProvider } from "./useStream";

// The provider only needs a user and a `signOut`; going through the real AuthProvider would mean
// logging in over MSW to test something that has nothing to do with authentication.
const signOut = vi.fn();
vi.mock("../auth/AuthProvider", () => ({
  useAuth: () => ({ user: { id: 1, username: "alice" }, signOut }),
}));

vi.mock("../auth/store", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../auth/store")>()),
  getAccessToken: () => "access-1",
}));

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  static readonly OPEN = 1;

  readyState = FakeWebSocket.OPEN;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;

  constructor(readonly url: string) {
    FakeWebSocket.instances.push(this);
  }
  send(payload: string) {
    this.sent.push(payload);
  }
  close() {
    this.onclose?.({ code: 1000 });
  }
  deliver(message: object) {
    act(() => this.onmessage?.({ data: JSON.stringify(message) }));
  }
}

let queryClient: QueryClient;
/** Loosely typed on purpose: the real signature is generic over the key, and reproducing it here
 *  buys nothing — every assertion below reads `queryKey` off the recorded argument. */
let invalidate: ReturnType<typeof vi.fn>;

/** The key arrays passed to every `invalidateQueries` call so far. */
const invalidatedKeys = (): unknown[][] =>
  invalidate.mock.calls.map(([arg]) => (arg as { queryKey: unknown[] }).queryKey);

const invalidated = (key: readonly unknown[]): boolean =>
  invalidatedKeys().some((actual) => JSON.stringify(actual) === JSON.stringify(key));

function mount() {
  render(
    <QueryClientProvider client={queryClient}>
      <Toaster>
        <StreamProvider>
          <p>shell</p>
        </StreamProvider>
      </Toaster>
    </QueryClientProvider>,
  );
  const socket = FakeWebSocket.instances.at(-1)!;
  act(() => socket.onopen?.());
  return socket;
}

beforeEach(() => {
  FakeWebSocket.instances = [];
  vi.stubGlobal("WebSocket", FakeWebSocket);
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  invalidate = vi.fn();
  queryClient.invalidateQueries = invalidate as unknown as QueryClient["invalidateQueries"];
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("StreamProvider", () => {
  it("invalidates the account, its transactions and the portfolio on a balance change", () => {
    const socket = mount();
    socket.deliver({ type: "balance.updated", account_id: "acc-1", balance: "150.0000" });

    expect(invalidated(keys.accounts)).toBe(true);
    expect(invalidated(keys.account("acc-1"))).toBe(true);
    expect(invalidated(keys.transactions("acc-1"))).toBe(true);
    expect(invalidated(keys.portfolio)).toBe(true);
  });

  it("does not write the balance it was handed into the cache", () => {
    // The event carries one account's balance while the list row it would patch belongs to a page
    // fetched as a whole. Splicing them together is the client maintaining its own copy of the
    // ledger's arithmetic, which is the thing ADR-0032 refuses.
    const socket = mount();
    queryClient.setQueryData(keys.accounts, { results: [{ id: "acc-1", balance: "0.0000" }] });

    socket.deliver({ type: "balance.updated", account_id: "acc-1", balance: "999.0000" });

    expect(queryClient.getQueryData(keys.accounts)).toEqual({
      results: [{ id: "acc-1", balance: "0.0000" }],
    });
  });

  it("refetches orders, holdings, portfolio and accounts when an order fills", () => {
    const socket = mount();
    socket.deliver({ type: "order.filled", order_id: "ord-1" });

    expect(invalidated(keys.orders)).toBe(true);
    expect(invalidated(keys.holdings)).toBe(true);
    expect(invalidated(keys.portfolio)).toBe(true);
    expect(invalidated(keys.accounts)).toBe(true);
  });

  it("refetches nothing money-shaped when an order is rejected", () => {
    // The observable half of ADR-0023: the posting rolled back, so no balance changed and there is
    // nothing to refetch. If this ever starts invalidating accounts, the events are claiming a
    // movement that never happened.
    const socket = mount();
    socket.deliver({ type: "order.rejected", order_id: "ord-1", reason: "insufficient_funds" });

    expect(invalidated(keys.orders)).toBe(true);
    expect(invalidated(keys.accounts)).toBe(false);
    expect(invalidated(keys.portfolio)).toBe(false);
    expect(invalidated(keys.holdings)).toBe(false);
  });

  it("keeps price ticks out of the cache entirely", () => {
    // 55 symbols ticking once a minute would invalidate constantly, and no query owns a live price.
    const socket = mount();
    socket.deliver({ type: "price.tick", symbol: "AAPL", price: "153.8500", at: "2026-08-09T00:00:00Z" });

    expect(invalidate).not.toHaveBeenCalled();
  });

  it("closes the socket when the provider unmounts", () => {
    const { unmount } = render(
      <QueryClientProvider client={queryClient}>
        <Toaster>
          <StreamProvider>
            <p>shell</p>
          </StreamProvider>
        </Toaster>
      </QueryClientProvider>,
    );
    const socket = FakeWebSocket.instances.at(-1)!;
    const closed = vi.spyOn(socket, "close");

    unmount();

    // A leaked socket keeps re-authenticating for a user who is no longer on the page.
    expect(closed).toHaveBeenCalled();
  });
});
