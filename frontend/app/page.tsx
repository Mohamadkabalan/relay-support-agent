export default function Home() {
  return (
    <main className="mx-auto flex max-w-2xl flex-col gap-4 px-4 py-16">
      <h1 className="text-3xl font-semibold tracking-tight">Relay</h1>
      <p className="text-lg text-slate-700">
        Customer support for Pixel &amp; Volt, a fictional electronics store.
      </p>
      <p className="text-slate-600">
        The chat widget and the operator console arrive in Phase 6. The API answers at{" "}
        <a className="font-medium text-blue-700 underline" href="/api/healthz">
          /api/healthz
        </a>
        .
      </p>
    </main>
  );
}
