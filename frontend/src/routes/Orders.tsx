import { useCancelOrder, useOrders } from "../api/hooks";
import type { OrderStatus } from "../api/types";
import { Amount } from "../components/Amount";
import { Badge, Button, Card, Empty, ErrorNote, Skeleton, Table, Td, Th } from "../components/ui";
import { formatQuantity } from "../money";

const TONE: Record<OrderStatus, "good" | "bad" | "pending" | "neutral"> = {
  filled: "good",
  rejected: "bad",
  open: "pending",
  cancelled: "neutral",
};

export default function Orders() {
  const orders = useOrders();
  const cancel = useCancelOrder();

  const rows = orders.data?.pages.flatMap((page) => page.results) ?? [];

  return (
    <Card title="Orders">
      {orders.isPending ? (
        <Skeleton className="h-40" />
      ) : orders.isError ? (
        <ErrorNote>Could not load your orders.</ErrorNote>
      ) : rows.length === 0 ? (
        <Empty title="No orders yet" hint="Place one from any instrument page." />
      ) : (
        <>
          <Table>
            <thead>
              <tr>
                <Th>Symbol</Th>
                <Th>Side</Th>
                <Th>Type</Th>
                <Th right>Quantity</Th>
                <Th right>Price</Th>
                <Th>Status</Th>
                <Th right />
              </tr>
            </thead>
            <tbody>
              {rows.map((order) => (
                <tr key={order.id}>
                  <Td>
                    <span className="font-medium">{order.symbol}</span>
                  </Td>
                  <Td>{order.side === "buy" ? "Buy" : "Sell"}</Td>
                  <Td>{order.order_type === "market" ? "Market" : "Limit"}</Td>
                  <Td right>{formatQuantity(order.quantity)}</Td>
                  <Td right>
                    {order.filled_price ? (
                      <Amount value={order.filled_price} />
                    ) : order.limit_price ? (
                      <span className="text-ink-muted">
                        <Amount value={order.limit_price} /> limit
                      </span>
                    ) : (
                      <span className="text-ink-muted">—</span>
                    )}
                  </Td>
                  <Td>
                    <Badge tone={TONE[order.status]}>{order.status}</Badge>
                    {order.reject_reason && (
                      <span className="ml-2 text-xs text-ink-muted">{order.reject_reason}</span>
                    )}
                  </Td>
                  <Td right>
                    {order.status === "open" && (
                      <Button
                        variant="secondary"
                        loading={cancel.isPending && cancel.variables === order.id}
                        onClick={() => cancel.mutate(order.id)}
                      >
                        Cancel
                      </Button>
                    )}
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>

          {orders.hasNextPage && (
            <div className="mt-4 flex justify-center">
              <Button
                variant="secondary"
                loading={orders.isFetchingNextPage}
                onClick={() => void orders.fetchNextPage()}
              >
                Load more
              </Button>
            </div>
          )}
        </>
      )}
    </Card>
  );
}
