"use client";

// The customer's chat (PLAN.md §5.10). The transcript comes from the API; the event stream adds
// what happens live: streaming text, status labels ("Checking your order…"), approval cards,
// retractions, and the end of each turn, which refreshes the transcript.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, type KeyboardEvent, useEffect, useRef, useState } from "react";

import { Badge, Button, ErrorText } from "@/components/ui";
import { api, message, type Schemas, unwrap } from "@/lib/api/client";
import { type LiveEvent, useEventStream } from "@/lib/events";

const MAX_CHARS = 2000;

type Bubble = Schemas["ChatMessage"];
type Approval = { number: string; summary: string; decision: string | null };

const DECISIONS: Record<string, string> = {
  executed: "Approved and done",
  rejected: "Declined by our team",
  failed: "Approved, but it couldn't be carried out",
  expired: "Not reviewed in time; passed to our team",
};

/** Find the open conversation, or start one. */
function useConversation(customerKey: string) {
  const queryClient = useQueryClient();
  return useQuery({
    queryKey: ["current-conversation", customerKey],
    queryFn: async () => {
      const mine = await unwrap(api.GET("/api/conversations"));
      const open = mine.find((c) => c.status !== "closed");
      if (open) return open.id;
      const created = await unwrap(api.POST("/api/conversations"));
      await queryClient.invalidateQueries({ queryKey: ["conversation"] });
      return created.id;
    },
    staleTime: Infinity,
  });
}

export function ChatWidget({ customerKey, name }: { customerKey: string; name: string }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      {open ? (
        <ChatPanel customerKey={customerKey} name={name} onClose={() => setOpen(false)} />
      ) : null}
      {!open ? (
        <Button
          className="fixed bottom-4 right-4 z-30 rounded-full px-5 py-3 shadow-lg"
          onClick={() => setOpen(true)}
        >
          Chat with support
        </Button>
      ) : null}
    </>
  );
}

function ChatPanel({
  customerKey,
  name,
  onClose,
}: {
  customerKey: string;
  name: string;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const current = useConversation(customerKey);
  const conversationId = current.data ?? null;
  const conversation = useQuery({
    queryKey: ["conversation", conversationId],
    queryFn: () =>
      unwrap(
        api.GET("/api/conversations/{conversation_id}", {
          params: { path: { conversation_id: conversationId ?? "" } },
        }),
      ),
    enabled: conversationId !== null,
  });

  const [live, setLive] = useState<Record<number, Bubble>>({});
  const [retracted, setRetracted] = useState<Set<number>>(new Set());
  const [streaming, setStreaming] = useState("");
  const [status, setStatus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [pending, setPending] = useState<{ baseline: number; texts: string[] }>({
    baseline: 0,
    texts: [],
  });

  const refresh = () =>
    queryClient.invalidateQueries({ queryKey: ["conversation", conversationId] });

  const onEvent = (event: LiveEvent) => {
    const data = event.data;
    switch (event.type) {
      case "turn.started":
        setBusy(true);
        break;
      case "status":
        setStatus(String(data.label));
        break;
      case "message.delta":
        setStatus(null);
        setStreaming((text) => text + String(data.text));
        break;
      case "message.completed": {
        const seq = Number(data.message_seq);
        const author = data.author === "team" ? "team" : "assistant";
        const bubble: Bubble = {
          seq,
          author,
          text: String(data.text),
          created_at: new Date().toISOString(),
          notice: (data.notice as string | undefined) ?? null,
        };
        setLive((all) => ({ ...all, [seq]: bubble }));
        setStreaming("");
        break;
      }
      case "message.retracted":
        setRetracted((all) => new Set(all).add(Number(data.message_seq)));
        break;
      case "approval.pending":
        setApprovals((all) =>
          all.some((a) => a.number === data.approval_number)
            ? all
            : [
                ...all,
                {
                  number: String(data.approval_number),
                  summary: String(data.summary),
                  decision: null,
                },
              ],
        );
        break;
      case "approval.resolved":
        setApprovals((all) =>
          all.map((a) =>
            a.number === data.approval_number ? { ...a, decision: String(data.decision) } : a,
          ),
        );
        break;
      case "turn.completed":
        setBusy(false);
        setStatus(null);
        setStreaming("");
        void refresh();
        break;
      case "conversation.escalated":
      case "conversation.updated":
        void refresh();
        break;
    }
  };

  const connection = useEventStream(
    conversationId ? `/api/conversations/${conversationId}/events?after=0` : null,
    onEvent,
  );

  const server = conversation.data?.messages ?? [];
  const known = new Set(server.map((m) => m.seq));
  const bubbles = [
    ...server,
    ...Object.values(live).filter((m) => !known.has(m.seq)),
  ]
    .filter((m) => !retracted.has(m.seq))
    .sort((a, b) => a.seq - b.seq);
  const customerCount = server.filter((m) => m.author === "customer").length;
  const unconfirmed = pending.texts.slice(Math.max(0, customerCount - pending.baseline));

  const send = useMutation({
    mutationFn: (text: string) =>
      unwrap(
        api.POST("/api/conversations/{conversation_id}/messages", {
          params: { path: { conversation_id: conversationId ?? "" } },
          body: { text },
        }),
      ),
  });
  const rate = useMutation({
    mutationFn: (rating: "up" | "down") =>
      unwrap(
        api.POST("/api/conversations/{conversation_id}/feedback", {
          params: { path: { conversation_id: conversationId ?? "" } },
          body: { rating },
        }),
      ),
    onSuccess: () => refresh(),
  });
  const startOver = useMutation({
    mutationFn: async () => {
      await unwrap(api.POST("/api/conversations"));
    },
    onSuccess: () => {
      setLive({});
      setApprovals([]);
      setPending({ baseline: 0, texts: [] });
      return queryClient.invalidateQueries({ queryKey: ["current-conversation", customerKey] });
    },
  });

  const [draft, setDraft] = useState("");
  const input = useRef<HTMLTextAreaElement>(null);
  const end = useRef<HTMLDivElement>(null);
  const ready = conversationId !== null;
  useEffect(() => {
    if (ready) input.current?.focus();
  }, [ready]);
  useEffect(() => {
    end.current?.scrollIntoView({ block: "end" });
  }, [bubbles.length, streaming, unconfirmed.length, approvals.length]);

  const closed = conversation.data?.status === "closed";
  const escalated = conversation.data?.status === "escalated";
  const tooLong = draft.length > MAX_CHARS;

  const submit = (event?: FormEvent) => {
    event?.preventDefault();
    const text = draft.trim();
    if (!text || tooLong || conversationId === null || closed) return;
    send.mutate(text, {
      onSuccess: () => {
        setDraft("");
        setPending((p) =>
          unconfirmed.length === 0
            ? { baseline: customerCount, texts: [text] }
            : { ...p, texts: [...p.texts, text] },
        );
      },
    });
  };
  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  };

  return (
    <section
      aria-label="Support chat"
      onKeyDown={(event) => {
        if (event.key === "Escape") onClose();
      }}
      className="fixed inset-0 z-40 flex flex-col bg-white sm:inset-auto sm:bottom-4 sm:right-4 sm:h-[38rem] sm:w-[26rem] sm:rounded-xl sm:border sm:border-slate-200 sm:shadow-2xl"
    >
      <header className="flex items-center justify-between gap-2 border-b border-slate-200 px-4 py-3">
        <div>
          <h2 className="font-semibold">Pixel &amp; Volt support</h2>
          <p className="text-xs text-slate-600">
            Signed in as {name}
            {connection === "reconnecting" ? " · Reconnecting…" : ""}
          </p>
        </div>
        <Button tone="ghost" onClick={onClose} aria-label="Close the chat">
          ✕
        </Button>
      </header>

      {escalated ? (
        <p className="bg-orange-50 px-4 py-2 text-sm text-orange-950">
          A member of our team is handling this conversation and will reply here.
        </p>
      ) : null}

      <div role="log" aria-live="polite" className="flex flex-1 flex-col gap-3 overflow-y-auto p-4">
        {current.error ? <ErrorText>{message(current.error)}</ErrorText> : null}
        {bubbles.length === 0 && unconfirmed.length === 0 ? (
          <p className="text-sm text-slate-600">
            Hi {name.split(" ")[0]}! Ask about an order, a delivery, a return or a refund.
          </p>
        ) : null}
        {bubbles.map((bubble) => (
          <ChatBubble key={bubble.seq} bubble={bubble} />
        ))}
        {unconfirmed.map((text, index) => (
          <ChatBubble
            key={`pending-${index}`}
            bubble={{ seq: -1, author: "customer", text, created_at: "", notice: null }}
          />
        ))}
        {approvals.map((approval) => (
          <ApprovalCard key={approval.number} approval={approval} />
        ))}
        {streaming ? (
          <ChatBubble
            bubble={{ seq: -1, author: "assistant", text: streaming, created_at: "", notice: null }}
          />
        ) : null}
        {busy && !streaming ? (
          <p className="text-sm italic text-slate-600" aria-live="polite">
            {status ?? "Typing…"}
          </p>
        ) : null}
        <div ref={end} />
      </div>

      {bubbles.some((b) => b.author === "assistant") && !busy ? (
        <div className="flex items-center gap-2 border-t border-slate-100 px-4 py-2 text-sm text-slate-700">
          <span>Was this helpful?</span>
          <Button
            tone={conversation.data?.rating === "up" ? "primary" : "secondary"}
            aria-pressed={conversation.data?.rating === "up"}
            aria-label="Thumbs up"
            onClick={() => rate.mutate("up")}
          >
            👍
          </Button>
          <Button
            tone={conversation.data?.rating === "down" ? "primary" : "secondary"}
            aria-pressed={conversation.data?.rating === "down"}
            aria-label="Thumbs down"
            onClick={() => rate.mutate("down")}
          >
            👎
          </Button>
        </div>
      ) : null}

      {closed ? (
        <div className="flex items-center justify-between gap-2 border-t border-slate-200 p-4 text-sm">
          <span>This conversation is closed.</span>
          <Button onClick={() => startOver.mutate()}>Start a new one</Button>
        </div>
      ) : (
        <form onSubmit={submit} className="flex flex-col gap-1 border-t border-slate-200 p-3">
          <label htmlFor="chat-input" className="sr-only">
            Your message
          </label>
          <div className="flex items-end gap-2">
            <textarea
              id="chat-input"
              ref={input}
              rows={2}
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={onKeyDown}
              disabled={!ready}
              placeholder={ready ? "Type a message" : "Starting the chat…"}
              className="min-h-11 flex-1 resize-none rounded-md border border-slate-300 px-3 py-2 text-sm focus-visible:outline-2 focus-visible:outline-blue-700"
            />
            <Button type="submit" disabled={!ready || !draft.trim() || tooLong || send.isPending}>
              Send
            </Button>
          </div>
          <div className="flex justify-between text-xs">
            <ErrorText>{send.error ? message(send.error) : tooLong ? "That message is too long." : null}</ErrorText>
            <span className={tooLong ? "text-red-700" : "text-slate-500"}>
              {draft.length}/{MAX_CHARS}
            </span>
          </div>
        </form>
      )}
    </section>
  );
}

function ChatBubble({ bubble }: { bubble: Bubble }) {
  const mine = bubble.author === "customer";
  const team = bubble.author === "team";
  return (
    <div className={`flex ${mine ? "justify-end" : "justify-start"}`}>
      <div
        className={`max-w-[85%] whitespace-pre-wrap rounded-2xl px-3 py-2 text-sm ${
          mine
            ? "bg-blue-700 text-white"
            : team
              ? "border border-orange-200 bg-orange-50 text-slate-900"
              : "bg-slate-100 text-slate-900"
        }`}
      >
        {team ? <span className="mb-1 block text-xs font-semibold">Support team</span> : null}
        {bubble.text}
      </div>
    </div>
  );
}

function ApprovalCard({ approval }: { approval: Approval }) {
  const decided = approval.decision !== null;
  return (
    <div
      className="rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm"
      aria-label={`Request ${approval.number}`}
    >
      <div className="mb-1 flex items-center justify-between gap-2">
        <span className="font-semibold">Request {approval.number}</span>
        <Badge value={approval.decision ?? "pending"} />
      </div>
      <p>{approval.summary}</p>
      <p className="mt-1 text-xs text-slate-700">
        {decided
          ? (DECISIONS[approval.decision ?? ""] ?? approval.decision)
          : "Waiting for a team member to review it. You'll see the decision here."}
      </p>
    </div>
  );
}
