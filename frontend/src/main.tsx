import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";

import App from "./App";
import { SessionExpired } from "./api/client";
import { AuthProvider } from "./auth/AuthProvider";
import { Toaster } from "./components/Toaster";
import "./index.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // The socket is what makes data fresh here, so polling on window focus would mostly be
      // duplicate work. Queries the socket cannot cover are invalidated explicitly.
      refetchOnWindowFocus: false,
      staleTime: 30_000,
      retry: (failureCount, error) =>
        // Never retry an expired session: `apiFetch` already tried a refresh and it failed, so
        // three more attempts would be three more failures and a slower redirect to the login page.
        !(error instanceof SessionExpired) && failureCount < 2,
    },
    // A mutation moves money. Retrying one automatically is a decision for the screen that owns the
    // idempotency key, not a global default.
    mutations: { retry: false },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <Toaster>
        <AuthProvider>
          <BrowserRouter>
            <App />
          </BrowserRouter>
        </AuthProvider>
      </Toaster>
    </QueryClientProvider>
  </StrictMode>,
);
