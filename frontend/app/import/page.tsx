"use client";

import { type ChangeEvent, useEffect, useState } from "react";
import { api, ApiError, type ImportRecord, uploadFile } from "@/lib/api";
import { Header } from "../Header";
import { useUser } from "../useUser";

const KIND_LABELS: Record<string, string> = {
  trade: "Buys and sells",
  income: "Dividends and distributions",
  interest: "Interest",
  benefit: "Saveback, bonuses, perks",
  delivery: "Free deliveries",
  corporate_action: "Corporate actions",
  tax: "Tax adjustments",
  transfer: "Deposits and withdrawals",
  card: "Card payments (amount only)",
  fee: "Fees",
  other: "Unrecognised",
};

const eur = (value: string) =>
  Number(value).toLocaleString("en-GB", { style: "currency", currency: "EUR" });

export default function ImportPage() {
  const user = useUser();
  const [history, setHistory] = useState<ImportRecord[]>([]);
  const [preview, setPreview] = useState<ImportRecord | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const refresh = () => api<ImportRecord[]>("/api/imports").then(setHistory);
  useEffect(() => {
    if (user) refresh();
  }, [user]);

  if (!user) return null;

  const onFile = async (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    setBusy(true);
    setError("");
    try {
      setPreview(await uploadFile<ImportRecord>("/api/imports", file));
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Upload failed.");
    } finally {
      setBusy(false);
    }
  };

  const decide = async (action: "commit" | "discard") => {
    if (!preview) return;
    setBusy(true);
    try {
      await api(
        action === "commit" ? `/api/imports/${preview.id}/commit` : `/api/imports/${preview.id}`,
        { method: action === "commit" ? "POST" : "DELETE" },
      );
      setPreview(null);
      refresh();
    } finally {
      setBusy(false);
    }
  };

  const s = preview?.summary;
  return (
    <>
      <Header user={user} />
      <main>
        <h1>Import transactions</h1>
        <div className="card">
          <p>
            In the Trade Republic app: <em>Profile → Settings → Account → Export transactions</em>. Upload
            the CSV here. You&apos;ll see a preview before anything is saved.
          </p>
          <p className="muted">
            Names, IBANs, merchants and payment references are removed while the file is read. The file
            itself is never stored. Uploading the same period twice doesn&apos;t create duplicates.
          </p>
          <label>
            <input type="file" accept=".csv,text/csv" onChange={onFile} disabled={busy} />
          </label>
          {busy && <p className="muted">Working…</p>}
          {error && <p className="error">{error}</p>}
        </div>

        {preview && s && (
          <>
            <h2>Preview</h2>
            <div className="card">
              <p>
                <strong>{s.rows.toLocaleString("en-GB")}</strong> transactions from {s.first_date} to{" "}
                {s.last_date}: <strong>{s.new.toLocaleString("en-GB")} new</strong>,{" "}
                {s.already_imported.toLocaleString("en-GB")} already imported
                {s.skipped > 0 && <>, {s.skipped} skipped</>}.
              </p>
              <div className="table-wrap">
                <table>
                  <tbody>
                    {Object.entries(s.by_kind).map(([kind, count]) => (
                      <tr key={kind}>
                        <td>{KIND_LABELS[kind] ?? kind}</td>
                        <td className="num">{count.toLocaleString("en-GB")}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="muted">
                {s.instruments} instruments, {s.savings_plan_executions} savings-plan execution
                {s.savings_plan_executions === 1 ? "" : "s"}. Net cash
                flow in this file: {eur(s.net_cash_flow)}. If the file covers your whole account history,
                this should match your cash balance.
              </p>
              {s.warning_count > 0 && (
                <div className="notice">
                  <p>
                    {s.warning_count} warning{s.warning_count > 1 ? "s" : ""}:
                  </p>
                  <ul>
                    {s.warnings.map((w) => (
                      <li key={w} className="mono">
                        {w}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              <p style={{ display: "flex", gap: 12, marginTop: 16 }}>
                <button onClick={() => decide("commit")} disabled={busy}>
                  Import {s.new.toLocaleString("en-GB")} transactions
                </button>
                <button className="link" onClick={() => decide("discard")} disabled={busy}>
                  Discard
                </button>
              </p>
            </div>
          </>
        )}

        <h2>Previous imports</h2>
        <div className="card table-wrap">
          <table>
            <thead>
              <tr>
                <th>Uploaded</th>
                <th>Period</th>
                <th>Status</th>
                <th className="num">Rows added</th>
              </tr>
            </thead>
            <tbody>
              {history.map((h) => (
                <tr key={h.id}>
                  <td>{new Date(h.created_at).toLocaleString("en-GB")}</td>
                  <td>
                    {h.summary.first_date} – {h.summary.last_date}
                  </td>
                  <td>{h.status}</td>
                  <td className="num">{h.status === "committed" ? h.rows_inserted : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {history.length === 0 && <p className="muted">Nothing imported yet.</p>}
        </div>
      </main>
    </>
  );
}
