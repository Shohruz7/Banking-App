/**
 * Place an order.
 *
 * Two rules are mirrored from `OrderCreateSerializer.validate` so the common mistake is a local
 * message rather than a round trip: a limit order needs a limit price, and a market order must not
 * carry one. The server still enforces both — this is a courtesy, not the guarantee.
 *
 * The status is not what says whether money moved. **Both** outcomes are 201: a market order comes
 * back `filled`, a limit order comes back `open` and waits for the market to cross it. The `status`
 * field is the answer, and the confirmation here says which happened.
 *
 * Same idempotency-key discipline as the transfer form — one key per attempt, held in a ref.
 */

import { useRef, useState, type FormEvent } from "react";

import { ApiError } from "../api/client";
import { useAccounts, usePlaceOrder } from "../api/hooks";
import type { OrderSide, OrderType } from "../api/types";
import { Amount } from "../components/Amount";
import { Button, Card, ErrorNote, Field, Select, Skeleton } from "../components/ui";
import type { Money } from "../money";

export default function OrderTicket({
  symbol,
  lastPrice,
}: {
  symbol: string;
  lastPrice?: Money | undefined;
}) {
  const accounts = useAccounts();
  const placeOrder = usePlaceOrder();

  const [cashAccount, setCashAccount] = useState("");
  const [side, setSide] = useState<OrderSide>("buy");
  const [orderType, setOrderType] = useState<OrderType>("market");
  const [quantity, setQuantity] = useState("");
  const [limitPrice, setLimitPrice] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [confirmation, setConfirmation] = useState<string | null>(null);

  const attemptKey = useRef<string | null>(null);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setFieldErrors({});
    setConfirmation(null);

    if (orderType === "limit" && !limitPrice) {
      setFieldErrors({ limit_price: "A limit order needs a limit price." });
      return;
    }

    attemptKey.current ??= crypto.randomUUID();

    try {
      const order = await placeOrder.mutateAsync({
        symbol,
        cash_account: cashAccount,
        side,
        order_type: orderType,
        quantity,
        // Explicitly null on a market order: the serializer rejects a limit price on one, and
        // sending "" would be a validation error rather than an omission.
        limit_price: orderType === "limit" ? limitPrice : null,
        idempotency_key: attemptKey.current,
      });

      setConfirmation(
        order.status === "filled"
          ? `Filled ${order.filled_quantity} ${symbol} at ${order.filled_price ?? "—"}.`
          : `Order resting. It will fill when ${symbol} crosses ${limitPrice}.`,
      );
      attemptKey.current = null;
      setQuantity("");
      setLimitPrice("");
    } catch (caught) {
      if (caught instanceof ApiError) {
        if (caught.code === "validation_error") {
          const errors: Record<string, string> = {};
          for (const field of ["quantity", "limit_price", "cash_account"]) {
            const message = caught.fieldError(field);
            if (message) errors[field] = message;
          }
          setFieldErrors(errors);
        } else if (caught.code === "insufficient_funds") {
          setError("There isn't enough cash in that account for this order.");
        } else if (caught.code === "insufficient_shares") {
          setError(`You don't hold enough ${symbol} to sell that many.`);
        } else if (caught.code === "order_key_conflict") {
          attemptKey.current = null;
          setError("That order conflicted with an earlier one. Please try again.");
        } else {
          setError(caught.message);
        }
      } else {
        setError("Could not reach the server. The order was not placed.");
      }
    }
  }

  const options = accounts.data?.results ?? [];

  // The ticket had neither branch: it rendered instantly with an empty "Cash account" select on
  // both first paint and outright failure, so "still loading" and "cannot load" looked identical
  // to "you have no accounts" — on the screen that spends money.
  if (accounts.isPending) {
    return (
      <Card title={`Trade ${symbol}`}>
        <Skeleton className="h-64" />
      </Card>
    );
  }

  if (accounts.isError) {
    return (
      <Card title={`Trade ${symbol}`}>
        <ErrorNote>Could not load your accounts, so an order cannot be placed.</ErrorNote>
      </Card>
    );
  }

  return (
    <Card title={`Trade ${symbol}`}>
      <form onSubmit={onSubmit} className="flex max-w-lg flex-col gap-4">
        {error && <ErrorNote>{error}</ErrorNote>}
        {confirmation && (
          <p role="status" className="rounded-lg bg-credit/10 px-3 py-2 text-sm text-credit">
            {confirmation}
          </p>
        )}

        <div className="grid gap-4 sm:grid-cols-2">
          <Select
            label="Side"
            value={side}
            onChange={(e) => setSide(e.target.value as OrderSide)}
          >
            <option value="buy">Buy</option>
            <option value="sell">Sell</option>
          </Select>

          <Select
            label="Type"
            value={orderType}
            onChange={(e) => setOrderType(e.target.value as OrderType)}
          >
            <option value="market">Market</option>
            <option value="limit">Limit</option>
          </Select>
        </div>

        <Select
          label="Cash account"
          value={cashAccount}
          onChange={(e) => setCashAccount(e.target.value)}
          error={fieldErrors["cash_account"]}
          required
        >
          <option value="">Choose an account</option>
          {options.map((account) => (
            <option key={account.id} value={account.id}>
              {account.name} — {account.number}
            </option>
          ))}
        </Select>

        <Field
          label="Quantity"
          value={quantity}
          onChange={(e) => setQuantity(e.target.value)}
          inputMode="decimal"
          placeholder="0.00000000"
          error={fieldErrors["quantity"]}
          required
        />

        {orderType === "limit" && (
          <Field
            label="Limit price"
            value={limitPrice}
            onChange={(e) => setLimitPrice(e.target.value)}
            inputMode="decimal"
            placeholder="0.0000"
            error={fieldErrors["limit_price"]}
            required
          />
        )}

        {lastPrice && (
          <p className="text-sm text-ink-muted">
            Last price: <Amount value={lastPrice} />
          </p>
        )}

        <Button type="submit" loading={placeOrder.isPending} disabled={!cashAccount}>
          {side === "buy" ? "Buy" : "Sell"} {symbol}
        </Button>
      </form>
    </Card>
  );
}
