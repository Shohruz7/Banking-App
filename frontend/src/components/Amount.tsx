/**
 * Rendering money.
 *
 * Two rules, both deliberate:
 *
 * **The sign is always shown**, not just the colour. Green-for-credit and red-for-debit is the
 * convention, and roughly one man in twelve cannot rely on it. A leading `+`/`−` costs one
 * character and makes the colour decoration rather than information.
 *
 * **The value is never a number.** The prop is `Money`, formatting goes through `formatMoney`, and
 * nothing here can coerce. See `src/money.ts`.
 */

import { formatMoney, signOf, type Money } from "../money";
import { cx } from "./ui";

export function Amount({
  value,
  signed = false,
  className,
}: {
  value: Money | string;
  /** Show an explicit +/− and colour by direction. Off for balances, on for movements. */
  signed?: boolean;
  // `| undefined` explicitly, because `exactOptionalPropertyTypes` distinguishes "absent" from
  // "present and undefined", and forwarding an optional prop hands over the latter.
  className?: string | undefined;
}) {
  const sign = signOf(value);
  const formatted = formatMoney(value);

  if (!signed) {
    return <span className={cx("tabular", sign < 0 && "text-debit", className)}>{formatted}</span>;
  }

  // formatMoney already emits a leading "-" for negatives; strip it so the marker below is the
  // only sign, and a screen reader does not read "minus minus".
  const magnitude = formatted.replace(/^-/, "");
  const marker = sign < 0 ? "−" : "+";

  return (
    <span className={cx("tabular", sign < 0 ? "text-debit" : "text-credit", className)}>
      {sign === 0 ? formatted : `${marker}${magnitude}`}
    </span>
  );
}

/** A profit-and-loss figure: same rules, but zero is neutral rather than a credit. */
export function PnL({
  value,
  className,
}: {
  value: Money | string;
  className?: string | undefined;
}) {
  if (signOf(value) === 0) {
    return <span className={cx("tabular text-ink-muted", className)}>{formatMoney(value)}</span>;
  }
  return <Amount value={value} signed className={className} />;
}
