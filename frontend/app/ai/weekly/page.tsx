"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api, ApiError, type AiCommentary, post } from "@/lib/api";
import { Header } from "../../Header";
import { useUser } from "../../useUser";

const day = (iso: string) =>
  new Date(iso + "T12:00:00").toLocaleDateString("en-GB", { dateStyle: "long" });

export default function WeeklyPage() {
  const user = useUser();
  const [items, setItems] = useState<AiCommentary[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      setItems(await api<AiCommentary[]>("/api/ai/commentaries"));
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Could not load.");
    }
  }, []);

  useEffect(() => {
    if (user) void load();
  }, [user, load]);
  if (!user) return null;

  const writeNow = async () => {
    setError("");
    setBusy(true);
    try {
      await post<AiCommentary>("/api/ai/commentaries/now");
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Could not write it.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <Header user={user} />
      <main>
        <h1>Weekly commentary</h1>
        <p className="muted">
          A short note on your portfolio, written every Sunday evening. If the AI is not switched
          on for you, or the monthly budget is used up, you get a plain summary of your data
          instead. <Link href="/ai">Back to the AI guide</Link>
        </p>
        {error && <p className="notice status-failed">{error}</p>}
        <p>
          <button onClick={writeNow} disabled={busy}>
            {busy ? "Writing…" : "Write this week's now"}
          </button>
        </p>
        {items && items.length === 0 && <p className="muted">No commentary yet.</p>}
        {items?.map((c) => (
          <div className="card" key={c.id}>
            <h2 style={{ marginTop: 0 }}>Week of {day(c.week_start)}</h2>
            <p style={{ whiteSpace: "pre-wrap" }}>{c.text}</p>
            <p className="muted">
              {c.kind === "ai" ? "Written by the AI." : "Plain summary, not written by the AI."}
            </p>
          </div>
        ))}
      </main>
    </>
  );
}
