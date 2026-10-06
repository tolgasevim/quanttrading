"use client";

import { type FormEvent, useCallback, useEffect, useState } from "react";
import { api, ApiError, type AiAnswer, type AiPick, type AiStatus, post } from "@/lib/api";
import { Header } from "../Header";
import { useUser } from "../useUser";

const when = (iso: string) =>
  new Date(iso).toLocaleDateString("en-GB", { dateStyle: "medium" });

function ConsentCard({ status, onDone }: { status: AiStatus; onDone: (s: AiStatus) => void }) {
  const [hide, setHide] = useState(status.anonymise_amounts);
  const [error, setError] = useState("");
  const accept = async () => {
    setError("");
    try {
      onDone(await post<AiStatus>("/api/ai/consent", { accept: true, anonymise_amounts: hide }));
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Could not save.");
    }
  };
  return (
    <div className="card">
      <h2 style={{ marginTop: 0 }}>Before you use the AI</h2>
      <p>{status.disclaimer}</p>
      <label style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <input
          type="checkbox"
          checked={hide}
          onChange={(e) => setHide(e.target.checked)}
          style={{ width: "auto" }}
        />
        Hide amounts: send only the weight of each position
      </label>
      <p>
        <button onClick={accept}>I understand and accept</button>
      </p>
      {error && <p className="error">{error}</p>}
    </div>
  );
}

function Picks({ picks }: { picks: AiPick[] }) {
  if (picks.length === 0) return <p className="muted">No picks yet.</p>;
  return (
    <div className="card table-wrap">
      <table>
        <thead>
          <tr>
            <th>Date</th>
            <th>Security</th>
            <th>Call</th>
            <th className="num">Horizon</th>
            <th className="num">Price then</th>
            <th>Reason</th>
          </tr>
        </thead>
        <tbody>
          {picks.map((p) => (
            <tr key={p.id}>
              <td>{when(p.created_at)}</td>
              <td>
                {p.name}
                {p.held ? " (held)" : ""}
                <div className="muted">{p.isin ?? p.ticker ?? ""}</div>
              </td>
              <td>{p.direction}</td>
              <td className="num">{p.horizon_months} mo</td>
              <td className="num">
                {p.price ? `${Number(p.price).toFixed(2)} ${p.price_currency ?? ""}` : "n/a"}
              </td>
              <td>{p.rationale}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function AiPage() {
  const user = useUser();
  const [status, setStatus] = useState<AiStatus | null>(null);
  const [picks, setPicks] = useState<AiPick[]>([]);
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState<AiAnswer | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      const [s, p] = await Promise.all([
        api<AiStatus>("/api/ai/status"),
        api<AiPick[]>("/api/ai/picks"),
      ]);
      setStatus(s);
      setPicks(p);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Could not load.");
    }
  }, []);

  useEffect(() => {
    if (user) void load();
  }, [user, load]);
  if (!user) return null;

  const ask = async (e: FormEvent) => {
    e.preventDefault();
    setError("");
    setAnswer(null);
    setBusy(true);
    try {
      setAnswer(await post<AiAnswer>("/api/ai/ask", { question }));
      await load(); // the pick log and the budget changed
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "The question failed.");
    } finally {
      setBusy(false);
    }
  };

  const b = status?.budget;
  return (
    <>
      <Header user={user} />
      <main>
        <h1>AI guide</h1>
        {error && <p className="notice status-failed">{error}</p>}
        {status && !status.configured && (
          <p className="notice">
            The AI is not set up. The owner must add an API key (QT_ANTHROPIC_API_KEY) and
            restart the app.
          </p>
        )}
        {status && !status.accepted && <ConsentCard status={status} onDone={setStatus} />}
        {status && status.accepted && (
          <form className="card" onSubmit={ask}>
            <h2 style={{ marginTop: 0 }}>Ask about your portfolio</h2>
            <textarea
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              maxLength={1000}
              rows={3}
              placeholder="For example: Which of my positions is the biggest risk?"
              style={{ width: "100%" }}
            />
            <p>
              <button type="submit" disabled={busy || question.trim().length < 3}>
                {busy ? "Thinking…" : "Ask"}
              </button>
            </p>
            <p className="muted">{status.label}</p>
            <label style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <input
                type="checkbox"
                checked={status.anonymise_amounts}
                disabled={busy}
                onChange={async (e) => {
                  setError("");
                  try {
                    setStatus(
                      await post<AiStatus>("/api/ai/consent", {
                        accept: true,
                        anonymise_amounts: e.target.checked,
                      }),
                    );
                  } catch (err) {
                    setError(err instanceof ApiError ? err.detail : "Could not save.");
                  }
                }}
                style={{ width: "auto" }}
              />
              Hide amounts: send only the weight of each position
            </label>
            <details>
              <summary className="muted">Disclaimer</summary>
              <p className="muted">{status.disclaimer}</p>
            </details>
          </form>
        )}
        {answer && (
          <div className="card">
            <p className="muted">{answer.label}</p>
            <p style={{ whiteSpace: "pre-wrap" }}>{answer.answer}</p>
            {answer.fallback_used && (
              <p className="muted">A backup model answered this ({answer.model}).</p>
            )}
            <p className="muted">Data used: {answer.data_points.join("; ")}.</p>
            {answer.notes.map((n) => (
              <p key={n} className="notice status-failed">
                {n}
              </p>
            ))}
            {answer.picks.length > 0 && (
              <p className="muted">
                {answer.picks.length} pick{answer.picks.length === 1 ? "" : "s"} added to the log
                below.
              </p>
            )}
          </div>
        )}
        {b && (
          <p className="muted">
            Budget {b.month}: you used {b.user_eur} of {b.user_cap_eur} EUR
            {b.total_eur !== null && b.total_cap_eur !== null
              ? `; all users ${b.total_eur} of ${b.total_cap_eur} EUR`
              : ""}
            .
          </p>
        )}
        <h2>Pick log</h2>
        <Picks picks={picks} />
      </main>
    </>
  );
}
