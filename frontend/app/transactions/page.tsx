"use client";

import { useCallback, useEffect, useState } from "react";
import {
  api,
  ApiError,
  del,
  type ManualSide,
  patch,
  post,
  type TransactionAudit,
  type TransactionItem,
  type TransactionList,
} from "@/lib/api";
import { Header } from "../Header";
import { useUser } from "../useUser";

const eur = (value: string | null) =>
  value === null ? "—" : Number(value).toLocaleString("en-GB", { style: "currency", currency: "EUR" });

const SIDES: { value: ManualSide; label: string }[] = [
  { value: "buy", label: "Buy" },
  { value: "sell", label: "Sell" },
  { value: "dividend", label: "Dividend" },
  { value: "interest", label: "Interest" },
  { value: "deposit", label: "Deposit" },
  { value: "withdrawal", label: "Withdrawal" },
  { value: "fee", label: "Fee" },
];
const TRADE = new Set<ManualSide>(["buy", "sell"]);
const NEEDS_ISIN = new Set<ManualSide>(["buy", "sell", "dividend"]);

type Form = {
  side: ManualSide;
  date: string;
  isin: string;
  name: string;
  shares: string;
  price: string;
  amount: string;
  fee: string;
  tax: string;
};
const today = () => new Date().toISOString().slice(0, 10);
const empty = (): Form => ({
  side: "buy",
  date: today(),
  isin: "",
  name: "",
  shares: "",
  price: "",
  amount: "",
  fee: "",
  tax: "",
});

// The server wants positive numbers. When editing, only the fields that changed are sent: an
// amount left as it was must not freeze the amount when the shares change. A field the user
// cleared is sent as null.
function body(form: Form, withSide: boolean, original?: Form) {
  const out: Record<string, string | null> = { date: form.date };
  if (withSide) out.side = form.side;
  for (const key of ["isin", "name", "shares", "price", "amount", "fee", "tax"] as const) {
    const value = form[key].trim();
    if (original) {
      if (value !== original[key].trim()) out[key] = value || null;
    } else if (value) {
      out[key] = value;
    }
  }
  return out;
}

function abs(value: string | null) {
  return value === null ? "" : String(Math.abs(Number(value)));
}

function sideOf(item: TransactionItem): ManualSide {
  if (item.type === "BUY") return "buy";
  if (item.type === "SELL") return "sell";
  if (item.type === "DIVIDEND") return "dividend";
  if (item.type === "INTEREST_PAYMENT") return "interest";
  if (item.type === "CUSTOMER_INBOUND") return "deposit";
  if (item.type === "CUSTOMER_OUTBOUND_REQUEST") return "withdrawal";
  return "fee";
}

export default function TransactionsPage() {
  const user = useUser();
  const [data, setData] = useState<TransactionList | null>(null);
  const [audit, setAudit] = useState<TransactionAudit[]>([]);
  const [form, setForm] = useState<Form>(empty());
  const [editing, setEditing] = useState<TransactionItem | null>(null);
  const [original, setOriginal] = useState<Form>(empty());
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [list, trail] = await Promise.all([
        api<TransactionList>("/api/transactions?limit=200"),
        api<TransactionAudit[]>("/api/transactions/audit?limit=50"),
      ]);
      setData(list);
      setAudit(trail);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Could not load the transactions.");
    }
  }, []);
  useEffect(() => {
    if (user) void load();
  }, [user, load]);

  if (!user) return null;

  // An imported row keeps its kind: only its numbers can change.
  const imported = editing !== null && editing.source !== "manual";

  const set = (key: keyof Form, value: string) => setForm((f) => ({ ...f, [key]: value }));

  const startEdit = (item: TransactionItem) => {
    setEditing(item);
    const start: Form = {
      side: sideOf(item),
      date: item.date,
      isin: item.isin ?? "",
      name: item.name ?? "",
      shares: abs(item.shares),
      price: item.price ?? "",
      amount: abs(item.amount),
      fee: abs(item.fee),
      tax: abs(item.tax),
    };
    setForm(start);
    setOriginal(start);
    window.scrollTo({ top: 0 });
  };

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      if (editing) {
        await patch(`/api/transactions/${editing.id}`, body(form, !imported, original));
      } else {
        await post("/api/transactions", body(form, true));
      }
      setEditing(null);
      setForm(empty());
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Could not save the transaction.");
    } finally {
      setBusy(false);
    }
  };

  const remove = async (item: TransactionItem) => {
    const what = `${item.type} ${item.name ?? item.isin ?? ""} on ${item.date}`;
    if (!window.confirm(`Delete this transaction (${what})? The audit trail keeps a copy.`)) return;
    setError("");
    try {
      await del(`/api/transactions/${item.id}`);
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Could not delete the transaction.");
    }
  };

  return (
    <>
      <Header user={user} />
      <main>
        <h1>Transactions</h1>
        <p className="notice">
          Add a transaction your import missed, or fix a wrong one. Type every number as a positive
          value. The kind says which way the money goes. Every change is kept in the audit trail below.
        </p>
        {error && <p className="notice status-failed">{error}</p>}

        <div className="card" style={{ marginBottom: 16 }}>
          <h2 style={{ marginTop: 0 }}>{editing ? "Edit transaction" : "Add a transaction"}</h2>
          <form onSubmit={submit}>
            <label>
              Kind
              <select
                value={form.side}
                disabled={imported}
                onChange={(e) => set("side", e.target.value)}
              >
                {SIDES.map((s) => (
                  <option key={s.value} value={s.value}>
                    {s.label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Date
              <input type="date" required value={form.date} onChange={(e) => set("date", e.target.value)} />
            </label>
            {NEEDS_ISIN.has(form.side) && (
              <label>
                ISIN
                <input
                  value={form.isin}
                  maxLength={12}
                  disabled={imported}
                  onChange={(e) => set("isin", e.target.value)}
                />
              </label>
            )}
            {NEEDS_ISIN.has(form.side) && (
              <label>
                Name (optional)
                <input value={form.name} onChange={(e) => set("name", e.target.value)} />
              </label>
            )}
            {TRADE.has(form.side) ? (
              <>
                <label>
                  Shares
                  <input inputMode="decimal" value={form.shares} onChange={(e) => set("shares", e.target.value)} />
                </label>
                <label>
                  Price per share (EUR)
                  <input inputMode="decimal" value={form.price} onChange={(e) => set("price", e.target.value)} />
                </label>
                <label>
                  Total paid or received (optional, EUR)
                  <input inputMode="decimal" value={form.amount} onChange={(e) => set("amount", e.target.value)} />
                </label>
              </>
            ) : (
              <label>
                Amount (EUR)
                <input inputMode="decimal" value={form.amount} onChange={(e) => set("amount", e.target.value)} />
              </label>
            )}
            <label>
              Fee (optional, EUR)
              <input inputMode="decimal" value={form.fee} onChange={(e) => set("fee", e.target.value)} />
            </label>
            <label>
              Tax withheld (optional, EUR)
              <input inputMode="decimal" value={form.tax} onChange={(e) => set("tax", e.target.value)} />
            </label>
            <div style={{ display: "flex", gap: 8 }}>
              <button type="submit" disabled={busy}>
                {editing ? "Save changes" : "Add"}
              </button>
              {editing && (
                <button
                  type="button"
                  className="link"
                  onClick={() => {
                    setEditing(null);
                    setForm(empty());
                  }}
                >
                  Cancel
                </button>
              )}
            </div>
          </form>
        </div>

        {data && (
          <div className="card table-wrap" style={{ marginBottom: 16 }}>
            <h2 style={{ marginTop: 0 }}>History</h2>
            <p className="muted">
              Cash balance from this history: <strong>{eur(data.cash_balance)}</strong>. Card spending is
              hidden.
            </p>
            <table>
              <thead>
                <tr>
                  <th>Date</th>
                  <th>Type</th>
                  <th>Instrument</th>
                  <th className="num">Shares</th>
                  <th className="num">Price</th>
                  <th className="num">Amount</th>
                  <th>Source</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {data.items.map((t) => (
                  <tr key={t.id}>
                    <td>{t.date}</td>
                    <td>{t.type}</td>
                    <td>{t.name ?? t.isin ?? ""}</td>
                    <td className="num">{t.shares === null ? "" : Number(t.shares)}</td>
                    <td className="num">{t.price === null ? "" : Number(t.price)}</td>
                    <td className="num">{eur(t.amount)}</td>
                    <td>{t.source === "manual" ? "typed in" : "import"}</td>
                    <td>
                      {t.editable && (
                        <button className="link" onClick={() => startEdit(t)}>
                          Edit
                        </button>
                      )}{" "}
                      <button className="link" onClick={() => remove(t)}>
                        Delete
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <div className="card table-wrap">
          <h2 style={{ marginTop: 0 }}>Audit trail</h2>
          {audit.length === 0 ? (
            <p className="muted">No manual changes yet.</p>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>When</th>
                  <th>Action</th>
                  <th>Source</th>
                  <th>Row</th>
                </tr>
              </thead>
              <tbody>
                {audit.map((a) => {
                  const row = a.after ?? a.before;
                  return (
                    <tr key={a.id}>
                      <td>{new Date(a.at).toLocaleString("en-GB")}</td>
                      <td>{a.action}</td>
                      <td>{a.source === "manual" ? "typed in" : "import"}</td>
                      <td>
                        {row?.type} {row?.name ?? row?.isin ?? ""} {row?.shares ? Number(row.shares) : ""}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      </main>
    </>
  );
}
