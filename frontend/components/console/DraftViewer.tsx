"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { Badge, Button, ErrorText } from "@/components/ui";
import { api, message, type Schemas, unwrap } from "@/lib/api/client";
import { micros, when } from "@/lib/format";

type Draft = Schemas["DraftView"];

/** One draft: polled while it is generating, then its YAML, rationale and problems. */
export function DraftViewer({ id, initial }: { id: string; initial?: Draft }) {
  const draft = useQuery({
    queryKey: ["draft", id],
    queryFn: () =>
      unwrap(
        api.GET("/api/console/scenario-drafts/{draft_id}", {
          params: { path: { draft_id: id } },
        }),
      ),
    initialData: initial,
    refetchInterval: (query) => (query.state.data?.status === "generating" ? 1500 : false),
  });
  const [copied, setCopied] = useState(false);
  const data = draft.data;
  if (!data) {
    return <ErrorText>{draft.error ? message(draft.error) : null}</ErrorText>;
  }
  const copy = async () => {
    await navigator.clipboard.writeText(data.yaml ?? "");
    setCopied(true);
  };
  return (
    <div className="flex flex-col gap-3" aria-live="polite">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <Badge value={data.status} />
        <span className="text-slate-600">
          {when(data.created_at)} · {data.model}
          {data.cost_micros != null ? ` · ${micros(data.cost_micros)}` : ""}
        </span>
      </div>
      <p className="text-sm">
        <span className="font-medium">Operator&apos;s note:</span> {data.note}
      </p>
      {data.status === "generating" ? (
        <p className="text-sm text-slate-700">Claude is drafting the scenario. This takes up to a minute.</p>
      ) : null}
      {data.rationale ? <p className="text-sm text-slate-700">{data.rationale}</p> : null}
      {data.problems.length ? (
        <div className="rounded-md border border-red-200 bg-red-50 p-3 text-sm">
          <p className="font-medium text-red-900">Problems</p>
          <ul className="list-disc pl-5 text-red-900">
            {data.problems.map((problem) => (
              <li key={problem}>{problem}</li>
            ))}
          </ul>
        </div>
      ) : null}
      {data.yaml ? (
        <>
          <pre className="max-h-96 overflow-auto rounded-md bg-slate-900 p-3 text-xs text-slate-100">
            {data.yaml}
          </pre>
          <div className="flex items-center gap-2">
            <Button tone="secondary" onClick={() => void copy()}>
              Copy YAML
            </Button>
            {copied ? <span className="text-sm text-slate-600">Copied.</span> : null}
          </div>
          <p className="text-xs text-slate-600">
            Valid drafts are written to evals/scenarios/drafts/ by <code>make eval-import-drafts</code>;
            a reviewed draft joins the suite when you move it up a level.
          </p>
        </>
      ) : null}
    </div>
  );
}
