/**
 * One account and its history.
 *
 * The full account number is behind a click. That is not decoration: `/accounts/{id}/` is the only
 * endpoint that decrypts the number (ADR-0027), and the list endpoint deliberately serves only the
 * last four. Revealing on demand keeps the plaintext off the screen — and out of a screenshot or a
 * shoulder-surf — until somebody actually needs to read it out.
 */

import { useState } from "react";
import { useParams } from "react-router-dom";

import { useAccount, useTransactions } from "../api/hooks";
import { Amount } from "../components/Amount";
import { Button, Card, Empty, ErrorNote, Skeleton, Table, Td, Th } from "../components/ui";

const formatDate = (iso: string) =>
  new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });

export default function AccountDetail() {
  const { id = "" } = useParams();
  const account = useAccount(id);
  const history = useTransactions(id);
  const [revealed, setRevealed] = useState(false);

  const lines = history.data?.pages.flatMap((page) => page.results) ?? [];

  return (
    <div className="flex flex-col gap-6">
      <Card
        title="Account"
        actions={
          account.data && (
            <Button variant="secondary" onClick={() => setRevealed((r) => !r)}>
              {revealed ? "Hide number" : "Show number"}
            </Button>
          )
        }
      >
        {account.isPending ? (
          <Skeleton className="h-16" />
        ) : account.isError ? (
          <ErrorNote>Could not load this account.</ErrorNote>
        ) : (
          <div>
            <p className="font-medium">{account.data.name}</p>
            <p className="mt-0.5 text-sm text-ink-muted tabular">
              {revealed ? account.data.number : `••••${account.data.number.slice(-4)}`}
            </p>
            <p className="mt-3 text-3xl font-semibold">
              <Amount value={account.data.balance} />
            </p>
          </div>
        )}
      </Card>

      <Card title="Transactions">
        {history.isPending ? (
          <Skeleton className="h-32" />
        ) : history.isError ? (
          <ErrorNote>Could not load this account's history.</ErrorNote>
        ) : lines.length === 0 ? (
          <Empty title="Nothing here yet" hint="Transfers and fills will appear as they post." />
        ) : (
          <>
            <Table>
              <thead>
                <tr>
                  <Th>Date</Th>
                  <Th>Description</Th>
                  <Th right>Amount</Th>
                </tr>
              </thead>
              <tbody>
                {lines.map((line) => (
                  <tr key={line.id}>
                    <Td>
                      <span className="text-ink-muted">{formatDate(line.created_at)}</span>
                    </Td>
                    <Td>{line.description}</Td>
                    <Td right>
                      <Amount value={line.amount} signed />
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>

            {history.hasNextPage && (
              <div className="mt-4 flex justify-center">
                <Button
                  variant="secondary"
                  loading={history.isFetchingNextPage}
                  onClick={() => void history.fetchNextPage()}
                >
                  Load more
                </Button>
              </div>
            )}
          </>
        )}
      </Card>
    </div>
  );
}
