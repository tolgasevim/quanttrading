"use client";

import { useCallback, useEffect, useState } from "react";
import {
  api,
  ApiError,
  type Consolidation,
  post,
  put,
  type Subgroups,
  type TradingBook,
} from "@/lib/api";
import { Header } from "../Header";
import { useUser } from "../useUser";

const eur = (value: string | null) =>
  value === null ? "—" : Number(value).toLocaleString("en-GB", { style: "currency", currency: "EUR" });
const pct = (value: string | null, digits = 1) => (value === null ? "—" : `${Number(value).toFixed(digits)}%`);

const STATUS_TEXT: Record<string, string> = {
  protected: "Protected small bet",
  consolidate: "Consolidate",
  exit_candidate: "Exit candidate",
  tracker: "Kept as tracker",
  review: "Small, left alone",
};

export default function PositionsPage() {
  const user = useUser();
  const [sub, setSub] = useState<Subgroups | null>(null);
  const [con, setCon] = useState<Consolidation | null>(null);
  const [trade, setTrade] = useState<TradingBook | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [stop, setStop] = useState<Record<string, string>>({});
  const [barrier, setBarrier] = useState<Record<string, string>>({});

  const load = useCallback(async () => {
    try {
      const [s, c, t] = await Promise.all([
        api<Subgroups>("/api/book/subgroups"),
        api<Consolidation>("/api/book/consolidation"),
        api<TradingBook>("/api/book/trading"),
      ]);
      setSub(s);
      setCon(c);
      setTrade(t);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Could not load the positions.");
    }
  }, []);
  useEffect(() => {
    if (user) void load();
  }, [user, load]);

  if (!user) return null;

  const run = async (action: () => Promise<unknown>) => {
    setBusy(true);
    setError("");
    try {
      await action();
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "That did not work.");
    } finally {
      setBusy(false);
    }
  };
  const tag = (isin: string, body: Record<string, unknown>) =>
    run(() => put(`/api/book/tags/${isin}`, body));

  return (
    <>
      <Header user={user} />
      <main>
        <h1>Positions: groups, small bets and the trading book</h1>
        <p className="notice">
          Being small is never a reason to sell on its own. The app only prompts about a small position
          when it duplicates another holding, or it has no usable price.
        </p>
        {error && <p className="notice status-failed">{error}</p>}

        {con && (
          <div className="card" style={{ marginBottom: 16 }}>
            <h2 style={{ marginTop: 0 }}>How lean is the portfolio?</h2>
            <p>
              You hold <strong>{con.direct}</strong> positions ({con.look_through} names counting what is inside
              funds). The soft target is {con.min_positions} to {con.max_positions}:{" "}
              <span className={con.state === "within" ? "status-success" : "status-failed"}>
                {con.state === "within" ? "inside it" : con.state === "below" ? "below it" : "above it"}
              </span>
              .
            </p>
            {con.notes.map((n) => (
              <p className="muted" key={n}>
                {n}
              </p>
            ))}
            <h3>This week&apos;s prompts</h3>
            {con.prompts.length === 0 ? (
              <p className="muted">Nothing to look at.</p>
            ) : (
              <ol>
                {con.prompts.map((p) => (
                  <li key={p.isin}>{p.text}</li>
                ))}
              </ol>
            )}
            {con.held_back > 0 && <p className="muted">{con.held_back} more wait for a later week.</p>}
          </div>
        )}

        {con && con.small.length > 0 && (
          <div className="card table-wrap" style={{ marginBottom: 16 }}>
            <h2 style={{ marginTop: 0 }}>Small positions (under {con.small_pct}% of your securities)</h2>
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th className="num">Share</th>
                  <th>Status</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {con.small.map((s) => (
                  <tr key={s.isin}>
                    <td>{s.name}</td>
                    <td className="num">{pct(s.pct, 2)}</td>
                    <td style={{ whiteSpace: "normal", maxWidth: 360 }}>
                      <strong>{STATUS_TEXT[s.status] ?? s.status}</strong>
                      <div className="muted">{s.why}</div>
                      {s.suggestion && <div className="muted">{s.suggestion}</div>}
                    </td>
                    <td>
                      <button className="link" disabled={busy} onClick={() => tag(s.isin, { pinned: s.status !== "protected" })}>
                        {s.status === "protected" ? "Unpin" : "Pin"}
                      </button>{" "}
                      <button
                        className="link"
                        disabled={busy}
                        onClick={() => {
                          const d = new Date();
                          d.setMonth(d.getMonth() + 6);
                          void tag(s.isin, { tracker_until: d.toISOString().slice(0, 10) });
                        }}
                      >
                        Keep as tracker for 6 months
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {con && con.overlaps.length > 0 && (
          <div className="card table-wrap" style={{ marginBottom: 16 }}>
            <h2 style={{ marginTop: 0 }}>Funds that overlap</h2>
            <table>
              <tbody>
                {con.overlaps.map((o) => (
                  <tr key={`${o.a_isin}-${o.b_isin}`}>
                    <td style={{ whiteSpace: "normal", maxWidth: 480 }}>
                      {o.suggestion}
                      <div className="muted">
                        {o.basis === "holdings" ? `Measured on the holdings files: ${pct(o.overlap_pct, 0)} shared.` : "Judged from the index in the names."}
                        {o.intentional && " You keep both on purpose."}
                        {o.reconfirm && " It is over a year since you confirmed this: confirm again if it still holds."}
                      </div>
                    </td>
                    <td>
                      <button
                        className="link"
                        disabled={busy}
                        onClick={() => run(() => post("/api/book/overlaps", { a: o.a_isin, b: o.b_isin, intentional: !o.intentional || o.reconfirm }))}
                      >
                        {o.intentional && !o.reconfirm ? "Stop treating as intentional" : "Keep both on purpose"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {sub && (
          <div className="card table-wrap" style={{ marginBottom: 16 }}>
            <h2 style={{ marginTop: 0 }}>Tech sub-groups (direct shares)</h2>
            <table>
              <thead>
                <tr>
                  <th>Group</th>
                  <th className="num">Of portfolio</th>
                  <th className="num">Of tech</th>
                  <th className="num">Cap</th>
                </tr>
              </thead>
              <tbody>
                {sub.groups.map((g) => (
                  <tr key={g.key}>
                    <td>{g.label}</td>
                    <td className={`num ${g.breached ? "status-failed" : ""}`}>{pct(g.pct)}</td>
                    <td className="num">{pct(g.pct_of_tech, 0)}</td>
                    <td className="num">{g.cap === null ? "none" : pct(g.cap, 0)}</td>
                  </tr>
                ))}
                <tr>
                  <td className="muted">Not tech (reported apart, no cap)</td>
                  <td className="num muted">{pct(sub.non_tech_pct)}</td>
                  <td />
                  <td />
                </tr>
              </tbody>
            </table>
            <h3>Every share and its group</h3>
            <table>
              <tbody>
                {sub.stocks.map((s) => (
                  <tr key={s.isin}>
                    <td>{s.name}</td>
                    <td>
                      <select
                        value={s.overridden ? s.subgroup : ""}
                        disabled={busy}
                        onChange={(e) => tag(s.isin, { subgroup: e.target.value || null })}
                      >
                        <option value="">Automatic: {s.auto_label}</option>
                        {Object.entries(sub.labels).map(([k, label]) => (
                          <option key={k} value={k}>
                            {label}
                          </option>
                        ))}
                      </select>
                    </td>
                    <td className="num">{eur(s.value)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {trade && (
          <div className="card table-wrap">
            <h2 style={{ marginTop: 0 }}>Trading book</h2>
            <p>
              {eur(trade.value)} is {pct(trade.pct)} of everything you hold, against a cap of {trade.cap}%.{" "}
              {trade.over_cap && <span className="status-failed">It is over the cap.</span>}
            </p>
            {trade.open.length === 0 ? (
              <p className="muted">No leveraged products, knock-outs or short-term trades held.</p>
            ) : (
              <table>
                <thead>
                  <tr>
                    <th>Name</th>
                    <th className="num">Value</th>
                    <th className="num">Days held</th>
                    <th>Stop price</th>
                    <th>Knock-out barrier</th>
                  </tr>
                </thead>
                <tbody>
                  {trade.open.map((o) => (
                    <tr key={o.isin}>
                      <td style={{ whiteSpace: "normal" }}>
                        {o.name}
                        {o.alerts.map((a) => (
                          <div className="status-failed" key={a.code}>
                            {a.text}
                          </div>
                        ))}
                        {o.flags.map((f) => (
                          <div className="muted" key={f}>
                            {f}
                          </div>
                        ))}
                      </td>
                      <td className="num">{eur(o.value)}</td>
                      <td className="num">{o.days_held ?? "—"}</td>
                      <td>
                        <input
                          style={{ width: 90 }}
                          inputMode="decimal"
                          placeholder={o.stop_price ?? "none"}
                          value={stop[o.isin] ?? ""}
                          onChange={(e) => setStop({ ...stop, [o.isin]: e.target.value })}
                          onBlur={() => stop[o.isin] && tag(o.isin, { stop_price: stop[o.isin] })}
                        />
                      </td>
                      <td>
                        <input
                          style={{ width: 90 }}
                          inputMode="decimal"
                          placeholder={o.barrier_distance_pct ? `${pct(o.barrier_distance_pct)} away` : "level"}
                          value={barrier[o.isin] ?? ""}
                          onChange={(e) => setBarrier({ ...barrier, [o.isin]: e.target.value })}
                          onBlur={() => barrier[o.isin] && tag(o.isin, { barrier: barrier[o.isin] })}
                        />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {trade.needs_thesis.length > 0 && (
              <p className="notice">
                Write your thesis, target and stop in the <a href="/journal">journal</a> for:{" "}
                {trade.needs_thesis.map(([, n]) => n).join(", ")}.
              </p>
            )}
            <h3>Closed trades</h3>
            {trade.stats.closed === 0 ? (
              <p className="muted">None yet.</p>
            ) : (
              <p>
                {trade.stats.closed} closed, {pct(trade.stats.hit_rate, 0)} made money. Profit and loss{" "}
                {eur(trade.stats.pnl)}. Held {trade.stats.avg_days} days on average.
                {trade.stats.expired > 0 && ` ${trade.stats.expired} ended worthless at a barrier.`}
              </p>
            )}
            <p className="muted">{trade.tax_note}</p>
            {Object.keys(trade.stats.by_year).length > 0 && (
              <p className="muted">
                By year: {Object.entries(trade.stats.by_year).map(([y, v]) => `${y}: ${eur(v)}`).join(", ")}
              </p>
            )}
          </div>
        )}
      </main>
    </>
  );
}
