/**
 * Money and share quantities, as strings, forever.
 *
 * The backend sends `"150.0000"` and `"6.50000000"` rather than numbers, and ADR-0009 explains why:
 * a double cannot hold every value a `NUMERIC(20,4)` can, so `Number("0.1") + Number("0.2")` is the
 * cheapest possible way to lose the guarantee the entire ledger is built on. The risk on this side
 * of the wire is that JavaScript will coerce silently — `+balance`, `parseFloat`, an accidental
 * arithmetic operator — and nothing will look wrong until a cent goes missing.
 *
 * So the two wire types are branded. A `Money` is not assignable to `number`, and the only
 * functions that turn one into a number live in this file: `toChartNumber`, which is allowed
 * because a pixel coordinate genuinely is a float, and the comparison helpers, which need
 * magnitude and not exactness.
 *
 * Arithmetic is deliberately absent. The client never computes a balance, a total or a P&L —
 * every one of those is derived by the server from the ledger, and recomputing any of them here
 * would be the "second source of truth" that ADR-0020 refuses.
 */

declare const moneyBrand: unique symbol;
declare const quantityBrand: unique symbol;

/** A decimal string at the money quantum, e.g. `"150.0000"`. Never a number. */
export type Money = string & { readonly [moneyBrand]: true };

/** A decimal string at the share quantum, e.g. `"6.50000000"`. Never a number. */
export type Quantity = string & { readonly [quantityBrand]: true };

/** Assert that a string off the wire is money. The API's contract, made explicit at the boundary. */
export const asMoney = (value: string): Money => value as Money;

export const asQuantity = (value: string): Quantity => value as Quantity;

export const ZERO_MONEY = asMoney("0.0000");

/**
 * Split a decimal string into sign, integer digits and fraction digits.
 *
 * Hand-rolled rather than via `Number`: this is the one place the exact digits matter, and parsing
 * is the operation that would throw them away.
 */
function parts(value: string): { negative: boolean; integer: string; fraction: string } {
  const negative = value.startsWith("-");
  const unsigned = negative ? value.slice(1) : value;
  const [integer = "0", fraction = ""] = unsigned.split(".");
  return { negative, integer, fraction };
}

/**
 * `"1234.5678"` → `"$1,234.57"`.
 *
 * Rounds half-up on the *string*, so the displayed cents match what a person would write down. The
 * underlying value is untouched; this output is for eyes only and never goes back over the wire.
 *
 * **No `Intl.NumberFormat`, and that is not a preference.** `Intl` formats a `number`, so handing
 * it one means converting first — and above 2^53 the conversion is lossy, which is how the first
 * version of this function turned `"9007199254740993.00"` into `…992.00`. Its own test caught it.
 * The arithmetic stays in `BigInt` and the grouping is done on the digit string, so the output is
 * exact at any magnitude a `NUMERIC(20,4)` can hold. USD only, per ADR-0007.
 */
export function formatMoney(value: Money | string): string {
  const { negative, integer, fraction } = parts(value);
  const cents = fraction.padEnd(3, "0");
  let whole = BigInt(integer || "0") * 100n + BigInt(cents.slice(0, 2) || "0");
  if (Number(cents[2] ?? "0") >= 5) whole += 1n;

  const dollars = (whole / 100n).toString();
  const remainder = (whole % 100n).toString().padStart(2, "0");
  const grouped = dollars.replace(/\B(?=(\d{3})+(?!\d))/g, ",");

  return `${negative ? "-" : ""}$${grouped}.${remainder}`;
}

/**
 * `"6.50000000"` → `"6.5"`; `"100.00000000"` → `"100"`.
 *
 * Trailing zeros are stripped for display because a holding of six and a half shares should not
 * read like a precision claim. The value keeps all eight places.
 */
export function formatQuantity(value: Quantity | string): string {
  if (!value.includes(".")) return value;
  return value.replace(/\.?0+$/, "") || "0";
}

/** Signed comparison against zero, without going through a float. */
export function signOf(value: Money | Quantity | string): -1 | 0 | 1 {
  const { negative, integer, fraction } = parts(value);
  const isZero = /^0*$/.test(integer) && /^0*$/.test(fraction);
  if (isZero) return 0;
  return negative ? -1 : 1;
}

export const isZero = (value: Money | Quantity | string): boolean => signOf(value) === 0;

export const isNegative = (value: Money | Quantity | string): boolean => signOf(value) === -1;

/**
 * Compare two decimal strings. Digit-wise, so `"10.0000"` beats `"9.9999"` — which a lexicographic
 * string compare gets wrong, and which is the bug this exists to make impossible.
 */
export function compareDecimal(a: string, b: string): number {
  const sa = signOf(a);
  const sb = signOf(b);
  if (sa !== sb) return sa - sb;

  const pa = parts(a);
  const pb = parts(b);
  const width = Math.max(pa.integer.length, pb.integer.length);
  const fractionWidth = Math.max(pa.fraction.length, pb.fraction.length);

  const left = pa.integer.padStart(width, "0") + pa.fraction.padEnd(fractionWidth, "0");
  const right = pb.integer.padStart(width, "0") + pb.fraction.padEnd(fractionWidth, "0");

  const magnitude = left === right ? 0 : left < right ? -1 : 1;
  return pa.negative ? -magnitude : magnitude;
}

/**
 * The one sanctioned lossy conversion: a y-value for a chart.
 *
 * A pixel is a float and a price line has no cents to lose, so this is safe *here* and nowhere
 * else. Kept in this module rather than inlined at the chart so `Number(` on a `Money` stays
 * greppable — and so a reviewer looking for "who turns money into a float" finds one answer.
 */
export const toChartNumber = (value: Money | string): number => Number(value);
