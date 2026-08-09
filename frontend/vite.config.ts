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
  build: {
    /*
     * Sourcemaps ship. Nothing collects errors yet — there is no Sentry here, deliberately — so
     * the only reader is whoever opens a console after a user reports the boundary's panel. That
     * is exactly the moment a minified frame is worthless. They cost nothing at runtime: the
     * browser fetches a `.map` only when devtools are open, and nginx serves them like any other
     * asset.
     *
     * Worth being clear about what this exposes, since the source is on GitHub anyway: readable
     * client code, which was never the secret. Nothing sensitive lives here — `BASE` is a relative
     * path and there is no build-time env in this project at all (ADR-0030).
     */
    sourcemap: true,
    rollupOptions: {
      output: {
        /*
         * One vendor chunk. Without it the entry is a single 315 kB file, so changing one line of a
         * route invalidates React, React-DOM, the router and TanStack Query for every returning
         * visitor. These four move on their own schedule — a dependency bump — and splitting them
         * out means an ordinary deploy re-downloads only the app.
         *
         * Recharts is deliberately absent: it is already isolated by the `lazy()` boundary in
         * `App.tsx` and naming it here would pull it back into a chunk every screen loads.
         */
        manualChunks: {
          vendor: ["react", "react-dom", "react-router-dom", "@tanstack/react-query"],
        },
      },
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
       * Gated per-module, not globally, and the omission is deliberate rather than an oversight.
       *
       * The modules below carry the behaviour worth protecting: the refresh dedupe, the socket
       * protocol, the decimal formatting, the idempotency-key discipline. Those are gated hard.
       *
       * Most route components are *not* gated. They are thin projections of query results, and the
       * only honest way to reach 80% on them is a dozen render-smoke tests that assert a heading
       * appeared — which would raise the number without protecting anything, exactly the kind of
       * measurement Week 7 removed from the backend when it stopped counting the test suite in its
       * own coverage.
       *
       * Week 9 applied that same criterion in the other direction and found three modules on the
       * wrong side of it. `useStream.tsx` is the socket→cache mapping ADR-0032 is *about*, and it
       * had no test at all while `socket.ts` next to it sat at 91%. `OrderTicket.tsx` carries the
       * identical per-attempt idempotency discipline that got `Transfer.tsx` tested. `Login.tsx`
       * holds the two-step MFA flow, where "a 200 is a challenge, not a success" is exactly the
       * kind of inversion a person reviewing a diff will not notice. None of the three was a thin
       * projection; the rule was right and the list was incomplete.
       */
      thresholds: {
        // `client.ts` and not `api/**`: `hooks.ts` next to it is declarative query wiring, and
        // testing that `useAccounts` calls `/accounts/` asserts the line above it, not a property.
        // Floors sit a point or two under what the suite actually achieves, the same way the
        // backend gate sits at 96 against 97: an honest small dip should not block a merge, while
        // a real regression still does.
        "src/api/client.ts": { lines: 85, functions: 85, branches: 74, statements: 85 },
        "src/realtime/socket.ts": { lines: 88, functions: 90, branches: 78, statements: 88 },
        "src/money.ts": { lines: 95, functions: 90, branches: 75, statements: 95 },

        // Session handling. Both were ungated while holding the token storage and the bootstrap
        // that decides whether a returning tab is signed in.
        "src/auth/store.ts": { lines: 85, functions: 95, branches: 64, statements: 85 },
        "src/auth/AuthProvider.tsx": { lines: 82, functions: 95, branches: 78, statements: 82 },

        // The three the criterion above had misfiled.
        "src/routes/OrderTicket.tsx": { lines: 85, functions: 82, branches: 70, statements: 85 },
        "src/routes/Transfer.tsx": { lines: 85, functions: 80, branches: 80, statements: 85 },
        "src/routes/Login.tsx": { lines: 90, functions: 95, branches: 70, statements: 90 },

        // `useStream.tsx`'s floor is frankly modest, and saying why is better than quietly setting
        // a number: the event→cache mapping is covered, `useLivePrice` and the subscribe/unsubscribe
        // callbacks are not, and those need a component that mounts a chart to exercise honestly.
        // The gate is set where the suite actually is so a regression in the mapping still trips it.
        "src/realtime/useStream.tsx": { lines: 65, functions: 20, branches: 55, statements: 65 },

        // The boundary is the only thing between a render throw and a blank page, and its
        // reload-once guard is the sort of logic that looks obviously right and was not — the test
        // caught it inverting on the first run.
        "src/components/ErrorBoundary.tsx": { lines: 85, functions: 70, branches: 75, statements: 85 },
      },
    },
  },
});
