"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { type AdminOverview, api, ApiError } from "@/lib/api";
import { Header } from "../Header";
import { useUser } from "../useUser";

const day = (value: string | null) => (value ? value.slice(0, 16).replace("T", " ") : "never");
const eur = (value: string) => Number(value).toLocaleString("en-GB", { style: "currency", currency: "EUR" });
const JOB_NAME: Record<string, string> = {
  ingest_prices: "Prices",
  ingest_fx: "Exchange rates",
  map_isins: "Ticker lookup",
  score_picks: "AI pick scoring",
  score_factors: "Factor scores",
  push_alerts: "Telegram push",
  send_digests: "Email digests",
  create_alerts: "Alerts",
  weekly_commentary: "Weekly commentary",
};

export default function AdminPage() {
  const user = useUser();
  const [data, setData] = useState<AdminOverview | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (user?.role !== "admin") return;
    api<AdminOverview>("/api/admin/overview")
      .then(setData)
      .catch((err) => setError(err instanceof ApiError ? err.detail : "Could not load the panel."));
  }, [user]);

  if (!user) return null;
  if (user.role !== "admin") return <p className="error">Admins only.</p>;

  const sick = data?.jobs.filter((j) => !j.healthy).length ?? 0;

  return (
    <>
      <Header user={user} />
      <main>
        <h1>Admin</h1>
        {error && <p className="notice status-failed">{error}</p>}
        {data && (
          <>
            <p className={sick > 0 ? "notice status-failed" : "notice status-success"}>
              {sick > 0 ? `${sick} data job${sick > 1 ? "s need" : " needs"} a look.` : "All data jobs are healthy."}{" "}
              {data.pending_invites} invite{data.pending_invites === 1 ? "" : "s"} wait to be used.{" "}
              <Link href="/admin/invites">Invite someone</Link>
            </p>

            <div className="card table-wrap" style={{ marginBottom: 16 }}>
              <h2 style={{ marginTop: 0 }}>Data jobs</h2>
              <table>
                <thead>
                  <tr>
                    <th>Job</th>
                    <th>State</th>
                    <th>Last run</th>
                    <th>Last good run</th>
                    <th className="num">Rows</th>
                    <th className="num">Failed items</th>
                    <th className="num">Failed runs, 7 days</th>
                  </tr>
                </thead>
                <tbody>
                  {data.jobs.map((j) => (
                    <tr key={j.job}>
                      <td>{JOB_NAME[j.job] ?? j.job}</td>
                      <td className={j.healthy ? "status-success" : "status-failed"}>{j.healthy ? "healthy" : j.problem}</td>
                      <td>
                        {day(j.last_started)}
                        {j.last_status && <div className="muted">{j.last_status}</div>}
                      </td>
                      <td>{day(j.last_success)}</td>
                      <td className="num">{j.rows_written}</td>
                      <td className="num">{j.error_count}</td>
                      <td className="num">{j.failures_7d}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="muted">Times are UTC. The Prices page lists which symbols failed.</p>
            </div>

            <div className="card table-wrap" style={{ marginBottom: 16 }}>
              <h2 style={{ marginTop: 0 }}>Users</h2>
              <table>
                <thead>
                  <tr>
                    <th>Person</th>
                    <th>Role</th>
                    <th>Sign-in</th>
                    <th>Last sign-in</th>
                    <th>Last import</th>
                    <th className="num">AI spend this month</th>
                  </tr>
                </thead>
                <tbody>
                  {data.users.map((u) => (
                    <tr key={u.id}>
                      <td>
                        {u.display_name}
                        <div className="muted">{u.email}</div>
                      </td>
                      <td>{u.role}</td>
                      <td>
                        {u.passkeys > 0 ? `${u.passkeys} passkey${u.passkeys > 1 ? "s" : ""}` : "password"}
                        {u.totp_enabled ? ", TOTP" : ""}
                        {u.locked && <div className="status-failed">locked after failed attempts</div>}
                      </td>
                      <td>{day(u.last_login_at)}</td>
                      <td>
                        {day(u.last_import_at)}
                        {u.last_import_source && <div className="muted">{u.last_import_source}</div>}
                      </td>
                      <td className="num">{eur(u.ai_spend_eur)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="muted">
                You see counts and times only, never anyone&apos;s holdings or amounts. Each person&apos;s data stays behind
                their own sign-in.
              </p>
            </div>

            <div className="card">
              <h2 style={{ marginTop: 0 }}>AI spend, {data.llm.month}</h2>
              <p>
                {eur(data.llm.total_eur)} of the household cap of {eur(data.llm.total_cap_eur)}. Each person is capped at{" "}
                {eur(data.llm.user_cap_eur)}.
              </p>
              {Object.keys(data.llm.by_purpose).length > 0 && (
                <table>
                  <tbody>
                    {Object.entries(data.llm.by_purpose).map(([purpose, cost]) => (
                      <tr key={purpose}>
                        <td>{purpose}</td>
                        <td className="num">{eur(cost)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </>
        )}
      </main>
    </>
  );
}
