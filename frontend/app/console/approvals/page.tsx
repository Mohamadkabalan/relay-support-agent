"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";

import { Badge, Button, Dialog, ErrorText, Field, inputClass } from "@/components/ui";
import { api, message, type Schemas, unwrap } from "@/lib/api/client";
import { cents, humanize, when } from "@/lib/format";

type Approval = Schemas["ApprovalRow"];

export default function Approvals() {
  const [showAll, setShowAll] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const queue = useQuery({
    queryKey: ["approvals", showAll ? "all" : "pending"],
    queryFn: () =>
      unwrap(
        api.GET("/api/console/approvals", {
          params: { query: showAll ? {} : { status: "pending" } },
        }),
      ),
    refetchInterval: 5000,
  });
  const chosen = queue.data?.find((row) => row.approval_number === selected) ?? null;
  return (
    <>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-2xl font-semibold">Approvals</h1>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={showAll} onChange={(e) => setShowAll(e.target.checked)} />
          Show decided ones too
        </label>
      </div>
      <ErrorText>{queue.error ? message(queue.error) : null}</ErrorText>
      {queue.data?.length === 0 ? (
        <p className="text-sm text-slate-600">Nothing is waiting for a decision.</p>
      ) : null}
      <ul className="flex flex-col gap-2">
        {(queue.data ?? []).map((row) => (
          <li key={row.approval_number}>
            <button
              type="button"
              onClick={() => setSelected(row.approval_number)}
              className="flex w-full flex-col gap-1 rounded-lg border border-slate-200 bg-white p-3 text-left hover:border-blue-400 focus-visible:outline-2 focus-visible:outline-blue-700"
            >
              <span className="flex flex-wrap items-center gap-2">
                <span className="font-semibold">{row.approval_number}</span>
                <Badge value={row.status} />
                <span className="text-sm text-slate-700">{row.customer_name}</span>
                <span className="text-sm font-medium">{cents(row.amount_cents)}</span>
              </span>
              <span className="text-sm text-slate-800">{row.summary}</span>
              <span className="text-xs text-slate-600">
                Requested {when(row.requested_at)} · expires {when(row.expires_at)}
              </span>
            </button>
          </li>
        ))}
      </ul>
      <Dialog
        open={chosen !== null}
        onClose={() => setSelected(null)}
        title={chosen ? `Decide ${chosen.approval_number}` : "Decide"}
      >
        {chosen ? <Decision approval={chosen} onDone={() => setSelected(null)} /> : null}
      </Dialog>
    </>
  );
}

function Decision({ approval, onDone }: { approval: Approval; onDone: () => void }) {
  const queryClient = useQueryClient();
  const [note, setNote] = useState("");
  const [rejectWithoutNote, setRejectWithoutNote] = useState(false);
  const decide = useMutation({
    mutationFn: (decision: "approve" | "reject") =>
      unwrap(
        api.POST("/api/console/approvals/{approval_number}/decision", {
          params: { path: { approval_number: approval.approval_number } },
          body: { decision, note: note.trim() || null },
        }),
      ),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["approvals"] });
      onDone();
    },
  });
  const pending = approval.status === "pending";
  const reject = () => {
    if (!note.trim()) {
      setRejectWithoutNote(true);
      return;
    }
    decide.mutate("reject");
  };
  return (
    <>
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-sm">
        <dt className="text-slate-600">Customer</dt>
        <dd>{approval.customer_name}</dd>
        <dt className="text-slate-600">Action</dt>
        <dd>{humanize(approval.action)}</dd>
        <dt className="text-slate-600">Order</dt>
        <dd>{approval.order_number}</dd>
        <dt className="text-slate-600">Amount</dt>
        <dd className="font-medium">{cents(approval.amount_cents)}</dd>
        <dt className="text-slate-600">Why review</dt>
        <dd>
          {approval.reason} <span className="text-xs text-slate-600">({approval.rules.join(", ")})</span>
        </dd>
        <dt className="text-slate-600">Requested</dt>
        <dd>{when(approval.requested_at)}</dd>
        <dt className="text-slate-600">Status</dt>
        <dd>
          <Badge value={approval.status} />
          {approval.outcome ? <span className="ml-2">{approval.outcome}</span> : null}
        </dd>
      </dl>
      <p className="rounded-md bg-slate-100 p-3 text-sm">{approval.summary}</p>
      {approval.conversation_id ? (
        <Link
          className="text-sm text-blue-800 underline"
          href={`/console/conversations/${approval.conversation_id}`}
        >
          Open the conversation
        </Link>
      ) : null}
      {pending ? (
        <>
          <p className="text-sm text-slate-700">
            Approving re-checks the policy on the current order, then carries it out. A
            rejection needs a note: the agent explains it to the customer.
          </p>
          <Field label="Note" hint={rejectWithoutNote && !note.trim() ? undefined : "Optional when approving."}>
            <textarea
              className={inputClass}
              rows={3}
              maxLength={1000}
              value={note}
              onChange={(event) => setNote(event.target.value)}
              aria-invalid={rejectWithoutNote && !note.trim()}
            />
          </Field>
          {rejectWithoutNote && !note.trim() ? (
            <ErrorText>Write a note before rejecting.</ErrorText>
          ) : null}
          <div className="flex flex-wrap gap-2">
            <Button onClick={() => decide.mutate("approve")} disabled={decide.isPending}>
              Approve
            </Button>
            <Button tone="danger" onClick={reject} disabled={decide.isPending}>
              Reject
            </Button>
          </div>
          <ErrorText>{decide.error ? message(decide.error) : null}</ErrorText>
        </>
      ) : (
        <p className="text-sm text-slate-700">Decided {when(approval.decided_at)}.</p>
      )}
    </>
  );
}
