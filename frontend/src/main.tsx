import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";

import App from "./App";
import { ApiError, SessionExpired } from "./api/client";
import { AuthProvider } from "./auth/AuthProvider";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { Toaster } from "./components/Toaster";
import "./index.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // The socket is what makes data fresh here, so polling on window focus would mostly be
      // duplicate work. Queries the socket cannot cover are invalidated explicitly.
      refetchOnWindowFocus: false,
      staleTime: 30_000,
      retry: (failureCount, error) => {
        // Never retry an expired session: `apiFetch` already tried a refresh and it failed, so
        // three more attempts would be three more failures and a slower redirect to the login page.
        if (error instanceof SessionExpired) return false;
        // Nor anything the server has already answered definitively. A 400 is not going to become
        // a 200 on the third ask, and a 404 is not going to start existing — retrying a client
        // error only delays the message by two round trips. 408 and 429 are the exceptions: both
        // explicitly mean "later", which is what a retry is.
        if (error instanceof ApiError && error.status >= 400 && error.status < 500) {
          if (error.status !== 408 && error.status !== 429) return false;
        }
        return failureCount < 2;
      },
    },
    // A mutation moves money. Retrying one automatically is a decision for the screen that owns the
    // idempotency key, not a global default.
    mutations: { retry: false },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    {/* Outside every provider on purpose: a provider that throws while initialising is exactly the
        case that would otherwise leave a blank page with no boundary mounted to catch it. */}
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <Toaster>
          <AuthProvider>
            <BrowserRouter>
              <App />
            </BrowserRouter>
          </AuthProvider>
        </Toaster>
      </QueryClientProvider>
    </ErrorBoundary>
  </StrictMode>,
);
