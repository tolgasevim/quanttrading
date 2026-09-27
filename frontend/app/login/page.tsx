"use client";

import { useRouter } from "next/navigation";
import { type FormEvent, useState } from "react";
import { ApiError, post } from "@/lib/api";

export default function Login() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [needsCode, setNeedsCode] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await post("/api/auth/login", { email, password, totp_code: needsCode ? code : undefined });
      router.replace("/");
    } catch (err) {
      if (err instanceof ApiError && err.detail === "totp_required") setNeedsCode(true);
      else if (err instanceof ApiError && err.status === 429) setError("Too many attempts. Try again later.");
      else setError("Email, password or code is wrong.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <main>
      <div className="card narrow">
        <h1>Sign in</h1>
        <form onSubmit={submit}>
          <label>
            Email
            <input type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} required />
          </label>
          <label>
            Password
            <input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required />
          </label>
          {needsCode && (
            <label>
              Authenticator code
              <input inputMode="numeric" autoComplete="one-time-code" value={code} onChange={(e) => setCode(e.target.value)} autoFocus required />
            </label>
          )}
          {error && <p className="error">{error}</p>}
          <button disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
        </form>
      </div>
    </main>
  );
}
