"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { type FormEvent, Suspense, useEffect, useState } from "react";
import { api, ApiError, post } from "@/lib/api";

function RegisterForm() {
  const router = useRouter();
  const token = useSearchParams().get("token") ?? "";
  const [email, setEmail] = useState<string | null>(null);
  const [invalid, setInvalid] = useState(false);
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    if (!token) return setInvalid(true);
    api<{ email: string }>(`/api/auth/invite/${encodeURIComponent(token)}`)
      .then((r) => setEmail(r.email))
      .catch(() => setInvalid(true));
  }, [token]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError("");
    try {
      await post("/api/auth/register", { token, display_name: name, password });
      router.replace("/");
    } catch (err) {
      setError(err instanceof ApiError && err.status === 422 ? "Password must be at least 12 characters." : "Could not create the account.");
    }
  };

  if (invalid) return <p className="error">This invite link is invalid or has expired.</p>;
  if (!email) return null;
  return (
    <form onSubmit={submit}>
      <p className="muted">Creating an account for {email}</p>
      <label>
        Your name
        <input value={name} onChange={(e) => setName(e.target.value)} required maxLength={100} />
      </label>
      <label>
        Password (12+ characters)
        <input type="password" autoComplete="new-password" minLength={12} value={password} onChange={(e) => setPassword(e.target.value)} required />
      </label>
      {error && <p className="error">{error}</p>}
      <button>Create account</button>
    </form>
  );
}

export default function Register() {
  return (
    <main>
      <div className="card narrow">
        <h1>Join QuantTrading</h1>
        <Suspense>
          <RegisterForm />
        </Suspense>
      </div>
    </main>
  );
}
