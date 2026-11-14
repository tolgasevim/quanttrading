"use client";

import { useCallback, useEffect, useState } from "react";
import {
  api,
  ApiError,
  type Cash,
  patch,
  post,
  type Reserve,
  type UnlockResult,
} from "@/lib/api";
import { Header } from "../Header";
import { useUser } from "../useUser";

const eur = (value: string | null) =>
  value === null ? "—" : Number(value).toLocaleString("en-GB", { style: "currency", currency: "EUR" });

const BAND_TEXT = {
  low: "Below the cash band",
  ok: "Inside the cash band",
  high: "Above the cash band: there is cash to invest",
} as const;

export default function CashPage() {
  const user = useUser();
  const [data, setData] = useState<Cash | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const [balance, setBalance] = useState({ amount: "", as_of: "", note: "" });
  const [reserve, setReserve] = useState({ name: "", kind: "opportunity", amount: "", target_date: "" });
  const [editing, setEditing] = useState<Reserve | null>(null);
  const [editAmount, setEditAmount] = useState("");

  const [unlockId, setUnlockId] = useState("");
  const [unlock, setUnlock] = useState({ isin: "", amount: "", manual: false });
  const [checks, setChecks] = useState<UnlockResult | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await api<Cash>("/api/cash"));
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Could not load the cash page.");
    }
  }, []);
  useEffect(() => {
    if (user) void load();
  }, [user, load]);

  if (!user) return null;

  // Run one change, then show the answer it returns.
  const run = async (action: () => Promise<Cash | void>) => {
    setBusy(true);
    setError("");
    try {
      const next = await action();
      if (next) setData(next);
      else await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "That did not work.");
    } finally {
      setBusy(false);
    }
  };

  const saveBalance = (e: React.FormEvent) => {
    e.preventDefault();
    void run(async () => {
      const body: Record<string, string> = { amount: balance.amount };
      if (balance.as_of) body.as_of = balance.as_of;
      if (balance.note.trim()) body.note = balance.note.trim();
      const next = await post<Cash>("/api/cash/balance", body);
      setBalance({ amount: "", as_of: "", note: "" });
      return next;
    });
  };

  const addReserve = (e: React.FormEvent) => {
    e.preventDefault();
    void run(async () => {
      const body: Record<string, string> = {
        name: reserve.name,
        kind: reserve.kind,
        amount: reserve.amount,
      };
      if (reserve.target_date) body.target_date = reserve.target_date;
      const next = await post<Cash>("/api/cash/reserves", body);
      setReserve({ name: "", kind: "opportunity", amount: "", target_date: "" });
      return next;
    });
  };

  const saveEdit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!editing) return;
    void run(async () => {
      const next = await patch<Cash>(`/api/cash/reserves/${editing.id}`, { amount: editAmount });
      setEditing(null);
      return next;
    });
  };

  const release = (r: Reserve) => {
    if (!window.confirm(`Release "${r.name}"? Its ${eur(r.amount)} becomes investable cash.`)) return;
    void run(() => post<Cash>(`/api/cash/reserves/${r.id}/release`));
  };

  const refill = (r: Reserve) => {
    const amount = window.prompt(`How much do you put back into "${r.name}"? (up to ${eur(r.loaned)})`, String(Number(r.loaned)));
    if (!amount) return;
    void run(() => post<Cash>(`/api/cash/reserves/${r.id}/refill`, { amount }));
  };

  const runChecks = (e: React.FormEvent) => {
    e.preventDefault();
    void run(async () => {
      setChecks(
        await post<UnlockResult>(`/api/cash/reserves/${unlockId}/unlock-check`, unlock),
      );
    });
  };

  const confirmUnlock = () => {
    if (!window.confirm("Confirm? The reserve shrinks by this amount. You place the trade at your broker.")) return;
    void run(async () => {
      const next = await post<Cash>(`/api/cash/reserves/${unlockId}/unlock`, unlock);
      setChecks(null);
      setUnlock({ isin: "", amount: "", manual: false });
      return next;
    });
  };

  const opportunity = data?.reserves.filter((r) => r.kind === "opportunity") ?? [];
  const s = data?.status;

  return (
    <>
      <Header user={user} />
      <main>
        <h1>Cash and reserves</h1>
        <p className="notice">
          The broker&apos;s cash account is not on your securities statement, so you enter the balance.
          The app checks it against the balance rebuilt from your history. Reserved money is never
          counted as cash to invest.
        </p>
        {error && <p className="notice status-failed">{error}</p>}
        {data?.warnings.map((w) => (
          <p className="notice status-failed" key={w}>
            {w}
          </p>
        ))}

        {data && s && (
          <div className="card table-wrap" style={{ marginBottom: 16 }}>
            <h2 style={{ marginTop: 0 }}>Where you stand</h2>
            <table>
              <tbody>
                <tr>
                  <td>
                    Cash balance
                    <div className="muted">
                      {s.cash_source === "entered" ? `entered for ${s.entered_as_of}` : "rebuilt from your history"}
                    </div>
                  </td>
                  <td className="num">{eur(s.cash)}</td>
                </tr>
                {s.entered !== null && (
                  <tr>
                    <td>
                      Rebuilt from your history
                      <div className="muted">Difference to what you entered, and how big it is</div>
                    </td>
                    <td className={`num ${s.mismatch ? "status-failed" : ""}`}>
                      {eur(s.history)} ({eur(s.difference)}
                      {s.difference_pct !== null ? `, ${Number(s.difference_pct).toFixed(1)}%` : ""})
                    </td>
                  </tr>
                )}
                <tr>
                  <td>Set aside in reserves</td>
                  <td className="num">{eur(s.reserves_total)}</td>
                </tr>
                <tr>
                  <td>
                    <strong>Investable cash</strong>
                  </td>
                  <td className="num">
                    <strong>{eur(s.investable)}</strong>
                  </td>
                </tr>
                <tr>
                  <td>Securities (valued holdings)</td>
                  <td className="num">{eur(data.securities_value)}</td>
                </tr>
                <tr>
                  <td>Net worth (securities plus all cash)</td>
                  <td className="num">{eur(data.net_worth)}</td>
                </tr>
                {data.band && (
                  <tr>
                    <td>
                      {BAND_TEXT[data.band.status]}
                      <div className="muted">
                        Investable cash is {Number(data.band.pct).toFixed(1)}% of securities plus investable
                        cash. The band is {data.band.low_pct}% to {data.band.high_pct}%, with a target of{" "}
                        {data.band.target_pct}%.
                      </div>
                    </td>
                    <td className={`num ${data.band.status === "ok" ? "status-success" : "status-failed"}`}>
                      {data.band.status === "high" ? `${eur(data.band.excess)} above target` : ""}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        )}

        <div className="card" style={{ marginBottom: 16 }}>
          <h2 style={{ marginTop: 0 }}>Enter your cash balance</h2>
          <form onSubmit={saveBalance}>
            <label>
              Balance (EUR)
              <input
                inputMode="decimal"
                required
                value={balance.amount}
                onChange={(e) => setBalance({ ...balance, amount: e.target.value })}
              />
            </label>
            <label>
              As of (empty: today)
              <input type="date" value={balance.as_of} onChange={(e) => setBalance({ ...balance, as_of: e.target.value })} />
            </label>
            <label>
              Note (optional)
              <input value={balance.note} maxLength={200} onChange={(e) => setBalance({ ...balance, note: e.target.value })} />
            </label>
            <div>
              <button type="submit" disabled={busy}>
                Save balance
              </button>
            </div>
          </form>
          {data && data.balances.length > 0 && (
            <p className="muted">
              Earlier entries:{" "}
              {data.balances.map((b) => `${b.as_of}: ${eur(b.amount)}`).join(" · ")}
            </p>
          )}
        </div>

        <div className="card table-wrap" style={{ marginBottom: 16 }}>
          <h2 style={{ marginTop: 0 }}>Reserves</h2>
          <p className="muted">
            A <strong>hard</strong> reserve is never invested. An <strong>opportunity</strong> reserve is
            not invested either, unless you unlock part of it under the strict rule below.
          </p>
          {data && data.reserves.length === 0 && <p className="muted">No reserves yet.</p>}
          {data && data.reserves.length > 0 && (
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Kind</th>
                  <th className="num">Amount</th>
                  <th>Needed by</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {data.reserves.map((r) => (
                  <tr key={r.id}>
                    <td>{r.name}</td>
                    <td>{r.kind}</td>
                    <td className="num">
                      {eur(r.amount)}
                      {Number(r.loaned) > 0 && (
                        <div className="muted">
                          {eur(r.loaned)} on loan
                          {r.refill_per_month !== null ? `, put back ${eur(r.refill_per_month)} a month` : ""}
                        </div>
                      )}
                    </td>
                    <td>{r.target_date ?? ""}</td>
                    <td>
                      {Number(r.loaned) > 0 && (
                        <>
                          <button className="link" onClick={() => refill(r)}>
                            Put money back
                          </button>{" "}
                        </>
                      )}
                      <button
                        className="link"
                        onClick={() => {
                          setEditing(r);
                          setEditAmount(String(Number(r.amount)));
                        }}
                      >
                        Change amount
                      </button>{" "}
                      <button className="link" onClick={() => release(r)}>
                        Release
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {editing && (
            <form onSubmit={saveEdit} style={{ marginTop: 12 }}>
              <label>
                New amount for {editing.name} (EUR). A smaller amount frees the difference as investable cash.
                <input inputMode="decimal" required value={editAmount} onChange={(e) => setEditAmount(e.target.value)} />
              </label>
              <div style={{ display: "flex", gap: 8 }}>
                <button type="submit" disabled={busy}>
                  Save
                </button>
                <button type="button" className="link" onClick={() => setEditing(null)}>
                  Cancel
                </button>
              </div>
            </form>
          )}
          <h3>Add a reserve</h3>
          <form onSubmit={addReserve}>
            <label>
              Name
              <input required value={reserve.name} maxLength={80} onChange={(e) => setReserve({ ...reserve, name: e.target.value })} />
            </label>
            <label>
              Kind
              <select value={reserve.kind} onChange={(e) => setReserve({ ...reserve, kind: e.target.value })}>
                <option value="opportunity">Opportunity (can be unlocked)</option>
                <option value="hard">Hard (never invested)</option>
              </select>
            </label>
            <label>
              Amount (EUR)
              <input inputMode="decimal" required value={reserve.amount} onChange={(e) => setReserve({ ...reserve, amount: e.target.value })} />
            </label>
            <label>
              Needed by (optional)
              <input type="date" value={reserve.target_date} onChange={(e) => setReserve({ ...reserve, target_date: e.target.value })} />
            </label>
            <div>
              <button type="submit" disabled={busy}>
                Add reserve
              </button>
            </div>
          </form>
        </div>

        {opportunity.length > 0 && (
          <div className="card" style={{ marginBottom: 16 }}>
            <h2 style={{ marginTop: 0 }}>Unlock part of an opportunity reserve</h2>
            <p className="muted">
              Strict rule: at least one trigger, at most half of the reserve, a liquid share or ETF
              only, and no share above 15% of your portfolio. The app places no trade. A confirmed
              unlock only shrinks the reserve.
            </p>
            <form onSubmit={runChecks}>
              <label>
                Reserve
                <select required value={unlockId} onChange={(e) => { setUnlockId(e.target.value); setChecks(null); }}>
                  <option value="">Choose…</option>
                  {opportunity.map((r) => (
                    <option key={r.id} value={r.id}>
                      {r.name} ({eur(r.amount)})
                    </option>
                  ))}
                </select>
              </label>
              <label>
                ISIN of the instrument
                <input required maxLength={12} value={unlock.isin} onChange={(e) => { setUnlock({ ...unlock, isin: e.target.value }); setChecks(null); }} />
              </label>
              <label>
                Amount (EUR)
                <input inputMode="decimal" required value={unlock.amount} onChange={(e) => { setUnlock({ ...unlock, amount: e.target.value }); setChecks(null); }} />
              </label>
              <label style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <input type="checkbox" checked={unlock.manual} onChange={(e) => { setUnlock({ ...unlock, manual: e.target.checked }); setChecks(null); }} />
                I flag this as an opportunity myself
              </label>
              <div>
                <button type="submit" disabled={busy || !unlockId}>
                  Check the rule
                </button>
              </div>
            </form>
            {checks && (
              <>
                <table style={{ marginTop: 12 }}>
                  <tbody>
                    {checks.checks.map((c) => (
                      <tr key={c.key}>
                        <td className={c.passed === true ? "status-success" : c.passed === false ? "status-failed" : "muted"}>
                          {c.passed === true ? "Yes" : c.passed === false ? "No" : "No data"}
                        </td>
                        <td>
                          {c.title}
                          <div className="muted">{c.detail}</div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <p className={checks.allowed ? "status-success" : "status-failed"}>
                  {checks.allowed ? "The rule allows this unlock." : "The rule does not allow this unlock."}
                </p>
                {checks.allowed && (
                  <button onClick={confirmUnlock} disabled={busy}>
                    Confirm unlock
                  </button>
                )}
              </>
            )}
          </div>
        )}

        {data && data.events.length > 0 && (
          <div className="card table-wrap">
            <h2 style={{ marginTop: 0 }}>History of the reserves</h2>
            <table>
              <thead>
                <tr>
                  <th>When</th>
                  <th>Reserve</th>
                  <th>What</th>
                  <th className="num">Before</th>
                  <th className="num">After</th>
                </tr>
              </thead>
              <tbody>
                {data.events.map((e) => (
                  <tr key={e.id}>
                    <td>{new Date(e.at).toLocaleString("en-GB")}</td>
                    <td>{e.name}</td>
                    <td>{e.action}</td>
                    <td className="num">{eur(e.amount_before)}</td>
                    <td className="num">{eur(e.amount_after)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </main>
    </>
  );
}
