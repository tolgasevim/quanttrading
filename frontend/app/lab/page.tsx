"use client";

import { useEffect, useState } from "react";
import { api, ApiError, type BacktestResult, type BacktestStats, type LabInfo, post, type Swing } from "@/lib/api";
import { Header } from "../Header";
import { useUser } from "../useUser";

const eur = (v: string) => Number(v).toLocaleString("en-GB", { style: "currency", currency: "EUR" });
const pct = (v: number | null) => (v === null ? "—" : `${v.toFixed(1)}%`);
const ratio = (v: number | null) => (v === null ? "—" : v.toFixed(2));
const SIGNAL_NAME: Record<string, string> = { trend: "Trend", reversion: "Dip in an uptrend" };

function Curve({ points }: { points: BacktestResult["curve"] }) {
  const w = 720;
  const h = 220;
  const values = points.flatMap((p) => [p.strategy, p.benchmark]);
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const x = (i: number) => (i / Math.max(points.length - 1, 1)) * w;
  const y = (v: number) => h - ((v - lo) / Math.max(hi - lo, 1e-9)) * (h - 10) - 5;
  const line = (key: "strategy" | "benchmark") => points.map((p, i) => `${x(i).toFixed(1)},${y(p[key]).toFixed(1)}`).join(" ");
  return (
    <figure style={{ margin: "12px 0" }}>
      <svg viewBox={`0 0 ${w} ${h}`} style={{ width: "100%", maxWidth: w }} role="img" aria-label="Value of the rule and of the benchmark over time">
        <polyline fill="none" stroke="currentColor" strokeWidth="2" points={line("strategy")} />
        <polyline fill="none" stroke="currentColor" strokeWidth="2" strokeDasharray="5 4" opacity="0.55" points={line("benchmark")} />
      </svg>
      <figcaption className="muted">
        Solid line: the rule. Dashed line: the benchmark. From {points[0]?.day} to {points[points.length - 1]?.day}. Range {eur(String(lo))} to{" "}
        {eur(String(hi))}.
      </figcaption>
    </figure>
  );
}

const ROWS: [string, (s: BacktestStats) => string][] = [
  ["Final value", (s) => eur(s.final_value)],
  ["Total return", (s) => pct(s.total_return_pct)],
  ["Return a year (CAGR)", (s) => pct(s.cagr_pct)],
  ["Volatility a year", (s) => pct(s.volatility_pct)],
  ["Sharpe ratio", (s) => ratio(s.sharpe)],
  ["Sortino ratio", (s) => ratio(s.sortino)],
  ["Largest fall", (s) => pct(s.max_drawdown_pct)],
  ["Trades", (s) => String(s.trades)],
  ["Fees paid", (s) => eur(s.fees)],
  ["Tax paid", (s) => eur(s.tax)],
  ["Turnover a year", (s) => pct(s.turnover_pct_year)],
];

export default function LabPage() {
  const user = useUser();
  const [info, setInfo] = useState<LabInfo | null>(null);
  const [swing, setSwing] = useState<Swing | null>(null);
  const [scope, setScope] = useState<"mine" | "all">("mine");
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState({
    strategy: "buy_hold",
    benchmark: "SXRV",
    start: "",
    end: "",
    capital: "10000",
    fee: "1",
    tax_pct: "26.375",
    top_n: "10",
    universe: "all",
    instrument: "",
  });

  useEffect(() => {
    if (!user) return;
    api<LabInfo>("/api/lab")
      .then((i) => {
        setInfo(i);
        if (i.benchmarks[0]) setForm((f) => ({ ...f, benchmark: i.benchmarks[0] }));
      })
      .catch(() => undefined);
  }, [user]);
  useEffect(() => {
    if (!user) return;
    api<Swing>(`/api/lab/swing?scope=${scope}`)
      .then(setSwing)
      .catch(() => undefined);
  }, [user, scope]);

  if (!user) return null;

  const run = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      setResult(
        await post<BacktestResult>("/api/lab/backtest", {
          strategy: form.strategy,
          benchmark: form.benchmark,
          start: form.start,
          end: form.end,
          capital: form.capital,
          fee: form.fee,
          tax_pct: form.tax_pct,
          top_n: Number(form.top_n),
          universe: form.universe,
          instrument: form.instrument || null,
        }),
      );
    } catch (err) {
      setResult(null);
      setError(err instanceof ApiError ? err.detail : "The test failed.");
    } finally {
      setBusy(false);
    }
  };
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setForm({ ...form, [k]: e.target.value });

  return (
    <>
      <Header user={user} />
      <main>
        <h1>Lab</h1>
        <p className="notice">
          Information only. A backtest is hypothetical and past results do not predict returns. The app places no trade.
        </p>

        <div className="card" style={{ marginBottom: 16 }}>
          <h2 style={{ marginTop: 0 }}>Backtest</h2>
          <form onSubmit={run}>
            <label>
              Rule
              <select value={form.strategy} onChange={set("strategy")}>
                {Object.keys(info?.strategies ?? { buy_hold: "" }).map((k) => (
                  <option key={k} value={k}>
                    {k}
                  </option>
                ))}
              </select>
            </label>
            <p className="muted">{info?.strategies[form.strategy]}</p>
            <label>
              Benchmark
              <select value={form.benchmark} onChange={set("benchmark")}>
                {(info?.benchmarks ?? []).map((b) => (
                  <option key={b}>{b}</option>
                ))}
              </select>
            </label>
            <label>
              From
              <input type="date" required value={form.start} onChange={set("start")} />
            </label>
            <label>
              To
              <input type="date" required value={form.end} onChange={set("end")} />
            </label>
            <label>
              Start capital (EUR)
              <input inputMode="decimal" value={form.capital} onChange={set("capital")} />
            </label>
            <label>
              Fee a trade (EUR)
              <input inputMode="decimal" value={form.fee} onChange={set("fee")} />
            </label>
            <label>
              Tax on gains (percent)
              <input inputMode="decimal" value={form.tax_pct} onChange={set("tax_pct")} />
            </label>
            {form.strategy === "trend" && (
              <label>
                Instrument code to trade
                <input value={form.instrument} onChange={set("instrument")} placeholder="for example SXRV" />
              </label>
            )}
            {(form.strategy === "momentum" || form.strategy === "score") && (
              <>
                <label>
                  Names to hold
                  <input inputMode="numeric" value={form.top_n} onChange={set("top_n")} />
                </label>
                <label>
                  Universe
                  <select value={form.universe} onChange={set("universe")}>
                    <option value="all">every share the app prices ({info?.universe ?? 0})</option>
                    <option value="held">what I hold ({info?.held ?? 0})</option>
                    <option value="watchlist">my watchlist ({info?.watchlist ?? 0})</option>
                  </select>
                </label>
              </>
            )}
            {form.strategy === "score" && (
              <p className="muted">
                {info?.score_first_day
                  ? `Stored scores start on ${info.score_first_day} (${info.score_days} days so far). The test needs 60 days.`
                  : "No scores are stored yet."}
              </p>
            )}
            <button disabled={busy}>{busy ? "Running…" : "Run the test"}</button>
          </form>
          {error && <p className="notice status-failed">{error}</p>}
        </div>

        {result && (
          <div className="card table-wrap" style={{ marginBottom: 16 }}>
            <h2 style={{ marginTop: 0 }}>Result</h2>
            <Curve points={result.curve} />
            <table>
              <thead>
                <tr>
                  <th />
                  <th className="num">The rule</th>
                  <th className="num">The benchmark</th>
                </tr>
              </thead>
              <tbody>
                {ROWS.map(([label, fmt]) => (
                  <tr key={label}>
                    <td>{label}</td>
                    <td className="num">{fmt(result.strategy)}</td>
                    <td className="num">{fmt(result.benchmark)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {result.notes.map((n) => (
              <p key={n}>{n}</p>
            ))}
            <ul className="muted">
              {result.warnings.map((w) => (
                <li key={w}>{w}</li>
              ))}
            </ul>
          </div>
        )}

        <div className="card table-wrap">
          <h2 style={{ marginTop: 0 }}>Swing signals</h2>
          {swing && (
            <>
              <p>
                A signal type is shown only while its own backtest passes: {swing.criteria}.
                {swing.scanned_on ? ` Last scan: ${swing.scanned_on}.` : " No scan has run yet."}
              </p>
              <table>
                <thead>
                  <tr>
                    <th>Signal</th>
                    <th className="num">Trades</th>
                    <th className="num">Wins</th>
                    <th className="num">Profit a trade</th>
                    <th className="num">Profit factor</th>
                    <th className="num">First half</th>
                    <th className="num">Second half</th>
                    <th>Result</th>
                  </tr>
                </thead>
                <tbody>
                  {swing.gates.map((g) => (
                    <tr key={g.signal}>
                      <td style={{ whiteSpace: "normal" }}>
                        {SIGNAL_NAME[g.signal] ?? g.signal}
                        <div className="muted">{g.text}</div>
                      </td>
                      <td className="num">{g.n_trades}</td>
                      <td className="num">{Number(g.win_rate_pct).toFixed(0)}%</td>
                      <td className="num">{Number(g.expectancy_pct).toFixed(2)}%</td>
                      <td className="num">{g.profit_factor === null ? "—" : Number(g.profit_factor).toFixed(2)}</td>
                      <td className="num">{g.first_half_pct === null ? "—" : `${Number(g.first_half_pct).toFixed(2)}%`}</td>
                      <td className="num">{g.second_half_pct === null ? "—" : `${Number(g.second_half_pct).toFixed(2)}%`}</td>
                      <td className={g.passed ? "status-success" : "status-failed"}>{g.passed ? "passes: signals shown" : "fails: signals hidden"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <label>
                Signals for
                <select value={scope} onChange={(e) => setScope(e.target.value as typeof scope)}>
                  <option value="mine">my holdings and watchlist</option>
                  <option value="all">every share and fund the app prices</option>
                </select>
              </label>
              {swing.signals.length === 0 ? (
                <p className="muted">
                  No signal to show today.
                  {swing.hidden > 0 && ` ${swing.hidden} signal${swing.hidden > 1 ? "s were" : " was"} hidden because its type failed the backtest.`}
                </p>
              ) : (
                <table>
                  <thead>
                    <tr>
                      <th>Name</th>
                      <th>Signal</th>
                      <th className="num">Entry</th>
                      <th className="num">Stop</th>
                      <th className="num">Target</th>
                      <th className="num">Risk / reward</th>
                      <th className="num">At most</th>
                    </tr>
                  </thead>
                  <tbody>
                    {swing.signals.map((s) => (
                      <tr key={`${s.code}-${s.signal}`}>
                        <td>
                          {s.name}
                          <div className="muted">
                            {s.day}
                            {s.held ? ", held" : ""}
                            {s.watched ? ", watched" : ""}
                          </div>
                        </td>
                        <td>{SIGNAL_NAME[s.signal] ?? s.signal}</td>
                        <td className="num">
                          {Number(s.entry).toFixed(2)} {s.currency}
                        </td>
                        <td className="num">{Number(s.stop).toFixed(2)}</td>
                        <td className="num">{Number(s.target).toFixed(2)}</td>
                        <td className="num">
                          -{s.risk_pct}% / +{s.reward_pct}%
                        </td>
                        <td className="num">{s.max_hold} days</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
              {swing.notes.map((n) => (
                <p className="muted" key={n}>
                  {n}
                </p>
              ))}
            </>
          )}
        </div>
      </main>
    </>
  );
}
