"use client";

import { useCallback, useEffect, useState } from "react";
import { api, ApiError, del, type Journal, post } from "@/lib/api";
import { Header } from "../Header";
import { useUser } from "../useUser";

const when = (iso: string) => new Date(iso).toLocaleDateString("en-GB", { dateStyle: "medium" });

export default function JournalPage() {
  const user = useUser();
  const [data, setData] = useState<Journal | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState({ action: "bought", name: "", isin: "", reason: "", guidance: "" });

  const load = useCallback(async () => {
    try {
      setData(await api<Journal>("/api/journal"));
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Could not load the journal.");
    }
  }, []);
  useEffect(() => {
    if (user) void load();
  }, [user, load]);

  if (!user) return null;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const body: Record<string, string> = { action: form.action, name: form.name, reason: form.reason };
      if (form.isin.trim()) body.isin = form.isin.trim();
      if (form.guidance.trim()) body.guidance = form.guidance.trim();
      await post("/api/journal", body);
      setForm({ action: "bought", name: "", isin: "", reason: "", guidance: "" });
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Could not save the entry.");
    } finally {
      setBusy(false);
    }
  };

  const remove = async (id: string) => {
    if (!window.confirm("Delete this entry?")) return;
    try {
      await del(`/api/journal/${id}`);
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Could not delete the entry.");
    }
  };

  return (
    <>
      <Header user={user} />
      <main>
        <h1>Decision journal</h1>
        <p className="notice">
          Write down what you did with a piece of guidance, and why. The price at that time is kept,
          so later you can see what followed.
        </p>
        {error && <p className="notice status-failed">{error}</p>}

        <div className="card" style={{ marginBottom: 16 }}>
          <h2 style={{ marginTop: 0 }}>New entry</h2>
          <form onSubmit={submit}>
            <label>
              What did you do?
              <select value={form.action} onChange={(e) => setForm({ ...form, action: e.target.value })}>
                <option value="bought">I bought</option>
                <option value="sold">I sold</option>
                <option value="ignored">I ignored the guidance</option>
              </select>
            </label>
            <label>
              What was it about?
              <input required value={form.name} maxLength={200} onChange={(e) => setForm({ ...form, name: e.target.value })} />
            </label>
            <label>
              ISIN (optional: it stores the price)
              <input value={form.isin} maxLength={20} onChange={(e) => setForm({ ...form, isin: e.target.value })} />
            </label>
            <label>
              The guidance (optional)
              <input value={form.guidance} maxLength={200} onChange={(e) => setForm({ ...form, guidance: e.target.value })} />
            </label>
            <label>
              Why?
              <textarea required rows={3} value={form.reason} maxLength={1000} onChange={(e) => setForm({ ...form, reason: e.target.value })} />
            </label>
            <div>
              <button type="submit" disabled={busy}>
                Save
              </button>
            </div>
          </form>
        </div>

        {data && (
          <div className="card table-wrap">
            <h2 style={{ marginTop: 0 }}>Your entries</h2>
            <p className="muted">
              Bought {data.counts.bought ?? 0}, sold {data.counts.sold ?? 0}, ignored {data.counts.ignored ?? 0}.
              {data.judged > 0 && ` Of ${data.judged} entries tied to an AI pick, its direction was right ${data.right} times.`}
            </p>
            {data.entries.length === 0 ? (
              <p className="muted">No entries yet.</p>
            ) : (
              <table>
                <thead>
                  <tr>
                    <th>Date</th>
                    <th>What</th>
                    <th>Why</th>
                    <th>Since then</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {data.entries.map((e) => (
                    <tr key={e.id}>
                      <td>{when(e.at)}</td>
                      <td>
                        {e.action}: {e.name}
                        {e.guidance && <div className="muted">Guidance: {e.guidance}</div>}
                      </td>
                      <td style={{ whiteSpace: "normal", maxWidth: 320 }}>{e.reason}</td>
                      <td style={{ whiteSpace: "normal", maxWidth: 280 }} className="muted">
                        {e.verdict}
                      </td>
                      <td>
                        <button className="link" onClick={() => remove(e.id)}>
                          Delete
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}
      </main>
    </>
  );
}
