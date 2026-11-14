"use client";

import { useCallback, useEffect, useState } from "react";
import { api, ApiError, del, type Markets, post } from "@/lib/api";
import { Header } from "../Header";
import { useUser } from "../useUser";

const num = (value: string | null, digits = 2) =>
  value === null ? "—" : Number(value).toLocaleString("en-GB", { minimumFractionDigits: digits, maximumFractionDigits: digits });
const change = (value: string | null | undefined) => {
  if (value === null || value === undefined) return <span className="muted">—</span>;
  const n = Number(value);
  return <span className={n > 0 ? "status-success" : n < 0 ? "status-failed" : ""}>{`${n > 0 ? "+" : ""}${n.toFixed(1)}%`}</span>;
};
const REGIME_TEXT = { "risk-on": "Risk-on", "risk-off": "Risk-off", mixed: "Mixed", unknown: "Not enough data" } as const;
const COMMODITIES = ["gold", "silver", "wti", "brent", "copper", "natgas"];

export default function MarketsPage() {
  const user = useUser();
  const [data, setData] = useState<Markets | null>(null);
  const [error, setError] = useState("");
  const [form, setForm] = useState({ commodity: "gold", isin: "", name: "", kind: "physical", issuer: "", ter: "", notes: "" });

  const load = useCallback(async () => {
    try {
      setData(await api<Markets>("/api/markets"));
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Could not load the markets.");
    }
  }, []);
  useEffect(() => {
    if (user) void load();
  }, [user, load]);

  if (!user) return null;

  const add = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    try {
      const body: Record<string, string> = { commodity: form.commodity, isin: form.isin, name: form.name, kind: form.kind };
      if (form.issuer) body.issuer = form.issuer;
      if (form.ter) body.ter = form.ter;
      if (form.notes) body.notes = form.notes;
      await post("/api/markets/products", body);
      setForm({ ...form, isin: "", name: "", issuer: "", ter: "", notes: "" });
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Could not add the product.");
    }
  };

  return (
    <>
      <Header user={user} />
      <main>
        <h1>Commodities and the backdrop</h1>
        <p className="notice">{data?.note ?? "Information only. The app places no trade."}</p>
        {error && <p className="notice status-failed">{error}</p>}

        {data && (
          <>
            <div className="card table-wrap" style={{ marginBottom: 16 }}>
              <h2 style={{ marginTop: 0 }}>Commodities</h2>
              <table>
                <thead>
                  <tr>
                    <th>Commodity</th>
                    <th className="num">USD</th>
                    <th className="num">EUR</th>
                    <th className="num">1 week</th>
                    <th className="num">1 month</th>
                    <th className="num">1 year</th>
                    <th className="num">5 years</th>
                    <th className="num">52-week range</th>
                  </tr>
                </thead>
                <tbody>
                  {data.commodities.map((c) => (
                    <tr key={c.code}>
                      <td>
                        {c.name}
                        <div className="muted">{c.unit}</div>
                        {c.stale && <div className="status-failed">{c.usd === null ? "No prices yet" : "The price is old"}</div>}
                      </td>
                      <td className="num">{num(c.usd)}</td>
                      <td className="num">{num(c.eur)}</td>
                      {["1w", "1m", "1y", "5y"].map((k) => (
                        <td className="num" key={k}>
                          {change(c.changes[k])}
                        </td>
                      ))}
                      <td className="num">
                        {num(c.low_52w)} to {num(c.high_52w)}
                        {c.range_pos !== null && <div className="muted">at {Number(c.range_pos).toFixed(0)}% of the range</div>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {data.drivers.map((g) => (
              <div className="card table-wrap" style={{ marginBottom: 16 }} key={g.title}>
                <h2 style={{ marginTop: 0 }}>{g.title}</h2>
                <table>
                  <tbody>
                    {g.items.map((i) => (
                      <tr key={i.label}>
                        <td style={{ width: "30%" }}>{i.label}</td>
                        <td className="num">{i.value === null ? "—" : `${num(i.value)} ${i.unit}`}</td>
                        <td style={{ whiteSpace: "normal" }} className={i.available ? "muted" : "status-failed"}>
                          {i.detail}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ))}

            <div className="card" style={{ marginBottom: 16 }}>
              <h2 style={{ marginTop: 0 }}>Regime: {REGIME_TEXT[data.regime.label]}</h2>
              <p>{data.regime.implication}</p>
              <table>
                <tbody>
                  {data.regime.signals.map((s) => (
                    <tr key={s.name}>
                      <td>{s.name}</td>
                      <td className={s.vote > 0 ? "status-success" : s.vote < 0 ? "status-failed" : "muted"}>{s.detail}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="muted">{data.regime.disclaimer}</p>
            </div>

            <div className="card" style={{ marginBottom: 16 }}>
              <h2 style={{ marginTop: 0 }}>Is gold hedging you?</h2>
              <p>{data.hedge.verdict}</p>
              {data.hedge.windows.length > 0 && (
                <table>
                  <thead>
                    <tr>
                      <th>Last</th>
                      <th className="num">Days used</th>
                      <th className="num">Correlation</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.hedge.windows.map((w) => (
                      <tr key={w.days}>
                        <td>{w.days} days</td>
                        <td className="num">{w.observations}</td>
                        <td className="num">{w.correlation === null ? "—" : w.correlation.toFixed(2)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
              <p className="muted">
                Correlation of the daily returns of your portfolio with gold in euros. -1 moves against, 0 not related, 1 moves with.
              </p>
            </div>

            <div className="card table-wrap">
              <h2 style={{ marginTop: 0 }}>Products that follow a commodity</h2>
              <p className="muted">The app cannot check what your broker offers. Confirm a product there before you rely on it.</p>
              {data.products.length === 0 ? (
                <p className="muted">None listed.</p>
              ) : (
                <table>
                  <thead>
                    <tr>
                      <th>Commodity</th>
                      <th>Product</th>
                      <th>Kind</th>
                      <th className="num">Cost a year</th>
                      <th>At your broker</th>
                      {user.role === "admin" && <th />}
                    </tr>
                  </thead>
                  <tbody>
                    {data.products.map((p) => (
                      <tr key={p.id}>
                        <td>{p.commodity}</td>
                        <td style={{ whiteSpace: "normal" }}>
                          {p.name}
                          <div className="muted">{p.isin}{p.issuer ? `, ${p.issuer}` : ""}</div>
                          <div className="muted">{p.risk}</div>
                          {p.notes && <div className="muted">{p.notes}</div>}
                        </td>
                        <td>{p.kind}</td>
                        <td className="num">{p.ter === null ? "unknown" : `${p.ter}%`}</td>
                        <td>{p.tr_available === null ? "not confirmed" : p.tr_available ? "yes" : "no"}</td>
                        {user.role === "admin" && (
                          <td>
                            <button className="link" onClick={() => del(`/api/markets/products/${p.id}`).then(load)}>
                              Remove
                            </button>
                          </td>
                        )}
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
              {user.role === "admin" && (
                <form onSubmit={add} style={{ marginTop: 12 }}>
                  <h3>Add a product</h3>
                  <label>
                    Commodity
                    <select value={form.commodity} onChange={(e) => setForm({ ...form, commodity: e.target.value })}>
                      {COMMODITIES.map((c) => (
                        <option key={c}>{c}</option>
                      ))}
                    </select>
                  </label>
                  <label>
                    ISIN
                    <input required maxLength={12} value={form.isin} onChange={(e) => setForm({ ...form, isin: e.target.value })} />
                  </label>
                  <label>
                    Name
                    <input required value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
                  </label>
                  <label>
                    Kind
                    <select value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value })}>
                      {Object.keys(data.risks).map((k) => (
                        <option key={k}>{k}</option>
                      ))}
                    </select>
                  </label>
                  <label>
                    Issuer (optional)
                    <input value={form.issuer} onChange={(e) => setForm({ ...form, issuer: e.target.value })} />
                  </label>
                  <label>
                    Cost a year, percent (optional)
                    <input inputMode="decimal" value={form.ter} onChange={(e) => setForm({ ...form, ter: e.target.value })} />
                  </label>
                  <div>
                    <button type="submit">Add</button>
                  </div>
                </form>
              )}
            </div>
          </>
        )}
      </main>
    </>
  );
}
