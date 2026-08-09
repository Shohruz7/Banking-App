/**
 * Routes and the authenticated shell.
 *
 * `RequireAuth` waits for the bootstrap before deciding. Redirecting on `!user` alone would bounce
 * every reload to the login screen for the moment it takes to spend the stored refresh token —
 * a logout that only looks like one, and the most irritating possible bug to leave in.
 */

import { Suspense, lazy } from "react";
import { Link, NavLink, Navigate, Outlet, Route, Routes, useLocation } from "react-router-dom";

import { useAuth } from "./auth/AuthProvider";
import { Button, Skeleton, cx } from "./components/ui";
import { StreamProvider, useStream } from "./realtime/useStream";
import { usePageTitle } from "./usePageTitle";
import AccountDetail from "./routes/AccountDetail";
import Dashboard from "./routes/Dashboard";
import Login from "./routes/Login";
import Markets from "./routes/Markets";
import Orders from "./routes/Orders";
import Portfolio from "./routes/Portfolio";
import Register from "./routes/Register";
import Settings from "./routes/Settings";
import Statements from "./routes/Statements";
import Transfer from "./routes/Transfer";

// Split out because it pulls in Recharts, which is over half the bundle on its own. Nothing else
// imports the charting library, so this one boundary keeps it off the critical path for every
// screen that is not a price chart.
const InstrumentDetail = lazy(() => import("./routes/InstrumentDetail"));

const NAV = [
  { to: "/", label: "Dashboard", end: true },
  { to: "/transfer", label: "Transfer", end: false },
  { to: "/markets", label: "Markets", end: false },
  { to: "/portfolio", label: "Portfolio", end: false },
  { to: "/orders", label: "Orders", end: false },
  { to: "/statements", label: "Statements", end: false },
  { to: "/settings", label: "Settings", end: false },
];

function RequireAuth() {
  const { user, loading } = useAuth();
  const location = useLocation();

  if (loading) {
    return (
      <div className="mx-auto max-w-5xl p-6">
        <Skeleton className="h-32" />
      </div>
    );
  }

  // `state` carries where they were headed, so signing in returns them there rather than to the
  // dashboard — which matters most for a link somebody followed from an email. `Login` reads it
  // back and validates it as a same-origin path before navigating.
  if (!user) return <Navigate to="/login" replace state={{ from: location }} />;

  return (
    <StreamProvider>
      <Shell />
    </StreamProvider>
  );
}

function LiveDot() {
  const { connected } = useStream();
  return (
    <span className="flex items-center gap-1.5 text-xs text-ink-muted">
      <span
        aria-hidden="true"
        className={cx("size-2 rounded-full", connected ? "bg-credit" : "bg-border")}
      />
      {/* Announced, not just coloured — the dot alone means nothing to a screen reader. */}
      <span>{connected ? "Live" : "Offline"}</span>
    </span>
  );
}

function Shell() {
  const { user, signOut } = useAuth();

  return (
    <div className="min-h-dvh">
      {/* Seven nav links, a status dot, a username and a sign-out button sit before the content on
          every single screen. Without this a keyboard user tabs through all of them each time they
          navigate. Visually hidden until focused, which is the only state it needs to exist in. */}
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:z-50 focus:m-3 focus:rounded-lg focus:bg-brand focus:px-4 focus:py-2 focus:text-brand-ink"
      >
        Skip to content
      </a>

      <header className="border-b border-border bg-surface">
        <div className="mx-auto flex max-w-5xl flex-wrap items-center gap-x-6 gap-y-3 px-4 py-3">
          <span className="font-semibold">Banking</span>
          <nav className="flex flex-1 flex-wrap gap-x-4 gap-y-1 text-sm">
            {NAV.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) =>
                  cx(
                    "rounded px-1 py-0.5 focus-visible:outline-2 focus-visible:outline-brand",
                    isActive ? "font-medium text-brand" : "text-ink-muted hover:text-ink",
                  )
                }
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
          <LiveDot />
          <span className="text-sm text-ink-muted">{user?.username}</span>
          <Button variant="secondary" onClick={() => void signOut()}>
            Sign out
          </Button>
        </div>
      </header>

      <main id="main" className="mx-auto max-w-5xl px-4 py-6">
        <Suspense fallback={<Skeleton className="h-64" />}>
          <Outlet />
        </Suspense>
      </main>
    </div>
  );
}

/** Already signed in? The login screen is not where you want to be. */
function RedirectIfAuthed({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth();
  // A skeleton rather than `null`. Hard-refreshing on /login with a stored session rendered a
  // completely blank page for the length of the token exchange — not even the box `RequireAuth`
  // shows for the same wait.
  if (loading) {
    return (
      <div className="mx-auto max-w-md p-6">
        <Skeleton className="h-32" />
      </div>
    );
  }
  if (user) return <Navigate to="/" replace />;
  return <>{children}</>;
}

/**
 * An unknown URL, said out loud.
 *
 * This used to be a silent `<Navigate to="/">`, which was worse than it looks: it discarded the URL
 * before anything could preserve it, so a signed-out user following a mistyped deep link was sent
 * to `/`, then bounced to `/login` carrying `/` as their intended destination. Rendering the 404 in
 * place keeps the address bar honest and leaves the back button working.
 */
function NotFound() {
  usePageTitle("Page not found");
  const location = useLocation();

  return (
    <main id="main" className="mx-auto flex min-h-dvh max-w-lg flex-col justify-center gap-4 p-6">
      <div className="rounded-xl border border-border bg-surface p-6">
        <h1 className="text-lg font-semibold">Page not found</h1>
        <p className="mt-2 text-sm text-ink-muted">
          Nothing lives at <code className="text-ink">{location.pathname}</code>.
        </p>
        <Link
          to="/"
          className="mt-5 inline-block rounded-lg bg-brand px-4 py-2 text-sm font-medium text-brand-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
        >
          Back to dashboard
        </Link>
      </div>
    </main>
  );
}

export default function App() {
  return (
    <Routes>
      <Route
        path="/login"
        element={
          <RedirectIfAuthed>
            <Login />
          </RedirectIfAuthed>
        }
      />
      <Route
        path="/register"
        element={
          <RedirectIfAuthed>
            <Register />
          </RedirectIfAuthed>
        }
      />

      <Route element={<RequireAuth />}>
        <Route path="/" element={<Dashboard />} />
        <Route path="/accounts/:id" element={<AccountDetail />} />
        <Route path="/transfer" element={<Transfer />} />
        <Route path="/markets" element={<Markets />} />
        <Route path="/markets/:symbol" element={<InstrumentDetail />} />
        <Route path="/portfolio" element={<Portfolio />} />
        <Route path="/orders" element={<Orders />} />
        <Route path="/statements" element={<Statements />} />
        <Route path="/settings" element={<Settings />} />
      </Route>

      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}
