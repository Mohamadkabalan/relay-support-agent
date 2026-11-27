"use client";

// A conversation's live events (PLAN.md §5.10) over EventSource. The browser reconnects by
// itself and sends Last-Event-ID, so the server replays what was missed. Text deltas carry no
// ID and are never replayed; the completed message has the whole text.
import { useEffect, useRef, useState } from "react";

export type LiveEvent = { type: string; data: Record<string, unknown>; id: number | null };
export type Connection = "connecting" | "live" | "reconnecting";

const EVENT_TYPES = [
  "turn.started",
  "turn.completed",
  "status",
  "message.delta",
  "message.completed",
  "message.retracted",
  "approval.pending",
  "approval.resolved",
  "conversation.escalated",
  "conversation.updated",
  "guard.input",
  "error",
];

/** Calls `onEvent` for every event of the stream at `url`; null means not connected. */
export function useEventStream(
  url: string | null,
  onEvent: (event: LiveEvent) => void,
): Connection {
  const [connection, setConnection] = useState<Connection>("connecting");
  const handler = useRef(onEvent);
  useEffect(() => {
    handler.current = onEvent;
  });

  useEffect(() => {
    if (url === null) {
      return undefined;
    }
    const source = new EventSource(url, { withCredentials: true });
    source.onopen = () => setConnection("live");
    source.onerror = () => setConnection("reconnecting");
    const listener = (event: MessageEvent<string>) => {
      const id = event.lastEventId ? Number(event.lastEventId) : null;
      handler.current({ type: event.type, data: JSON.parse(event.data), id });
    };
    for (const type of EVENT_TYPES) {
      source.addEventListener(type, listener);
    }
    return () => source.close();
  }, [url]);

  return connection;
}
