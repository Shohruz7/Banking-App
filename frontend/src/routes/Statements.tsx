/**
 * Generated monthly statements.
 *
 * Downloading needs `downloadFile`, not an `<a href>`. The endpoint requires an `Authorization`
 * header and a plain anchor sends none, so the browser would navigate to a 401 — and `MEDIA_URL`
 * is deliberately not routed, so there is no unauthenticated path to the file either. Fetch it,
 * make an object URL, click it, revoke.
 */

import { useState } from "react";

import { downloadFile } from "../api/client";
import { useStatements } from "../api/hooks";
import { useToast } from "../components/Toaster";
import { Button, Card, Empty, ErrorNote, Skeleton, Table, Td, Th } from "../components/ui";

const formatPeriod = (start: string) =>
  new Date(start).toLocaleDateString(undefined, { month: "long", year: "numeric" });

export default function Statements() {
  const statements = useStatements();
  const toast = useToast();
  const [downloading, setDownloading] = useState<string | null>(null);

  async function download(id: string, label: string) {
    setDownloading(id);
    try {
      await downloadFile(`/statements/${id}/download/`, `${label}.pdf`);
    } catch {
      toast("Could not download that statement.", "bad");
    } finally {
      setDownloading(null);
    }
  }

  return (
    <Card title="Statements">
      {statements.isPending ? (
        <Skeleton className="h-32" />
      ) : statements.isError ? (
        <ErrorNote>Could not load your statements.</ErrorNote>
      ) : statements.data.results.length === 0 ? (
        <Empty
          title="No statements yet"
          hint="One is generated for each account on the 1st of the month."
        />
      ) : (
        <Table>
          <thead>
            <tr>
              <Th>Period</Th>
              <Th>Kind</Th>
              <Th right />
            </tr>
          </thead>
          <tbody>
            {statements.data.results.map((statement) => {
              const label = `${formatPeriod(statement.period_start)} ${statement.kind}`;
              return (
                <tr key={statement.id}>
                  <Td>{formatPeriod(statement.period_start)}</Td>
                  <Td>
                    <span className="capitalize text-ink-muted">{statement.kind}</span>
                  </Td>
                  <Td right>
                    <Button
                      variant="secondary"
                      loading={downloading === statement.id}
                      onClick={() => void download(statement.id, label)}
                    >
                      Download PDF
                    </Button>
                  </Td>
                </tr>
              );
            })}
          </tbody>
        </Table>
      )}
    </Card>
  );
}
