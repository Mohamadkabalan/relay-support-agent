// Container healthcheck target. Dynamic, so it reflects the running server, not the build.
export const dynamic = "force-dynamic";

export function GET(): Response {
  return Response.json({ status: "ok" });
}
