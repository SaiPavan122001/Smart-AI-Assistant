import { useState, type FormEvent } from "react";
import { ErrorBanner } from "../components/primitives";
import { api } from "../services/api";
import { setToken } from "../services/auth";

export function LoginPage({ onSignedIn }: { onSignedIn: () => void }) {
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setToken(value);
    try {
      await api.agentStatus(); // any authenticated call proves the token
      onSignedIn();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="card login" onSubmit={submit} aria-label="Sign in">
      <h1>Smart AI Assistant</h1>
      <p className="muted">
        This agent can control your computer, so its API is protected. Paste the API token printed by the backend at startup
        (or the value of <code>SAA_SECURITY__API_TOKEN</code>). It is kept only in this browser tab.
      </p>
      <label htmlFor="token">API token</label>
      <input id="token" type="password" autoComplete="off" value={value} onChange={(e) => setValue(e.target.value)} placeholder="paste token" />
      <div style={{ height: 12 }} />
      <ErrorBanner error={error} />
      <div style={{ height: 12 }} />
      <button className="primary" type="submit" disabled={busy || !value.trim()}>
        {busy ? "Checking…" : "Connect"}
      </button>
    </form>
  );
}
