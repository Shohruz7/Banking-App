// From `vitest/config`, not `vite`: same `defineConfig`, widened to accept the `test` block below.
// Importing it from `vite` typechecks everything except the test configuration — the one part you
// would not notice until CI.
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

/**
 * Dev runs same-origin, deliberately (ADR-0029).
 *
 * The reflex when a browser client meets a Django API is to install `django-cors-headers`. This
 * proxy is why that dependency does not exist here: `/api` and `/ws` are proxied to the backend, so
 * the browser only ever talks to `localhost:5173`, and Week 9's nginx serves the built bundle and
 * proxies the API on one origin too. A cross-origin request is never made in either environment,
 * so there is no preflight to allow and no origin list to keep in step with a deploy.
 *
 * `ws: true` matters as much as the HTTP entry: without it the proxy answers the WebSocket
 * handshake with a 200 and the socket never upgrades.
 */
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://localhost:8000", changeOrigin: true },
      "/ws": { target: "ws://localhost:8000", ws: true },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    coverage: {
      provider: "v8",
      include: ["src/**/*.{ts,tsx}"],
      exclude: ["src/**/*.test.{ts,tsx}", "src/test/**", "src/api/schema.d.ts", "src/main.tsx"],
      /*
       * Gated per-directory, not globally, and the omission is deliberate rather than an oversight.
       *
       * The modules below carry the behaviour worth protecting: the refresh dedupe, the socket
       * protocol, the decimal formatting, the idempotency-key discipline. Those are gated hard.
       *
       * The route components are *not* gated. They are thin projections of query results, and the
       * only honest way to reach 80% on them is a dozen render-smoke tests that assert a heading
       * appeared — which would raise the number without protecting anything, exactly the kind of
       * measurement Week 7 removed from the backend when it stopped counting the test suite in its
       * own coverage. `Transfer.tsx` is tested because it holds real logic; the rest earn tests
       * when they earn behaviour.
       */
      thresholds: {
        // `client.ts` and not `api/**`: `hooks.ts` next to it is declarative query wiring, and
        // testing that `useAccounts` calls `/accounts/` asserts the line above it, not a property.
        // Floors sit a point or two under what the suite actually achieves, the same way the
        // backend gate sits at 96 against 97: an honest small dip should not block a merge, while
        // a real regression still does.
        "src/api/client.ts": { lines: 80, functions: 75, branches: 70, statements: 80 },
        "src/realtime/socket.ts": { lines: 80, functions: 80, branches: 75, statements: 80 },
        "src/money.ts": { lines: 95, functions: 90, branches: 75, statements: 95 },
      },
    },
  },
});
