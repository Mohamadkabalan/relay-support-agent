"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { type FormEvent, type ReactNode, useState } from "react";

import { useSession } from "@/app/providers";
import { Button, Card, ErrorText, Field, inputClass } from "@/components/ui";
import { api, message, unwrap } from "@/lib/api/client";

const TABS = [
  { href: "/console", label: "Conversations" },
  { href: "/console/approvals", label: "Approvals" },
  { href: "/console/escalations", label: "Escalations" },
  { href: "/console/metrics", label: "Metrics" },
  { href: "/console/drafts", label: "Scenario drafts" },
];

export default function ConsoleLayout({ children }: { children: ReactNode }) {
  const session = useSession();
  const operator = session.data?.operator ?? null;
  const pathname = usePathname();
  const queryClient = useQueryClient();
  const pendingCount = useQuery({
    queryKey: ["approvals", "pending"],
    queryFn: () => unwrap(api.GET("/api/console/approvals", { params: { query: { status: "pending" } } })),
    enabled: operator !== null,
    refetchInterval: 5000,
  });
  const logout = useMutation({
    mutationFn: () => unwrap(api.POST("/api/auth/logout")),
    onSuccess: () => queryClient.invalidateQueries(),
  });

  if (session.isPending) {
    return <p className="p-4 text-sm text-slate-600">Loading…</p>;
  }
  if (operator === null) {
    return <SignIn demoMode={session.data?.demo_mode ?? false} />;
  }
  const active = (href: string) =>
    href === "/console" ? pathname === "/console" || pathname.startsWith("/console/conversations") : pathname.startsWith(href);
  return (
    <>
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-2 px-4 py-3">
          <div className="flex items-center gap-3">
            <Link href="/" className="font-bold tracking-tight">
              Pixel &amp; Volt
            </Link>
            <span className="text-sm text-slate-600">Operator console</span>
          </div>
          <div className="flex items-center gap-2 text-sm">
            <span className="text-slate-700">
              {operator.name}
              {operator.scoped_to_visitor ? " (your demo data only)" : ""}
            </span>
            <Button tone="secondary" onClick={() => logout.mutate()}>
              Sign out
            </Button>
          </div>
        </div>
        <nav aria-label="Console" className="mx-auto max-w-6xl overflow-x-auto px-4">
          <ul className="flex gap-1">
            {TABS.map((tab) => (
              <li key={tab.href}>
                <Link
                  href={tab.href}
                  aria-current={active(tab.href) ? "page" : undefined}
                  className={`inline-flex items-center gap-1 whitespace-nowrap border-b-2 px-3 py-2 text-sm ${
                    active(tab.href)
                      ? "border-blue-700 font-semibold text-blue-800"
                      : "border-transparent text-slate-700 hover:text-slate-900"
                  }`}
                >
                  {tab.label}
                  {tab.href === "/console/approvals" && pendingCount.data?.length ? (
                    <span className="rounded-full bg-amber-200 px-1.5 text-xs text-amber-950">
                      {pendingCount.data.length}
                    </span>
                  ) : null}
                </Link>
              </li>
            ))}
          </ul>
        </nav>
      </header>
      <main id="main" className="mx-auto flex max-w-6xl flex-col gap-4 px-4 py-6">
        {children}
      </main>
    </>
  );
}

function SignIn({ demoMode }: { demoMode: boolean }) {
  const queryClient = useQueryClient();
  const [email, setEmail] = useState("operator@relay.example");
  const [password, setPassword] = useState("");
  const done = (session: unknown) => {
    queryClient.setQueryData(["session"], session);
  };
  const demo = useMutation({
    mutationFn: () => unwrap(api.POST("/api/auth/demo-operator")),
    onSuccess: done,
  });
  const login = useMutation({
    mutationFn: () => unwrap(api.POST("/api/auth/operator", { body: { email, password } })),
    onSuccess: done,
  });
  const submit = (event: FormEvent) => {
    event.preventDefault();
    login.mutate();
  };
  return (
    <main id="main" className="mx-auto flex max-w-md flex-col gap-4 px-4 py-12">
      <h1 className="text-2xl font-semibold">Operator console</h1>
      {demoMode ? (
        <Card className="flex flex-col gap-2">
          <p className="text-sm text-slate-700">
            The demo console shows only the conversations of this browser&apos;s demo customers.
          </p>
          <Button onClick={() => demo.mutate()} disabled={demo.isPending}>
            Open the demo console
          </Button>
          <ErrorText>{demo.error ? message(demo.error) : null}</ErrorText>
        </Card>
      ) : null}
      <Card>
        <form onSubmit={submit} className="flex flex-col gap-3">
          <Field label="Email">
            <input
              className={inputClass}
              type="email"
              autoComplete="username"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
            />
          </Field>
          <Field label="Password">
            <input
              className={inputClass}
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
          </Field>
          <Button type="submit" tone="secondary" disabled={login.isPending || !password}>
            Sign in
          </Button>
          <ErrorText>{login.error ? message(login.error) : null}</ErrorText>
        </form>
      </Card>
      <Link href="/" className="text-sm text-blue-800 underline">
        Back to the store
      </Link>
    </main>
  );
}
