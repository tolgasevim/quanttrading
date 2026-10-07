"use client";

import { type FormEvent, useEffect, useState } from "react";
import { api, ApiError, type AlertSettings, type Notifications, post, put } from "@/lib/api";
import { Header } from "../Header";
import { useUser } from "../useUser";

// Tells the header to read the unread count again.
const changed = () => window.dispatchEvent(new Event("qt-notifications"));

const PAGE = 100;

const KIND_TEXT: Record<string, string> = {
  daily_move: "Price move",
  job_failed: "Data job failed",
  ai_budget: "AI budget",
  weekly_commentary: "Weekly commentary",
};

const when = (iso: string) =>
  new Date(iso).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" });

function SettingsCard() {
  const [form, setForm] = useState<AlertSettings | null>(null);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    api<AlertSettings>("/api/alerts/settings")
      .then(setForm)
      .catch((err) => setError(err instanceof ApiError ? err.detail : "Could not load the settings."));
  }, []);
  if (!form) return error ? <p className="notice status-failed">{error}</p> : null;

  const set = (patch: Partial<AlertSettings>) => {
    setSaved(false);
    setForm({ ...form, ...patch });
  };
  const save = async (e: FormEvent) => {
    e.preventDefault();
    setError("");
    for (const [key, label] of [
      ["move_stock_pct", "shares"],
      ["move_fund_pct", "funds"],
      ["move_crypto_pct", "coins"],
    ] as const) {
      const value = Number(form[key]);
      if (!/^\d+(\.\d{1,2})?$/.test(String(form[key])) || !(value >= 0.01 && value <= 100)) {
        setError(`The limit for ${label} is a number from 0.01 to 100, with at most two decimals.`);
        return;
      }
    }
    // One time without the other is refused by the server; say so before the round trip.
    if (!form.quiet_start !== !form.quiet_end) {
      setError("Set both quiet hours times, or switch quiet hours off.");
      return;
    }
    try {
      setForm(await put<AlertSettings>("/api/alerts/settings", form));
      setSaved(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Could not save.");
    }
  };
  const quiet = form.quiet_start !== null && form.quiet_end !== null;

  return (
    <form className="card" onSubmit={save}>
      <h2 style={{ marginTop: 0 }}>Alert settings</h2>
      <label style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <input
          type="checkbox"
          checked={form.daily_moves_enabled}
          onChange={(e) => set({ daily_moves_enabled: e.target.checked })}
          style={{ width: "auto" }}
        />
        Alert me when a holding moves a lot in one day
      </label>
      <p className="muted">
        The limit is the change from the previous close, in percent, up or down.
      </p>
      <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
        {(
          [
            ["move_stock_pct", "Shares"],
            ["move_fund_pct", "Funds"],
            ["move_crypto_pct", "Coins"],
          ] as const
        ).map(([key, label]) => (
          <label key={key}>
            {label} (%)
            <input
              type="number"
              min="0.01"
              max="100"
              step="0.01"
              value={form[key]}
              onChange={(e) => set({ [key]: e.target.value })}
              style={{ width: 100 }}
            />
          </label>
        ))}
      </div>
      <h3>Quiet hours</h3>
      <label style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <input
          type="checkbox"
          checked={quiet}
          onChange={(e) =>
            set(e.target.checked ? { quiet_start: "22:00", quiet_end: "07:00" } : { quiet_start: null, quiet_end: null })
          }
          style={{ width: "auto" }}
        />
        No messages during these hours (Europe/Berlin)
      </label>
      {quiet && (
        <div style={{ display: "flex", gap: 16, marginTop: 8 }}>
          <label>
            From
            <input type="time" value={form.quiet_start ?? ""} onChange={(e) => set({ quiet_start: e.target.value })} />
          </label>
          <label>
            To
            <input type="time" value={form.quiet_end ?? ""} onChange={(e) => set({ quiet_end: e.target.value })} />
          </label>
        </div>
      )}
      <p className="muted">
        Quiet hours will apply to email and Telegram messages, which are not switched on yet. This
        list always keeps every alert.
      </p>
      <button type="submit">Save</button>
      {saved && <span className="status-success"> Saved.</span>}
      {error && <p className="error">{error}</p>}
    </form>
  );
}

export default function NotificationsPage() {
  const user = useUser();
  const [data, setData] = useState<Notifications | null>(null);
  const [more, setMore] = useState(false); // the last page was full, so there may be older alerts
  const [error, setError] = useState("");

  useEffect(() => {
    if (!user) return;
    api<Notifications>(`/api/notifications?limit=${PAGE}`)
      .then((body) => {
        setData(body);
        setMore(body.items.length === PAGE);
      })
      .catch((err) => setError(err instanceof ApiError ? err.detail : "Could not load."));
  }, [user]);
  if (!user) return null;

  const showOlder = async () => {
    if (!data) return;
    try {
      const older = await api<Notifications>(
        `/api/notifications?limit=${PAGE}&offset=${data.items.length}`,
      );
      // New alerts may have arrived at the top meanwhile, which shifts the pages: skip repeats.
      const have = new Set(data.items.map((n) => n.id));
      setData({
        ...older,
        items: [...data.items, ...older.items.filter((n) => !have.has(n.id))],
      });
      setMore(older.items.length === PAGE);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Could not load.");
    }
  };

  // `id` is the one alert marked read, or none for "all". The answer carries the newest alerts
  // only, so the rows on the page are updated here, by id.
  const mutate = async (path: string, id?: string) => {
    try {
      // "All" means the alerts the page has shown: one that arrived meanwhile, or an older one the
      // page never loaded, stays unread.
      const shown = (data?.items ?? []).slice(0, 1000).map((n) => n.id);
      const sent = new Set(shown); // only these are marked on the server, so only these here
      const range = id === undefined && shown.length > 0 ? { ids: shown } : undefined;
      const body = await post<Notifications>(path, range);
      setData((old) => ({
        unread: body.unread,
        items: (old?.items ?? body.items).map((n) =>
          (id === undefined ? sent.has(n.id) : n.id === id) ? { ...n, read: true } : n,
        ),
      }));
      changed();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Could not save.");
    }
  };

  return (
    <>
      <Header user={user} />
      <main>
        <h1>Alerts</h1>
        {error && <p className="notice status-failed">{error}</p>}
        {data && data.items.length === 0 && (
          <p className="notice">
            No alerts yet. A price move over your limit, or a failed data job (owner only), shows up
            here after the evening price run.
          </p>
        )}
        {data && data.items.length > 0 && (
          <div className="card">
            <p>
              <strong>{data.unread}</strong> unread.{" "}
              {data.items.some((n) => !n.read) && (
                <button className="link" onClick={() => mutate("/api/notifications/read-all")}>
                  Mark all as read
                </button>
              )}
            </p>
            {data.items.map((n) => (
              <div key={n.id} style={{ borderTop: "1px solid var(--border, #ddd)", padding: "8px 0" }}>
                <div style={{ fontWeight: n.read ? "normal" : 600 }}>
                  <span className={n.severity === "warning" ? "status-failed" : ""}>
                    {KIND_TEXT[n.kind] ?? n.kind}
                  </span>
                  : {n.title}
                </div>
                <div>{n.body}</div>
                <div className="muted">
                  {when(n.created_at)}
                  {!n.read && (
                    <>
                      {" · "}
                      <button className="link" onClick={() => mutate(`/api/notifications/${n.id}/read`, n.id)}>
                        Mark as read
                      </button>
                    </>
                  )}
                </div>
              </div>
            ))}
            {data.unread > 0 && !data.items.some((n) => !n.read) && (
              <p className="muted">The unread alerts are older than the ones shown here.</p>
            )}
            {more && (
              <p>
                <button className="link" onClick={showOlder}>
                  Show older alerts
                </button>
              </p>
            )}
          </div>
        )}
        <SettingsCard />
      </main>
    </>
  );
}
