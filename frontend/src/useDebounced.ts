/**
 * Hold a fast-changing value still until it stops changing.
 *
 * Written for the markets search, where every keystroke was a new React Query key and therefore a
 * new request: typing "AAPL" cost four round trips, and `staleTime` could not help because no two
 * of those keys were the same. Four users typing is a burst in the access log that looks like
 * traffic and is really one search.
 *
 * The trailing edge is the right one here. A leading-edge debounce would fire on "A" — the least
 * useful query of the four — and a throttle would fire on a fixed cadence regardless of whether
 * the user had finished. This fires once, when they pause.
 */

import { useEffect, useState } from "react";

export function useDebounced<T>(value: T, delayMs = 250): T {
  const [settled, setSettled] = useState(value);

  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), delayMs);
    // Clearing on every change is what makes this a debounce rather than a queue of timers: each
    // keystroke cancels the pending one and starts the wait again.
    return () => clearTimeout(timer);
  }, [value, delayMs]);

  return settled;
}
