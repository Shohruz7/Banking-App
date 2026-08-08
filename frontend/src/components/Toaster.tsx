/**
 * Transient notices — mostly things the socket told us about.
 *
 * The live region is the point. A fill that arrives over the WebSocket is a change the user did not
 * initiate, so it has to be announced, not just drawn: `aria-live="polite"` on a container that is
 * always mounted, with messages appended into it. Mounting the region only when a toast exists
 * would mean screen readers never announce the first one.
 */

import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from "react";

import { cx } from "./ui";

export type ToastTone = "info" | "good" | "bad";

interface Toast {
  id: number;
  message: string;
  tone: ToastTone;
}

const ToastContext = createContext<((message: string, tone?: ToastTone) => void) | null>(null);

export function Toaster({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const nextId = useRef(0);

  const push = useCallback((message: string, tone: ToastTone = "info") => {
    const id = nextId.current++;
    setToasts((current) => [...current, { id, message, tone }]);
    setTimeout(() => setToasts((current) => current.filter((t) => t.id !== id)), 6000);
  }, []);

  const value = useMemo(() => push, [push]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div
        aria-live="polite"
        aria-atomic="false"
        className="pointer-events-none fixed bottom-4 right-4 z-50 flex w-80 flex-col gap-2"
      >
        {toasts.map((toast) => (
          <div
            key={toast.id}
            className={cx(
              "pointer-events-auto rounded-lg border px-4 py-3 text-sm shadow-lg",
              "border-border bg-surface",
              toast.tone === "good" && "border-credit/40",
              toast.tone === "bad" && "border-debit/40",
            )}
          >
            {toast.message}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast() {
  const push = useContext(ToastContext);
  if (!push) throw new Error("useToast must be used inside a Toaster");
  return push;
}
