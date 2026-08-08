import { useState } from "react";
import { Link } from "react-router-dom";

import { useInstruments } from "../api/hooks";
import { Amount } from "../components/Amount";
import { Card, Empty, ErrorNote, Field, Skeleton, Table, Td, Th } from "../components/ui";

export default function Markets() {
  const [search, setSearch] = useState("");
  // Server-side search: the viewset already supports `?q=` against symbol and name, so filtering
  // here would be a second, worse implementation of a query the API answers.
  const instruments = useInstruments(search);

  return (
    <Card title="Markets">
      <div className="mb-4 max-w-xs">
        <Field
          label="Search"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Symbol or name"
          type="search"
        />
      </div>

      {instruments.isPending ? (
        <Skeleton className="h-40" />
      ) : instruments.isError ? (
        <ErrorNote>Could not load instruments.</ErrorNote>
      ) : instruments.data.results.length === 0 ? (
        <Empty title="Nothing matches" hint="Try a different symbol." />
      ) : (
        <Table>
          <thead>
            <tr>
              <Th>Symbol</Th>
              <Th>Name</Th>
              <Th>Sector</Th>
              <Th right>Last price</Th>
            </tr>
          </thead>
          <tbody>
            {instruments.data.results.map((instrument) => (
              <tr key={instrument.id}>
                <Td>
                  <Link
                    to={`/markets/${instrument.symbol}`}
                    className="font-medium text-brand underline"
                  >
                    {instrument.symbol}
                  </Link>
                </Td>
                <Td>{instrument.name}</Td>
                <Td>
                  <span className="text-ink-muted">{instrument.sector}</span>
                </Td>
                <Td right>
                  <Amount value={instrument.last_price} />
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
    </Card>
  );
}
