"use client";

import { useCallback, useEffect, useState } from "react";
import { type Allocation, api, ApiError, del, post, put } from "@/lib/api";
import { Header } from "../Header";
import { useUser } from "../useUser";

const eur = (value: string) =>
  Number(value).toLocaleString("en-GB", { style: "currency", currency: "EUR" });
const pct = (value: string, digits = 1) => `${Number(value).toFixed(digits)}%`;
const STATUS_TEXT = { below: "Below the band", ok: "In the band", above: "Above the band" } as const;
const RULE_TEXT: Record<string, string> = {
  single_stock: "One share",
  sector: "Sector",
  country: "Country",
  min_cash: "Minimum cash",
  crypto: "Crypto together",
  trading_book: "Trading book",
  drawdown: "Fall from the peak",
};
const GUARDRAIL_LABEL: Record<string, string> = {
  max_single_stock: "Largest single share (%)",
  max_sector: "Largest sector (%)",
  max_country: "Largest country (%)",
  min_cash: "Smallest free cash (%)",
  max_crypto: "Largest crypto, together (%)",
  max_trading_book: "Largest trading book (%)",
  drawdown_alert: "Alert at a fall of (%)",
};

export default function AllocationPage() {
  const user = useUser();
  const [data, setData] = useState<Allocation | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [bands, setBands] = useState<Record<string, { target: string; low: string; high: string }>>({});
  const [limits, setLimits] = useState<Record<string, string>>({});
  const [tranches, setTranches] = useState("6");

  const show = useCallback((next: Allocation) => {
    setData(next);
    setBands(Object.fromEntries(next.buckets.map((b) => [b.bucket, { target: b.target, low: b.low, high: b.high }])));
    setLimits(next.guardrails);
  }, []);

  const load = useCallback(async () => {
    try {
      show(await api<Allocation>("/api/allocation"));
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Could not load the allocation.");
    }
  }, [show]);
  useEffect(() => {
    if (user) void load();
  }, [user, load]);

  if (!user) return null;

  const run = async (action: () => Promise<Allocation>) => {
    setBusy(true);
    setError("");
    try {
      show(await action());
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "That did not work.");
    } finally {
      setBusy(false);
    }
  };

  const saveTargets = (e: React.FormEvent) => {
    e.preventDefault();
    void run(() => put<Allocation>("/api/allocation/targets", { targets: bands }));
  };
  const saveLimits = (e: React.FormEvent) => {
    e.preventDefault();
    void run(() => put<Allocation>("/api/allocation/guardrails", limits));
  };
  const setBand = (bucket: string, key: "target" | "low" | "high", value: string) =>
    setBands({ ...bands, [bucket]: { ...bands[bucket], [key]: value } });

  const plan = data?.deployment;

  return (
    <>
      <Header user={user} />
      <main>
        <h1>Allocation, limits and what to do next</h1>
        <p className="notice">
          Guidance only: the app never places a trade. Changing a savings plan comes before selling,
          because a sale can create a taxable gain. Money you set aside in reserves is not counted.
        </p>
        {error && <p className="notice status-failed">{error}</p>}
        {data?.warnings.map((w) => (
          <p className="notice" key={w}>
            {w}
          </p>
        ))}
        {data && !data.has_portfolio && (
          <p className="notice">
            Nothing to show yet. <a href="/import">Import your transactions</a> first.
          </p>
        )}

        {data && data.has_portfolio && (
          <>
            <div className="card table-wrap" style={{ marginBottom: 16 }}>
              <h2 style={{ marginTop: 0 }}>Where you are against your targets</h2>
              <p className="muted">
                Portfolio for this comparison: {eur(data.base)} (valued holdings plus free cash).
                {Number(data.trading_book) > 0 &&
                  ` The trading book (${eur(data.trading_book)}) is kept apart and left out.`}
              </p>
              <table>
                <thead>
                  <tr>
                    <th>Bucket</th>
                    <th className="num">Now</th>
                    <th className="num">Target</th>
                    <th className="num">Band</th>
                    <th>Status</th>
                    <th className="num">To the target</th>
                  </tr>
                </thead>
                <tbody>
                  {data.buckets.map((b) => (
                    <tr key={b.bucket}>
                      <td>{b.label}</td>
                      <td className="num">{pct(b.pct)}</td>
                      <td className="num">{pct(b.target, 0)}</td>
                      <td className="num">
                        {b.low}–{b.high}%
                      </td>
                      <td className={b.status === "ok" ? "status-success" : "status-failed"}>{STATUS_TEXT[b.status]}</td>
                      <td className="num">{Number(b.gap_eur) > 0 ? `+${eur(b.gap_eur)}` : eur(b.gap_eur)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="card" style={{ marginBottom: 16 }}>
              <h2 style={{ marginTop: 0 }}>What to do</h2>
              {data.suggestions.length === 0 ? (
                <p className="muted">Nothing to do: every bucket is inside its band, or there is no cash to place.</p>
              ) : (
                <ol>
                  {data.suggestions.map((s) => (
                    <li key={s.text} style={{ marginBottom: 8 }}>
                      {s.text}
                      <div className="muted">{s.why}</div>
                    </li>
                  ))}
                </ol>
              )}
            </div>

            <div className="card table-wrap" style={{ marginBottom: 16 }}>
              <h2 style={{ marginTop: 0 }}>Your limits</h2>
              <p className={data.breaches > 0 ? "status-failed" : "status-success"}>
                {data.breaches > 0 ? `${data.breaches} limit(s) are broken.` : "All your limits hold."}
              </p>
              <table>
                <thead>
                  <tr>
                    <th>Limit</th>
                    <th>What</th>
                    <th className="num">Now</th>
                    <th className="num">Limit</th>
                  </tr>
                </thead>
                <tbody>
                  {data.rules.slice(0, 25).map((r) => (
                    <tr key={`${r.rule}-${r.subject}`}>
                      <td>{RULE_TEXT[r.rule] ?? r.rule}</td>
                      <td>
                        {r.subject}
                        {r.note && <div className="muted">{r.note}</div>}
                      </td>
                      <td className={`num ${r.breached ? "status-failed" : ""}`}>{pct(r.current_pct)}</td>
                      <td className="num">
                        {r.rule === "min_cash" ? "at least " : ""}
                        {pct(r.limit_pct, 0)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="card table-wrap" style={{ marginBottom: 16 }}>
              <h2 style={{ marginTop: 0 }}>Cash to invest</h2>
              {plan && plan.excess !== "0.00" && Number(plan.excess) > 0 ? (
                <>
                  <p>
                    {plan.active
                      ? `Your plan started on ${plan.started}: ${eur(plan.excess)} in ${plan.tranches} monthly steps. Invested since then: ${eur(plan.invested_since)}.`
                      : `You hold ${eur(plan.excess)} more cash than your target. A plan spreads it over months.`}
                  </p>
                  {plan.pull_forward && <p className="notice">{plan.pull_forward_note}</p>}
                  <table>
                    <thead>
                      <tr>
                        <th>Step</th>
                        <th>Month</th>
                        <th className="num">Amount</th>
                        <th>Where</th>
                        <th />
                      </tr>
                    </thead>
                    <tbody>
                      {plan.plan.map((t) => (
                        <tr key={t.number}>
                          <td>{t.number}</td>
                          <td>{t.due}</td>
                          <td className="num">{eur(t.amount)}</td>
                          <td className="muted">
                            {t.split.map((s) => `${eur(s.amount)} ${s.label}${s.instrument ? ` (${s.instrument})` : ""}`).join(", ")}
                          </td>
                          <td className={t.done ? "status-success" : "muted"}>{plan.active ? (t.done ? "Done" : "Open") : ""}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {plan.active ? (
                    <p>
                      <button className="link" onClick={() => run(() => del("/api/allocation/deployment").then(() => api<Allocation>("/api/allocation")))}>
                        Stop this plan
                      </button>
                    </p>
                  ) : (
                    <form onSubmit={(e) => { e.preventDefault(); void run(() => post<Allocation>("/api/allocation/deployment", { tranches: Number(tranches) })); }} style={{ marginTop: 12 }}>
                      <label>
                        Number of monthly steps (1 to 12)
                        <input inputMode="numeric" value={tranches} onChange={(e) => setTranches(e.target.value)} />
                      </label>
                      <div>
                        <button type="submit" disabled={busy}>
                          Start the plan
                        </button>
                      </div>
                    </form>
                  )}
                </>
              ) : (
                <p className="muted">Your free cash is not above the cash band, so there is nothing to spread.</p>
              )}
            </div>

            <div className="card" style={{ marginBottom: 16 }}>
              <h2 style={{ marginTop: 0 }}>Change your targets {data.custom_targets ? "" : "(these are the defaults)"}</h2>
              <form onSubmit={saveTargets}>
                <table>
                  <thead>
                    <tr>
                      <th>Bucket</th>
                      <th>Target %</th>
                      <th>Lowest %</th>
                      <th>Highest %</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.buckets.map((b) => (
                      <tr key={b.bucket}>
                        <td>{b.label}</td>
                        {(["target", "low", "high"] as const).map((k) => (
                          <td key={k}>
                            <input
                              style={{ width: 80 }}
                              inputMode="decimal"
                              value={bands[b.bucket]?.[k] ?? ""}
                              onChange={(e) => setBand(b.bucket, k, e.target.value)}
                            />
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
                <div style={{ display: "flex", gap: 8 }}>
                  <button type="submit" disabled={busy}>
                    Save targets
                  </button>
                  {data.custom_targets && (
                    <button type="button" className="link" onClick={() => run(() => del("/api/allocation/targets").then(() => api<Allocation>("/api/allocation")))}>
                      Back to the defaults
                    </button>
                  )}
                </div>
              </form>
            </div>

            <div className="card">
              <h2 style={{ marginTop: 0 }}>Change your limits</h2>
              <form onSubmit={saveLimits}>
                {Object.keys(GUARDRAIL_LABEL).map((k) => (
                  <label key={k}>
                    {GUARDRAIL_LABEL[k]}
                    <input inputMode="decimal" value={limits[k] ?? ""} onChange={(e) => setLimits({ ...limits, [k]: e.target.value })} />
                  </label>
                ))}
                <div>
                  <button type="submit" disabled={busy}>
                    Save limits
                  </button>
                </div>
              </form>
            </div>
          </>
        )}
      </main>
    </>
  );
}
