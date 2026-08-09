import { Link } from "react-router-dom";

import { useAccounts, usePortfolio } from "../api/hooks";
import { Amount, PnL } from "../components/Amount";
import { Card, Empty, ErrorNote, Skeleton } from "../components/ui";
import { usePageTitle } from "../usePageTitle";

export default function Dashboard() {
  usePageTitle("Dashboard");
  const accounts = useAccounts();
  const portfolio = usePortfolio();

  return (
    <div className="flex flex-col gap-6">
      <Card title="Total value">
        {portfolio.isPending ? (
          <Skeleton className="h-9 w-48" />
        ) : portfolio.isError ? (
          <ErrorNote>Could not load your portfolio.</ErrorNote>
        ) : (
          <div className="flex flex-wrap items-end gap-x-10 gap-y-4">
            <div>
              <p className="text-3xl font-semibold tabular">
                <Amount value={portfolio.data.total_value} />
              </p>
              <p className="mt-1 text-sm text-ink-muted">Cash and holdings</p>
            </div>
            <dl className="flex gap-8 text-sm">
              <div>
                <dt className="text-ink-muted">Cash</dt>
                <dd className="mt-0.5 font-medium"><Amount value={portfolio.data.cash} /></dd>
              </div>
              <div>
                <dt className="text-ink-muted">Holdings</dt>
                <dd className="mt-0.5 font-medium">
                  <Amount value={portfolio.data.holdings_value} />
                </dd>
              </div>
              <div>
                <dt className="text-ink-muted">Unrealized</dt>
                <dd className="mt-0.5 font-medium">
                  <PnL value={portfolio.data.unrealized_pnl} />
                </dd>
              </div>
              <div>
                <dt className="text-ink-muted">Realized</dt>
                <dd className="mt-0.5 font-medium"><PnL value={portfolio.data.realized_pnl} /></dd>
              </div>
            </dl>
          </div>
        )}
      </Card>

      <Card title="Accounts">
        {accounts.isPending ? (
          <div className="flex flex-col gap-3">
            <Skeleton className="h-16" />
            <Skeleton className="h-16" />
          </div>
        ) : accounts.isError ? (
          <ErrorNote>Could not load your accounts.</ErrorNote>
        ) : accounts.data.results.length === 0 ? (
          <Empty title="No accounts yet" hint="New accounts are opened when you register." />
        ) : (
          <ul className="grid gap-3 sm:grid-cols-2">
            {accounts.data.results.map((account) => (
              <li key={account.id}>
                <Link
                  to={`/accounts/${account.id}`}
                  className="block rounded-lg border border-border p-4 transition hover:bg-surface-sunken focus-visible:outline-2 focus-visible:outline-brand"
                >
                  <p className="font-medium">{account.name}</p>
                  <p className="mt-0.5 text-xs text-ink-muted tabular">{account.number}</p>
                  <p className="mt-3 text-2xl font-semibold">
                    <Amount value={account.balance} />
                  </p>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
