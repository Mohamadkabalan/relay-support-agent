// The typed API client. `schema.d.ts` is generated from the backend's OpenAPI document
// (`make api-types`); CI fails if either drifts from the backend.
import createClient from "openapi-fetch";

import type { components, paths } from "./schema";

export type Schemas = components["schemas"];

// Same origin: Caddy sends /api/* to the API, so the session cookie just works.
export const api = createClient<paths>({ baseUrl: "", credentials: "same-origin" });

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

/** The data of a successful call, or an ApiError carrying the API's own explanation. */
export async function unwrap<T>(
  call: Promise<{ data?: T; error?: unknown; response: Response }>,
): Promise<T> {
  const { data, error, response } = await call;
  if (response.ok) {
    return data as T;
  }
  throw new ApiError(response.status, describe(response.status, error));
}

function describe(status: number, error: unknown): string {
  const detail = (error as { detail?: unknown } | undefined)?.detail;
  if (typeof detail === "string") {
    return detail;
  }
  if (Array.isArray(detail)) {
    return "Please check what you entered.";
  }
  if (status === 429) {
    return "That's a lot of requests. Please wait a moment.";
  }
  return `Something went wrong (${status}).`;
}

export function message(error: unknown): string {
  return error instanceof Error ? error.message : "Something went wrong.";
}
