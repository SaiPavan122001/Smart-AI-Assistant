import { ErrorBanner, money } from "../components/primitives";
import { useAsync } from "../hooks/useAsync";
import { api } from "../services/api";

function Section({ title, data }: { title: string; data: unknown }) {
  if (typeof data !== "object" || data === null) {
    return <div className="kv"><span className="k">{title}</span><span className="v">{String(data)}</span></div>;
  }
  return (
    <section className="card">
      <h3 style={{ textTransform: "capitalize" }}>{title}</h3>
      <table>
        <tbody>
          {Object.entries(data as Record<string, unknown>).map(([k, v]) => (
            <tr key={k}>
              <td style={{ width: 220 }} className="muted">{k.replace(/_/g, " ")}</td>
              <td className="mono">{Array.isArray(v) ? v.join(", ") || "—" : typeof v === "boolean" ? (v ? "yes" : "no") : String(v ?? "—")}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

export function SettingsPage() {
  const settings = useAsync(() => api.settings(), []);
  const ready = useAsync(() => api.ready(), [], 10000);
  const metrics = useAsync(() => api.metrics(), [], 10000);
  const m = metrics.data;

  return (
    <div className="page">
      <h1>Settings</h1>
      <p className="muted">
        Effective, non-secret configuration. It is read-only here by design: change it with environment variables (see <code>.env.example</code>) and restart.
        API keys are never sent to the browser.
      </p>
      <ErrorBanner error={settings.error ?? ready.error} />
      {ready.data && (
        <section className="card">
          <h3>Readiness</h3>
          <div className="row">
            {Object.entries(ready.data.checks).map(([k, v]) => (
              <span key={k} className={`badge ${v === true ? "ok" : v === false ? "warn" : ""}`}>{k.replace(/_/g, " ")}: {String(v)}</span>
            ))}
          </div>
          {ready.data.checks.llm_configured === false && (
            <div className="banner warn" style={{ marginTop: 10 }}>No LLM provider is configured, so tasks cannot be planned. Set <code>SAA_LLM__PROVIDER</code> and an API key.</div>
          )}
        </section>
      )}
      {m && (
        <section className="card">
          <h3>Agent metrics (this process)</h3>
          <div className="statusbar">
            <div className="kv"><span className="k">Tasks finished</span><span className="v">{m.tasks_finished}</span></div>
            <div className="kv"><span className="k">Success rate</span><span className="v">{Math.round((m.task_success_rate ?? 0) * 100)}%</span></div>
            <div className="kv"><span className="k">Avg duration</span><span className="v">{(m.average_task_duration_s ?? 0).toFixed(1)}s</span></div>
            <div className="kv"><span className="k">Avg LLM latency</span><span className="v">{Math.round(m.average_llm_latency_ms ?? 0)} ms</span></div>
          </div>
          <p className="muted" style={{ marginBottom: 0 }}>Prometheus metrics (cost: {money(undefined)} per task shown in history) are served at <code>/metrics</code>.</p>
        </section>
      )}
      <div className="grid2">
        {Object.entries(settings.data ?? {}).map(([k, v]) => <Section key={k} title={k} data={v} />)}
      </div>
    </div>
  );
}
