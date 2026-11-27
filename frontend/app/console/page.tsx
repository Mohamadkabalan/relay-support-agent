"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";

import { Badge, ErrorText } from "@/components/ui";
import { api, message, unwrap } from "@/lib/api/client";
import { micros, when } from "@/lib/format";

const FILTERS = ["all", "active", "escalated", "closed"] as const;

export default function Conversations() {
  const [filter, setFilter] = useState<(typeof FILTERS)[number]>("all");
  const rows = useQuery({
    queryKey: ["conversations", filter],
    queryFn: () =>
      unwrap(
        api.GET("/api/console/conversations", {
          params: { query: filter === "all" ? {} : { status: filter } },
        }),
      ),
    refetchInterval: 5000,
  });
  return (
    <>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-2xl font-semibold">Live conversations</h1>
        <div role="group" aria-label="Filter by status" className="flex gap-1">
          {FILTERS.map((value) => (
            <button
              key={value}
              type="button"
              aria-pressed={filter === value}
              onClick={() => setFilter(value)}
              className={`rounded-full px-3 py-1 text-sm ring-1 focus-visible:outline-2 focus-visible:outline-blue-700 ${
                filter === value ? "bg-blue-700 text-white ring-blue-700" : "bg-white ring-slate-300"
              }`}
            >
              {value}
            </button>
          ))}
        </div>
      </div>
      <ErrorText>{rows.error ? message(rows.error) : null}</ErrorText>
      {rows.data?.length === 0 ? (
        <p className="text-sm text-slate-600">
          No conversations yet. Pick a customer on the store page and start a chat.
        </p>
      ) : null}
      <ul className="flex flex-col gap-2">
        {(rows.data ?? []).map((row) => (
          <li key={row.id}>
            <Link
              href={`/console/conversations/${row.id}`}
              className="flex flex-col gap-1 rounded-lg border border-slate-200 bg-white p-3 hover:border-blue-400 focus-visible:outline-2 focus-visible:outline-blue-700 sm:flex-row sm:items-center sm:justify-between"
            >
              <span className="flex flex-wrap items-center gap-2">
                <span className="font-medium">{row.customer_name}</span>
                <Badge value={row.status} />
                {row.pending_approvals ? <Badge value="pending" /> : null}
                {row.open_escalation ? (
                  <span className="text-xs text-orange-900">{row.open_escalation}</span>
                ) : null}
                {row.flags + row.injection_flags > 0 ? (
                  <span className="text-xs text-red-800">
                    {row.flags + row.injection_flags} flag{row.flags + row.injection_flags === 1 ? "" : "s"}
                  </span>
                ) : null}
              </span>
              <span className="flex flex-wrap gap-3 text-xs text-slate-600">
                <span>{row.turns} turns</span>
                <span>{micros(row.cost_micros)}</span>
                <span>last activity {when(row.last_activity_at)}</span>
              </span>
            </Link>
          </li>
        ))}
      </ul>
    </>
  );
}
