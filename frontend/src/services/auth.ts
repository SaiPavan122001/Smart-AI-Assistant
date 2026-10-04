// The API token lives in sessionStorage only (cleared when the tab closes) and is never logged.
const KEY = "saa.api-token";
const SESSION_KEY = "saa.session-id";

function safe<T>(fn: () => T, fallback: T): T {
  try {
    return fn();
  } catch {
    return fallback; // storage can be blocked (private mode, embedded views)
  }
}

export const getToken = (): string => safe(() => sessionStorage.getItem(KEY) ?? "", "");
export const setToken = (t: string): void => safe(() => sessionStorage.setItem(KEY, t.trim()), undefined);
export const clearToken = (): void => safe(() => sessionStorage.removeItem(KEY), undefined);

export function getSessionId(): string {
  return safe(() => {
    let id = sessionStorage.getItem(SESSION_KEY);
    if (!id) {
      id = `ui_${crypto.randomUUID().replace(/-/g, "").slice(0, 16)}`;
      sessionStorage.setItem(SESSION_KEY, id);
    }
    return id;
  }, "ui_anonymous");
}
