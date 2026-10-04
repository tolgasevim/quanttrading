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

export type ImportSummary = {
  rows: number;
  skipped: number;
  by_kind: Record<string, number>;
  first_date: string | null;
  last_date: string | null;
  instruments: number;
  savings_plan_executions: number;
  net_cash_flow: string;
  warnings: string[];
  warning_count: number;
  new: number;
  already_imported: number;
};

export type ImportRecord = {
  id: string;
  source: string;
  status: "preview" | "committed" | "discarded";
  summary: ImportSummary;
  created_at: string;
  committed_at: string | null;
  rows_inserted: number;
};

/** Multipart upload; the browser sets the Content-Type boundary itself. */
export async function uploadFile<T>(path: string, file: File): Promise<T> {
  const form = new FormData();
  form.append("file", file);
  const response = await fetch(path, { method: "POST", body: form, credentials: "same-origin" });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      detail = (await response.json()).detail ?? detail;
    } catch {
      // keep statusText
    }
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as T;
}

export type Position = {
  isin: string;
  name: string | null;
  asset_class: string | null;
  quantity: string;
  first_date: string;
  last_date: string;
  corporate_action: boolean;
  verified: boolean;
  differs: boolean;
};

export type Finding = {
  status: "match" | "quantity_mismatch" | "missing_in_history" | "not_on_statement";
  isin: string | null;
  name: string;
  history_quantity: string | null;
  statement_quantity: string | null;
  difference: string | null;
};

export type Reconciliation = {
  source: string;
  as_of: string;
  counts: Record<Finding["status"], number>;
  review: Finding[];
};

export type Holdings = {
  positions: Position[];
  by_class: Record<string, number>;
  verified: number;
  reconciliations: Reconciliation[];
};
