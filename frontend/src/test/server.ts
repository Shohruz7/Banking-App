/**
 * The mock API.
 *
 * Handlers return values typed as the real wire types, which come from the generated schema — so a
 * fixture that drifts from the backend fails `tsc` rather than passing a green test against a shape
 * the server never sends. That is the whole reason the types are generated rather than written.
 */

import { setupServer } from "msw/node";
import { http, HttpResponse } from "msw";

import type { Account, JournalEntry, Page, User } from "../api/types";
import { asMoney } from "../money";

export const server = setupServer();

export const BASE = "/api/v1";

export const aUser: User = {
  id: 1,
  username: "alice",
  email: "alice@example.com",
  mfa_enabled: false,
  date_joined: "2026-08-01T00:00:00Z",
};

export const anAccount: Account = {
  id: "11111111-1111-7111-8111-111111111111",
  name: "Checking",
  number: "••••6789",
  account_type: "asset",
  currency: "USD",
  balance: asMoney("1000.0000"),
  created_at: "2026-08-01T00:00:00Z",
};

export const accountsPage: Page<Account> = { next: null, previous: null, results: [anAccount] };

export const anEntry: JournalEntry = {
  id: "22222222-2222-7222-8222-222222222222",
  description: "Transfer",
  idempotency_key: null,
  created_at: "2026-08-08T12:00:00Z",
  lines: [],
};

/** The single error envelope every endpoint uses (ADR-0006). */
export const errorBody = (code: string, message: string, details: Record<string, unknown> = {}) => ({
  error: { code, message, details },
});

type JsonBody = Parameters<typeof HttpResponse.json>[0];

export const json = (body: JsonBody, status = 200) => HttpResponse.json(body, { status });

export { http };
