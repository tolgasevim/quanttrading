"use client";

import { useEffect, useState } from "react";
import { api, ApiError, type PriceItem, type Prices, put } from "@/lib/api";
import { Header } from "../Header";
import { useUser } from "../useUser";

const STATUS_TEXT: Record<PriceItem["status"], string> = {
  priced: "Priced",
  stale: "Last price is too old: the ticker may be wrong or delisted",
  no_rate: "No euro exchange rate for the currency of this price",
  inactive: "Switched off",
  waiting: "Ticker found, waiting for the first price",
  unmapped: "No ticker found",
  not_checked: "Not looked up yet",
  unsupported: "No ticker expected",
};

const SOURCE_TEXT: Record<string, string> = {
  yahoo: "found by Yahoo search",
  openfigi: "found by OpenFIGI",
  manual: "entered by hand",
};

// Display only: the server keeps every figure as an exact decimal.
const num = (value: string) => Number(value).toLocaleString("en-GB", { maximumFractionDigits: 4 });

function Row({
  item,
  admin,
  onChange,
}: {
  item: PriceItem;
  admin: boolean;
  onChange: (items: PriceItem[]) => void;
}) {
  const [symbol, setSymbol] = useState(item.symbol ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const editable = admin && item.status !== "unsupported";
  const unchanged = symbol.trim() === (item.symbol ?? "");
  // A saved ticker with no price yet can be fetched again without changing it.
  const retry = unchanged && item.status === "waiting";

  const save = async () => {
    setBusy(true);
    setError("");
    try {
      onChange((await put<Prices>(`/api/prices/${item.isin}`, { symbol: symbol.trim() })).items);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Could not save.");
      // The ticker may be saved even when the price fetch failed: show the real state.
      try {
        onChange((await api<Prices>("/api/prices")).items);
      } catch {
        // keep the message above
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <tr>
      <td>
        {item.name ?? "—"}
        <div className="mono muted">{item.isin}</div>
      </td>
      <td className={["unmapped", "stale", "no_rate"].includes(item.status) ? "status-failed" : ""}>
        {STATUS_TEXT[item.status]}
        {item.mapping_source && item.mapping_source !== "none" && (
          <div className="muted">{SOURCE_TEXT[item.mapping_source] ?? item.mapping_source}</div>
        )}
      </td>
      <td>
        {editable ? (
          <input
            aria-label={`Ticker for ${item.name ?? item.isin}`}
            placeholder="e.g. SAP.DE"
            value={symbol}
            onChange={(e) => setSymbol(e.target.value)}
            style={{ width: 120 }}
          />
        ) : (
          (item.symbol ?? "—")
        )}
      </td>
      <td className="num">
        {item.last_close !== null ? (
          <>
            {num(item.last_close)} {item.currency}
            <div className="muted">{item.last_date}</div>
          </>
        ) : (
          "—"
        )}
      </td>
      <td>
        {editable && (
          <button
            onClick={save}
            disabled={busy || symbol.trim() === "" || (unchanged && !retry)}
          >
            {retry ? "Fetch price again" : "Save"}
          </button>
        )}
        {error && <div className="status-failed">{error}</div>}
      </td>
    </tr>
  );
}

export default function PricesPage() {
  const user = useUser();
  const [items, setItems] = useState<PriceItem[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!user) return;
    api<Prices>("/api/prices")
      .then((body) => setItems(body.items))
      .catch((err) => setError(err instanceof ApiError ? err.detail : "Could not load."));
  }, [user]);

  if (!user) return null;
  const admin = user.role === "admin";
  const missing = items?.filter((i) => i.status === "unmapped").length ?? 0;

  return (
    <>
      <Header user={user} />
      <main>
        <h1>Prices</h1>
        <p>
          Each share and fund you hold needs a ticker so its price can be fetched every evening. The
          app looks tickers up from the ISIN (Yahoo search, then OpenFIGI) once a day. Prices come from
          Yahoo Finance in the currency of the listing and are converted to euros at the ECB rate of
          the same day, so they can differ slightly from the price Trade Republic quotes.
          Crypto is priced from your statement.
        </p>
        {error && <p className="notice status-failed">{error}</p>}
        {items && items.length === 0 && (
          <p className="notice">
            No open positions yet. <a href="/import">Import your transactions</a> first.
          </p>
        )}
        {missing > 0 && (
          <p className="notice">
            <strong>
              {missing} {missing === 1 ? "holding has" : "holdings have"} no ticker.
            </strong>{" "}
            {admin
              ? "Enter its Yahoo ticker below (for example SAP.DE for a Xetra listing) and the price is fetched at once."
              : "Ask the owner to enter it."}
          </p>
        )}
        {items && items.length > 0 && (
          <div className="card table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Instrument</th>
                  <th>Status</th>
                  <th>Ticker</th>
                  <th className="num">Last price</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <Row
                    key={`${item.isin}:${item.symbol}:${item.status}`}
                    item={item}
                    admin={admin}
                    onChange={setItems}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </main>
    </>
  );
}
