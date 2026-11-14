"use client";

import { useCallback, useEffect, useState } from "react";
import { api, ApiError, type Concentration, type ConcentrationSlice, del, type FundFile } from "@/lib/api";
import { Header } from "../Header";
import { useUser } from "../useUser";

const eur = (value: string) =>
  Number(value).toLocaleString("en-GB", { style: "currency", currency: "EUR" });
const pct = (value: string) => `${Number(value).toFixed(1)}%`;

const TYPE_TEXT: Record<string, string> = {
  stock: "Share",
  etf: "ETF",
  etc: "Commodity ETC",
  crypto: "Coin",
  leveraged: "Leveraged ETP",
  knockout: "Knock-out",
  bond: "Bond fund",
  private_fund: "Private-market fund",
  other: "Other",
};

function Bars({ title, rows }: { title: string; rows: ConcentrationSlice[] }) {
  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <h2 style={{ marginTop: 0 }}>{title}</h2>
      <table>
        <tbody>
          {rows.slice(0, 12).map((r) => (
            <tr key={r.label}>
              <td style={{ width: "40%" }}>{r.label}</td>
              <td style={{ width: "40%" }}>
                <div
                  style={{
                    height: 10,
                    width: `${Math.min(Number(r.pct), 100)}%`,
                    background: "var(--accent)",
                    borderRadius: 3,
                  }}
                />
              </td>
              <td className="num">{pct(r.pct)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length > 12 && <p className="muted">{rows.length - 12} smaller lines are not shown.</p>}
    </div>
  );
}

export default function ConcentrationPage() {
  const user = useUser();
  const [data, setData] = useState<Concentration | null>(null);
  const [files, setFiles] = useState<FundFile[]>([]);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [isin, setIsin] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [c, f] = await Promise.all([
        api<Concentration>("/api/concentration"),
        api<FundFile[]>("/api/lookthrough"),
      ]);
      setData(c);
      setFiles(f);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Could not load the concentration.");
    }
  }, []);
  useEffect(() => {
    if (user) void load();
  }, [user, load]);

  if (!user) return null;

  const upload = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const input = e.currentTarget.elements.namedItem("file") as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const body = new FormData();
      body.append("file", file);
      body.append("source", "issuer file");
      // No JSON header here: the browser sets the multipart boundary itself.
      const response = await fetch(`/api/lookthrough/${isin.trim().toUpperCase()}`, {
        method: "POST",
        body,
        credentials: "same-origin",
      });
      if (!response.ok) {
        const detail = await response.json().catch(() => ({}));
        throw new ApiError(response.status, typeof detail.detail === "string" ? detail.detail : response.statusText);
      }
      const result: FundFile = await response.json();
      setMessage(`Stored ${result.rows} holdings for ${result.etf_isin}.`);
      setIsin("");
      input.value = "";
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "The upload failed.");
    } finally {
      setBusy(false);
    }
  };

  const remove = async (f: FundFile) => {
    if (!window.confirm(`Remove the holdings file of ${f.name ?? f.etf_isin}?`)) return;
    try {
      await del(`/api/lookthrough/${f.etf_isin}`);
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Could not remove the file.");
    }
  };

  return (
    <>
      <Header user={user} />
      <main>
        <h1>Concentration and look-through</h1>
        <p className="notice">
          Every euro of your valued holdings placed on the shares behind it. A fund counts for the
          shares in its holdings file, so the same share held directly and through funds is one
          line. A fund with no file is shown as it is, not guessed.
        </p>
        {error && <p className="notice status-failed">{error}</p>}
        {message && <p className="notice status-success">{message}</p>}
        {data?.warnings.map((w) => (
          <p className="notice" key={w}>
            {w}
          </p>
        ))}

        {data && data.positions.length === 0 && (
          <p className="notice">
            Nothing to show yet. <a href="/import">Import your transactions</a> first.
          </p>
        )}

        {data && data.positions.length > 0 && (
          <>
            <div className="card table-wrap" style={{ marginBottom: 16 }}>
              <h2 style={{ marginTop: 0 }}>The biggest shares, with what the funds add</h2>
              <p className="muted">
                Valued holdings: {eur(data.total)}.
                {data.covered_pct !== null && ` ${pct(data.covered_pct)} of your fund value is looked through.`}{" "}
                Coins and crypto-linked shares together: {pct(data.crypto_pct)}.
              </p>
              <table>
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Sector</th>
                    <th>Country</th>
                    <th className="num">Directly</th>
                    <th className="num">Through funds</th>
                    <th className="num">Total</th>
                    <th>Through</th>
                  </tr>
                </thead>
                <tbody>
                  {data.underlying.map((u) => (
                    <tr key={u.key}>
                      <td>{u.name}</td>
                      <td>{u.sector}</td>
                      <td>{u.country}</td>
                      <td className="num">{pct(u.direct_pct)}</td>
                      <td className="num">{pct(u.indirect_pct)}</td>
                      <td className="num">
                        <strong>{pct(u.total_pct)}</strong>
                      </td>
                      <td className="muted">{u.via.map((v) => `${v.fund} ${pct(v.pct)}`).join(", ")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div style={{ display: "grid", gap: 0 }}>
              <Bars title="By sector" rows={data.by_sector} />
              <Bars title="By country" rows={data.by_country} />
              <Bars title="By currency (what the money is exposed to)" rows={data.by_currency} />
              <Bars title="By type of instrument" rows={data.by_class.map((r) => ({ ...r, label: TYPE_TEXT[r.label] ?? r.label }))} />
            </div>

            <div className="card table-wrap" style={{ marginBottom: 16 }}>
              <h2 style={{ marginTop: 0 }}>Your positions by type</h2>
              <table>
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Type</th>
                    <th className="num">Share</th>
                    <th>Notes</th>
                  </tr>
                </thead>
                <tbody>
                  {data.positions.map((p) => (
                    <tr key={p.isin}>
                      <td>{p.name}</td>
                      <td>{TYPE_TEXT[p.type] ?? p.type}</td>
                      <td className="num">{pct(p.pct)}</td>
                      <td className="muted">
                        {[
                          p.trading_book ? "Trading book: kept apart from your long-term plan." : "",
                          p.crypto_linked ? "Counts as crypto exposure." : "",
                          p.looked_through === false ? "No holdings file." : "",
                          ...p.flags,
                        ]
                          .filter(Boolean)
                          .join(" ")}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}

        <div className="card table-wrap">
          <h2 style={{ marginTop: 0 }}>Holdings files</h2>
          {files.length === 0 ? (
            <p className="muted">No holdings file has been stored yet.</p>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Fund</th>
                  <th className="num">Rows</th>
                  <th className="num">Weights</th>
                  <th>As of</th>
                  {user.role === "admin" && <th />}
                </tr>
              </thead>
              <tbody>
                {files.map((f) => (
                  <tr key={f.etf_isin}>
                    <td>
                      {f.name ?? f.etf_isin}
                      <div className="muted">{f.etf_isin}</div>
                    </td>
                    <td className="num">{f.rows}</td>
                    <td className="num">{pct(f.total_weight)}</td>
                    <td>{f.as_of}</td>
                    {user.role === "admin" && (
                      <td>
                        <button className="link" onClick={() => remove(f)}>
                          Remove
                        </button>
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {user.role === "admin" ? (
            <>
              <h3>Upload a holdings file</h3>
              <p className="muted">
                Download the holdings file from the fund issuer&apos;s page (a CSV with a weight column).
                One file serves every user. A new file replaces the old one.
              </p>
              <form onSubmit={upload}>
                <label>
                  ISIN of the fund
                  <input required maxLength={12} value={isin} onChange={(e) => setIsin(e.target.value)} />
                </label>
                <label>
                  File
                  <input name="file" type="file" accept=".csv,.txt" required />
                </label>
                <div>
                  <button type="submit" disabled={busy}>
                    Upload
                  </button>
                </div>
              </form>
            </>
          ) : (
            <p className="muted">Only the admin can upload a holdings file.</p>
          )}
        </div>
      </main>
    </>
  );
}
