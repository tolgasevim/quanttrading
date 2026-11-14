"use client";

import { useEffect, useState } from "react";
import { api, ApiError, type Crypto } from "@/lib/api";
import { Header } from "../Header";
import { useUser } from "../useUser";

const eur = (value: string | null) =>
  value === null ? "—" : Number(value).toLocaleString("en-GB", { style: "currency", currency: "EUR" });

export default function CryptoPage() {
  const user = useUser();
  const [data, setData] = useState<Crypto | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!user) return;
    api<Crypto>("/api/crypto")
      .then(setData)
      .catch((err) => setError(err instanceof ApiError ? err.detail : "Could not load crypto."));
  }, [user]);

  if (!user) return null;

  return (
    <>
      <Header user={user} />
      <main>
        <h1>Crypto and tax</h1>
        <p className="notice">
          Units you hold <strong>more than one year</strong> are tax-free in Germany. Units held a year or less are taxed
          at your personal rate, and only if the year&apos;s gains reach the Freigrenze: one euro over, and all of it is
          taxable. Units are sold oldest first. {data?.note}
        </p>
        {error && <p className="notice status-failed">{error}</p>}

        {data && (
          <>
            <div className="card table-wrap" style={{ marginBottom: 16 }}>
              <h2 style={{ marginTop: 0 }}>{data.year} so far</h2>
              <table>
                <tbody>
                  <tr>
                    <td>Gains from units held a year or less (taxable)</td>
                    <td className={`num ${data.over_limit ? "status-failed" : ""}`}>{eur(data.realised_taxable)}</td>
                  </tr>
                  <tr>
                    <td>Freigrenze</td>
                    <td className="num">{eur(data.freigrenze)}</td>
                  </tr>
                  <tr>
                    <td>Room left under the Freigrenze</td>
                    <td className={`num ${data.over_limit ? "status-failed" : "status-success"}`}>
                      {data.over_limit ? "none: all of it is taxable" : eur(data.headroom)}
                    </td>
                  </tr>
                  <tr>
                    <td>Gains from units held over a year (tax-free)</td>
                    <td className="num">{eur(data.realised_free)}</td>
                  </tr>
                </tbody>
              </table>
            </div>

            <div className="card" style={{ marginBottom: 16 }}>
              <h2 style={{ marginTop: 0 }}>What you hold</h2>
              <p>
                {eur(data.value)} in coins, {eur(data.free_value)} of it already tax-free. Selling the rest now would
                realise about {eur(data.taxable_gain_if_sold)} of taxable gain. Coins and crypto-linked shares together are{" "}
                <strong>{Number(data.exposure_pct).toFixed(1)}%</strong> of your portfolio, against a limit of {Number(data.cap_pct).toFixed(0)}%.
              </p>
            </div>

            {data.coins.length === 0 && <p className="notice">You hold no coins.</p>}
            {data.coins.map((c) => (
              <div className="card table-wrap" style={{ marginBottom: 16 }} key={c.isin}>
                <h2 style={{ marginTop: 0 }}>{c.name}</h2>
                <p>
                  {Number(c.quantity)} units, {eur(c.value)}. {Number(c.free_share_pct).toFixed(0)}% of the units are already
                  tax-free.
                  {c.next_free_date && ` The next ${Number(c.next_free_quantity)} units turn tax-free on ${c.next_free_date}.`}
                </p>
                <table>
                  <thead>
                    <tr>
                      <th>Bought</th>
                      <th className="num">Units</th>
                      <th className="num">Cost</th>
                      <th className="num">Value</th>
                      <th className="num">Gain</th>
                      <th>Tax-free from</th>
                    </tr>
                  </thead>
                  <tbody>
                    {c.lots.map((l) => (
                      <tr key={`${l.acquired}-${l.quantity}`}>
                        <td>{l.acquired}</td>
                        <td className="num">{Number(l.quantity)}</td>
                        <td className="num">{eur(l.cost)}</td>
                        <td className="num">{eur(l.value)}</td>
                        <td className="num">{eur(l.gain)}</td>
                        <td className={l.free ? "status-success" : ""}>
                          {l.free ? "tax-free now" : `${l.tax_free_from} (${l.days_left} days)`}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ))}
          </>
        )}
      </main>
    </>
  );
}
