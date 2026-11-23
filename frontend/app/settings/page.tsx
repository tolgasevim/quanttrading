"use client";

import { type FormEvent, useCallback, useEffect, useState } from "react";
import { api, ApiError, del, type Passkey, post, type User } from "@/lib/api";
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

  const loadKeys = useCallback(
    () =>
      api<Passkey[]>("/api/auth/passkeys")
        .then(setKeys)
        .catch(() => undefined),
    [],
  );
  useEffect(() => {
    if (user) void loadKeys();
  }, [user, loadKeys]);

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
