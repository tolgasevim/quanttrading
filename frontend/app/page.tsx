"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api, type Fx, type InstrumentStatus, type JobRun } from "@/lib/api";
import { Header } from "./Header";
import { useUser } from "./useUser";

const fmtTime = (iso: string) =>
  new Date(iso).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" });

export default function Dashboard() {
  const user = useUser();
  const [prices, setPrices] = useState<InstrumentStatus[]>([]);
  const [fx, setFx] = useState<Fx[]>([]);
  const [jobs, setJobs] = useState<JobRun[]>([]);

  useEffect(() => {
    if (!user) return;
    api<InstrumentStatus[]>("/api/market/status").then(setPrices);
    api<Fx[]>("/api/market/fx").then(setFx);
    if (user.role === "admin") api<JobRun[]>("/api/admin/jobs?limit=10").then(setJobs);
  }, [user]);

  if (!user) return null;
  return (
    <>
      <Header user={user} />
      <main>
        <h1>Hello, {user.display_name}</h1>
        {!user.totp_enabled && (
          <p className="notice">
            Two-factor sign-in is off. <Link href="/settings">Turn it on</Link>.
          </p>
        )}

        <h2>End-of-day prices</h2>
        <div className="card table-wrap">
          <table>
            <thead>
              <tr>
                <th>Code</th>
                <th>Name</th>
                <th>Class</th>
                <th className="num">Last close</th>
                <th>Date</th>
                <th>Source</th>
                <th className="num">Days stored</th>
              </tr>
            </thead>
            <tbody>
              {prices.map((p) => (
                <tr key={p.code}>
                  <td className="mono">{p.code}</td>
                  <td>{p.name}</td>
                  <td>{p.asset_class}</td>
                  <td className="num">
                    {p.last_close ? `${Number(p.last_close).toFixed(2)} ${p.currency}` : "—"}
                  </td>
                  <td>{p.last_date ?? <span className="status-failed">no data yet</span>}</td>
                  <td>{p.source ?? "—"}</td>
                  <td className="num">{p.rows}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {prices.length === 0 && <p className="muted">No instruments yet. Run the seed command.</p>}
        </div>

        <h2>ECB reference rates (1 EUR =)</h2>
        <div className="card table-wrap">
          <table>
            <tbody>
              {fx.map((r) => (
                <tr key={r.quote}>
                  <td className="mono">{r.quote}</td>
                  <td className="num">{Number(r.rate).toFixed(4)}</td>
                  <td className="muted">{r.date}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {fx.length === 0 && <p className="muted">No rates yet.</p>}
        </div>

        {user.role === "admin" && (
          <>
            <h2>Data jobs</h2>
            <div className="card table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Job</th>
                    <th>Status</th>
                    <th>Started</th>
                    <th className="num">Rows</th>
                    <th>Problems</th>
                  </tr>
                </thead>
                <tbody>
                  {jobs.map((j) => {
                    const errors = Object.keys((j.details.errors ?? {}) as Record<string, string>);
                    const crash = j.details.error as string | undefined;
                    const problems = crash
                      ? "crashed"
                      : errors.length > 3
                        ? `${errors.length} failed`
                        : errors.join(", ") || "—";
                    return (
                      <tr key={j.id}>
                        <td className="mono">{j.job}</td>
                        <td className={`status-${j.status}`}>{j.status}</td>
                        <td>{fmtTime(j.started_at)}</td>
                        <td className="num">{j.rows_written}</td>
                        <td className="muted" title={crash ?? errors.join(", ")}>
                          {problems}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              {jobs.length === 0 && <p className="muted">No job has run yet.</p>}
            </div>
          </>
        )}
      </main>
    </>
  );
}
