/** Render a component with the providers it expects, and a query client that never retries. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, type RenderResult } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import type { ReactElement } from "react";

import { AuthProvider } from "../auth/AuthProvider";
import { Toaster } from "../components/Toaster";

export function renderWithProviders(ui: ReactElement, { route = "/" } = {}): RenderResult {
  const queryClient = new QueryClient({
    // Retries turn one deliberate 400 into three, and turn a fast assertion into a timeout.
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <Toaster>
        <AuthProvider>
          <MemoryRouter initialEntries={[route]}>{ui}</MemoryRouter>
        </AuthProvider>
      </Toaster>
    </QueryClientProvider>,
  );
}
