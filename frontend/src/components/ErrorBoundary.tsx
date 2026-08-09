/**
 * The last line of defence, and the one deploys actually need.
 *
 * React unmounts the whole tree when a render throws, so without a boundary the user gets a blank
 * `<div id="root">` — no message, no reload button, nothing to do but guess. That is bad on any
 * app; here it is specifically a *deploy* failure mode, which is why this arrived with Week 9
 * rather than with the client.
 *
 * **The chunk case.** `App.tsx` lazy-loads the price chart because Recharts is over half the
 * bundle. `<Suspense>` catches a *pending* promise, not a rejected one. So when a release changes
 * the hashed chunk name, anybody with a tab already open is holding an index bundle that points at
 * `InstrumentDetail-<oldhash>.js` — a file the new release does not serve. Clicking a symbol
 * rejects the dynamic import, and with no boundary above it that is a white screen, caused by the
 * act of shipping.
 *
 * A reload is not a workaround there, it is the whole fix: the user's HTML is stale, and fetching
 * it again gets the new asset names. So a chunk-load failure reloads once, automatically, and
 * anything else renders a panel. `sessionStorage` holds the "I already tried that" flag, because a
 * reload loop against a genuinely missing asset is worse than the error it was papering over — and
 * it clears on success, so a later, unrelated failure still gets its one reload.
 */

import { Component, type ErrorInfo, type ReactNode } from "react";

import { Button } from "./ui";

const RELOAD_FLAG = "banking.chunk-reload";

/**
 * Did this throw because a code-split chunk could not be fetched?
 *
 * There is no error class for this. Vite's preload helper, the browser's own module loader and
 * each engine's `import()` failure all word it differently, so matching on the message is the only
 * portable test — deliberately broad, since the cost of a false positive is one reload and the
 * cost of a false negative is the white screen this exists to prevent.
 */
export function isChunkLoadError(error: unknown): boolean {
  if (!(error instanceof Error)) return false;
  const text = `${error.name} ${error.message}`;
  return (
    /ChunkLoadError/i.test(text) ||
    /Loading chunk [\w-]+ failed/i.test(text) ||
    /Failed to fetch dynamically imported module/i.test(text) ||
    /error loading dynamically imported module/i.test(text) ||
    /Importing a module script failed/i.test(text)
  );
}

type Props = { children: ReactNode };
type State = { error: Error | null };

export class ErrorBoundary extends Component<Props, State> {
  override state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    // A stale bundle is the expected, self-healing case: reload once and the user never learns
    // there was a problem. The flag survives the reload; clearing it is `componentDidMount`'s job
    // below, so a *second* failure after a successful load still earns its own retry.
    if (isChunkLoadError(error) && !sessionStorage.getItem(RELOAD_FLAG)) {
      try {
        sessionStorage.setItem(RELOAD_FLAG, "1");
      } catch {
        // Storage unavailable (Safari private mode). Reloading without the guard risks a loop, so
        // fall through to the panel instead — a visible error beats an invisible refresh cycle.
        return;
      }
      window.location.reload();
      return;
    }

    // Nothing ships errors anywhere yet (no Sentry, deliberately — see the Week 9 plan). Sourcemaps
    // are built, so this is at least readable in a browser console when someone reports a blank
    // panel. This is the only `console` call in the app, and it earns its place.
    console.error("Unhandled render error", error, info.componentStack);
  }

  override componentDidMount(): void {
    // The error path mounts this component too — React commits the fallback and calls
    // `componentDidMount` *before* `componentDidCatch`. Clearing unconditionally therefore wiped
    // the guard a moment before the handler above checked it, which turned "reload at most once"
    // into "reload every time" against a chunk that is never coming back. Only a clean render
    // means the reload worked.
    if (this.state.error) return;

    try {
      sessionStorage.removeItem(RELOAD_FLAG);
    } catch {
      // Nothing to clean up if storage was never writable.
    }
  }

  override render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;

    return (
      <div className="mx-auto flex min-h-dvh max-w-lg flex-col justify-center gap-4 p-6">
        <div className="rounded-xl border border-border bg-surface p-6">
          <h1 className="text-lg font-semibold text-ink">Something went wrong</h1>
          <p className="mt-2 text-sm text-ink-muted">
            This screen failed to load. Your accounts and balances are unaffected — nothing here
            moves money.
          </p>
          <div className="mt-5 flex gap-3">
            <Button onClick={() => window.location.reload()}>Reload</Button>
            <Button
              variant="secondary"
              onClick={() => {
                window.location.href = "/";
              }}
            >
              Back to dashboard
            </Button>
          </div>
        </div>
      </div>
    );
  }
}
