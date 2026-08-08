/**
 * The wire shapes — aliases onto the generated schema, never hand-written (ADR-0031).
 *
 * `src/api/schema.d.ts` is produced by `npm run schema` from `openapi.json`, which
 * `manage.py spectacular` emits and a backend test asserts generates without warnings. Nothing in
 * this file describes the API; it only gives the generated types names worth importing. Rename a
 * serializer field on the backend and `tsc` fails here rather than a component rendering
 * `undefined` — and CI regenerates both files and fails on a diff, so the two cannot drift.
 *
 * The one thing layered on top is the money brand. The schema types money as `string`, which is
 * correct but not *load-bearing*: a plain string is assignable to anything a plain string is
 * assignable to, so nothing would stop `Number(account.balance)`. Re-declaring those fields as
 * `Money`/`Quantity` (which are strings at runtime and nothing else) is what makes ADR-0009 a
 * compile error instead of a code-review habit.
 */

import type { Money, Quantity } from "../money";
import type { components } from "./schema";

type Schemas = components["schemas"];

/** Replace the named keys of `T` with branded decimal strings. */
type Branded<T, K extends keyof T, B> = Omit<T, K> & { [P in K]: B };

// ---------------------------------------------------------------------------------------------
// Errors and pagination
// ---------------------------------------------------------------------------------------------

/** Every non-2xx response, from a 400 to an unrouted 404 to an unhandled 500 (ADR-0006). */
export type ErrorEnvelope = Schemas["ErrorEnvelope"];

/** Cursor pagination. `next`/`previous` are full URLs, or null at the end of the list. */
export interface Page<T> {
  next?: string | null;
  previous?: string | null;
  results: T[];
}

// ---------------------------------------------------------------------------------------------
// Identity
// ---------------------------------------------------------------------------------------------

export type User = Schemas["User"];
export type RegisterRequest = Schemas["Register"];
export type MfaEnrollment = Schemas["MfaEnrollResponse"];

export interface TokenPair {
  access: string;
  refresh: string;
}

export interface MfaChallenge {
  mfa_required: true;
  mfa_token: string;
}

/**
 * `POST /auth/token/` returns one of these, both with **status 200**.
 *
 * The 200 on the challenge branch is deliberate on the backend's side — the credentials *were*
 * correct, the flow simply is not finished. A 401 would make a generic interceptor bounce a correct
 * password back to the login screen. So the client branches on the body, never on the status.
 *
 * Spelled out rather than aliased because `TokenObtainPairWithMFA` describes the *request*; the
 * response union is documented in the serializer's docstring and has no schema of its own.
 */
export type LoginResponse = TokenPair | MfaChallenge;

export const isMfaChallenge = (body: LoginResponse): body is MfaChallenge =>
  "mfa_required" in body && body.mfa_required === true;

// ---------------------------------------------------------------------------------------------
// Accounts and the ledger
// ---------------------------------------------------------------------------------------------

export type AccountType = Schemas["AccountTypeEnum"];

/** Balance is derived, never stored; `number` is masked here and full on {@link AccountDetail}. */
export type Account = Branded<Schemas["Account"], "balance", Money>;

export type AccountDetail = Branded<Schemas["AccountDetail"], "balance", Money>;

/** `amount` is signed: negative leaving the account, positive arriving. */
export type JournalLine = Omit<Schemas["JournalLine"], "amount" | "quantity"> & {
  amount: Money;
  /** Null on a money line; the signed share count on a line touching a position account. */
  quantity: Quantity | null;
};

export type JournalEntry = Omit<Schemas["JournalEntry"], "lines"> & { lines: JournalLine[] };

export type TransferRequest = Schemas["TransferCreate"];

// ---------------------------------------------------------------------------------------------
// Markets and trading
// ---------------------------------------------------------------------------------------------

export type Instrument = Branded<Schemas["Instrument"], "last_price", Money>;
export type PriceTick = Branded<Schemas["PriceTick"], "price", Money>;

export type OrderSide = Schemas["SideEnum"];
export type OrderType = Schemas["OrderTypeEnum"];
export type OrderStatus = Schemas["StatusEnum"];

export type Order = Omit<
  Schemas["Order"],
  "quantity" | "filled_quantity" | "limit_price" | "filled_price"
> & {
  quantity: Quantity;
  filled_quantity: Quantity;
  limit_price: Money | null;
  filled_price: Money | null;
};

export type OrderRequest = Schemas["OrderCreate"];

export type Holding = Branded<
  Schemas["Holding"],
  "quantity" | "cost_basis" | "average_cost" | "last_price" | "market_value" | "unrealized_pnl",
  Money
> & { quantity: Quantity };

export type Portfolio = Branded<
  Omit<Schemas["Portfolio"], "positions">,
  "cash" | "holdings_value" | "cost_basis" | "unrealized_pnl" | "realized_pnl" | "total_value",
  Money
> & { positions: Holding[] };

// ---------------------------------------------------------------------------------------------
// Statements
// ---------------------------------------------------------------------------------------------

export type Statement = Schemas["Statement"];
