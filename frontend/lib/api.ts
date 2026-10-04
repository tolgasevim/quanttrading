export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: string,
  ) {
    super(detail);
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      // keep statusText
    }
    throw new ApiError(response.status, detail);
  }
  return (response.status === 204 ? undefined : await response.json()) as T;
}

export const post = <T>(path: string, body?: unknown) =>
  api<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export type User = {
  id: string;
  email: string;
  display_name: string;
  role: "admin" | "member";
  totp_enabled: boolean;
};

export type InstrumentStatus = {
  code: string;
  name: string;
  asset_class: string;
  currency: string;
  last_date: string | null;
  last_close: string | null;
  source: string | null;
  rows: number;
};

export type Fx = { quote: string; date: string; rate: string };

export type JobRun = {
  id: number;
  job: string;
  status: "running" | "success" | "partial" | "failed";
  started_at: string;
  finished_at: string | null;
  rows_written: number;
  details: Record<string, unknown>;
};
