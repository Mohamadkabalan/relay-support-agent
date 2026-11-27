"use client";

// The dashboard (§10). Rates are stat tiles with their definitions; conversations per day is the
// one chart: a single series, so no legend, with a hover tooltip and the numbers as a table.
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { Card, ErrorText } from "@/components/ui";
import { api, message, type Schemas, unwrap } from "@/lib/api/client";
import { duration, micros, percent } from "@/lib/format";

type Rate = Schemas["Rate"];

const SERIES = "#257de0"; // slot 1 of the validated reference palette (light surface)

export default function Metrics() {
  const [days, setDays] = useState(14);
  const metrics = useQuery({
    queryKey: ["metrics", days],
    queryFn: () => unwrap(api.GET("/api/console/metrics", { params: { query: { days } } })),
    refetchInterval: 15000,
  });
  const data = metrics.data;
  return (
    <>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-2xl font-semibold">Metrics</h1>
        <label className="flex items-center gap-2 text-sm">
          Window
          <select className="rounded-md border border-slate-300 bg-white px-2 py-1" value={days} onChange={(e) => setDays(Number(e.target.value))}>
            {[1, 7, 14, 30, 90].map((value) => (
              <option key={value} value={value}>
                last {value} day{value === 1 ? "" : "s"}
              </option>
            ))}
          </select>
        </label>
      </div>
      <ErrorText>{metrics.error ? message(metrics.error) : null}</ErrorText>
      {data ? (
        <>
          <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Tile label="Conversations" value={String(data.conversations)} />
            <RateTile label="Resolution rate" rate={data.resolution_rate} definition={data.definitions.resolution_rate} />
            <RateTile label="Escalation rate" rate={data.escalation_rate} definition={data.definitions.escalation_rate} />
            <RateTile label="Thumbs up" rate={data.thumbs_up_rate} definition={data.definitions.thumbs_up_rate} />
            <Tile
              label="Approval turnaround"
              value={`${duration(data.approval_turnaround_s.p50)} median`}
              detail={`p90 ${duration(data.approval_turnaround_s.p90)} · ${data.approval_turnaround_s.count} decided`}
              definition={data.definitions.approval_turnaround}
            />
            <Tile
              label="Cost per conversation"
              value={micros(data.cost_per_conversation_micros.mean)}
              detail={`p90 ${micros(data.cost_per_conversation_micros.p90)}`}
              definition={data.definitions.cost_per_conversation}
            />
            <Tile
              label="Time to first token"
              value={data.time_to_first_token_ms.p50 == null ? "-" : `${Math.round(data.time_to_first_token_ms.p50)} ms`}
              detail={data.time_to_first_token_ms.p95 == null ? undefined : `p95 ${Math.round(data.time_to_first_token_ms.p95)} ms`}
              definition={data.definitions.time_to_first_token}
            />
            <RateTile label="Input from cache" rate={data.cache_share} definition={data.definitions.cache_share} />
            <RateTile label="Tool error rate" rate={data.tool_error_rate} definition={data.definitions.tool_error_rate} />
            <Tile label="Refusals / fallbacks" value={`${data.refusals} / ${data.fallbacks}`} />
            <Tile label="Injection flags" value={String(data.injection_flags)} />
          </ul>
          <PerDay days={data.conversations_per_day} />
          <Card>
            <h2 className="mb-2 font-semibold">Tool calls</h2>
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead>
                  <tr className="text-slate-600">
                    <th className="py-1 pr-4">Tool</th>
                    <th className="pr-4">Calls</th>
                    <th className="pr-4">Errors</th>
                    <th className="pr-4">Policy denials</th>
                    <th>Error codes</th>
                  </tr>
                </thead>
                <tbody>
                  {data.tools.map((tool) => (
                    <tr key={tool.tool} className="border-t border-slate-100">
                      <td className="py-1 pr-4 font-mono text-xs">{tool.tool}</td>
                      <td className="pr-4">{tool.calls}</td>
                      <td className="pr-4">{tool.errors}</td>
                      <td className="pr-4">{tool.denials}</td>
                      <td className="text-xs">
                        {Object.entries(tool.error_codes).map(([code, n]) => `${code} ${n}`).join(", ") || "-"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        </>
      ) : null}
    </>
  );
}

function Tile({ label, value, detail, definition }: { label: string; value: string; detail?: string; definition?: string }) {
  return (
    <li>
      <Card className="flex h-full flex-col gap-1">
        <p className="text-sm text-slate-600">{label}</p>
        <p className="text-2xl font-semibold tabular-nums">{value}</p>
        {detail ? <p className="text-xs text-slate-600">{detail}</p> : null}
        {definition ? <p className="mt-auto pt-1 text-xs text-slate-500">{definition}</p> : null}
      </Card>
    </li>
  );
}

function RateTile({ label, rate, definition }: { label: string; rate: Rate; definition?: string }) {
  return <Tile label={label} value={percent(rate.value)} detail={`${rate.numerator} of ${rate.denominator}`} definition={definition} />;
}

function PerDay({ days }: { days: Schemas["DayCount"][] }) {
  const rows = days.map((d) => ({ day: d.day.slice(5), conversations: d.conversations }));
  return (
    <Card>
      <h2 className="mb-2 font-semibold">Conversations per day</h2>
      {rows.length === 0 ? (
        <p className="text-sm text-slate-600">No conversations in this window.</p>
      ) : (
        <>
          <div className="h-56 w-full" aria-hidden="true">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={rows} margin={{ top: 8, right: 8, bottom: 0, left: -16 }}>
                <CartesianGrid vertical={false} stroke="#e2e8f0" />
                <XAxis dataKey="day" tickLine={false} axisLine={{ stroke: "#cbd5e1" }} tick={{ fill: "#475569", fontSize: 12 }} />
                <YAxis allowDecimals={false} tickLine={false} axisLine={false} tick={{ fill: "#475569", fontSize: 12 }} />
                <Tooltip cursor={{ fill: "#f1f5f9" }} />
                <Bar dataKey="conversations" name="Conversations" fill={SERIES} maxBarSize={24} radius={[4, 4, 0, 0]} isAnimationActive={false} />
              </BarChart>
            </ResponsiveContainer>
          </div>
          <details className="mt-2 text-sm">
            <summary className="cursor-pointer text-slate-700">The numbers</summary>
            <table className="mt-1 text-left">
              <thead>
                <tr className="text-slate-600">
                  <th className="pr-4">Day (store time)</th>
                  <th>Conversations</th>
                </tr>
              </thead>
              <tbody>
                {days.map((d) => (
                  <tr key={d.day}>
                    <td className="pr-4">{d.day}</td>
                    <td>{d.conversations}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </details>
        </>
      )}
    </Card>
  );
}
