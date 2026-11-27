"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";

import { DraftViewer } from "@/components/console/DraftViewer";
import { Badge, Card, ErrorText } from "@/components/ui";
import { api, message, unwrap } from "@/lib/api/client";
import { when } from "@/lib/format";

export default function Drafts() {
  const drafts = useQuery({
    queryKey: ["drafts"],
    queryFn: () => unwrap(api.GET("/api/console/scenario-drafts")),
    refetchInterval: 5000,
  });
  const [selected, setSelected] = useState<string | null>(null);
  const chosen = drafts.data?.find((d) => d.id === selected) ?? drafts.data?.[0] ?? null;
  return (
    <>
      <h1 className="text-2xl font-semibold">Scenario drafts</h1>
      <p className="max-w-3xl text-sm text-slate-700">
        Eval scenarios drafted from conversations with &quot;Create eval scenario&quot;. Open a
        conversation to make one.
      </p>
      <ErrorText>{drafts.error ? message(drafts.error) : null}</ErrorText>
      {drafts.data?.length === 0 ? <p className="text-sm text-slate-600">No drafts yet.</p> : null}
      {chosen ? (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-[18rem_1fr]">
          <ul className="flex flex-col gap-2" aria-label="Drafts">
            {(drafts.data ?? []).map((draft) => (
              <li key={draft.id}>
                <button
                  type="button"
                  aria-current={draft.id === chosen.id}
                  onClick={() => setSelected(draft.id)}
                  className={`flex w-full flex-col gap-1 rounded-lg border bg-white p-3 text-left text-sm focus-visible:outline-2 focus-visible:outline-blue-700 ${
                    draft.id === chosen.id ? "border-blue-600" : "border-slate-200"
                  }`}
                >
                  <span className="flex items-center gap-2">
                    <Badge value={draft.status} />
                    <span className="text-xs text-slate-600">{when(draft.created_at)}</span>
                  </span>
                  <span className="line-clamp-2">{draft.note}</span>
                </button>
              </li>
            ))}
          </ul>
          <Card className="flex flex-col gap-2">
            <Link className="text-sm text-blue-800 underline" href={`/console/conversations/${chosen.conversation_id}`}>
              The conversation it came from
            </Link>
            <DraftViewer key={chosen.id} id={chosen.id} initial={chosen} />
          </Card>
        </div>
      ) : null}
    </>
  );
}
