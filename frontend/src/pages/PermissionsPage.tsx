import { useEffect, useState } from "react";
import { ErrorBanner } from "../components/primitives";
import { useAsync } from "../hooks/useAsync";
import { api } from "../services/api";
import { RISK_LABEL, type Action, type PermissionPolicy, type RiskLevel, type TrustedOperation } from "../types/api";

const RISKS: RiskLevel[] = [1, 2, 3, 4];
const ACTIONS: Action[] = ["allow", "ask", "deny"];
const CAPABILITIES = ["api", "browser", "desktop", "filesystem", "terminal", "vision", "verification"];

export function PermissionsPage() {
  const policy = useAsync(() => api.getPermissions(), []);
  const tools = useAsync(() => api.tools(), []);
  const [draft, setDraft] = useState<PermissionPolicy | null>(null);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [newOp, setNewOp] = useState<TrustedOperation>({ tool: "", resource_glob: "*", max_risk: 3, note: "" });

  useEffect(() => {
    if (policy.data) setDraft(structuredClone(policy.data));
  }, [policy.data]);

  if (!draft) return <div className="page"><h1>Permissions</h1><ErrorBanner error={policy.error} /></div>;
  const toolNames = (tools.data ?? []).map((t) => t.name).sort();
  const edit = (fn: (d: PermissionPolicy) => void) => {
    const next = structuredClone(draft);
    fn(next);
    setDraft(next);
    setSaved(false);
  };

  const save = async () => {
    setError(null);
    try {
      const updated = await api.updatePermissions(draft);
      setDraft(updated);
      setSaved(true);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <div className="page">
      <h1>Permissions</h1>
      <p className="muted">
        Every action passes the permission engine before it runs. <strong>Critical</strong> actions always require approval and cannot be
        trusted or auto-allowed. Tasks that read content containing prompt-injection patterns ignore trusted operations and task-scoped approvals.
      </p>

      <section className="card">
        <h2>Default behaviour by risk</h2>
        <table aria-label="Risk policy">
          <tbody>
            {RISKS.map((r) => (
              <tr key={r}>
                <td><span className={`badge risk-${r}`}>{RISK_LABEL[r]}</span></td>
                <td style={{ width: 220 }}>
                  <select aria-label={`${RISK_LABEL[r]} risk action`} value={draft.risk_actions[String(r)] ?? "ask"} onChange={(e) => edit((d) => { d.risk_actions[String(r)] = e.target.value as Action; })}>
                    {ACTIONS.filter((a) => !(r === 4 && a === "allow")).map((a) => <option key={a} value={a}>{a === "ask" ? "ask me first" : a}</option>)}
                  </select>
                </td>
                <td className="muted">{r === 1 ? "reading, searching, opening" : r === 2 ? "creating/modifying files, downloads" : r === 3 ? "deleting, running code, installing" : "irreversible or sweeping changes"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="card">
        <h2>Disabled capabilities</h2>
        <div className="row">
          {CAPABILITIES.map((c) => (
            <label key={c} style={{ display: "flex", gap: 6, alignItems: "center", fontWeight: 400 }}>
              <input type="checkbox" style={{ width: "auto" }} checked={draft.disabled_capabilities.includes(c)} onChange={(e) => edit((d) => { d.disabled_capabilities = e.target.checked ? [...d.disabled_capabilities, c] : d.disabled_capabilities.filter((x) => x !== c); })} />
              {c}
            </label>
          ))}
        </div>
      </section>

      <section className="card">
        <h2>Per-tool overrides</h2>
        <table aria-label="Tool overrides">
          <tbody>
            {Object.entries(draft.tool_overrides).map(([tool, action]) => (
              <tr key={tool}>
                <td><code>{tool}</code></td>
                <td style={{ width: 200 }}>
                  <select value={action} onChange={(e) => edit((d) => { d.tool_overrides[tool] = e.target.value as Action; })}>
                    {ACTIONS.map((a) => <option key={a}>{a}</option>)}
                  </select>
                </td>
                <td style={{ width: 90 }}><button onClick={() => edit((d) => { delete d.tool_overrides[tool]; })}>Remove</button></td>
              </tr>
            ))}
            <tr>
              <td colSpan={3}>
                <select aria-label="Add override for tool" value="" onChange={(e) => e.target.value && edit((d) => { d.tool_overrides[e.target.value] = "ask"; })}>
                  <option value="">Add an override for a tool…</option>
                  {toolNames.filter((n) => !(n in draft.tool_overrides)).map((n) => <option key={n}>{n}</option>)}
                </select>
              </td>
            </tr>
          </tbody>
        </table>
      </section>

      <section className="card">
        <h2>Trusted operations</h2>
        <p className="muted">Skip approval for a specific tool on resources matching a pattern (never for critical risk, never in tainted tasks).</p>
        <table aria-label="Trusted operations">
          <thead><tr><th>Tool</th><th>Resource pattern</th><th>Max risk</th><th>Note</th><th /></tr></thead>
          <tbody>
            {draft.trusted_operations.map((op, i) => (
              <tr key={i}>
                <td><code>{op.tool}</code></td><td className="mono">{op.resource_glob}</td><td>{RISK_LABEL[op.max_risk]}</td><td>{op.note}</td>
                <td><button onClick={() => edit((d) => { d.trusted_operations.splice(i, 1); })}>Remove</button></td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="grid2" style={{ marginTop: 12 }}>
          <div><label htmlFor="op-tool">Tool</label>
            <select id="op-tool" value={newOp.tool} onChange={(e) => setNewOp({ ...newOp, tool: e.target.value })}>
              <option value="">Select…</option>{toolNames.map((n) => <option key={n}>{n}</option>)}
            </select></div>
          <div><label htmlFor="op-glob">Resource pattern</label><input id="op-glob" value={newOp.resource_glob} onChange={(e) => setNewOp({ ...newOp, resource_glob: e.target.value })} placeholder="C:\Users\me\Desktop\Spring Docs\*" /></div>
          <div><label htmlFor="op-risk">Max risk</label>
            <select id="op-risk" value={newOp.max_risk} onChange={(e) => setNewOp({ ...newOp, max_risk: Number(e.target.value) as RiskLevel })}>
              {[1, 2, 3].map((r) => <option key={r} value={r}>{RISK_LABEL[r as RiskLevel]}</option>)}
            </select></div>
          <div><label htmlFor="op-note">Note</label><input id="op-note" value={newOp.note} onChange={(e) => setNewOp({ ...newOp, note: e.target.value })} /></div>
        </div>
        <div style={{ marginTop: 10 }}>
          <button disabled={!newOp.tool || !newOp.resource_glob} onClick={() => { edit((d) => { d.trusted_operations.push(newOp); }); setNewOp({ tool: "", resource_glob: "*", max_risk: 3, note: "" }); }}>Add trusted operation</button>
        </div>
      </section>

      <ErrorBanner error={error} />
      {saved && <div className="banner ok" role="status">Saved. Policy version {draft.version}. Applies to running and future tasks.</div>}
      <div className="row">
        <button className="primary" onClick={() => void save()}>Save permissions</button>
        <span className="muted">Policy version {draft.version}</span>
      </div>
    </div>
  );
}
