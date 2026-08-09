/**
 * The primitives. Eight of them, hand-built, because a six-screen app does not need a component
 * library and the library would be the largest dependency in the project.
 */

import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode, SelectHTMLAttributes } from "react";

export const cx = (...parts: (string | false | null | undefined)[]): string =>
  parts.filter(Boolean).join(" ");

// ---------------------------------------------------------------------------------------------

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "danger";
  loading?: boolean;
};

export function Button({
  variant = "primary",
  loading = false,
  className,
  children,
  disabled,
  ...rest
}: ButtonProps) {
  const styles = {
    primary: "bg-brand text-brand-ink hover:opacity-90",
    secondary: "bg-surface text-ink border border-border hover:bg-surface-sunken",
    danger: "bg-debit text-white hover:opacity-90",
  }[variant];

  return (
    <button
      {...rest}
      disabled={disabled || loading}
      // aria-busy rather than swapping the label for a spinner: a screen reader announcing "Sending"
      // and then "Send" again is noisier than the state change itself.
      aria-busy={loading}
      className={cx(
        "inline-flex items-center justify-center gap-2 rounded-lg px-4 py-2 text-sm font-medium",
        "transition disabled:cursor-not-allowed disabled:opacity-50",
        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand",
        styles,
        className,
      )}
    >
      {loading && (
        <span
          aria-hidden="true"
          className="size-3.5 animate-spin rounded-full border-2 border-current border-t-transparent"
        />
      )}
      {children}
    </button>
  );
}

// ---------------------------------------------------------------------------------------------

export function Card({
  title,
  actions,
  children,
  className,
}: {
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section
      className={cx("rounded-xl border border-border bg-surface p-5 shadow-sm", className)}
    >
      {(title || actions) && (
        <header className="mb-4 flex items-center justify-between gap-4">
          {title && <h2 className="text-sm font-semibold text-ink-muted uppercase tracking-wide">{title}</h2>}
          {actions}
        </header>
      )}
      {children}
    </section>
  );
}

// ---------------------------------------------------------------------------------------------

type FieldProps = InputHTMLAttributes<HTMLInputElement> & { label: string; error?: string | undefined };

export function Field({ label, error, id, className, ...rest }: FieldProps) {
  const inputId = id ?? `field-${label.toLowerCase().replace(/\s+/g, "-")}`;
  const errorId = `${inputId}-error`;

  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={inputId} className="text-sm font-medium">
        {label}
      </label>
      <input
        {...rest}
        id={inputId}
        aria-invalid={error ? true : undefined}
        // Wired to the message so the error is announced when focus lands, rather than being a red
        // border that a screen reader cannot see.
        aria-describedby={error ? errorId : undefined}
        className={cx(
          "rounded-lg border bg-surface px-3 py-2 text-sm",
          "focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-brand",
          error ? "border-debit" : "border-border",
          className,
        )}
      />
      {error && (
        <p id={errorId} role="alert" className="text-xs text-debit">
          {error}
        </p>
      )}
    </div>
  );
}

type SelectProps = SelectHTMLAttributes<HTMLSelectElement> & { label: string; error?: string | undefined };

export function Select({ label, error, id, children, className, ...rest }: SelectProps) {
  const selectId = id ?? `select-${label.toLowerCase().replace(/\s+/g, "-")}`;
  const errorId = `${selectId}-error`;

  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={selectId} className="text-sm font-medium">
        {label}
      </label>
      <select
        {...rest}
        id={selectId}
        aria-invalid={error ? true : undefined}
        // `Field` above has always done this and `Select` never did, which left the message visible
        // but unannounced on focus. Live rather than theoretical: both transfer dropdowns and the
        // order ticket's cash account route their validation errors through here.
        aria-describedby={error ? errorId : undefined}
        className={cx(
          "rounded-lg border bg-surface px-3 py-2 text-sm",
          "focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-brand",
          error ? "border-debit" : "border-border",
          className,
        )}
      >
        {children}
      </select>
      {error && (
        <p id={errorId} role="alert" className="text-xs text-debit">
          {error}
        </p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------------------------

export const Table = ({ children }: { children: ReactNode }) => (
  <div className="overflow-x-auto">
    <table className="w-full text-sm">{children}</table>
  </div>
);

// `children` is optional so an actions column can have a deliberately empty header — a table with
// a "Cancel" button column needs the cell for alignment but has nothing to title it.
export const Th = ({ children, right }: { children?: ReactNode; right?: boolean }) => (
  <th
    scope="col"
    className={cx(
      "border-b border-border pb-2 text-xs font-semibold uppercase tracking-wide text-ink-muted",
      right ? "text-right" : "text-left",
    )}
  >
    {children}
  </th>
);

export const Td = ({ children, right }: { children: ReactNode; right?: boolean }) => (
  <td className={cx("border-b border-border py-2.5", right && "text-right tabular")}>{children}</td>
);

// ---------------------------------------------------------------------------------------------

export const Skeleton = ({ className }: { className?: string }) => (
  <div
    aria-hidden="true"
    className={cx("animate-pulse rounded bg-surface-sunken", className ?? "h-4 w-full")}
  />
);

export function Empty({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="py-10 text-center">
      <p className="text-sm font-medium">{title}</p>
      {hint && <p className="mt-1 text-sm text-ink-muted">{hint}</p>}
    </div>
  );
}

/** A failed query, said plainly. `role="alert"` so it is announced rather than merely rendered. */
export const ErrorNote = ({ children }: { children: ReactNode }) => (
  <p role="alert" className="rounded-lg bg-debit/10 px-3 py-2 text-sm text-debit">
    {children}
  </p>
);

export const Badge = ({ children, tone = "neutral" }: { children: ReactNode; tone?: "neutral" | "good" | "bad" | "pending" }) => {
  const styles = {
    neutral: "bg-surface-sunken text-ink-muted",
    good: "bg-credit/12 text-credit",
    bad: "bg-debit/12 text-debit",
    pending: "bg-brand/12 text-brand",
  }[tone];
  return (
    <span className={cx("inline-block rounded-full px-2 py-0.5 text-xs font-medium", styles)}>
      {children}
    </span>
  );
};
