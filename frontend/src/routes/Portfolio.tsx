/**
 * Holdings and what they are worth.
 *
 * Every figure on this page is derived by the server from the ledger on read — quantity and cost
 * basis are sums over journal lines, everything else is arithmetic on top (ADR-0020). None of it is
 * recomputed here, deliberately: a client that adds up its own market value is a second source of
 * truth for a number the ledger already answers.
 */

import { Link } from "react-router-dom";

import { useHoldings, usePortfolio } from "../api/hooks";
import { Amount, PnL } from "../components/Amount";
import { Card, Empty, ErrorNote, Skeleton, Table, Td, Th } from "../components/ui";
import { formatQuantity } from "../money";

export default function Portfolio() {
  const portfolio = usePortfolio();
  const holdings = useHoldings();

  return (
    <div className="flex flex-col gap-6">
      <Card title="Portfolio">
        {portfolio.isPending ? (
          <Skeleton className="h-20" />
        ) : portfolio.isError ? (
          <ErrorNote>Could not load your portfolio.</ErrorNote>
        ) : (
          <dl className="grid gap-6 sm:grid-cols-3 lg:grid-cols-6">
            {(
              [
                ["Total", <Amount key="t" value={portfolio.data.total_value} />],
                ["Cash", <Amount key="c" value={portfolio.data.cash} />],
                ["Holdings", <Amount key="h" value={portfolio.data.holdings_value} />],
                ["Cost basis", <Amount key="b" value={portfolio.data.cost_basis} />],
                ["Unrealized", <PnL key="u" value={portfolio.data.unrealized_pnl} />],
                ["Realized", <PnL key="r" value={portfolio.data.realized_pnl} />],
              ] as const
            ).map(([label, node]) => (
              <div key={label}>
                <dt className="text-xs uppercase tracking-wide text-ink-muted">{label}</dt>
                <dd className="mt-1 text-lg font-semibold">{node}</dd>
              </div>
            ))}
          </dl>
        )}
      </Card>

      <Card title="Holdings">
        {holdings.isPending ? (
          <Skeleton className="h-32" />
        ) : holdings.isError ? (
          <ErrorNote>Could not load your holdings.</ErrorNote>
        ) : holdings.data.results.length === 0 ? (
          <Empty title="No positions" hint="Buy something from the markets page." />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Symbol</Th>
                <Th right>Shares</Th>
                <Th right>Avg cost</Th>
                <Th right>Last</Th>
                <Th right>Value</Th>
                <Th right>Unrealized</Th>
              </tr>
            </thead>
            <tbody>
              {holdings.data.results.map((holding) => (
                <tr key={holding.account_id}>
                  <Td>
                    <Link
                      to={`/markets/${holding.symbol}`}
                      className="font-medium text-brand underline"
                    >
                      {holding.symbol}
                    </Link>
                    <span className="ml-2 text-ink-muted">{holding.name}</span>
                  </Td>
                  <Td right>{formatQuantity(holding.quantity)}</Td>
                  <Td right>
                    <Amount value={holding.average_cost} />
                  </Td>
                  <Td right>
                    <Amount value={holding.last_price} />
                  </Td>
                  <Td right>
                    <Amount value={holding.market_value} />
                  </Td>
                  <Td right>
                    <PnL value={holding.unrealized_pnl} />
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
    </div>
  );
}
