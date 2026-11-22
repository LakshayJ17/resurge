// Same-origin JSON calls to `resurge serve`. The session lives in an HttpOnly cookie, so nothing secret is stored in
// the browser; every POST carries the page's Origin header, which the server checks.

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

type Options = { method?: "GET" | "POST"; body?: unknown };

export async function api<T>(path: string, { method, body }: Options = {}): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, {
      method: method ?? (body === undefined ? "GET" : "POST"),
      headers: body === undefined ? { Accept: "application/json" }
        : { Accept: "application/json", "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "same-origin",
      cache: "no-store",
    });
  } catch {
    throw new ApiError(0, "Can't reach the server. Check your connection and try again.");
  }
  let data: unknown = null;
  try {
    data = await res.json();
  } catch {
    /* empty or non-JSON body */
  }
  if (!res.ok) {
    const detail = (data as { detail?: unknown } | null)?.detail;
    const fallback = res.status >= 500 ? "The server is busy. Please try again in a minute." : "Something went wrong.";
    throw new ApiError(res.status, typeof detail === "string" ? detail : fallback);
  }
  return data as T;
}

export const post = <T,>(path: string, body: unknown = {}) => api<T>(path, { method: "POST", body });

/** A same-site path to return to after logging in; anything else falls back to the dashboard. */
export function safeNext(value: string | null): string {
  if (value && value.startsWith("/") && !value.startsWith("//") && !value.includes("\\")) return value;
  return "/dashboard";
}
