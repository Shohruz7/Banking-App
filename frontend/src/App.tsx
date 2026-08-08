/**
 * Routes and the authenticated shell.
 *
 * `RequireAuth` waits for the bootstrap before deciding. Redirecting on `!user` alone would bounce
 * every reload to the login screen for the moment it takes to spend the stored refresh token —
 * a logout that only looks like one, and the most irritating possible bug to leave in.
 */

import { Suspense, lazy } from "react";
import { NavLink, Navigate, Outlet, Route, Routes, useLocation } from "react-router-dom";

import { useAuth } from "./auth/AuthProvider";
import { Button, Skeleton, cx } from "./components/ui";
import { StreamProvider, useStream } from "./realtime/useStream";
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
  // dashboard — which matters most for a link somebody followed from an email.
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

      <main className="mx-auto max-w-5xl px-4 py-6">
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
  if (loading) return null;
  if (user) return <Navigate to="/" replace />;
  return <>{children}</>;
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

      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
