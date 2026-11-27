"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";

import { ChatWidget } from "@/components/chat/ChatWidget";
import { Button, Card, ErrorText } from "@/components/ui";
import { api, message, unwrap } from "@/lib/api/client";

import { useSession } from "./providers";

const PRODUCTS = [
  { name: "Aurora ANC 700 Headphones", price: "$349.99", tag: "Audio" },
  { name: "Slate 11 Tablet 128 GB", price: "$499.99", tag: "Tablets" },
  { name: "Kestrel Air 14 Laptop", price: "$1,199.99", tag: "Computers" },
  { name: "Pulse 4 Smartwatch", price: "$299.99", tag: "Wearables" },
];

export default function Store() {
  const session = useSession();
  const customer = session.data?.customer ?? null;
  return (
    <>
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-5xl flex-wrap items-center justify-between gap-2 px-4 py-3">
          <span className="text-lg font-bold tracking-tight">Pixel &amp; Volt</span>
          <nav className="flex items-center gap-3 text-sm">
            {customer ? <span className="text-slate-700">Hi, {customer.name}</span> : null}
            <Link className="font-medium text-blue-800 underline" href="/console">
              Operator console
            </Link>
          </nav>
        </div>
      </header>
      <main id="main" className="mx-auto flex max-w-5xl flex-col gap-8 px-4 py-8 pb-24">
        <section className="flex flex-col gap-2">
          <h1 className="text-3xl font-semibold tracking-tight">Electronics, delivered.</h1>
          <p className="max-w-2xl text-slate-700">
            A fictional store with a real support agent. Pick a demo customer below, then open
            the chat: track a package, change an address, return an item or ask for a refund.
            Risky actions wait for a human in the operator console.
          </p>
        </section>
        <PersonaPicker current={customer?.persona ?? null} />
        <section aria-labelledby="products" className="flex flex-col gap-3">
          <h2 id="products" className="text-xl font-semibold">
            Popular right now
          </h2>
          <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
            {PRODUCTS.map((product) => (
              <li key={product.name}>
                <Card>
                  <p className="text-xs uppercase tracking-wide text-slate-600">{product.tag}</p>
                  <p className="font-medium">{product.name}</p>
                  <p className="text-slate-800">{product.price}</p>
                </Card>
              </li>
            ))}
          </ul>
        </section>
      </main>
      {customer ? (
        <ChatWidget key={customer.persona ?? customer.name} customerKey={customer.persona ?? customer.name} name={customer.name} />
      ) : null}
    </>
  );
}

function PersonaPicker({ current }: { current: string | null }) {
  const queryClient = useQueryClient();
  const personas = useQuery({
    queryKey: ["personas"],
    queryFn: () => unwrap(api.GET("/api/auth/personas")),
  });
  const signIn = useMutation({
    mutationFn: (persona: string) =>
      unwrap(api.POST("/api/auth/demo", { body: { persona: persona as "dana" } })),
    onSuccess: (session) => {
      queryClient.setQueryData(["session"], session);
      return queryClient.invalidateQueries({ queryKey: ["current-conversation"] });
    },
  });
  return (
    <section aria-labelledby="personas" className="flex flex-col gap-3">
      <h2 id="personas" className="text-xl font-semibold">
        Who are you today?
      </h2>
      <ErrorText>{personas.error ? message(personas.error) : null}</ErrorText>
      <ErrorText>{signIn.error ? message(signIn.error) : null}</ErrorText>
      <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {(personas.data ?? []).map((persona) => {
          const chosen = persona.key === current;
          return (
            <li key={persona.key}>
              <Card className={`flex h-full flex-col gap-2 ${chosen ? "ring-2 ring-blue-700" : ""}`}>
                <p className="font-semibold">{persona.name}</p>
                <p className="flex-1 text-sm text-slate-700">{persona.story}</p>
                <Button
                  tone={chosen ? "secondary" : "primary"}
                  disabled={signIn.isPending}
                  aria-pressed={chosen}
                  onClick={() => signIn.mutate(persona.key)}
                >
                  {chosen ? `Signed in as ${persona.name.split(" ")[0]}` : `Be ${persona.name.split(" ")[0]}`}
                </Button>
              </Card>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
