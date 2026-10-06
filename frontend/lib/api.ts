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
      detail =
        typeof body.detail === "string"
          ? body.detail
          : Array.isArray(body.detail)
            ? // FastAPI validation errors: a list of {msg, loc, ...}
              body.detail.map((d: { msg?: string }) => d.msg ?? JSON.stringify(d)).join("; ")
            : JSON.stringify(body.detail);
    } catch {
      // keep statusText
    }
    throw new ApiError(response.status, detail);
  }
  return (response.status === 204 ? undefined : await response.json()) as T;
}

export const put = <T>(path: string, body: unknown) =>
  api<T>(path, { method: "PUT", body: JSON.stringify(body) });

export const del = (path: string) => api<void>(path, { method: "DELETE" });

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
  purchase_value: string | null;
  acquisition_costs: string | null;
  total_cost: string | null;
  average_cost: string | null;
  cost_flags: (
    | "cost_unknown"
    | "price_derived"
    | "carried"
    | "incomplete_history"
    | "cost_entered"
  )[];
  price: string | null;
  price_as_of: string | null;
  valued_quantity: string | null;
  market_value: string | null;
  unrealised_pnl: string | null;
  unrealised_pct: string | null;
  weight_pct: string | null;
  sector: string | null;
  industry: string | null;
};

export type Finding = {
  status: "match" | "quantity_mismatch" | "missing_in_history" | "not_on_statement";
  isin: string | null;
  name: string;
  history_quantity: string | null;
  statement_quantity: string | null;
  difference: string | null;
};

export type CostCheck = {
  name: string;
  statement_cost: string;
  computed_cost: string;
  difference: string;
  ok: boolean;
};

export type Reconciliation = {
  source: string;
  as_of: string;
  counts: Record<Finding["status"], number>;
  review: Finding[];
  cost_checks: CostCheck[];
};

export type YearPnl = {
  year: number;
  gains: string;
  losses: string;
  net: string;
  fees: string;
  tax_withheld: string;
  disposals: number;
  cost_unknown_sales: number;
  history_gap_sales: number;
};

export type InstrumentPnl = {
  isin: string;
  name: string | null;
  realised_pnl: string;
  cost_unknown: boolean;
  history_gap: boolean;
};

export type Realised = {
  by_year: YearPnl[];
  best: InstrumentPnl[];
  worst: InstrumentPnl[];
  net_total: string;
};

export type UnattributedCash = {
  isin: string;
  name: string | null;
  date: string;
  type: string;
  amount: string;
};

export type Holdings = {
  positions: Position[];
  by_class: Record<string, number>;
  valued_total: string;
  valued_positions: number;
  verified: number;
  reconciliations: Reconciliation[];
  realised: Realised;
  review: { unpriced: number; cost_unknown: number; unattributed_cash: UnattributedCash[] };
};

export type CryptoTax = {
  taxable_gain: string;
  tax_free_gain: string;
  freigrenze: string;
  under_freigrenze: boolean;
};

export type YearTax = {
  year: number;
  stock_pnl: string;
  fund_pnl: string;
  other_pnl: string;
  income: string;
  stock_loss_brought: string;
  general_loss_brought: string;
  stock_loss_carried: string;
  general_loss_carried: string;
  taxable_before_allowance: string;
  allowance_used: string;
  taxable: string;
  tax: string;
  withheld: string;
  to_settle: string;
  fund_disposals: number;
  cost_unknown_sales: number;
  history_gap_sales: number;
  crypto: CryptoTax;
};

export type TaxEstimate = { years: YearTax[]; assumptions: string[] };

export type CostItem = {
  isin: string;
  name: string | null;
  asset_class: string | null;
  open_units: string;
  sold_units: string;
  sales: number;
  unit_cost: string | null;
  note: string | null;
};

export type Costs = { items: CostItem[] };

export type PriceItem = {
  isin: string;
  name: string | null;
  asset_class: string | null;
  status: "priced" | "stale" | "no_rate" | "inactive" | "waiting" | "unmapped" | "not_checked" | "unsupported";
  symbol: string | null;
  mapping_source: string | null;
  currency: string | null;
  last_date: string | null;
  last_close: string | null;
};

export type Prices = { items: PriceItem[] };

export type NotificationItem = {
  id: string;
  kind: string;
  severity: "info" | "warning";
  title: string;
  body: string;
  isin: string | null;
  created_at: string;
  read: boolean;
};

export type Notifications = { items: NotificationItem[]; unread: number };

export type AlertSettings = {
  daily_moves_enabled: boolean;
  move_stock_pct: string;
  move_fund_pct: string;
  move_crypto_pct: string;
  quiet_start: string | null;
  quiet_end: string | null;
};

export type AiBudget = {
  month: string;
  user_eur: string;
  user_cap_eur: string;
  total_eur: string | null;
  total_cap_eur: string | null;
};

export type AiStatus = {
  configured: boolean;
  accepted: boolean;
  anonymise_amounts: boolean;
  disclaimer: string;
  disclaimer_version: number;
  label: string;
  budget: AiBudget;
};

export type AiPick = {
  id: string;
  created_at: string;
  question: string;
  name: string;
  isin: string | null;
  ticker: string | null;
  direction: "buy" | "sell" | "hold";
  horizon_months: number;
  rationale: string;
  held: boolean;
  price: string | null;
  price_currency: string | null;
  price_date: string | null;
};

export type AiAnswer = {
  answer: string;
  label: string;
  model: string;
  fallback_used: boolean;
  refused: boolean;
  picks: AiPick[];
  data_points: string[];
};
