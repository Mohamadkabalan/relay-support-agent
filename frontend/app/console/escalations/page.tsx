"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";

import { Badge, Button, Dialog, ErrorText, Field, inputClass } from "@/components/ui";
import { api, message, type Schemas, unwrap } from "@/lib/api/client";
import { humanize, when } from "@/lib/format";

type Escalation = Schemas["EscalationRow"];

export default function Escalations() {
  const [showResolved, setShowResolved] = useState(false);
  const [selected, setSelected] = useState<Escalation | null>(null);
  const rows = useQuery({
    queryKey: ["escalations", showResolved],
    queryFn: () =>
      unwrap(
        api.GET("/api/console/escalations", {
          params: { query: showResolved ? {} : { status: "open" } },
        }),
      ),
    refetchInterval: 5000,
  });
  return (
    <>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-2xl font-semibold">Escalations</h1>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={showResolved} onChange={(e) => setShowResolved(e.target.checked)} />
          Show resolved ones too
        </label>
      </div>
      <ErrorText>{rows.error ? message(rows.error) : null}</ErrorText>
      {rows.data?.length === 0 ? <p className="text-sm text-slate-600">No open cases.</p> : null}
      <ul className="flex flex-col gap-2">
        {(rows.data ?? []).map((row) => (
          <li key={row.escalation_number} className="flex flex-col gap-2 rounded-lg border border-slate-200 bg-white p-3 sm:flex-row sm:items-center sm:justify-between">
            <div className="flex flex-col gap-1">
              <span className="flex flex-wrap items-center gap-2">
                <span className="font-semibold">{row.escalation_number}</span>
                <Badge value={row.status} />
                {row.priority === "high" ? <Badge value="high priority" /> : null}
                <span className="text-sm">{row.customer_name}</span>
              </span>
              <span className="text-sm text-slate-800">
                {humanize(row.reason)}: {row.summary}
              </span>
              <span className="text-xs text-slate-600">Opened {when(row.created_at)}</span>
            </div>
            <div className="flex gap-2">
              {row.conversation_id ? (
                <Link className="inline-flex min-h-10 items-center rounded-md px-3 text-sm text-blue-800 underline" href={`/console/conversations/${row.conversation_id}`}>
                  Conversation
                </Link>
              ) : null}
              {row.status === "open" ? (
                <Button tone="secondary" onClick={() => setSelected(row)}>
                  Resolve
                </Button>
              ) : null}
            </div>
          </li>
        ))}
      </ul>
      <Dialog open={selected !== null} onClose={() => setSelected(null)} title={selected ? `Resolve ${selected.escalation_number}` : "Resolve"}>
        {selected ? <Resolve escalation={selected} onDone={() => setSelected(null)} /> : null}
      </Dialog>
    </>
  );
}

function Resolve({ escalation, onDone }: { escalation: Escalation; onDone: () => void }) {
  const queryClient = useQueryClient();
  const [note, setNote] = useState("");
  const [handBack, setHandBack] = useState(true);
  const resolve = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/api/console/escalations/{escalation_number}/resolve", {
          params: { path: { escalation_number: escalation.escalation_number } },
          body: { note: note.trim() || null, hand_back: handBack },
        }),
      ),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["escalations"] });
      onDone();
    },
  });
  return (
    <>
      <p className="text-sm">{escalation.summary}</p>
      <Field label="Resolution note">
        <textarea className={inputClass} rows={3} maxLength={1000} value={note} onChange={(e) => setNote(e.target.value)} />
      </Field>
      <fieldset className="flex flex-col gap-2 text-sm">
        <legend className="font-medium">Then</legend>
        <label className="flex items-center gap-2">
          <input type="radio" name="after" checked={handBack} onChange={() => setHandBack(true)} />
          Hand the conversation back to the agent
        </label>
        <label className="flex items-center gap-2">
          <input type="radio" name="after" checked={!handBack} onChange={() => setHandBack(false)} />
          Close the conversation
        </label>
      </fieldset>
      <Button onClick={() => resolve.mutate()} disabled={resolve.isPending}>
        Resolve
      </Button>
      <ErrorText>{resolve.error ? message(resolve.error) : null}</ErrorText>
    </>
  );
}
