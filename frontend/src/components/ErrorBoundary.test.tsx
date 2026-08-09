/**
 * The boundary's two jobs, which are not the same job.
 *
 * An ordinary throw has to *stop* at the boundary and show a panel. A chunk-load failure has to
 * reload instead, exactly once — that one is a deploy artefact rather than a bug, and it is the
 * case worth a test because it cannot be reproduced by hand without shipping twice.
 */

import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ErrorBoundary, isChunkLoadError } from "./ErrorBoundary";

const RELOAD_FLAG = "banking.chunk-reload";

function Boom({ error }: { error: Error }): never {
  throw error;
}

/** React logs a caught error to console.error; that is expected here and only adds noise. */
let consoleError: ReturnType<typeof vi.spyOn>;
let reload: ReturnType<typeof vi.fn>;

beforeEach(() => {
  consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
  reload = vi.fn();
  // jsdom's `location.reload` is not writable, so replace the property rather than the method.
  Object.defineProperty(window, "location", {
    configurable: true,
    value: { ...window.location, reload, href: "/" },
  });
  sessionStorage.clear();
});

afterEach(() => {
  consoleError.mockRestore();
  vi.restoreAllMocks();
});

describe("isChunkLoadError", () => {
  it("recognises how each engine words a failed dynamic import", () => {
    // Three real wordings: Vite's preload helper, Chrome, and Safari. There is no error class to
    // match on, which is the whole reason this predicate exists.
    expect(isChunkLoadError(new Error("Failed to fetch dynamically imported module: /x.js"))).toBe(
      true,
    );
    expect(isChunkLoadError(new Error("Importing a module script failed."))).toBe(true);
    expect(isChunkLoadError(new Error("Loading chunk vendor-abc123 failed."))).toBe(true);
  });

  it("does not mistake an ordinary error for a stale bundle", () => {
    // A false positive here costs a reload loop on a genuine bug, so the predicate has to be
    // specific about the thing it claims to detect.
    expect(isChunkLoadError(new Error("Cannot read properties of undefined"))).toBe(false);
    expect(isChunkLoadError(new TypeError("x is not a function"))).toBe(false);
    expect(isChunkLoadError("not even an error")).toBe(false);
  });
});

describe("ErrorBoundary", () => {
  it("renders its children when nothing throws", () => {
    render(
      <ErrorBoundary>
        <p>All fine</p>
      </ErrorBoundary>,
    );
    expect(screen.getByText("All fine")).toBeInTheDocument();
  });

  it("shows a panel instead of a blank page when a render throws", () => {
    render(
      <ErrorBoundary>
        <Boom error={new Error("kaboom")} />
      </ErrorBoundary>,
    );

    expect(screen.getByRole("heading", { name: /something went wrong/i })).toBeInTheDocument();
    // The reassurance matters on a banking screen: a render bug is not a money bug, and the panel
    // should say so rather than leaving the user to wonder.
    expect(screen.getByText(/nothing here moves money/i)).toBeInTheDocument();
    expect(reload).not.toHaveBeenCalled();
  });

  it("reloads once when a code-split chunk has gone missing", () => {
    // The redeploy case: the user's HTML points at a chunk the new release does not serve, so the
    // stale document is the problem and fetching it again is the entire fix.
    render(
      <ErrorBoundary>
        <Boom error={new Error("Failed to fetch dynamically imported module: /assets/x.js")} />
      </ErrorBoundary>,
    );

    expect(reload).toHaveBeenCalledTimes(1);
    expect(sessionStorage.getItem(RELOAD_FLAG)).toBe("1");
  });

  it("does not reload a second time if the chunk is genuinely gone", () => {
    // Without the flag this is an infinite refresh cycle against an asset that will never arrive —
    // strictly worse than the error it was papering over.
    sessionStorage.setItem(RELOAD_FLAG, "1");

    render(
      <ErrorBoundary>
        <Boom error={new Error("Failed to fetch dynamically imported module: /assets/x.js")} />
      </ErrorBoundary>,
    );

    expect(reload).not.toHaveBeenCalled();
    expect(screen.getByRole("heading", { name: /something went wrong/i })).toBeInTheDocument();
  });

  it("clears the flag once a render succeeds, so a later failure gets its own retry", () => {
    sessionStorage.setItem(RELOAD_FLAG, "1");

    render(
      <ErrorBoundary>
        <p>Recovered</p>
      </ErrorBoundary>,
    );

    expect(sessionStorage.getItem(RELOAD_FLAG)).toBeNull();
  });
});
