"use client";

import { useEffect, useState } from "react";
import { api, ApiError, type CostItem, type Costs, del, put } from "@/lib/api";
import { Header } from "../Header";
import { useUser } from "../useUser";

const CLASS_LABELS: Record<string, string> = {
  STOCK: "Stock",
  FUND: "Fund or ETF",
  CRYPTO: "Crypto",
  BOND: "Bond",
  PRIVATE_FUND: "Private-market fund",
  DERIVATIVE: "Derivative",
};

// Display only: the server keeps every figure as an exact decimal.
const qty = (value: string) => Number(value).toLocaleString("en-GB", { maximumFractionDigits: 8 });
const eur = (value: number) =>
  value.toLocaleString("en-GB", { style: "currency", currency: "EUR" });

function Row({ item, onChange }: { item: CostItem; onChange: (items: CostItem[]) => void }) {
  const [cost, setCost] = useState(item.unit_cost ?? "");
  const [note, setNote] = useState(item.note ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const parsed = Number(cost.replace(",", "."));
  const valid = cost.trim() !== "" && Number.isFinite(parsed) && parsed >= 0;
  const changed = cost !== (item.unit_cost ?? "") || note !== (item.note ?? "");

  const save = async () => {
    setBusy(true);
    setError("");
    try {
      const body = await put<Costs>(`/api/costs/${item.isin}`, {
        unit_cost: cost.trim().replace(",", "."),
        note: note.trim() === "" ? null : note.trim(),
      });
      onChange(body.items);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Could not save.");
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    setBusy(true);
    setError("");
    try {
      await del(`/api/costs/${item.isin}`);
      onChange((await api<Costs>("/api/costs")).items);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Could not remove.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <tr>
      <td>
        {item.name ?? "—"}
        <div className="mono muted">{item.isin}</div>
        <div className="muted">{CLASS_LABELS[item.asset_class ?? ""] ?? item.asset_class ?? ""}</div>
      </td>
      <td className="num">{Number(item.open_units) > 0 ? qty(item.open_units) : "—"}</td>
      <td className="num">
        {Number(item.sold_units) > 0 ? qty(item.sold_units) : "—"}
        {item.sales > 0 && <div className="muted">{item.sales} sale{item.sales > 1 ? "s" : ""}</div>}
      </td>
      <td>
        <input
          inputMode="decimal"
          aria-label={`Cost per unit of ${item.name ?? item.isin}, in euros`}
          placeholder="€ per unit"
          value={cost}
          onChange={(e) => setCost(e.target.value)}
          style={{ width: 110 }}
        />
        {valid && Number(item.open_units) > 0 && (
          <div className="muted">
            about {eur(parsed * Number(item.open_units))} for the {qty(item.open_units)} units you hold
          </div>
        )}
      </td>
      <td>
        <input
          aria-label={`Note for ${item.name ?? item.isin}`}
          placeholder="Where it comes from (optional)"
          maxLength={200}
          value={note}
          onChange={(e) => setNote(e.target.value)}
        />
      </td>
      <td>
        <button onClick={save} disabled={busy || !valid || !changed}>
          Save
        </button>{" "}
        {item.unit_cost !== null && (
          <button className="link" onClick={remove} disabled={busy}>
            Remove
          </button>
        )}
        {error && <div className="status-failed">{error}</div>}
      </td>
    </tr>
  );
}

export default function CostsPage() {
  const user = useUser();
  const [items, setItems] = useState<CostItem[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!user) return;
    api<Costs>("/api/costs")
      .then((body) => setItems(body.items))
      .catch((err) => setError(err instanceof ApiError ? err.detail : "Could not load."));
  }, [user]);

  if (!user) return null;

  const needed = items?.filter((i) => i.unit_cost === null) ?? [];

  return (
    <>
      <Header user={user} />
      <main>
        <h1>Missing costs</h1>
        <p>
          Trade Republic doesn&apos;t say what a spin-off, a rights issue or units transferred in
          cost. Enter what <strong>one unit</strong> cost you, in euros, as it was when you received it (if the instrument has split since, use the cost per unit before the split). It is used for every such
          unit of that instrument, whether you still hold it or have sold it, and flows into your cost
          basis, profit and loss, and the tax estimate. Leave it empty if you don&apos;t know:
          the figures are then flagged as overstated rather than guessed.
        </p>
        {error && <p className="notice status-failed">{error}</p>}
        {items && items.length === 0 && (
          <p className="notice">Nothing needs a cost: every unit in your history has one.</p>
        )}
        {items && items.length > 0 && (
          <>
            <p className="muted">
              {needed.length === 0
                ? "All of them have a cost."
                : `${needed.length} ${needed.length === 1 ? "instrument still needs" : "instruments still need"} a cost.`}
            </p>
            <div className="card table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Instrument</th>
                    <th className="num">Held, no cost</th>
                    <th className="num">Sold, no cost</th>
                    <th>Cost per unit</th>
                    <th>Note</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((item) => (
                    <Row key={`${item.isin}:${item.unit_cost}:${item.note}`} item={item} onChange={setItems} />
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </main>
    </>
  );
}
