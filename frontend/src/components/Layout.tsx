import { NavLink, Outlet } from "react-router-dom";
import { useAgent } from "../hooks/AgentContext";
import { clearToken } from "../services/auth";
import { EmergencyStop } from "./EmergencyStop";

const NAV = [
  { to: "/", label: "Dashboard", end: true },
  { to: "/history", label: "Task history" },
  { to: "/logs", label: "Execution logs" },
  { to: "/permissions", label: "Permissions" },
  { to: "/settings", label: "Settings" },
];

export function Layout({ onSignOut }: { onSignOut: () => void }) {
  const { status, error } = useAgent();
  return (
    <div className="shell">
      <header className="topbar">
        <div className="brand"><span className="brand-mark" aria-hidden>✓</span> Smart AI Assistant</div>
        <span className="spacer" />
        {error && <span className="badge err" title={error}>backend unreachable</span>}
        {status && status.pending_approvals > 0 && <span className="badge warn">{status.pending_approvals} approval(s) waiting</span>}
        {status && <span className="muted">{status.active_tasks.length} running</span>}
        <EmergencyStop />
        <button onClick={() => { clearToken(); onSignOut(); }} title="Forget the API token for this tab">Sign out</button>
      </header>
      <nav className="sidebar" aria-label="Main">
        {NAV.map((n) => (
          <NavLink key={n.to} to={n.to} end={n.end} className={({ isActive }) => (isActive ? "active" : "")}>
            {n.label}
          </NavLink>
        ))}
      </nav>
      <main className="main">
        <Outlet />
      </main>
    </div>
  );
}
