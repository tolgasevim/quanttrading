"use client";

import { type ChangeEvent, useEffect, useState } from "react";
import { api, ApiError, type Finding, type Holdings, uploadFile } from "@/lib/api";
import { Header } from "../Header";
import { useUser } from "../useUser";

const CLASS_LABELS: Record<string, string> = {
  STOCK: "Stocks",
  FUND: "Funds and ETFs",
  CRYPTO: "Crypto",
  BOND: "Bonds",
  PRIVATE_FUND: "Private-market funds",
  DERIVATIVE: "Derivatives",
};

const STATUS_TEXT: Record<Finding["status"], string> = {
  match: "Matches",
  quantity_mismatch: "Quantity differs",
  missing_in_history: "On the statement, not in your history",
  not_on_statement: "In your history, not on the statement",
};

const SOURCE_TEXT: Record<string, string> = { tr_crypto_statement: "Crypto statement" };

const qty = (value: string | null) =>
  value === null ? "—" : Number(value).toLocaleString("en-GB", { maximumFractionDigits: 8 });

export default function HoldingsPage() {
  const user = useUser();
  const [data, setData] = useState<Holdings | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (user) api<Holdings>("/api/holdings").then(setData);
  }, [user]);

  if (!user) return null;

  const onFile = async (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    setBusy(true);
    setError("");
    try {
      setData(await uploadFile<Holdings>("/api/holdings/statements/crypto", file));
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Upload failed.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <Header user={user} />
      <main>
        <h1>Holdings</h1>
        {data && data.positions.length === 0 && (
          <p className="notice">
            No holdings yet. <a href="/import">Import your transactions</a> first.
          </p>
        )}

        {data && data.positions.length > 0 && (
          <div className="card">
            <p>
              <strong>{data.positions.length}</strong> open positions rebuilt from your transaction
              history
              {data.verified > 0 && <>, {data.verified} confirmed by a statement</>}.
            </p>
            <p className="muted">
              {Object.entries(data.by_class)
                .map(([cls, n]) => `${CLASS_LABELS[cls] ?? cls}: ${n}`)
                .join(" · ")}
            </p>
          </div>
        )}

        <h2>Check against a statement</h2>
        <div className="card">
          <p>
            Upload the <em>Crypto-Übersicht</em> PDF from Trade Republic (Profile → Documents). The
            app compares it with the holdings above and lists every difference. The file is read and
            discarded; your name and account number are never stored.
          </p>
          <input type="file" accept="application/pdf,.pdf" onChange={onFile} disabled={busy} />
          {busy && <p className="muted">Reading…</p>}
          {error && <p className="error">{error}</p>}
          <p className="muted">The securities statement (Depotauszug) check follows in the next update.</p>
        </div>

        {data?.reconciliations.map((r) => (
          <div key={r.source} className="card" style={{ marginTop: 16 }}>
            <h2 style={{ marginTop: 0 }}>
              {SOURCE_TEXT[r.source] ?? r.source}, {r.as_of}
            </h2>
            {r.review.length === 0 ? (
              <p className="status-success">
                All {r.counts.match} positions match the statement.
              </p>
            ) : (
              <>
                <p>
                  {r.counts.match} match, <strong>{r.review.length}</strong> {r.review.length === 1 ? "needs" : "need"} a look:
                </p>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>Instrument</th>
                        <th>What differs</th>
                        <th className="num">Your history</th>
                        <th className="num">Statement</th>
                      </tr>
                    </thead>
                    <tbody>
                      {r.review.map((f) => (
                        <tr key={`${f.isin ?? f.name}-${f.status}`}>
                          <td>{f.name}</td>
                          <td className="status-failed">{STATUS_TEXT[f.status]}</td>
                          <td className="num">{qty(f.history_quantity)}</td>
                          <td className="num">{qty(f.statement_quantity)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            )}
          </div>
        ))}

        {data && data.positions.length > 0 && (
          <>
            <h2>Open positions</h2>
            <div className="card table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Instrument</th>
                    <th>ISIN</th>
                    <th>Type</th>
                    <th className="num">Quantity</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {data.positions.map((p) => (
                    <tr key={p.isin}>
                      <td>{p.name ?? "—"}</td>
                      <td className="mono">{p.isin}</td>
                      <td>{CLASS_LABELS[p.asset_class ?? ""] ?? p.asset_class ?? "—"}</td>
                      <td className="num">{qty(p.quantity)}</td>
                      <td>
                        {p.verified ? (
                          <span className="status-success">confirmed</span>
                        ) : p.differs ? (
                          <span className="status-failed">differs from statement</span>
                        ) : (
                          <span className="muted">not yet checked</span>
                        )}
                        {p.corporate_action && <span className="muted"> · split, merger or similar</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </main>
    </>
  );
}
