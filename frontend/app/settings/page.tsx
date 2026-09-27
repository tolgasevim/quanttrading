"use client";

import { type FormEvent, useState } from "react";
import { post, type User } from "@/lib/api";
import { Header } from "../Header";
import { useUser } from "../useUser";

export default function Settings() {
  const user = useUser();
  const [setup, setSetup] = useState<{ secret: string; otpauth_uri: string } | null>(null);
  const [code, setCode] = useState("");
  const [enabled, setEnabled] = useState(false);
  const [error, setError] = useState("");

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
        <p className="muted">Passkeys arrive in Phase 1.</p>
      </main>
    </>
  );
}
