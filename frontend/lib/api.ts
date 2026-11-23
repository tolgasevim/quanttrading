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
    // The disclaimer changed (or was never accepted) while the page was open: show it.
    if (
      response.status === 403 &&
      detail === "accept the disclaimer first" &&
      typeof window !== "undefined" &&
      window.location.pathname !== "/disclaimer"
    ) {
      window.location.assign("/disclaimer");
    }
    throw new ApiError(response.status, detail);
  }
  return (response.status === 204 ? undefined : await response.json()) as T;
}

export const put = <T>(path: string, body: unknown) =>
  api<T>(path, { method: "PUT", body: JSON.stringify(body) });

export const patch = <T>(path: string, body: unknown) =>
  api<T>(path, { method: "PATCH", body: JSON.stringify(body) });

export const del = (path: string) => api<void>(path, { method: "DELETE" });

export const post = <T>(path: string, body?: unknown) =>
  api<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export type User = {
  id: string;
  email: string;
  display_name: string;
  role: "admin" | "member";
  totp_enabled: boolean;
  // From /api/auth/me only: true or false there, null (unknown) in the login answer.
  disclaimer_accepted: boolean | null;
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
  notes: string[];
  tool_calls: { name: string; input: string; summary: string; error: boolean }[];
};

export type AiScore = {
  window_months: number;
  end_date: string;
  pick_return_pct: string;
  benchmark_return_pct: string;
  excess_pct: string;
  hit: boolean;
};

export type AiWindowSummary = {
  window_months: number;
  scored: number;
  hits: number;
  hit_rate_pct: string | null;
  avg_pick_return_pct: string | null;
  avg_benchmark_return_pct: string | null;
  avg_excess_pct: string | null;
};

export type AiTrackRecord = {
  benchmark: string;
  picks: number;
  unscored: number;
  windows: AiWindowSummary[];
  items: { pick: AiPick; scores: AiScore[] }[];
};

export type AiCommentary = {
  id: string;
  week_start: string;
  created_at: string;
  kind: "ai" | "template";
  why_template: string | null;
  text: string;
};

export type Disclaimer = { text: string; version: number; accepted: boolean };

export type TransactionItem = {
  id: number;
  executed_at: string;
  date: string;
  kind: string;
  type: string;
  asset_class: string | null;
  isin: string | null;
  name: string | null;
  shares: string | null;
  price: string | null;
  amount: string | null;
  fee: string | null;
  tax: string | null;
  currency: string | null;
  savings_plan: boolean;
  source: string;
  editable: boolean;
};

export type TransactionList = {
  counts: Record<string, number>;
  cash_balance: string;
  items: TransactionItem[];
};

export type TransactionAudit = {
  id: number;
  at: string;
  action: "add" | "edit" | "delete";
  source: string;
  external_id: string;
  before: Record<string, string | null> | null;
  after: Record<string, string | null> | null;
};

export type ManualSide =
  | "buy"
  | "sell"
  | "dividend"
  | "interest"
  | "deposit"
  | "withdrawal"
  | "fee";

export type PerformanceSeriesPoint = { day: string; portfolio: number; benchmark: number | null };

export type Performance = {
  benchmark: string;
  benchmark_name: string;
  benchmarks: { code: string; name: string }[];
  period: string;
  has_history: boolean;
  start: string | null;
  end: string | null;
  current_value: string;
  net_invested: string;
  income: string;
  twr: number | null;
  xirr: number | null;
  benchmark_twr: number | null;
  benchmark_xirr: number | null;
  volatility: number | null;
  benchmark_volatility: number | null;
  max_drawdown: number | null;
  drawdown_peak: string | null;
  drawdown_low: string | null;
  beta: number | null;
  var95: number | null;
  cvar95: number | null;
  observations: number;
  days: number;
  estimated_days: number;
  unpriced: string[];
  estimated: string[];
  series: PerformanceSeriesPoint[];
  correlation: { isins: string[]; names: string[]; values: (number | null)[][] } | null;
  warnings: string[];
};

export type CashStatus = {
  entered: string | null;
  entered_as_of: string | null;
  history: string;
  difference: string | null;
  difference_pct: string | null;
  mismatch: boolean;
  cash: string;
  cash_source: "entered" | "history";
  reserves_total: string;
  shortfall: string;
  investable: string;
};

export type Reserve = {
  id: string;
  name: string;
  kind: "hard" | "opportunity";
  amount: string;
  loaned: string;
  refill_per_month: string | null;
  target_date: string | null;
  note: string | null;
};

export type ReserveEvent = {
  id: number;
  at: string;
  reserve_id: string;
  action: "create" | "change" | "release" | "unlock" | "refill";
  name: string;
  amount_before: string | null;
  amount_after: string | null;
  detail: Record<string, unknown> | null;
};

export type Cash = {
  status: CashStatus;
  securities_value: string;
  net_worth: string;
  band: {
    pct: string;
    status: "low" | "ok" | "high";
    target_pct: string;
    low_pct: string;
    high_pct: string;
    excess: string;
  } | null;
  reserves: Reserve[];
  balances: { id: number; as_of: string; amount: string; note: string | null }[];
  events: ReserveEvent[];
  warnings: string[];
};

export type UnlockCheck = {
  key: string;
  title: string;
  passed: boolean | null;
  detail: string;
  blocking: boolean;
};

export type UnlockResult = { allowed: boolean; triggers: string[]; checks: UnlockCheck[] };

export type ConcentrationSlice = { label: string; value: string; pct: string };

export type ConcentrationPosition = {
  isin: string;
  name: string;
  type: string;
  flags: string[];
  value: string;
  pct: string;
  trading_book: boolean;
  crypto_linked: boolean;
  looked_through: boolean | null;
};

export type ConcentrationUnderlying = {
  key: string;
  name: string;
  isin: string | null;
  type: string;
  sector: string;
  country: string;
  direct_pct: string;
  indirect_pct: string;
  total_pct: string;
  via: { fund: string; pct: string }[];
};

export type Concentration = {
  total: string;
  unvalued: number;
  covered_pct: string | null;
  crypto_pct: string;
  positions: ConcentrationPosition[];
  underlying: ConcentrationUnderlying[];
  by_class: ConcentrationSlice[];
  by_sector: ConcentrationSlice[];
  by_country: ConcentrationSlice[];
  by_currency: ConcentrationSlice[];
  unlooked: string[];
  warnings: string[];
};

export type FundFile = {
  etf_isin: string;
  name: string | null;
  rows: number;
  total_weight: string;
  as_of: string;
  source: string;
};

export type AllocationBucket = {
  bucket: string;
  label: string;
  value: string;
  pct: string;
  target: string;
  low: string;
  high: string;
  status: "below" | "ok" | "above";
  gap_pp: string;
  gap_eur: string;
};

export type GuardrailRule = {
  rule: string;
  subject: string;
  current_pct: string;
  limit_pct: string;
  breached: boolean;
  note: string;
};

export type Suggestion = {
  kind: string;
  text: string;
  amount: string;
  bucket: string;
  why: string;
  gain_estimate: string | null;
};

export type Tranche = {
  number: number;
  due: string;
  amount: string;
  done: boolean;
  split: { bucket: string; label: string; amount: string; instrument: string | null }[];
};

export type Allocation = {
  has_portfolio: boolean;
  base: string;
  free_cash: string;
  trading_book: string;
  buckets: AllocationBucket[];
  rules: GuardrailRule[];
  breaches: number;
  suggestions: Suggestion[];
  savings_plans: Record<string, string>;
  deployment: {
    active: boolean;
    started: string | null;
    tranches: number;
    excess: string;
    invested_since: string;
    plan: Tranche[];
    pull_forward: boolean;
    pull_forward_note: string;
  } | null;
  custom_targets: boolean;
  custom_guardrails: boolean;
  guardrails: Record<string, string>;
  warnings: string[];
};

export type JournalEntry = {
  id: string;
  at: string;
  action: "bought" | "sold" | "ignored";
  name: string;
  isin: string | null;
  reason: string;
  guidance: string | null;
  pick_id: string | null;
  price: string | null;
  price_currency: string | null;
  since_pct: string | null;
  guidance_right: boolean | null;
  verdict: string;
};

export type Journal = {
  entries: JournalEntry[];
  counts: Record<string, number>;
  judged: number;
  right: number;
};

export type SubgroupRow = {
  key: string;
  label: string;
  value: string;
  pct: string;
  pct_of_tech: string;
  target: string | null;
  cap: string | null;
  breached: boolean;
  members: { isin: string; name: string; value: string; overridden: boolean }[];
};

export type Subgroups = {
  base: string;
  groups: SubgroupRow[];
  non_tech: string;
  non_tech_pct: string;
  stocks: {
    isin: string;
    name: string;
    subgroup: string;
    label: string;
    auto: string;
    auto_label: string;
    overridden: boolean;
    value: string;
  }[];
  labels: Record<string, string>;
};

export type BookSettings = {
  min_positions: number;
  max_positions: number;
  weekly_prompts: number;
  hold_days: number;
  barrier_pct: string;
};

export type Consolidation = {
  direct: number;
  look_through: number;
  min_positions: number;
  max_positions: number;
  state: "below" | "within" | "above";
  small_pct: string;
  small: {
    isin: string;
    name: string;
    type: string;
    value: string | null;
    pct: string | null;
    status: string;
    why: string;
    suggestion: string;
    gain_estimate: string | null;
  }[];
  overlaps: {
    a_isin: string;
    a_name: string;
    b_isin: string;
    b_name: string;
    overlap_pct: string | null;
    basis: string;
    intentional: boolean;
    reconfirm: boolean;
    suggestion: string;
  }[];
  prompts: { isin: string; name: string; kind: string; text: string }[];
  held_back: number;
  notes: string[];
  settings: BookSettings;
};

export type TradingBook = {
  value: string;
  pct: string;
  cap: string;
  over_cap: boolean;
  open: {
    isin: string;
    name: string;
    type: string;
    value: string;
    days_held: number | null;
    price: string | null;
    stop_price: string | null;
    barrier_distance_pct: string | null;
    alerts: { code: string; text: string }[];
    flags: string[];
  }[];
  stats: {
    closed: number;
    wins: number;
    hit_rate: string | null;
    expired: number;
    pnl: string;
    avg_days: string | null;
    by_year: Record<string, string>;
  };
  needs_thesis: [string, string][];
  tax_note: string;
  settings: BookSettings;
};

export type CommodityRow = {
  code: string;
  name: string;
  unit: string;
  usd: string | null;
  eur: string | null;
  as_of: string | null;
  changes: Record<string, string | null>;
  low_52w: string | null;
  high_52w: string | null;
  range_pos: string | null;
  stale: boolean;
};

export type Markets = {
  today: string;
  commodities: CommodityRow[];
  drivers: {
    title: string;
    items: { label: string; value: string | null; unit: string; detail: string; available: boolean }[];
  }[];
  regime: {
    label: "risk-on" | "risk-off" | "mixed" | "unknown";
    score: number;
    signals: { name: string; vote: number; available: boolean; detail: string }[];
    implication: string;
    disclaimer: string;
  };
  hedge: {
    available: boolean;
    windows: { days: number; observations: number; correlation: number | null }[];
    verdict: string;
  };
  products: {
    id: number;
    commodity: string;
    isin: string;
    name: string;
    issuer: string | null;
    kind: string;
    ter: string | null;
    tr_available: boolean | null;
    notes: string | null;
    risk: string;
  }[];
  risks: Record<string, string>;
  note: string;
};

export type Crypto = {
  year: number;
  coins: {
    isin: string;
    name: string;
    quantity: string;
    price: string | null;
    value: string | null;
    free_quantity: string;
    free_share_pct: string;
    next_free_date: string | null;
    next_free_quantity: string;
    taxable_gain_if_sold: string | null;
    lots: {
      acquired: string;
      quantity: string;
      cost: string;
      value: string | null;
      gain: string | null;
      tax_free_from: string;
      free: boolean;
      days_left: number;
      origin: string;
    }[];
  }[];
  value: string;
  free_value: string;
  taxable_gain_if_sold: string;
  realised_taxable: string;
  realised_free: string;
  freigrenze: string;
  headroom: string;
  over_limit: boolean;
  exposure_pct: string;
  cap_pct: string;
  note: string;
};

export type Score = {
  isin: string;
  name: string;
  sector: string | null;
  day: string;
  value: string | null;
  quality: string | null;
  growth: string | null;
  momentum: string | null;
  low_vol: string | null;
  composite: string | null;
  composite_pct: string | null;
  data_quality: "ok" | "partial" | "poor";
  flags: string[];
  flag_text: string[];
  peers: number;
  held: boolean;
  watched: boolean;
};

export type Scores = { day: string | null; scores: Score[]; weights: Record<string, number>; note: string };

export type ScoreDetail = {
  score: Score | null;
  history: {
    day: string;
    value: string | null;
    quality: string | null;
    growth: string | null;
    momentum: string | null;
    low_vol: string | null;
    composite: string | null;
  }[];
  trends: string[];
  note: string;
};

export type WatchItem = { isin: string; note: string | null; name: string | null; has_score: boolean };

export type FundCard = {
  isin: string;
  name: string;
  kind: "etf" | "etc";
  held: boolean;
  lines: { label: string; value: string; verdict: "good" | "watch" | "bad" | "info" | "unknown"; note: string }[];
  missing: string[];
  overlaps: string[];
  tax_note: string;
};

export type Passkey = { id: string; name: string; created_at: string; last_used_at: string | null };

export type AdminOverview = {
  now: string;
  users: {
    id: string;
    email: string;
    display_name: string;
    role: string;
    totp_enabled: boolean;
    passkeys: number;
    created_at: string;
    last_login_at: string | null;
    locked: boolean;
    last_import_at: string | null;
    last_import_source: string | null;
    ai_spend_eur: string;
  }[];
  jobs: {
    job: string;
    healthy: boolean;
    problem: string | null;
    last_status: string | null;
    last_started: string | null;
    last_finished: string | null;
    last_success: string | null;
    rows_written: number;
    failures_7d: number;
    error_count: number;
  }[];
  llm: { month: string; total_eur: string; total_cap_eur: string; user_cap_eur: string; by_purpose: Record<string, string> };
  pending_invites: number;
};

export type Delivery = {
  email_available: boolean;
  email_mode: "off" | "weekly" | "daily";
  email_to: string | null;
  account_email: string;
  telegram_available: boolean;
  telegram_linked: boolean;
  telegram_bot_name: string | null;
  link_pending: boolean;
  privacy: string;
};
