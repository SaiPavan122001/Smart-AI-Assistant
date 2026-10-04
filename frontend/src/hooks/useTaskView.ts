import { useEffect, useReducer, useRef, useState } from "react";
import { api } from "../services/api";
import { EventStream, type StreamState } from "../services/stream";
import { buildTaskView, emptyView, reduceEvent, type TaskView } from "../features/tasks/taskView";
import type { ExecutionEvent } from "../types/api";
import { isTerminal } from "../types/api";

type Action = { type: "event"; ev: ExecutionEvent } | { type: "reset"; taskId: string } | { type: "load"; view: TaskView };

function reducer(state: TaskView, a: Action): TaskView {
  switch (a.type) {
    case "event":
      return reduceEvent(state, a.ev);
    case "reset":
      return emptyView(a.taskId);
    case "load":
      return a.view;
  }
}

export interface UseTaskView {
  view: TaskView;
  connection: StreamState;
  error: string | null;
}

/**
 * Reconstructs a task's state from its event timeline.
 * - live=true: WebSocket stream with replay + reconnect (running tasks)
 * - live=false: one REST fetch (history/detail pages for finished tasks)
 */
export function useTaskView(taskId: string | null, live: boolean): UseTaskView {
  const [view, dispatch] = useReducer(reducer, emptyView(taskId ?? ""));
  const [connection, setConnection] = useState<StreamState>("closed");
  const [error, setError] = useState<string | null>(null);
  const streamRef = useRef<EventStream | null>(null);

  useEffect(() => {
    dispatch({ type: "reset", taskId: taskId ?? "" });
    setError(null);
    if (!taskId) return;
    let cancelled = false;

    if (!live) {
      api
        .events(taskId)
        .then((events) => !cancelled && dispatch({ type: "load", view: buildTaskView(taskId, events) }))
        .catch((e: Error) => !cancelled && setError(e.message));
      return () => {
        cancelled = true;
      };
    }

    const stream = new EventStream({
      path: `/ws/tasks/${taskId}`,
      onEvent: (ev) => dispatch({ type: "event", ev }),
      onState: (s) => !cancelled && setConnection(s),
    });
    streamRef.current = stream;
    stream.start();
    return () => {
      cancelled = true;
      stream.close();
    };
  }, [taskId, live]);

  return { view, connection, error };
}

export const isActive = (v: TaskView): boolean => !!v.taskId && !isTerminal(v.status);
