"use client";

import { type FormEvent, useCallback, useEffect, useState } from "react";
import { api, ApiError, del, type Delivery, type Passkey, post, put, type User } from "@/lib/api";
import { createCredential, passkeysSupported } from "@/lib/passkeys";
import { Header } from "../Header";
import { useUser } from "../useUser";

export default function Settings() {
  const user = useUser();
  const [setup, setSetup] = useState<{ secret: string; otpauth_uri: string } | null>(null);
  const [code, setCode] = useState("");
  const [enabled, setEnabled] = useState(false);
  const [error, setError] = useState("");
  const [keys, setKeys] = useState<Passkey[]>([]);
  const [keyName, setKeyName] = useState("");
  const [keyError, setKeyError] = useState("");
  const [delivery, setDelivery] = useState<Delivery | null>(null);
  const [mailTo, setMailTo] = useState("");
  const [link, setLink] = useState<{ code: string; bot_name: string | null } | null>(null);
  const [deliveryMsg, setDeliveryMsg] = useState("");

  const loadKeys = useCallback(
    () =>
      api<Passkey[]>("/api/auth/passkeys")
        .then(setKeys)
        .catch(() => undefined),
    [],
  );
  const loadDelivery = useCallback(
    () =>
      api<Delivery>("/api/delivery")
        .then((d) => {
          setDelivery(d);
          setMailTo(d.email_to ?? "");
        })
        .catch(() => undefined),
    [],
  );
  useEffect(() => {
    if (user) {
      void loadKeys();
      void loadDelivery();
    }
  }, [user, loadKeys, loadDelivery]);

  // While a code waits, ask every 3 seconds whether the chat has sent it (up to its 15 minutes).
  useEffect(() => {
    if (!link) return;
    const timer = setInterval(() => {
      post<{ linked: boolean }>("/api/delivery/telegram/check")
        .then((r) => {
          if (r.linked) {
            setLink(null);
            setDeliveryMsg("Telegram is linked.");
            void loadDelivery();
          }
        })
        .catch(() => undefined);
    }, 3000);
    return () => clearInterval(timer);
  }, [link, loadDelivery]);

  if (!user) return null;
  const isOn = user.totp_enabled || enabled;

  const start = async () => setSetup(await post("/api/auth/totp/setup"));
  const confirm = async (e: FormEvent) => {
    e.preventDefault();
    try {
      await post<User>("/api/auth/totp/enable", { code });
      setEnabled(true);
    } catch {
      setError("That code didn't match. Check the time on your phone and try again.");
    }
  };

  const addKey = async (e: FormEvent) => {
    e.preventDefault();
    setKeyError("");
    try {
      const { challenge_id, options } = await post<{ challenge_id: string; options: unknown }>(
        "/api/auth/passkey/register/options",
      );
      const credential = await createCredential(options);
      await post("/api/auth/passkey/register", { challenge_id, credential, name: keyName || "My device" });
      setKeyName("");
      await loadKeys();
    } catch (err) {
      setKeyError(err instanceof ApiError ? err.detail : "The passkey was not added.");
    }
  };

  const run = async (work: () => Promise<unknown>, done: string) => {
    setDeliveryMsg("");
    try {
      await work();
      setDeliveryMsg(done);
      await loadDelivery();
    } catch (err) {
      setDeliveryMsg(err instanceof ApiError ? err.detail : "That did not work.");
    }
  };

  return (
    <>
      <Header user={user} />
      <main>
        <h1>Settings</h1>
        <h2>Two-factor sign-in (TOTP)</h2>
        <div className="card">
          {isOn ? (
            <p className="status-success">Two-factor sign-in is on.</p>
          ) : !setup ? (
            <button onClick={start}>Set up an authenticator app</button>
          ) : (
            <form onSubmit={confirm}>
              <p>Add this key to your authenticator app (1Password, Google Authenticator, …), then enter the 6-digit code.</p>
              <p className="mono">{setup.secret}</p>
              <p className="muted mono">{setup.otpauth_uri}</p>
              <label>
                Code
                <input inputMode="numeric" value={code} onChange={(e) => setCode(e.target.value)} required />
              </label>
              {error && <p className="error">{error}</p>}
              <button>Turn on</button>
            </form>
          )}
        </div>
        <h2>Delivery: email and Telegram</h2>
        <div className="card">
          <p className="muted">
            Every alert is always in the app. Here you can also get them by email or in Telegram. Your quiet hours (on the{" "}
            <a href="/notifications">Alerts page</a>) hold Telegram messages and digests until they end.
          </p>
          {delivery && (
            <>
              <p className="muted">{delivery.privacy}</p>
              <h3>Email digest</h3>
              {!delivery.email_available ? (
                <p className="muted">The owner has not set up a mail server (QT_SMTP_HOST), so email is off.</p>
              ) : (
                <form
                  onSubmit={(e) => {
                    e.preventDefault();
                    void run(
                      () => put("/api/delivery/email", { mode: delivery.email_mode, to: mailTo || null }),
                      "Saved.",
                    );
                  }}
                >
                  <label>
                    How often
                    <select
                      value={delivery.email_mode}
                      onChange={(e) => setDelivery({ ...delivery, email_mode: e.target.value as Delivery["email_mode"] })}
                    >
                      <option value="off">Never</option>
                      <option value="weekly">Weekly, Monday morning</option>
                      <option value="daily">Daily, in the morning</option>
                    </select>
                  </label>
                  <label>
                    Send to (leave empty for {delivery.account_email})
                    <input type="email" value={mailTo} onChange={(e) => setMailTo(e.target.value)} />
                  </label>
                  <button type="submit">Save</button>{" "}
                  <button type="button" className="link" onClick={() => run(() => post("/api/delivery/test", { channel: "email" }), "Test mail sent.")}>
                    Send a test mail
                  </button>
                </form>
              )}

              <h3>Telegram</h3>
              {!delivery.telegram_available ? (
                <p className="muted">The owner has not set up a Telegram bot (QT_TELEGRAM_BOT_TOKEN), so Telegram is off.</p>
              ) : delivery.telegram_linked ? (
                <p>
                  <span className="status-success">Linked.</span>{" "}
                  <button className="link" onClick={() => run(() => post("/api/delivery/test", { channel: "telegram" }), "Test message sent.")}>
                    Send a test message
                  </button>{" "}
                  <button className="link" onClick={() => run(() => del("/api/delivery/telegram"), "Unlinked.")}>
                    Unlink
                  </button>
                </p>
              ) : link ? (
                <p>
                  Open Telegram, find {link.bot_name ? <strong>{link.bot_name}</strong> : "the bot"} and send it this code
                  within 15 minutes:
                  <br />
                  <strong className="mono">{link.code}</strong>
                  <br />
                  <span className="muted">This page checks every few seconds.</span>
                </p>
              ) : (
                <button
                  onClick={() =>
                    post<{ code: string; bot_name: string | null }>("/api/delivery/telegram/link")
                      .then(setLink)
                      .catch((err) => setDeliveryMsg(err instanceof ApiError ? err.detail : "That did not work."))
                  }
                >
                  Link my Telegram
                </button>
              )}
              {deliveryMsg && <p className="notice">{deliveryMsg}</p>}
            </>
          )}
        </div>

        <h2>Passkeys</h2>
        <div className="card">
          {!passkeysSupported() ? (
            <p className="muted">This browser cannot make passkeys. Use the password and the authenticator app.</p>
          ) : (
            <>
              <p className="muted">
                A passkey signs you in with your fingerprint, face or device PIN, and needs no password. Your password
                still works.
              </p>
              {keys.length > 0 && (
                <table>
                  <tbody>
                    {keys.map((k) => (
                      <tr key={k.id}>
                        <td>{k.name}</td>
                        <td className="muted">
                          added {k.created_at.slice(0, 10)}
                          {k.last_used_at ? `, used ${k.last_used_at.slice(0, 10)}` : ", not used yet"}
                        </td>
                        <td>
                          <button
                            className="link"
                            onClick={() =>
                              del(`/api/auth/passkeys/${k.id}`)
                                .then(loadKeys)
                                .catch(() => setKeyError("Could not remove it."))
                            }
                          >
                            Remove
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
              <form onSubmit={addKey}>
                <label>
                  Name of this device
                  <input maxLength={60} value={keyName} onChange={(e) => setKeyName(e.target.value)} placeholder="My phone" />
                </label>
                {keyError && <p className="error">{keyError}</p>}
                <button>Add a passkey</button>
              </form>
            </>
          )}
        </div>
      </main>
    </>
  );
}
