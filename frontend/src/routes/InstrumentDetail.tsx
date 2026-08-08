/**
 * One symbol: its price history, live ticks, and the order ticket.
 *
 * The chart is the one place in this app where a decimal string legitimately becomes a float, and
 * it goes through `toChartNumber` so that conversion stays greppable (see `src/money.ts`). Every
 * label still renders from the string.
 *
 * Live ticks append to local state rather than invalidating a query — 55 symbols ticking every
 * minute would thrash the cache, and no query owns a live price (ADR-0032).
 */

import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { useInstrument, usePrices } from "../api/hooks";
import { Amount } from "../components/Amount";
import { Card, ErrorNote, Skeleton } from "../components/ui";
import { formatMoney, toChartNumber, type Money } from "../money";
import { useLivePrice } from "../realtime/useStream";
import OrderTicket from "./OrderTicket";

interface Point {
  at: string;
  price: Money;
}

export default function InstrumentDetail() {
  const { symbol = "" } = useParams();
  const instrument = useInstrument(symbol);
  const history = usePrices(symbol);

  const [series, setSeries] = useState<Point[]>([]);

  useEffect(() => {
    if (history.data) {
      setSeries(history.data.map((tick) => ({ at: tick.created_at, price: tick.price })));
    }
  }, [history.data]);

  useLivePrice(symbol, (_symbol, price, at) => {
    // Bounded: a tab left open for a day would otherwise accumulate a point a minute forever.
    setSeries((current) => [...current, { at, price: price as Money }].slice(-500));
  });

  const latest = series.at(-1)?.price ?? instrument.data?.last_price;

  return (
    <div className="flex flex-col gap-6">
      <Card title={symbol.toUpperCase()}>
        {instrument.isPending ? (
          <Skeleton className="h-16" />
        ) : instrument.isError ? (
          <ErrorNote>Could not load {symbol.toUpperCase()}.</ErrorNote>
        ) : (
          <div className="flex flex-wrap items-baseline justify-between gap-4">
            <div>
              <p className="font-medium">{instrument.data.name}</p>
              <p className="text-sm text-ink-muted">{instrument.data.sector}</p>
            </div>
            <p className="text-3xl font-semibold">{latest && <Amount value={latest} />}</p>
          </div>
        )}

        <div className="mt-6 h-64">
          {history.isPending ? (
            <Skeleton className="h-full" />
          ) : series.length === 0 ? (
            <p className="text-sm text-ink-muted">
              No price history yet — Celery Beat advances the market once a minute.
            </p>
          ) : (
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={series} margin={{ top: 8, right: 8, bottom: 0, left: 8 }}>
                <CartesianGrid stroke="var(--color-border)" strokeDasharray="3 3" vertical={false} />
                <XAxis
                  dataKey="at"
                  tickFormatter={(iso: string) =>
                    new Date(iso).toLocaleTimeString(undefined, {
                      hour: "2-digit",
                      minute: "2-digit",
                    })
                  }
                  stroke="var(--color-ink-muted)"
                  fontSize={12}
                  minTickGap={40}
                />
                <YAxis
                  domain={["auto", "auto"]}
                  tickFormatter={(value: number) => `$${value.toFixed(2)}`}
                  stroke="var(--color-ink-muted)"
                  fontSize={12}
                  width={70}
                />
                <Tooltip
                  // Formats from the *string*, so the tooltip shows the exact price even though the
                  // plotted y-value went through a float.
                  formatter={(_value, _name, item: { payload?: Point }) => [
                    item.payload ? formatMoney(item.payload.price) : "",
                    "Price",
                  ]}
                  labelFormatter={(iso: string) => new Date(iso).toLocaleString()}
                  contentStyle={{
                    background: "var(--color-surface)",
                    border: "1px solid var(--color-border)",
                    borderRadius: "0.5rem",
                  }}
                />
                <Line
                  type="monotone"
                  dataKey={(point: Point) => toChartNumber(point.price)}
                  name="Price"
                  stroke="var(--color-brand)"
                  strokeWidth={2}
                  dot={false}
                  isAnimationActive={false}
                />
              </LineChart>
            </ResponsiveContainer>
          )}
        </div>
      </Card>

      <OrderTicket symbol={symbol.toUpperCase()} lastPrice={latest} />
    </div>
  );
}
