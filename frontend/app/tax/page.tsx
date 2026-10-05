"use client";

import { useEffect, useState } from "react";
import { api, ApiError, type TaxEstimate } from "@/lib/api";
import { Header } from "../Header";
import { useUser } from "../useUser";

// Display only: every figure is computed on the server as an exact decimal.
const eur = (value: string | null) =>
  value === null ? "—" : Number(value).toLocaleString("en-GB", { style: "currency", currency: "EUR" });
const tone = (value: string) =>
  Number(value) === 0 ? "" : Number(value) > 0 ? "status-success" : "status-failed";

export default function TaxPage() {
  const user = useUser();
  const [data, setData] = useState<TaxEstimate | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!user) return;
    api<TaxEstimate>("/api/tax")
      .then(setData)
      .catch((err) => setError(err instanceof ApiError ? err.detail : "Could not load the tax estimate."));
  }, [user]);

  if (!user) return null;

  const years = data ? [...data.years].reverse() : [];

  return (
    <>
      <Header user={user} />
      <main>
        <h1>Tax estimate</h1>
        <p className="notice">
          An estimate for planning, not tax advice. It follows German capital-gains rules and works
          from your imported history.
        </p>

        {error && <p className="notice status-failed">{error}</p>}

        {data && data.years.length === 0 && (
          <p className="notice">
            Nothing to estimate yet. <a href="/import">Import your transactions</a> first.
          </p>
        )}

        {years.map((y) => (
          <div className="card table-wrap" key={y.year} style={{ marginBottom: 16 }}>
            <h2 style={{ marginTop: 0 }}>{y.year}</h2>
            {y.cost_unknown_sales > 0 && (
              <p className="status-failed">
                {y.cost_unknown_sales} sale{y.cost_unknown_sales > 1 ? "s" : ""} of units without a known
                cost: the gain, and so the tax, is overstated.
              </p>
            )}
            <table>
              <tbody>
                <tr>
                  <td>Gains and losses on shares</td>
                  <td className={`num ${tone(y.stock_pnl)}`}>{eur(y.stock_pnl)}</td>
                </tr>
                <tr>
                  <td>
                    Funds and ETFs, after the 30% exemption
                    {y.fund_disposals > 0 && (
                      <div className="muted">{y.fund_disposals} sales, all treated as equity funds</div>
                    )}
                  </td>
                  <td className={`num ${tone(y.fund_pnl)}`}>{eur(y.fund_pnl)}</td>
                </tr>
                <tr>
                  <td>Bonds, derivatives and private funds</td>
                  <td className={`num ${tone(y.other_pnl)}`}>{eur(y.other_pnl)}</td>
                </tr>
                <tr>
                  <td>Dividends, distributions and interest</td>
                  <td className="num">{eur(y.income)}</td>
                </tr>
                {(Number(y.stock_loss_brought) > 0 || Number(y.general_loss_brought) > 0) && (
                  <tr>
                    <td>Losses brought forward (shares / other)</td>
                    <td className="num">
                      {eur(y.stock_loss_brought)} / {eur(y.general_loss_brought)}
                    </td>
                  </tr>
                )}
                <tr>
                  <td>Taxable before the allowance</td>
                  <td className="num">{eur(y.taxable_before_allowance)}</td>
                </tr>
                <tr>
                  <td>Sparerpauschbetrag used</td>
                  <td className="num">−{eur(y.allowance_used)}</td>
                </tr>
                <tr>
                  <td>
                    <strong>Estimated tax</strong>
                    <div className="muted">25% plus 5.5% solidarity surcharge on {eur(y.taxable)}</div>
                  </td>
                  <td className="num">
                    <strong>{eur(y.tax)}</strong>
                  </td>
                </tr>
                <tr>
                  <td>Withheld by Trade Republic, net of its refunds</td>
                  <td className="num">{eur(y.withheld)}</td>
                </tr>
                <tr>
                  <td>
                    {Number(y.to_settle) > 0
                      ? "Still owed on this estimate"
                      : Number(y.to_settle) < 0
                        ? "Refundable on this estimate"
                        : "Nothing more to settle"}
                  </td>
                  <td className={`num ${tone(String(-Number(y.to_settle)))}`}>
                    {eur(String(Math.abs(Number(y.to_settle))))}
                  </td>
                </tr>
                {(Number(y.stock_loss_carried) > 0 || Number(y.general_loss_carried) > 0) && (
                  <tr>
                    <td>Losses carried to next year (shares / other)</td>
                    <td className="num">
                      {eur(y.stock_loss_carried)} / {eur(y.general_loss_carried)}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
            {(Number(y.crypto.taxable_gain) !== 0 || Number(y.crypto.tax_free_gain) !== 0) && (
              <p className="muted">
                Crypto (taxed as private sales, not at the flat rate): {eur(y.crypto.tax_free_gain)} is
                tax-free after a year&apos;s holding. {eur(y.crypto.taxable_gain)} is from units held a
                year or less;{" "}
                {y.crypto.under_freigrenze
                  ? `below the ${eur(y.crypto.freigrenze)} limit, so nothing is due`
                  : `above the ${eur(y.crypto.freigrenze)} limit, so all of it is taxed at your personal rate`}
                .
              </p>
            )}
          </div>
        ))}

        {data && data.years.length > 0 && (
          <div className="card">
            <h2 style={{ marginTop: 0 }}>What this assumes</h2>
            <ul>
              {data.assumptions.map((a) => (
                <li key={a}>{a}</li>
              ))}
            </ul>
          </div>
        )}
      </main>
    </>
  );
}
