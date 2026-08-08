# Frontend

React 19 + TypeScript single-page client for the banking API. Vite, TanStack Query, Tailwind v4,
Recharts.

```sh
npm ci
npm run dev          # http://localhost:5173, proxying /api and /ws to :8000
```

The backend must be running (`cd ../backend && uv run python manage.py runserver`). Celery worker
and Beat are optional — without them the market simply stands still.

```sh
npm run typecheck    # tsc --noEmit, strict
npm run lint         # eslint
npm test             # vitest
npm run coverage     # vitest with the gate
npm run build        # tsc + vite build
npm run schema       # regenerate src/api/schema.d.ts from openapi.json
```

## How it is put together

**Same-origin, always.** Vite proxies `/api` and `/ws` in dev; nginx serves the bundle and proxies
both in prod. The browser never makes a cross-origin request, which is why there is no CORS
dependency in the backend and no API-host setting here — `BASE` is the relative string `/api/v1`
(ADR-0030).

**The types are generated.** `openapi.json` comes from `manage.py spectacular`; `src/api/schema.d.ts`
comes from that. `src/api/types.ts` only names them. CI regenerates both and fails on a diff, so the
client cannot drift from the API (ADR-0032).

**Money is a string and stays one.** `Money` and `Quantity` are branded string types, formatting
lives in `src/money.ts`, and an eslint rule refuses `Number(...)` everywhere except that module. The
one sanctioned conversion is `toChartNumber`, at the charting boundary, because a pixel really is a
float (ADR-0009).

**One refresh, not N.** `apiFetch` deduplicates concurrent 401s onto a single in-flight refresh.
Firing five would mean four replays of a rotated token, which the backend correctly reads as theft
and answers by revoking the whole family — logging the user out *because* their session was healthy
(ADR-0013).

**The socket re-authenticates before it is closed.** The server closes the connection when the
access token expires, so `StreamClient` sends a fresh auth frame 30s before that, keeping the
connection and its subscriptions. It reconnects with backoff on a transport failure and does *not*
reconnect on 4403, which means the session was revoked (ADR-0022).

**Events invalidate the query cache rather than writing to it**, so every screen is still correct
with the socket closed — it updates on refetch instead of instantly (ADR-0032).

## Layout

```
src/
  api/       client.ts (fetch, refresh dedupe, error envelope) · hooks.ts · types.ts · schema.d.ts
  auth/      store.ts (where the tokens live) · AuthProvider.tsx (the two-step login)
  realtime/  socket.ts (the protocol) · useStream.tsx (events → cache)
  components/ ui.tsx · Amount.tsx · Toaster.tsx
  routes/    the screens
  money.ts   the decimal-string contract
  test/      MSW server, providers, jsdom setup
```

## Testing

Vitest + Testing Library + MSW, with handlers typed off the generated schema — a fixture that drifts
from the API fails `tsc` rather than passing against a shape the server never sends.

The suite tests properties rather than rendering: that concurrent 401s produce exactly one refresh,
that a rotated refresh token is stored, that one idempotency key survives a retry and a new attempt
mints a fresh one, that a 200 from `/transfers/` reads as "already sent", that the socket sends its
auth frame first and does not reconnect after 4403, and that formatting a value larger than 2^53
stays exact.

Coverage is gated per-module rather than globally: `client.ts`, `socket.ts` and `money.ts` carry the
behaviour worth protecting and are gated hard. The route components are not gated, deliberately —
they are thin projections of query results, and the only honest way to reach a global 80% on them is
a dozen smoke tests asserting that a heading appeared, which raises the number without protecting
anything.
