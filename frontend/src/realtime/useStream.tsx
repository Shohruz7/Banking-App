/**
 * Wiring the socket into React Query.
 *
 * **Events invalidate; they do not write** (ADR-0032). It is tempting to take `balance.updated`'s
 * `balance` field and `setQueryData` it straight into the accounts list — one fewer request, and
 * the number is right there. The reason not to is that the event carries *one* account's balance
 * while the list row it would patch is part of a page fetched as a whole, and a client that starts
 * splicing server state together is maintaining a second copy of the ledger's arithmetic. The
 * server derives every figure from the ledger on read; the client's job is to know *when* to ask
 * again, which is exactly what an event tells it.
 *
 * `price.tick` is the deliberate exception and goes nowhere near the cache: 55 symbols ticking once
 * a minute would invalidate constantly, and no query owns a live price anyway. It is published on a
 * plain subscription that the chart reads directly.
 */

import { createContext, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { keys } from "../api/hooks";
import { useAuth } from "../auth/AuthProvider";
import { getAccessToken, onTokensChanged } from "../auth/store";
import { useToast } from "../components/Toaster";
import { StreamClient, type StreamEvent } from "./socket";

type TickListener = (symbol: string, price: string, at: string) => void;

interface StreamValue {
  subscribe: (symbols: string[]) => void;
  unsubscribe: (symbols: string[]) => void;
  /** Live price ticks, outside the query cache. Returns an unsubscribe function. */
  onTick: (listener: TickListener) => () => void;
  connected: boolean;
}

const StreamContext = createContext<StreamValue | null>(null);

export function StreamProvider({ children }: { children: ReactNode }) {
  const { user, signOut } = useAuth();
  const queryClient = useQueryClient();
  const toast = useToast();
  const [connected, setConnected] = useState(false);

  const clientRef = useRef<StreamClient | null>(null);
  const tickListeners = useRef(new Set<TickListener>());

  useEffect(() => {
    if (!user) return;

    const client = new StreamClient({
      getToken: getAccessToken,
      onSessionLost: () => {
        toast("Your session was ended.", "bad");
        void signOut();
      },
      onEvent: (event: StreamEvent) => {
        switch (event.type) {
          case "auth.ok":
            setConnected(true);
            break;

          case "price.tick":
            for (const listener of tickListeners.current) {
              listener(event.symbol, event.price, event.at);
            }
            break;

          case "balance.updated":
            void queryClient.invalidateQueries({ queryKey: keys.accounts });
            void queryClient.invalidateQueries({ queryKey: keys.account(event.account_id) });
            void queryClient.invalidateQueries({ queryKey: keys.transactions(event.account_id) });
            void queryClient.invalidateQueries({ queryKey: keys.portfolio });
            break;

          case "transfer.posted":
            toast(`Transfer posted: ${event.description}`, "good");
            void queryClient.invalidateQueries({ queryKey: keys.accounts });
            break;

          case "order.filled":
            toast("An order filled.", "good");
            void queryClient.invalidateQueries({ queryKey: keys.orders });
            void queryClient.invalidateQueries({ queryKey: keys.holdings });
            void queryClient.invalidateQueries({ queryKey: keys.portfolio });
            void queryClient.invalidateQueries({ queryKey: keys.accounts });
            break;

          case "order.rejected":
            // No balance invalidation, and that is the observable half of ADR-0023: a rejected
            // order rolled back, so nothing about the money changed and there is nothing to refetch.
            toast("An order was rejected.", "bad");
            void queryClient.invalidateQueries({ queryKey: keys.orders });
            break;

          case "order.cancelled":
            void queryClient.invalidateQueries({ queryKey: keys.orders });
            break;

          default:
            break;
        }
      },
    });

    clientRef.current = client;
    client.connect();

    // A refreshed access token has to reach the socket, or the server closes it when the *old*
    // token expires. This is the subscription that keeps a long-lived tab streaming.
    const stopWatching = onTokensChanged(() => client.reauthenticate());

    // A laptop waking from sleep has a dead socket and a stale token at the same time. The token is
    // handled by the next query's 401-and-refresh; this handles the socket.
    const onVisible = () => {
      if (document.visibilityState === "visible") client.connect();
    };
    document.addEventListener("visibilitychange", onVisible);

    return () => {
      stopWatching();
      document.removeEventListener("visibilitychange", onVisible);
      client.close();
      clientRef.current = null;
      setConnected(false);
    };
  }, [user, queryClient, toast, signOut]);

  const value = useMemo<StreamValue>(
    () => ({
      subscribe: (symbols) => clientRef.current?.subscribe(symbols),
      unsubscribe: (symbols) => clientRef.current?.unsubscribe(symbols),
      onTick: (listener) => {
        tickListeners.current.add(listener);
        return () => tickListeners.current.delete(listener);
      },
      connected,
    }),
    [connected],
  );

  return <StreamContext.Provider value={value}>{children}</StreamContext.Provider>;
}

export function useStream(): StreamValue {
  const value = useContext(StreamContext);
  if (!value) throw new Error("useStream must be used inside a StreamProvider");
  return value;
}

/** Subscribe to a symbol's ticks for as long as the component is mounted. */
export function useLivePrice(symbol: string | undefined, onTick: TickListener): void {
  const { subscribe, unsubscribe, onTick: register } = useStream();

  useEffect(() => {
    if (!symbol) return;
    subscribe([symbol]);
    const stop = register((tickSymbol, price, at) => {
      if (tickSymbol === symbol.toUpperCase()) onTick(tickSymbol, price, at);
    });
    return () => {
      stop();
      unsubscribe([symbol]);
    };
    // `onTick` deliberately excluded: callers pass an inline closure, and including it would
    // resubscribe on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbol, subscribe, unsubscribe, register]);
}
