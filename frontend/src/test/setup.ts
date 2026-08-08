import "@testing-library/jest-dom/vitest";

import { afterAll, afterEach, beforeAll } from "vitest";

/**
 * Node 26 defines its own `localStorage` global, which is `undefined` unless the runtime was
 * started with `--localstorage-file`. It shadows the one jsdom provides, so under Vitest the app's
 * storage calls hit a global that exists and is undefined — which is exactly the case `store.ts`
 * cannot distinguish from Safari private mode. An in-memory stand-in restores the browser
 * behaviour these tests are about.
 */
if (!globalThis.localStorage) {
  const entries = new Map<string, string>();
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: {
      getItem: (key: string) => entries.get(key) ?? null,
      setItem: (key: string, value: string) => void entries.set(key, String(value)),
      removeItem: (key: string) => void entries.delete(key),
      clear: () => entries.clear(),
      key: (index: number) => [...entries.keys()][index] ?? null,
      get length() {
        return entries.size;
      },
    } satisfies Storage,
  });
}

import { __resetRefreshState } from "../api/client";
import { clearSession } from "../auth/store";
import { server } from "./server";

// `onUnhandledRequest: "error"` on purpose. A request nobody wrote a handler for is a test that is
// silently exercising a different code path than it claims to, and the default ("warn") hides it in
// output nobody reads.
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));

afterEach(() => {
  server.resetHandlers();
  clearSession();
  __resetRefreshState();
  localStorage.clear();
});

afterAll(() => server.close());
