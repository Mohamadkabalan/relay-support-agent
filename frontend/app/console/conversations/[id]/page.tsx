"use client";

// One conversation, everything about it: the transcript as the model saw it, each turn's model
// and tool calls with tokens, cost and latency, the policy decisions in the tool results, trace
// links to Phoenix, and the operator's actions (reply, create an eval scenario).
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useParams } from "next/navigation";
import { type FormEvent, type ReactNode, useState } from "react";

import { DraftViewer } from "@/components/console/DraftViewer";
import { Badge, Button, Card, Dialog, ErrorText, Field, inputClass } from "@/components/ui";
import { api, message, type Schemas, unwrap } from "@/lib/api/client";
import { useEventStream } from "@/lib/events";
import { cents, humanize, micros, when } from "@/lib/format";

type Replay = Schemas["ConversationReplay"];
type Turn = Schemas["ReplayTurn"];
type Entry = Schemas["ReplayMessage"];

export default function ConversationReplay() {
  const { id } = useParams<{ id: string }>();
  const queryClient = useQueryClient();
  const replay = useQuery({
    queryKey: ["replay", id],
    queryFn: () =>
      unwrap(
        api.GET("/api/console/conversations/{conversation_id}", {
          params: { path: { conversation_id: id } },
        }),
      ),
  });
  const connection = useEventStream(`/api/console/conversations/${id}/events`, (event) => {
    if (event.type !== "message.delta" && event.type !== "status") {
      void queryClient.invalidateQueries({ queryKey: ["replay", id] });
    }
  });
  if (replay.error) return <ErrorText>{message(replay.error)}</ErrorText>;
  const data = replay.data;
  if (!data) return <p className="text-sm text-slate-600">Loading…</p>;
  return (
    <>
      <Header data={data} live={connection === "live"} />
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[1fr_20rem]">
        <Timeline data={data} />
        <aside className="flex flex-col gap-4">
          <Actions data={data} />
          <Requests data={data} />
        </aside>
      </div>
    </>
  );
}

function Header({ data, live }: { data: Replay; live: boolean }) {
  return (
    <Card className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-xl font-semibold">{data.customer_name}</h1>
        <Badge value={data.status} />
        <span className="text-sm text-slate-600">{data.customer_email}</span>
        <span className="text-xs text-slate-500">{live ? "● live" : "○ connecting"}</span>
      </div>
      <dl className="flex flex-wrap gap-x-6 gap-y-1 text-sm">
        <Fact label="Model" value={`${data.provider} ${data.model}${data.effort ? ` (${data.effort})` : ""}`} />
        <Fact label="Prompt" value={data.prompt_version} />
        <Fact label="Policy" value={data.policy_version} />
        <Fact label="Cost" value={`${micros(data.cost_micros)}${data.cost_complete ? "" : " (incomplete)"}`} />
        <Fact label="Started" value={when(data.created_at)} />
        <Fact label="Rating" value={data.rating == null ? "-" : data.rating > 0 ? "👍" : "👎"} />
        <Fact label="Injection flags" value={String(data.injection_flags)} />
      </dl>
      {data.flags.length ? (
        <ul className="flex flex-wrap gap-2 text-xs">
          {data.flags.map((flag, index) => (
            <li key={index} className="rounded bg-red-50 px-2 py-0.5 text-red-900 ring-1 ring-red-200">
              {String((flag as { type?: string }).type ?? "flag")} (turn {String((flag as { turn?: number }).turn ?? "?")})
            </li>
          ))}
        </ul>
      ) : null}
      {data.session_url ? (
        <a className="text-sm text-blue-800 underline" href={data.session_url} target="_blank" rel="noreferrer">
          All turns in Phoenix
        </a>
      ) : null}
    </Card>
  );
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex gap-1">
      <dt className="text-slate-600">{label}:</dt>
      <dd>{value}</dd>
    </div>
  );
}

function Timeline({ data }: { data: Replay }) {
  const turns = new Map(data.turns.map((turn) => [turn.id, turn]));
  const shown = new Set<string>();
  return (
    <ol className="flex flex-col gap-2" aria-label="Transcript">
      {data.messages.map((entry) => {
        const turn = entry.turn_id ? turns.get(entry.turn_id) : undefined;
        const header = turn && !shown.has(turn.id);
        if (turn) shown.add(turn.id);
        return (
          <li key={entry.seq} className="flex flex-col gap-2">
            {header ? <TurnHeader turn={turn} /> : null}
            <EntryView entry={entry} />
          </li>
        );
      })}
    </ol>
  );
}

function TurnHeader({ turn }: { turn: Turn }) {
  const tokens = turn.input_tokens + turn.output_tokens + turn.cache_read_tokens + turn.cache_write_tokens;
  return (
    <details className="mt-3 rounded-md border border-slate-300 bg-slate-100 px-3 py-2 text-sm">
      <summary className="flex cursor-pointer flex-wrap items-center gap-x-3 gap-y-1">
        <span className="font-semibold">Turn {turn.seq}</span>
        <Badge value={turn.status} />
        <span className="text-slate-700">{humanize(turn.trigger)}</span>
        <span className="text-slate-600">
          {plural(turn.steps, "model call")} · {plural(turn.tool_call_count, "tool call")} ·{" "}
          {tokens.toLocaleString()} tokens ·{" "}
          {micros(turn.cost_micros)}
          {turn.latency_ms != null ? ` · ${(turn.latency_ms / 1000).toFixed(1)} s` : ""}
          {turn.ttft_ms != null ? ` · first token ${turn.ttft_ms} ms` : ""}
        </span>
        {turn.trace_url ? (
          <a className="text-blue-800 underline" href={turn.trace_url} target="_blank" rel="noreferrer">
            Trace
          </a>
        ) : null}
      </summary>
      {turn.error ? <p className="mt-2 text-red-800">Error: {turn.error}</p> : null}
      <div className="mt-2 overflow-x-auto">
        <table className="w-full text-left text-xs">
          <caption className="sr-only">Model calls of turn {turn.seq}</caption>
          <thead>
            <tr className="text-slate-600">
              <th className="pr-3">Call</th>
              <th className="pr-3">Served model</th>
              <th className="pr-3">Stop</th>
              <th className="pr-3">Input</th>
              <th className="pr-3">Cache read</th>
              <th className="pr-3">Output</th>
              <th className="pr-3">Cost</th>
              <th>Latency</th>
            </tr>
          </thead>
          <tbody>
            {turn.model_calls.map((call) => (
              <tr key={call.seq}>
                <td className="pr-3">{call.seq}</td>
                <td className="pr-3">
                  {call.served_model}
                  {call.fallback_used ? " (fallback)" : ""}
                </td>
                <td className="pr-3">{call.stop_reason}</td>
                <td className="pr-3">{call.input_tokens.toLocaleString()}</td>
                <td className="pr-3">{call.cache_read_tokens.toLocaleString()}</td>
                <td className="pr-3">{call.output_tokens.toLocaleString()}</td>
                <td className="pr-3">{micros(call.cost_micros)}</td>
                <td>{call.latency_ms} ms</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {turn.model_calls.some((call) => call.thinking) ? (
        <div className="mt-2 flex flex-col gap-1">
          <p className="text-xs font-medium text-slate-700">Thinking (summarized)</p>
          {turn.model_calls.map((call) =>
            call.thinking ? (
              <p key={call.seq} className="whitespace-pre-wrap text-xs text-slate-700">
                {call.thinking}
              </p>
            ) : null,
          )}
        </div>
      ) : null}
    </details>
  );
}

function plural(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

function EntryView({ entry }: { entry: Entry }) {
  const content = entry.content as Record<string, unknown>;
  const text = typeof content.text === "string" ? content.text : "";
  switch (entry.kind) {
    case "customer":
      return <Line who="Customer" tone="bg-blue-50 ring-blue-200">{text}</Line>;
    case "assistant": {
      const calls = (content.tool_calls as { id: string; name: string; arguments: unknown }[] | undefined) ?? [];
      return (
        <Line who={content.written_by === "runtime" ? "Runtime (as the agent)" : "Agent"} tone="bg-white ring-slate-200">
          {text ? <p className="whitespace-pre-wrap">{text}</p> : null}
          {calls.map((call) => (
            <p key={call.id} className="mt-1 font-mono text-xs text-slate-700">
              → {call.name}({JSON.stringify(call.arguments)})
            </p>
          ))}
        </Line>
      );
    }
    case "tool_results": {
      const results = (content.results as { call_id: string; name: string; content: string; is_error: boolean }[]) ?? [];
      return (
        <div className="flex flex-col gap-1 pl-4">
          {results.map((result) => (
            <ToolResult key={result.call_id} {...result} />
          ))}
        </div>
      );
    }
    case "system_note":
      return <Line who="Trusted note to the agent" tone="bg-amber-50 ring-amber-200">{text}</Line>;
    case "event":
      return <Line who="Automated" tone="bg-slate-50 ring-slate-200">{text}</Line>;
    case "notice":
      return (
        <Line
          who={content.reason === "operator" ? `Operator (${String(content.author ?? "")})` : `Notice (${String(content.reason ?? "")})`}
          tone="bg-orange-50 ring-orange-200"
        >
          {text}
        </Line>
      );
    default:
      return null;
  }
}

function Line({ who, tone, children }: { who: string; tone: string; children: ReactNode }) {
  return (
    <div className={`rounded-md p-2 text-sm ring-1 ${tone}`}>
      <p className="text-xs font-medium text-slate-600">{who}</p>
      <div className="whitespace-pre-wrap">{children}</div>
    </div>
  );
}

/** A tool result, with the policy decision it carries pulled out (§7). */
function ToolResult({ name, content, is_error }: { name: string; content: string; is_error: boolean }) {
  let parsed: Record<string, unknown> | null = null;
  try {
    parsed = JSON.parse(content) as Record<string, unknown>;
  } catch {
    parsed = null;
  }
  const status = typeof parsed?.status === "string" ? parsed.status : null;
  const decision = status && status !== "ok" ? status : null;
  return (
    <details className={`rounded-md p-2 text-xs ring-1 ${is_error ? "bg-red-50 ring-red-200" : "bg-slate-50 ring-slate-200"}`}>
      <summary className="flex cursor-pointer flex-wrap items-center gap-2">
        <span className="font-mono">{name}</span>
        {is_error ? <Badge value="error" /> : null}
        {decision ? <Badge value={decision === "pending_approval" ? "pending" : decision} /> : null}
        {decision === "denied" ? (
          <span className="text-red-900">
            {String(parsed?.rule)}: {String(parsed?.explanation)}
          </span>
        ) : null}
        {decision === "preview" ? (
          <span>
            {String(parsed?.summary)}
            {parsed?.needs_operator_approval ? ` (needs approval: ${String(parsed?.approval_reason)})` : ""}
          </span>
        ) : null}
        {is_error ? <span className="text-red-900">{content.slice(0, 120)}</span> : null}
      </summary>
      <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-words">
        {parsed ? JSON.stringify(parsed, null, 2) : content}
      </pre>
    </details>
  );
}

function Actions({ data }: { data: Replay }) {
  return (
    <Card className="flex flex-col gap-3">
      <h2 className="font-semibold">Actions</h2>
      {data.status === "escalated" ? <Reply conversationId={data.id} /> : null}
      <CreateScenario conversationId={data.id} />
    </Card>
  );
}

function Reply({ conversationId }: { conversationId: string }) {
  const queryClient = useQueryClient();
  const [text, setText] = useState("");
  const send = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/api/console/conversations/{conversation_id}/messages", {
          params: { path: { conversation_id: conversationId } },
          body: { text },
        }),
      ),
    onSuccess: () => {
      setText("");
      return queryClient.invalidateQueries({ queryKey: ["replay", conversationId] });
    },
  });
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (text.trim()) send.mutate();
  };
  return (
    <form onSubmit={submit} className="flex flex-col gap-2">
      <Field label="Reply to the customer" hint="The agent sees it as a trusted note when it takes over again.">
        <textarea className={inputClass} rows={3} value={text} onChange={(e) => setText(e.target.value)} maxLength={2000} />
      </Field>
      <Button type="submit" disabled={!text.trim() || send.isPending}>
        Send reply
      </Button>
      <ErrorText>{send.error ? message(send.error) : null}</ErrorText>
    </form>
  );
}

function CreateScenario({ conversationId }: { conversationId: string }) {
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState("");
  const [draftId, setDraftId] = useState<string | null>(null);
  const create = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/api/console/conversations/{conversation_id}/scenario-drafts", {
          params: { path: { conversation_id: conversationId } },
          body: { note },
        }),
      ),
    onSuccess: (draft) => setDraftId(draft.id),
  });
  return (
    <>
      <Button tone="secondary" onClick={() => setOpen(true)}>
        Create eval scenario
      </Button>
      <Dialog open={open} onClose={() => setOpen(false)} title="Create eval scenario">
        {draftId ? (
          <DraftViewer id={draftId} />
        ) : (
          <form
            className="flex flex-col gap-3"
            onSubmit={(event) => {
              event.preventDefault();
              if (note.trim()) create.mutate();
            }}
          >
            <p className="text-sm text-slate-700">
              Claude turns this conversation into a scenario for the eval suite, with every name,
              email and address replaced. A draft that still holds real customer details fails
              validation.
            </p>
            <Field label="What should have happened?">
              <textarea className={inputClass} rows={4} maxLength={1000} value={note} onChange={(e) => setNote(e.target.value)} />
            </Field>
            <Button type="submit" disabled={!note.trim() || create.isPending}>
              Draft the scenario
            </Button>
            <ErrorText>{create.error ? message(create.error) : null}</ErrorText>
          </form>
        )}
      </Dialog>
    </>
  );
}

function Requests({ data }: { data: Replay }) {
  if (!data.approvals.length && !data.escalations.length) return null;
  return (
    <Card className="flex flex-col gap-2 text-sm">
      <h2 className="font-semibold">Requests</h2>
      {data.approvals.map((approval) => (
        <div key={approval.approval_number} className="flex flex-wrap items-center gap-2">
          <Link className="text-blue-800 underline" href="/console/approvals">
            {approval.approval_number}
          </Link>
          <Badge value={approval.status} />
          <span>{cents(approval.amount_cents)}</span>
        </div>
      ))}
      {data.escalations.map((escalation) => (
        <div key={escalation.escalation_number} className="flex flex-wrap items-center gap-2">
          <Link className="text-blue-800 underline" href="/console/escalations">
            {escalation.escalation_number}
          </Link>
          <Badge value={escalation.status} />
          <span>{humanize(escalation.reason)}</span>
        </div>
      ))}
    </Card>
  );
}
