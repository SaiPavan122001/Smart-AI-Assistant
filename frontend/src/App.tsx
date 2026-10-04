import { useEffect, useState } from "react";
import { Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { AgentProvider } from "./hooks/AgentContext";
import { DashboardPage } from "./pages/DashboardPage";
import { HistoryPage } from "./pages/HistoryPage";
import { LoginPage } from "./pages/LoginPage";
import { LogsPage } from "./pages/LogsPage";
import { PermissionsPage } from "./pages/PermissionsPage";
import { SettingsPage } from "./pages/SettingsPage";
import { TaskDetailPage } from "./pages/TaskDetailPage";
import { onUnauthorized } from "./services/api";
import { getToken } from "./services/auth";

export default function App() {
  const [authed, setAuthed] = useState(() => !!getToken());
  useEffect(() => onUnauthorized(() => setAuthed(false)), []);

  if (!authed) return <LoginPage onSignedIn={() => setAuthed(true)} />;
  return (
    <AgentProvider>
      <Routes>
        <Route element={<Layout onSignOut={() => setAuthed(false)} />}>
          <Route index element={<DashboardPage />} />
          <Route path="history" element={<HistoryPage />} />
          <Route path="history/:id" element={<TaskDetailPage />} />
          <Route path="logs" element={<LogsPage />} />
          <Route path="permissions" element={<PermissionsPage />} />
          <Route path="settings" element={<SettingsPage />} />
          <Route path="*" element={<div className="page"><h1>Not found</h1></div>} />
        </Route>
      </Routes>
    </AgentProvider>
  );
}
