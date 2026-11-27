"use client";

import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { type ReactNode, useState } from "react";

import { api, unwrap } from "@/lib/api/client";

export function Providers({ children }: { children: ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: true, staleTime: 2000 } },
      }),
  );
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

/** Who is signed in. Every page reads it; logins and logouts refresh it. */
export function useSession() {
  return useQuery({
    queryKey: ["session"],
    queryFn: () => unwrap(api.GET("/api/auth/session")),
  });
}

export function DemoBanner() {
  const session = useSession();
  if (!session.data?.demo_mode) return null;
  return (
    <div className="bg-amber-100 px-4 py-2 text-center text-sm text-amber-950">
      Demo data. Don&apos;t enter real personal information. Resets nightly.
    </div>
  );
}
