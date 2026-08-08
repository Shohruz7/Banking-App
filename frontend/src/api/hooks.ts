/**
 * One typed hook per endpoint. No component calls `apiFetch` directly.
 *
 * The query keys are the contract the socket layer writes against: `realtime/useStream.ts`
 * invalidates by key when an event arrives, so the keys live here, next to the queries that own
 * them, rather than as strings scattered across components.
 */

import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
} from "@tanstack/react-query";

import { apiFetch, apiFetchWithStatus, fetchCursor } from "./client";
import type {
  Account,
  AccountDetail,
  Holding,
  Instrument,
  JournalEntry,
  JournalLine,
  Order,
  OrderRequest,
  Page,
  Portfolio,
  PriceTick,
  Statement,
  TransferRequest,
} from "./types";

export const keys = {
  accounts: ["accounts"] as const,
  account: (id: string) => ["accounts", id] as const,
  transactions: (id: string) => ["accounts", id, "transactions"] as const,
  instruments: (search: string) => ["instruments", search] as const,
  instrument: (symbol: string) => ["instruments", symbol] as const,
  prices: (symbol: string) => ["instruments", symbol, "prices"] as const,
  orders: ["orders"] as const,
  holdings: ["holdings"] as const,
  portfolio: ["portfolio"] as const,
  statements: ["statements"] as const,
};

// ---------------------------------------------------------------------------------------------
// Accounts and history
// ---------------------------------------------------------------------------------------------

export const useAccounts = () =>
  useQuery({
    queryKey: keys.accounts,
    queryFn: () => apiFetch<Page<Account>>("/accounts/"),
  });

export const useAccount = (id: string) =>
  useQuery({
    queryKey: keys.account(id),
    queryFn: () => apiFetch<AccountDetail>(`/accounts/${id}/`),
  });

/**
 * An account's history, page by page.
 *
 * Cursor pagination, so the pages are stable as new entries arrive — a new posting does not shift
 * rows into or out of a page you have already read, the way an offset would. What it *does* do is
 * make page one stale, which is why the socket invalidates this key rather than prepending to it.
 */
export const useTransactions = (id: string) =>
  useInfiniteQuery({
    queryKey: keys.transactions(id),
    queryFn: ({ pageParam }) =>
      pageParam
        ? fetchCursor<Page<JournalLine>>(pageParam)
        : apiFetch<Page<JournalLine>>(`/accounts/${id}/transactions/`),
    initialPageParam: "",
    getNextPageParam: (last: Page<JournalLine>) => last.next ?? undefined,
  });

// ---------------------------------------------------------------------------------------------
// Transfers
// ---------------------------------------------------------------------------------------------

export interface TransferOutcome {
  entry: JournalEntry;
  /** False when an idempotency key replayed an entry that had already posted (HTTP 200). */
  posted: boolean;
}

/**
 * Post a transfer.
 *
 * The status is load-bearing and is why this uses `apiFetchWithStatus`: **201** means this request
 * moved the money, **200** means a previous one already did and this key replayed it. Telling the
 * user "sent" both times is how a double-click becomes a support ticket about a missing second
 * transfer that never existed.
 */
export function useTransfer(): UseMutationResult<TransferOutcome, Error, TransferRequest> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: TransferRequest) => {
      const { status, data } = await apiFetchWithStatus<JournalEntry>("/transfers/", {
        method: "POST",
        body,
      });
      return { entry: data, posted: status === 201 };
    },
    onSuccess: (_result, body) => {
      void queryClient.invalidateQueries({ queryKey: keys.accounts });
      void queryClient.invalidateQueries({ queryKey: keys.portfolio });
      void queryClient.invalidateQueries({ queryKey: keys.transactions(body.source_account) });
      void queryClient.invalidateQueries({ queryKey: keys.transactions(body.destination_account) });
    },
  });
}

// ---------------------------------------------------------------------------------------------
// Markets
// ---------------------------------------------------------------------------------------------

export const useInstruments = (search: string) =>
  useQuery({
    queryKey: keys.instruments(search),
    queryFn: () =>
      apiFetch<Page<Instrument>>(`/instruments/${search ? `?q=${encodeURIComponent(search)}` : ""}`),
  });

export const useInstrument = (symbol: string) =>
  useQuery({
    queryKey: keys.instrument(symbol),
    queryFn: () => apiFetch<Instrument>(`/instruments/${symbol}/`),
  });

/**
 * A symbol's price history.
 *
 * The API returns newest-first (the index it is reading); a chart wants oldest-first, and the
 * reversal happens here so no component has to remember.
 */
export const usePrices = (symbol: string) =>
  useQuery({
    queryKey: keys.prices(symbol),
    queryFn: async () => {
      const page = await apiFetch<Page<PriceTick>>(`/instruments/${symbol}/prices/`);
      return [...page.results].reverse();
    },
  });

// ---------------------------------------------------------------------------------------------
// Trading
// ---------------------------------------------------------------------------------------------

export const useOrders = () =>
  useInfiniteQuery({
    queryKey: keys.orders,
    queryFn: ({ pageParam }) =>
      pageParam ? fetchCursor<Page<Order>>(pageParam) : apiFetch<Page<Order>>("/orders/"),
    initialPageParam: "",
    getNextPageParam: (last: Page<Order>) => last.next ?? undefined,
  });

export function usePlaceOrder(): UseMutationResult<Order, Error, OrderRequest> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: OrderRequest) => apiFetch<Order>("/orders/", { method: "POST", body }),
    onSuccess: () => {
      // A market order fills inside the request, so cash, holdings and the portfolio have all
      // already changed by the time this resolves. A limit order changes none of them yet — but
      // invalidating four keys costs less than reasoning about which case this was.
      void queryClient.invalidateQueries({ queryKey: keys.orders });
      void queryClient.invalidateQueries({ queryKey: keys.holdings });
      void queryClient.invalidateQueries({ queryKey: keys.portfolio });
      void queryClient.invalidateQueries({ queryKey: keys.accounts });
    },
  });
}

export function useCancelOrder(): UseMutationResult<Order, Error, string> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => apiFetch<Order>(`/orders/${id}/cancel/`, { method: "POST" }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: keys.orders }),
  });
}

export const useHoldings = () =>
  useQuery({
    queryKey: keys.holdings,
    queryFn: () => apiFetch<{ results: Holding[] }>("/holdings/"),
  });

export const usePortfolio = () =>
  useQuery({
    queryKey: keys.portfolio,
    queryFn: () => apiFetch<Portfolio>("/portfolio/"),
  });

// ---------------------------------------------------------------------------------------------
// Statements
// ---------------------------------------------------------------------------------------------

export const useStatements = () =>
  useQuery({
    queryKey: keys.statements,
    queryFn: () => apiFetch<Page<Statement>>("/statements/"),
  });
