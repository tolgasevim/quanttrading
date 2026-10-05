"use client";

import { type ChangeEvent, useEffect, useState } from "react";
import { api, ApiError, type Finding, type Holdings, type Position, uploadFile } from "@/lib/api";
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

const FLAG_TEXT: Record<Position["cost_flags"][number], string> = {
  cost_unknown: "cost not given by the broker",
  price_derived: "valued at the price on receipt",
  carried: "carried over by a corporate action",
  incomplete_history: "history incomplete",
  cost_entered: "cost entered by you",
};

// Display only: every figure is computed on the server as an exact decimal.
const qty = (value: string | null) =>
  value === null ? "—" : Number(value).toLocaleString("en-GB", { maximumFractionDigits: 8 });
const eur = (value: string | null) =>
  value === null ? "—" : Number(value).toLocaleString("en-GB", { style: "currency", currency: "EUR" });
const tone = (value: string | null) =>
  value === null || Number(value) === 0 ? "" : Number(value) > 0 ? "status-success" : "status-failed";

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

  const unknownCost = data?.positions.filter((p) => p.cost_flags.includes("cost_unknown")) ?? [];
  const historyGap = data?.positions.filter((p) => p.cost_flags.includes("incomplete_history")) ?? [];

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

        {unknownCost.length > 0 && (
          <p className="notice">
            <strong>
              {unknownCost.length} {unknownCost.length === 1 ? "position has" : "positions have"} no cost
              basis.
            </strong>{" "}
            Trade Republic doesn&apos;t say what a spin-off, rights issue or free share cost, so profit
            on {unknownCost.length === 1 ? "it" : "them"} can&apos;t be worked out yet:{" "}
            {unknownCost.map((p) => p.name ?? p.isin).join(", ")}.{" "}
            <a href="/costs">Enter what a unit cost</a> and it is included everywhere.
          </p>
        )}
        {data && data.review.unpriced > 0 && (
          <p className="notice">
            <strong>
              {data.review.unpriced} open {data.review.unpriced === 1 ? "position has" : "positions have"} no
              market price.
            </strong>{" "}
            Crypto is priced from your statement; for shares and funds, <a href="/prices">see which
            tickers are missing</a>.
          </p>
        )}
        {historyGap.length > 0 && (
          <p className="notice">
            <strong>
              {historyGap.length} {historyGap.length === 1 ? "position includes" : "positions include"} units
              your history never bought.
            </strong>{" "}
            That usually means an import is missing, so cost and profit on{" "}
            {historyGap.length === 1 ? "it" : "them"} can&apos;t be worked out:{" "}
            {historyGap.map((p) => p.name ?? p.isin).join(", ")}.{" "}
            <a href="/import">Import more history</a>
          </p>
        )}
        {data && data.review.unattributed_cash.length > 0 && (
          <p className="notice">
            {data.review.unattributed_cash.length === 1
              ? "One cash payment from a corporate action doesn't fit any share movement and is left out of profit and loss"
              : `${data.review.unattributed_cash.length} cash payments from corporate actions don't fit any share movement and are left out of profit and loss`}
            :{" "}
            {data.review.unattributed_cash
              .map((u) => `${u.name ?? u.isin} ${u.date} (${eur(u.amount)})`)
              .join("; ")}
            .
          </p>
        )}

        <h2>Check against a statement</h2>
        <div className="card">
          <p>
            Upload the <em>Crypto-Übersicht</em> PDF from Trade Republic (Profile → Documents). The
            app compares it with the holdings below, checks the purchase value it prints against the
            cost rebuilt from your history, and lists every difference. The file is read and
            discarded; your name and account number are never stored.
          </p>
          <input type="file" accept="application/pdf,.pdf" onChange={onFile} disabled={busy} />
          {busy && <p className="muted">Reading…</p>}
          {error && <p className="error">{error}</p>}
          <p className="muted">The securities statement (Depotauszug) check follows in the next update.</p>
        </div>

        {data?.reconciliations.map((r) => {
          const badCosts = r.cost_checks.filter((c) => !c.ok);
          return (
            <div key={r.source} className="card" style={{ marginTop: 16 }}>
              <h2 style={{ marginTop: 0 }}>
                {SOURCE_TEXT[r.source] ?? r.source}, {r.as_of}
              </h2>
              {r.review.length === 0 ? (
                <p className="status-success">All {r.counts.match} positions match the statement.</p>
              ) : (
                <>
                  <p>
                    {r.counts.match} match, <strong>{r.review.length}</strong>{" "}
                    {r.review.length === 1 ? "needs" : "need"} a look:
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
              {r.cost_checks.length > 0 &&
                (badCosts.length === 0 ? (
                  <p className="status-success">
                    The purchase value matches the statement for all {r.cost_checks.length} positions.
                  </p>
                ) : (
                  <>
                    <p>
                      Purchase value differs for <strong>{badCosts.length}</strong> of{" "}
                      {r.cost_checks.length}:
                    </p>
                    <div className="table-wrap">
                      <table>
                        <thead>
                          <tr>
                            <th>Instrument</th>
                            <th className="num">Statement</th>
                            <th className="num">Rebuilt from history</th>
                            <th className="num">Difference</th>
                          </tr>
                        </thead>
                        <tbody>
                          {badCosts.map((c) => (
                            <tr key={c.name}>
                              <td>{c.name}</td>
                              <td className="num">{eur(c.statement_cost)}</td>
                              <td className="num">{eur(c.computed_cost)}</td>
                              <td className="num status-failed">{eur(c.difference)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </>
                ))}
            </div>
          );
        })}

        {data && data.realised.by_year.length > 0 && (
          <>
            <h2>Realised profit and loss</h2>
            <div className="card table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Year</th>
                    <th className="num">Gains</th>
                    <th className="num">Losses</th>
                    <th className="num">Net</th>
                    <th className="num">Fees</th>
                    <th className="num">Tax withheld</th>
                    <th className="num">Sales</th>
                  </tr>
                </thead>
                <tbody>
                  {data.realised.by_year.map((y) => (
                    <tr key={y.year}>
                      <td>{y.year}</td>
                      <td className="num">{eur(y.gains)}</td>
                      <td className="num">{eur(y.losses)}</td>
                      <td className={`num ${tone(y.net)}`}>
                        {eur(y.net)}
                        {y.cost_unknown_sales > 0 && (
                          <div className="status-failed">
                            {y.cost_unknown_sales} sale{y.cost_unknown_sales > 1 ? "s" : ""} without
                            a known cost: gain overstated
                          </div>
                        )}
                        {y.history_gap_sales > 0 && (
                          <div className="status-failed">
                            {y.history_gap_sales} sale{y.history_gap_sales > 1 ? "s" : ""} of units your
                            history never bought: an import is probably missing
                          </div>
                        )}
                      </td>
                      <td className="num">{eur(y.fees)}</td>
                      <td className="num">{eur(y.tax_withheld)}</td>
                      <td className="num">{y.disposals}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="muted">
                Before tax and after fees, selling the oldest units first. Sales, liquidations and
                expiries all count. Tax Trade Republic withheld is shown separately and is not
                deducted. Everything from your history combined: {eur(data.realised.net_total)}.
              </p>
            </div>
            {(data.realised.best.length > 0 || data.realised.worst.length > 0) && (
              <div className="card table-wrap" style={{ marginTop: 16 }}>
                <table>
                  <thead>
                    <tr>
                      <th>Biggest gains</th>
                      <th className="num"></th>
                      <th>Biggest losses</th>
                      <th className="num"></th>
                    </tr>
                  </thead>
                  <tbody>
                    {Array.from({
                      length: Math.max(data.realised.best.length, data.realised.worst.length),
                    }).map((_, i) => {
                      const g = data.realised.best[i];
                      const l = data.realised.worst[i];
                      return (
                        <tr key={i}>
                          <td>
                            {g ? (g.name ?? g.isin) : ""}
                            {g?.cost_unknown && <div className="status-failed">cost unknown</div>}
                            {g?.history_gap && <div className="status-failed">history incomplete</div>}
                          </td>
                          <td className={`num ${tone(g?.realised_pnl ?? null)}`}>{g ? eur(g.realised_pnl) : ""}</td>
                          <td>
                            {l ? (l.name ?? l.isin) : ""}
                            {l?.cost_unknown && <div className="status-failed">cost unknown</div>}
                            {l?.history_gap && <div className="status-failed">history incomplete</div>}
                          </td>
                          <td className={`num ${tone(l?.realised_pnl ?? null)}`}>{l ? eur(l.realised_pnl) : ""}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}

        {data && data.positions.length > 0 && (
          <>
            <h2>Open positions</h2>
            <div className="card table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Instrument</th>
                    <th>Type</th>
                    <th className="num">Quantity</th>
                    <th className="num">Avg cost</th>
                    <th className="num">Cost</th>
                    <th className="num">Value</th>
                    <th className="num">Unrealised</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {data.positions.map((p) => {
                    // A cost of 0.00, or a partial one, would read as complete when the broker gave
                    // none for some units.
                    const partial = p.cost_flags.some((f) => f === "cost_unknown" || f === "incomplete_history");
                    const noCost = partial && Number(p.total_cost) === 0;
                    return (
                    <tr key={p.isin}>
                      <td>
                        {p.name ?? "—"}
                        <div className="mono muted">{p.isin}</div>
                      </td>
                      <td>{CLASS_LABELS[p.asset_class ?? ""] ?? p.asset_class ?? "—"}</td>
                      <td className="num">{qty(p.quantity)}</td>
                      <td className="num">{partial ? "—" : eur(p.average_cost)}</td>
                      <td className="num">
                        {noCost ? "—" : eur(p.total_cost)}
                        {partial && !noCost && <div className="muted">partial</div>}
                      </td>
                      <td className="num">
                        {eur(p.market_value)}
                        {p.price_as_of && (
                          <div className="muted">
                            as of {p.price_as_of}
                            {p.valued_quantity !== null && Number(p.valued_quantity) !== Number(p.quantity)
                              ? `, ${qty(p.valued_quantity)} units`
                              : ""}
                          </div>
                        )}
                      </td>
                      <td className={`num ${tone(p.unrealised_pnl)}`}>
                        {eur(p.unrealised_pnl)}
                        {p.unrealised_pct !== null && (
                          <div className="muted">{Number(p.unrealised_pct).toLocaleString("en-GB")}%</div>
                        )}
                      </td>
                      <td>
                        {p.verified ? (
                          <span className="status-success">confirmed</span>
                        ) : p.differs ? (
                          <span className="status-failed">differs from statement</span>
                        ) : (
                          <span className="muted">not yet checked</span>
                        )}
                        {p.corporate_action && <div className="muted">split, merger or similar</div>}
                        {p.cost_flags.map((f) => (
                          <div key={f} className={f === "cost_unknown" ? "status-failed" : "muted"}>
                            {FLAG_TEXT[f]}
                          </div>
                        ))}
                      </td>
                    </tr>
                    );
                  })}
                </tbody>
              </table>
              <p className="muted">
                Cost includes fees and transaction taxes. Value and unrealised profit appear where a
                price is known: a market price (see <a href="/prices">Prices</a>) or a broker
                statement, whichever is newer.
              </p>
            </div>
          </>
        )}
      </main>
    </>
  );
}
