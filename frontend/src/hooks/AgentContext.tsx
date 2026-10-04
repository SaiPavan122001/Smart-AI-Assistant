import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { api } from "../services/api";
import type { AgentStatus } from "../types/api";

interface Ctx {
  status: AgentStatus | null;
  refresh: () => Promise<void>;
  stop: () => Promise<void>;
  release: () => Promise<void>;
  error: string | null;
}

const AgentCtx = createContext<Ctx | null>(null);

export function AgentProvider({ children, pollMs = 2000 }: { children: ReactNode; pollMs?: number }) {
  const [status, setStatus] = useState<AgentStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setStatus(await api.agentStatus());
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const t = setInterval(() => void refresh(), pollMs);
    return () => clearInterval(t);
  }, [refresh, pollMs]);

  const stop = useCallback(async () => {
    await api.stopAgent(); // one click, no confirmation: an emergency stop must never be slowed down
    await refresh();
  }, [refresh]);
  const release = useCallback(async () => {
    await api.releaseAgent();
    await refresh();
  }, [refresh]);

  const value = useMemo(() => ({ status, refresh, stop, release, error }), [status, refresh, stop, release, error]);
  return <AgentCtx.Provider value={value}>{children}</AgentCtx.Provider>;
}

export function useAgent(): Ctx {
  const c = useContext(AgentCtx);
  if (!c) throw new Error("useAgent must be used inside <AgentProvider>");
  return c;
}
