"use client";

import { useEffect, useState } from "react";
import { api, ApiError, type Performance } from "@/lib/api";
import { Header } from "../Header";
import { useUser } from "../useUser";

const PERIODS = [
  { value: "all", label: "Since the start" },
  { value: "ytd", label: "This year" },
  { value: "1y", label: "1 year" },
  { value: "3y", label: "3 years" },
];

const pct = (value: number | null, digits = 1) =>
  value === null ? "—" : `${(value * 100).toLocaleString("en-GB", { minimumFractionDigits: digits, maximumFractionDigits: digits })}%`;
const tone = (value: number | null) =>
  value === null || value === 0 ? "" : value > 0 ? "status-success" : "status-failed";
const eur = (value: string) =>
  Number(value).toLocaleString("en-GB", { style: "currency", currency: "EUR" });

// A plain SVG line chart: no chart library needed for two lines.
function Chart({ data }: { data: Performance["series"] }) {
  if (data.length < 2) return null;
  const w = 720;
  const h = 240;
  const pad = 36;
  const values = data.flatMap((p) => [p.portfolio, ...(p.benchmark === null ? [] : [p.benchmark])]);
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const span = hi - lo || 1;
  const x = (i: number) => pad + (i / (data.length - 1)) * (w - pad - 8);
  const y = (v: number) => h - pad - ((v - lo) / span) * (h - pad - 12);
  const line = (pick: (p: Performance["series"][number]) => number | null) =>
    data
      .map((p, i) => {
        const v = pick(p);
        return v === null ? null : `${x(i).toFixed(1)},${y(v).toFixed(1)}`;
      })
      .filter((s): s is string => s !== null)
      .join(" ");
  const hasBenchmark = data.every((p) => p.benchmark !== null);
  return (
    <svg viewBox={`0 0 ${w} ${h}`} role="img" aria-label="Portfolio and benchmark, 100 at the start" style={{ width: "100%", height: "auto" }}>
      {[lo, (lo + hi) / 2, hi].map((v) => (
        <g key={v}>
          <line x1={pad} x2={w - 8} y1={y(v)} y2={y(v)} stroke="var(--border)" />
          <text x={pad - 4} y={y(v) + 4} textAnchor="end" fontSize="11" fill="var(--muted)">
            {v.toFixed(0)}
          </text>
        </g>
      ))}
      <text x={pad} y={h - 8} fontSize="11" fill="var(--muted)">
        {data[0].day}
      </text>
      <text x={w - 8} y={h - 8} fontSize="11" fill="var(--muted)" textAnchor="end">
        {data[data.length - 1].day}
      </text>
      {hasBenchmark && (
        <polyline points={line((p) => p.benchmark)} fill="none" stroke="var(--muted)" strokeWidth="1.5" strokeDasharray="4 3" />
      )}
      <polyline points={line((p) => p.portfolio)} fill="none" stroke="var(--accent)" strokeWidth="2" />
    </svg>
  );
}

export default function PerformancePage() {
  const user = useUser();
  const [period, setPeriod] = useState("all");
  const [benchmark, setBenchmark] = useState("SXRV");
  const [data, setData] = useState<Performance | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    try {
      const saved = window.localStorage.getItem("qt-benchmark");
      if (saved) setBenchmark(saved);
    } catch {
      // storage can be blocked: keep the default
    }
  }, []);

  useEffect(() => {
    if (!user) return;
    setLoading(true);
    setError("");
    api<Performance>(`/api/performance?period=${period}&benchmark=${benchmark}`)
      .then(setData)
      .catch((err) => setError(err instanceof ApiError ? err.detail : "Could not load the performance."))
      .finally(() => setLoading(false));
  }, [user, period, benchmark]);

  if (!user) return null;

  const pick = (code: string) => {
    setBenchmark(code);
    try {
      window.localStorage.setItem("qt-benchmark", code);
    } catch {
      // not a problem
    }
  };
  const excess =
    data && data.twr !== null && data.benchmark_twr !== null ? data.twr - data.benchmark_twr : null;

  return (
    <>
      <Header user={user} />
      <main>
        <h1>Performance and risk</h1>
        <p className="notice">
          Your shares, funds and coins, valued each weekday at the latest stored closing price in
          euros. Cash is not included, so a deposit never counts as a gain. Past results do not
          predict future ones.
        </p>
        {error && <p className="notice status-failed">{error}</p>}

        <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 16 }}>
          <label>
            Period
            <select value={period} onChange={(e) => setPeriod(e.target.value)}>
              {PERIODS.map((p) => (
                <option key={p.value} value={p.value}>
                  {p.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            Benchmark
            <select value={benchmark} onChange={(e) => pick(e.target.value)}>
              {(data?.benchmarks ?? [{ code: "SXRV", name: "Nasdaq-100" }]).map((b) => (
                <option key={b.code} value={b.code}>
                  {b.name}
                </option>
              ))}
            </select>
          </label>
        </div>

        {loading && !data && <p className="muted">Loading…</p>}
        {data && !data.has_history && (
          <p className="notice">
            Nothing to show yet. <a href="/import">Import your transactions</a> first.
          </p>
        )}

        {data && data.has_history && (
          <>
            {data.warnings.map((w) => (
              <p className="notice" key={w}>
                {w}
              </p>
            ))}
            <div className="card" style={{ marginBottom: 16 }}>
              <h2 style={{ marginTop: 0 }}>
                {data.start} to {data.end}
              </h2>
              <Chart data={data.series} />
              <p className="muted">
                Solid line: your portfolio. Dashed line: {data.benchmark_name}. Both start at 100.
              </p>
            </div>

            <div className="card table-wrap" style={{ marginBottom: 16 }}>
              <h2 style={{ marginTop: 0 }}>Return</h2>
              <table>
                <thead>
                  <tr>
                    <th />
                    <th className="num">You</th>
                    <th className="num">{data.benchmark_name}</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td>
                      Time-weighted return
                      <div className="muted">How the holdings did, whatever you added or took out</div>
                    </td>
                    <td className={`num ${tone(data.twr)}`}>{pct(data.twr)}</td>
                    <td className="num">{pct(data.benchmark_twr)}</td>
                  </tr>
                  <tr>
                    <td>
                      Money-weighted return (XIRR, per year)
                      <div className="muted">What your money earned, given when you paid it in</div>
                    </td>
                    <td className={`num ${tone(data.xirr)}`}>{pct(data.xirr)}</td>
                    <td className="num">{pct(data.benchmark_xirr)}</td>
                  </tr>
                  <tr>
                    <td>Ahead of the benchmark</td>
                    <td className={`num ${tone(excess)}`}>{pct(excess)}</td>
                    <td />
                  </tr>
                </tbody>
              </table>
              <p className="muted">
                Value now {eur(data.current_value)}. Paid in, net of sales: {eur(data.net_invested)}.
                Dividends and other income: {eur(data.income)}.
              </p>
            </div>

            <div className="card table-wrap" style={{ marginBottom: 16 }}>
              <h2 style={{ marginTop: 0 }}>Risk</h2>
              <table>
                <tbody>
                  <tr>
                    <td>Volatility (a year)</td>
                    <td className="num">{pct(data.volatility)}</td>
                    <td className="num muted">{data.benchmark_name}: {pct(data.benchmark_volatility)}</td>
                  </tr>
                  <tr>
                    <td>Largest fall from a peak</td>
                    <td className="num status-failed">{data.max_drawdown === null ? "—" : `-${pct(data.max_drawdown)}`}</td>
                    <td className="num muted">
                      {data.drawdown_peak && data.drawdown_low ? `${data.drawdown_peak} to ${data.drawdown_low}` : ""}
                    </td>
                  </tr>
                  <tr>
                    <td>Beta against {data.benchmark_name}</td>
                    <td className="num">{data.beta === null ? "—" : data.beta.toFixed(2)}</td>
                    <td className="num muted">1 moves like the benchmark</td>
                  </tr>
                  <tr>
                    <td>One-day loss, 95% of days (VaR)</td>
                    <td className="num">{pct(data.var95, 2)}</td>
                    <td className="num muted">last year of returns</td>
                  </tr>
                  <tr>
                    <td>Average loss on the worst 5% of days (CVaR)</td>
                    <td className="num">{pct(data.cvar95, 2)}</td>
                    <td />
                  </tr>
                </tbody>
              </table>
              <p className="muted">
                A blank means there is too little history for that number ({data.observations} daily
                returns so far).
                {data.estimated_days > 0 &&
                  ` ${data.estimated_days} of ${data.days} days use a last-trade price for some holding.`}
              </p>
            </div>

            {data.correlation && (
              <div className="card table-wrap">
                <h2 style={{ marginTop: 0 }}>Correlation of your biggest holdings</h2>
                <p className="muted">
                  Daily price moves over the last year. 1 means they move together, 0 means they do
                  not, -1 means they move apart.
                </p>
                <table>
                  <thead>
                    <tr>
                      <th />
                      {data.correlation.names.map((n, i) => (
                        <th className="num" key={data.correlation!.isins[i]} title={n}>
                          {n.slice(0, 10)}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {data.correlation.values.map((row, i) => (
                      <tr key={data.correlation!.isins[i]}>
                        <td>{data.correlation!.names[i]}</td>
                        {row.map((v, j) => (
                          <td className="num" key={data.correlation!.isins[j]}>
                            {v === null ? "—" : v.toFixed(2)}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}
      </main>
    </>
  );
}
