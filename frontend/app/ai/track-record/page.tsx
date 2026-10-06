"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api, ApiError, type AiTrackRecord } from "@/lib/api";
import { Header } from "../../Header";
import { useUser } from "../../useUser";

const pct = (v: string | null) => (v === null ? "–" : `${Number(v).toFixed(2)} %`);

export default function TrackRecordPage() {
  const user = useUser();
  const [data, setData] = useState<AiTrackRecord | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!user) return;
    api<AiTrackRecord>("/api/ai/track-record")
      .then(setData)
      .catch((err) => setError(err instanceof ApiError ? err.detail : "Could not load."));
  }, [user]);
  if (!user) return null;

  return (
    <>
      <Header user={user} />
      <main>
        <h1>AI track record</h1>
        <p className="muted">
          Every pick the AI made, scored after 1, 3, 6 and 12 months against the benchmark (
          {data?.benchmark ?? "SXRV"}, a Nasdaq-100 fund). A buy or hold is a hit when it beat the
          benchmark. A sell is a hit when the share trailed it afterwards. Returns are price
          returns, before fees and tax. <Link href="/ai">Back to the AI guide</Link>
        </p>
        {error && <p className="notice status-failed">{error}</p>}
        {data && (
          <>
            <p>
              {data.picks} pick{data.picks === 1 ? "" : "s"}; {data.unscored} not scored yet (too
              young, or no prices).
            </p>
            <div className="card table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>After</th>
                    <th className="num">Scored</th>
                    <th className="num">Hit rate</th>
                    <th className="num">Avg pick</th>
                    <th className="num">Avg benchmark</th>
                    <th className="num">Avg excess</th>
                  </tr>
                </thead>
                <tbody>
                  {data.windows.map((w) => (
                    <tr key={w.window_months}>
                      <td>{w.window_months} mo</td>
                      <td className="num">{w.scored}</td>
                      <td className="num">{w.hit_rate_pct === null ? "–" : `${w.hit_rate_pct} %`}</td>
                      <td className="num">{pct(w.avg_pick_return_pct)}</td>
                      <td className="num">{pct(w.avg_benchmark_return_pct)}</td>
                      <td className="num">{pct(w.avg_excess_pct)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <h2>Picks</h2>
            {data.items.length === 0 && <p className="muted">No picks yet.</p>}
            {data.items.length > 0 && (
              <div className="card table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Date</th>
                      <th>Security</th>
                      <th>Call</th>
                      <th>Scores (pick vs benchmark)</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.items.map(({ pick, scores }) => (
                      <tr key={pick.id}>
                        <td>{new Date(pick.created_at).toLocaleDateString("en-GB")}</td>
                        <td>{pick.name}</td>
                        <td>{pick.direction}</td>
                        <td>
                          {scores.length === 0
                            ? "not scored yet"
                            : scores.map((s) => (
                                <div key={s.window_months}>
                                  {s.window_months} mo: {pct(s.pick_return_pct)} vs{" "}
                                  {pct(s.benchmark_return_pct)} {s.hit ? "✔ hit" : "✘ miss"}
                                </div>
                              ))}
                        </td>
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
