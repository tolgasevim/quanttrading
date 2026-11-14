"use client";

import { useCallback, useEffect, useState } from "react";
import {
  api,
  ApiError,
  del,
  type FundCard,
  post,
  type ScoreDetail,
  type Scores,
  type WatchItem,
} from "@/lib/api";
import { Header } from "../Header";
import { useUser } from "../useUser";

const pts = (value: string | null) => (value === null ? "—" : Number(value).toFixed(0));
const FACTORS = ["momentum", "quality", "growth", "value", "low_vol"] as const;
const FACTOR_NAME = { momentum: "Momentum", quality: "Quality", growth: "Growth", value: "Value", low_vol: "Low risk" };
const QUALITY_TEXT = { ok: "Good data", partial: "Some gaps", poor: "Poor data" } as const;
const QUALITY_CLASS = { ok: "status-success", partial: "", poor: "status-failed" } as const;
const VERDICT_CLASS = { good: "status-success", watch: "", bad: "status-failed", info: "muted", unknown: "muted" } as const;

// "isin,name" lines for a universe list.
function members(text: string) {
  return text
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean)
    .map((l) => {
      const [isin, ...rest] = l.split(",");
      return { isin: isin.trim(), name: rest.join(",").trim() || null };
    });
}

// A CSV with a header row: isin,as_of,roic,... Empty cells are left out.
function fundamentalRows(text: string) {
  const [head, ...lines] = text
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean);
  if (!head) return [];
  const cols = head.split(",").map((c) => c.trim());
  return lines.map((line) => {
    const cells = line.split(",").map((c) => c.trim());
    const row: Record<string, string> = {};
    cols.forEach((c, i) => {
      if (cells[i]) row[c] = cells[i];
    });
    return row;
  });
}

export default function ScoresPage() {
  const user = useUser();
  const [scope, setScope] = useState<"all" | "held" | "watched">("all");
  const [data, setData] = useState<Scores | null>(null);
  const [cards, setCards] = useState<FundCard[]>([]);
  const [watch, setWatch] = useState<WatchItem[]>([]);
  const [open, setOpen] = useState<ScoreDetail | null>(null);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [newIsin, setNewIsin] = useState("");
  const [listName, setListName] = useState("");
  const [listText, setListText] = useState("");
  const [fundText, setFundText] = useState("");

  const load = useCallback(async () => {
    try {
      const [s, w, c] = await Promise.all([
        api<Scores>(`/api/scores?scope=${scope}`),
        api<WatchItem[]>("/api/scores/watchlist"),
        api<FundCard[]>("/api/scores/funds"),
      ]);
      setData(s);
      setWatch(w);
      setCards(c);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Could not load the scores.");
    }
  }, [scope]);
  useEffect(() => {
    if (user) void load();
  }, [user, load]);

  if (!user) return null;

  const run = async (work: () => Promise<unknown>, done: string) => {
    setError("");
    setMessage("");
    try {
      await work();
      if (done) setMessage(done);
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "That did not work.");
    }
  };

  const show = async (isin: string) => {
    try {
      setOpen(await api<ScoreDetail>(`/api/scores/${isin}`));
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Could not load the history.");
    }
  };

  return (
    <>
      <Header user={user} />
      <main>
        <h1>Scores</h1>
        <p className="notice">{data?.note ?? "Information only. The app places no trade."}</p>
        {error && <p className="notice status-failed">{error}</p>}
        {message && <p className="notice status-success">{message}</p>}

        <div className="card table-wrap" style={{ marginBottom: 16 }}>
          <h2 style={{ marginTop: 0 }}>Factor scores{data?.day ? `, ${data.day}` : ""}</h2>
          <label>
            Show
            <select value={scope} onChange={(e) => setScope(e.target.value as typeof scope)}>
              <option value="all">the whole universe</option>
              <option value="held">what I hold</option>
              <option value="watched">my watchlist</option>
            </select>
          </label>
          {data && data.scores.length === 0 ? (
            <p className="muted">No scores yet. They are made every evening after the prices.</p>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Share</th>
                  <th className="num">Score</th>
                  {FACTORS.map((f) => (
                    <th className="num" key={f}>
                      {FACTOR_NAME[f]} ({data?.weights[f]})
                    </th>
                  ))}
                  <th>Data</th>
                </tr>
              </thead>
              <tbody>
                {data?.scores.map((s) => (
                  <tr key={s.isin}>
                    <td style={{ whiteSpace: "normal" }}>
                      <button className="link" onClick={() => show(s.isin)}>
                        {s.name}
                      </button>
                      <div className="muted">
                        {s.sector ?? "no sector"}
                        {s.held ? ", held" : ""}
                        {s.watched ? ", watched" : ""}
                      </div>
                    </td>
                    <td className="num">
                      {pts(s.composite)}
                      {s.composite_pct !== null && <div className="muted">top {(100 - Number(s.composite_pct)).toFixed(0)}%</div>}
                    </td>
                    {FACTORS.map((f) => (
                      <td className="num" key={f}>
                        {pts(s[f])}
                      </td>
                    ))}
                    <td style={{ whiteSpace: "normal" }} className={QUALITY_CLASS[s.data_quality]}>
                      {QUALITY_TEXT[s.data_quality]}
                      {s.flag_text.map((t) => (
                        <div className="muted" key={t}>
                          {t}
                        </div>
                      ))}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <p className="muted">
            Each factor is 0 to 100 against the share&apos;s sector peers. The score re-spreads the weights over the
            factors that exist. A dash means the data is missing, not that the score is zero.
          </p>
        </div>

        {open && open.score && (
          <div className="card table-wrap" style={{ marginBottom: 16 }}>
            <h2 style={{ marginTop: 0 }}>{open.score.name}: history</h2>
            {open.trends.length === 0 ? (
              <p className="muted">No steady trend over the last three months, or not enough history yet.</p>
            ) : (
              <ul>
                {open.trends.map((t) => (
                  <li key={t}>{t}</li>
                ))}
              </ul>
            )}
            <table>
              <thead>
                <tr>
                  <th>Day</th>
                  <th className="num">Score</th>
                  {FACTORS.map((f) => (
                    <th className="num" key={f}>
                      {FACTOR_NAME[f]}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {[...open.history].reverse().slice(0, 30).map((h) => (
                  <tr key={h.day}>
                    <td>{h.day}</td>
                    <td className="num">{pts(h.composite)}</td>
                    {FACTORS.map((f) => (
                      <td className="num" key={f}>
                        {pts(h[f])}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
            <button className="link" onClick={() => setOpen(null)}>
              Close
            </button>
          </div>
        )}

        <div className="card" style={{ marginBottom: 16 }}>
          <h2 style={{ marginTop: 0 }}>Watchlist</h2>
          <p className="muted">A share you watch gets a ticker, prices and a score, like one you hold.</p>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void run(() => post("/api/scores/watchlist", { isin: newIsin }), "Added.").then(() => setNewIsin(""));
            }}
          >
            <label>
              ISIN
              <input required maxLength={12} value={newIsin} onChange={(e) => setNewIsin(e.target.value)} />
            </label>
            <button type="submit">Watch</button>
          </form>
          {watch.length > 0 && (
            <table>
              <tbody>
                {watch.map((w) => (
                  <tr key={w.isin}>
                    <td>{w.name ?? w.isin}</td>
                    <td className="muted">{w.has_score ? "scored" : "waiting for a ticker and prices"}</td>
                    <td>
                      <button className="link" onClick={() => run(() => del(`/api/scores/watchlist/${w.isin}`), "Removed.")}>
                        Remove
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        <div className="card" style={{ marginBottom: 16 }}>
          <h2 style={{ marginTop: 0 }}>Fund and ETC scorecards</h2>
          {cards.length === 0 && <p className="muted">You hold no fund or ETC, and no facts are entered for one.</p>}
          {cards.map((c) => (
            <div key={c.isin} style={{ marginBottom: 16 }}>
              <h3>
                {c.name} <span className="muted">({c.kind.toUpperCase()}, {c.isin}{c.held ? ", held" : ""})</span>
              </h3>
              <table>
                <tbody>
                  {c.lines.map((l) => (
                    <tr key={l.label}>
                      <td style={{ width: "22%" }}>{l.label}</td>
                      <td className={VERDICT_CLASS[l.verdict]} style={{ width: "18%" }}>
                        {l.value}
                      </td>
                      <td style={{ whiteSpace: "normal" }} className="muted">
                        {l.note}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {c.overlaps.length > 0 && <p>Overlap with funds you hold: {c.overlaps.join("; ")}.</p>}
              {c.missing.length > 0 && <p className="muted">Not entered yet: {c.missing.join(", ")}.</p>}
              {c.tax_note && <p className="muted">{c.tax_note}</p>}
            </div>
          ))}
        </div>

        {user.role === "admin" && (
          <div className="card">
            <h2 style={{ marginTop: 0 }}>Load data (admin)</h2>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void run(async () => {
                  const out = await post<{ members: number; rejected: string[] }>("/api/scores/universe", {
                    list_name: listName,
                    members: members(listText),
                  });
                  setMessage(`List stored: ${out.members} shares. Left out (bad ISIN): ${out.rejected.join(", ") || "none"}.`);
                }, "");
              }}
            >
              <h3>Universe list</h3>
              <p className="muted">
                One list replaces the old one of the same name. Lines of <code>ISIN,name</code>. Refresh it every month.
              </p>
              <label>
                List name
                <input required maxLength={30} value={listName} onChange={(e) => setListName(e.target.value)} />
              </label>
              <textarea rows={5} value={listText} onChange={(e) => setListText(e.target.value)} />
              <div>
                <button type="submit">Store the list</button>
              </div>
            </form>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void run(async () => {
                  const out = await post<{ stored: number; rejected: string[] }>("/api/scores/fundamentals", {
                    rows: fundamentalRows(fundText),
                  });
                  setMessage(`Stored ${out.stored} rows. Left out (bad ISIN): ${out.rejected.join(", ") || "none"}.`);
                }, "");
              }}
            >
              <h3>Fundamentals</h3>
              <p className="muted">
                CSV with a header row. Columns: isin, as_of (YYYY-MM-DD), ev_ebit, fcf_yield, ev_sales, roic, gross_margin,
                net_debt_ebitda, revenue_growth, revenue_cagr_3y, eps_revision. Yields, margins, returns and growth are
                percent. Leave a cell empty when unknown.
              </p>
              <textarea rows={5} value={fundText} onChange={(e) => setFundText(e.target.value)} />
              <div>
                <button type="submit">Store the rows</button>
              </div>
            </form>
            <p className="muted">
              Fund facts (cost, size, domicile) are entered with <code>PUT /api/scores/facts/&lt;ISIN&gt;</code>.
            </p>
          </div>
        )}
      </main>
    </>
  );
}
