"use client";

import { type FormEvent, useState } from "react";
import { post } from "@/lib/api";
import { Header } from "../../Header";
import { useUser } from "../../useUser";

type Invite = { email: string; token: string; expires_at: string };

export default function Invites() {
  const user = useUser();
  const [email, setEmail] = useState("");
  const [created, setCreated] = useState<Invite | null>(null);
  const [error, setError] = useState("");

  if (!user) return null;
  if (user.role !== "admin") return <p className="error">Admins only.</p>;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError("");
    try {
      setCreated(await post<Invite>("/api/admin/invites", { email }));
      setEmail("");
    } catch {
      setError("Could not create the invite. Does this person already have an account?");
    }
  };
  const link = created ? `${window.location.origin}/register?token=${created.token}` : "";

  return (
    <>
      <Header user={user} />
      <main>
        <h1>Invite a family member</h1>
        <div className="card">
          <form onSubmit={submit}>
            <label>
              Email
              <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
            </label>
            {error && <p className="error">{error}</p>}
            <button>Create invite link</button>
          </form>
        </div>
        {created && (
          <div className="card" style={{ marginTop: 16 }}>
            <p>
              Send this link to {created.email}. It works once and expires{" "}
              {new Date(created.expires_at).toLocaleString("en-GB")}. It won&apos;t be shown again.
            </p>
            <p className="mono">{link}</p>
          </div>
        )}
      </main>
    </>
  );
}
