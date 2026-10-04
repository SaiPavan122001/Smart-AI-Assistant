import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, onUnauthorized } from "../src/services/api";
import { getToken, setToken } from "../src/services/auth";
import { EventStream } from "../src/services/stream";
import { ev, resetSeq } from "./fixtures";

const json = (body: unknown, status = 200) =>
  Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } }));

describe("api client", () => {
  beforeEach(() => setToken("tok-123"));
  afterEach(() => vi.restoreAllMocks());

  it("sends the bearer token and a session id, and parses JSON", async () => {
    const f = vi.spyOn(globalThis, "fetch").mockImplementation(() => json({ items: [], limit: 5, offset: 0 }));
    const out = await api.listTasks({ limit: 5, status: "FAILED" });
    expect(out.items).toEqual([]);
    const [url, init] = f.mock.calls[0]!;
    expect(url).toBe("/api/v1/tasks?limit=5&status=FAILED");
    const h = (init as RequestInit).headers as Record<string, string>;
    expect(h.Authorization).toBe("Bearer tok-123");
    expect(h["X-Session-Id"]).toMatch(/^ui_/);
  });

  it("posts JSON bodies", async () => {
    const f = vi.spyOn(globalThis, "fetch").mockImplementation(() => json({ id: "t1" }, 201));
    await api.createTask("do it");
    const init = f.mock.calls[0]![1] as RequestInit;
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ instruction: "do it" });
    expect((init.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
  });

  it("sends an Idempotency-Key so a repeated delivery starts one task, and a fresh key per user action", async () => {
    const f = vi.spyOn(globalThis, "fetch").mockImplementation(() => json({ id: "t1" }, 201));
    await api.createTask("do it");
    await api.createTask("do it");
    await api.createTask("do it", "fixed-key");
    const keys = f.mock.calls.map((c) => ((c[1] as RequestInit).headers as Record<string, string>)["Idempotency-Key"]);
    expect(keys[0]).toBeTruthy();
    expect(keys[0]).not.toBe(keys[1]);
    expect(keys[2]).toBe("fixed-key");
  });

  it("maps structured backend errors to ApiError", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(() =>
      json({ error: { code: "kill_switch_active", message: "The emergency stop is engaged.", request_id: "req_9" } }, 409),
    );
    const err: unknown = await api.createTask("x").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 409, code: "kill_switch_active", requestId: "req_9" });
    expect((err as ApiError).message).toBe("The emergency stop is engaged.");
  });

  it("clears the token and notifies listeners on 401", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(() => json({ error: { code: "unauthorized", message: "bad token" } }, 401));
    const listener = vi.fn();
    const off = onUnauthorized(listener);
    await expect(api.agentStatus()).rejects.toMatchObject({ unauthorized: true });
    expect(listener).toHaveBeenCalledTimes(1);
    expect(getToken()).toBe("");
    off();
  });

  it("reports an unreachable backend in plain words", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("fetch failed"));
    await expect(api.agentStatus()).rejects.toMatchObject({ code: "network_error", message: expect.stringContaining("Cannot reach") });
  });

  it("encodes query parameters", async () => {
    const f = vi.spyOn(globalThis, "fetch").mockImplementation(() => json([]));
    await api.logs("task_1", "ERROR");
    expect(f.mock.calls[0]![0]).toBe("/api/v1/tasks/task_1/logs?level=ERROR");
  });
});

// ------------------------------------------------------------------------------------------------
class FakeSocket {
  static instances: FakeSocket[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((m: MessageEvent) => void) | null = null;
  onclose: ((c: CloseEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  constructor(public url: string, public protocols: string[]) {
    FakeSocket.instances.push(this);
  }
  close() {
    this.closed = true;
  }
  open() {
    this.onopen?.();
  }
  send(e: unknown) {
    this.onmessage?.({ data: JSON.stringify(e) } as MessageEvent);
  }
  drop(code = 1006) {
    this.onclose?.({ code } as CloseEvent);
  }
}

describe("EventStream", () => {
  beforeEach(() => {
    FakeSocket.instances = [];
    setToken("secret-token");
    resetSeq();
    vi.useFakeTimers();
  });
  afterEach(() => vi.useRealTimers());

  const make = (onEvent: (e: unknown) => void, onState?: (s: string) => void) =>
    new EventStream({
      path: "/ws/tasks/task_1",
      onEvent,
      onState,
      socketFactory: (u, p) => new FakeSocket(u, p) as unknown as WebSocket,
    });

  it("authenticates through a subprotocol, never the URL", () => {
    make(() => {}).start();
    const s = FakeSocket.instances[0]!;
    expect(s.protocols).toEqual(["saa.v1", "bearer.secret-token"]);
    expect(s.url).not.toContain("secret-token");
    expect(s.url).toContain("/api/v1/ws/tasks/task_1?after_seq=0");
  });

  it("delivers events in order and drops duplicates", () => {
    const got: number[] = [];
    make((e) => got.push((e as { seq: number }).seq)).start();
    const s = FakeSocket.instances[0]!;
    s.open();
    const events = [ev("task_created"), ev("planning_started"), ev("plan_created")];
    events.forEach((e) => s.send(e));
    s.send(events[1]); // duplicate
    s.onmessage?.({ data: "pong" } as MessageEvent);
    s.onmessage?.({ data: "not json" } as MessageEvent);
    expect(got).toEqual([1, 2, 3]);
  });

  it("reconnects with backoff and resumes after the last seen sequence", () => {
    const states: string[] = [];
    const stream = make(() => {}, (s) => states.push(s));
    stream.start();
    const first = FakeSocket.instances[0]!;
    first.open();
    first.send(ev("task_created"));
    first.send(ev("planning_started"));
    first.drop();
    expect(states.at(-1)).toBe("reconnecting");
    vi.advanceTimersByTime(10_000);
    const second = FakeSocket.instances[1]!;
    expect(second.url).toContain("after_seq=2");
    second.open();
    expect(states.at(-1)).toBe("open");
  });

  it("does not hammer the server after a policy violation (bad token / origin)", () => {
    const stream = make(() => {});
    stream.start();
    FakeSocket.instances[0]!.drop(1008);
    vi.advanceTimersByTime(60_000);
    expect(FakeSocket.instances).toHaveLength(1);
    stream.close();
  });

  it("stops reconnecting once closed by the user", () => {
    const stream = make(() => {});
    stream.start();
    stream.close();
    FakeSocket.instances[0]!.drop();
    vi.advanceTimersByTime(60_000);
    expect(FakeSocket.instances).toHaveLength(1);
    expect(FakeSocket.instances[0]!.closed).toBe(true);
  });
});
