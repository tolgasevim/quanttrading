"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { api, ApiError, type Disclaimer, post } from "@/lib/api";

// Not behind `useUser`, which sends everyone without an acceptance here.
export default function DisclaimerPage() {
  const router = useRouter();
  const [info, setInfo] = useState<Disclaimer | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<Disclaimer>("/api/auth/disclaimer")
      .then((d) => {
        if (d.accepted) router.replace("/");
        else setInfo(d);
      })
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) router.replace("/login");
        else setError("Could not load the disclaimer.");
      });
  }, [router]);

  const accept = async () => {
    setBusy(true);
    setError("");
    try {
      await post<Disclaimer>("/api/auth/disclaimer", { version: info?.version });
      router.replace("/");
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : "Could not save.");
      setBusy(false);
    }
  };

  return (
    <main>
      <h1>Before you start</h1>
      {error && <p className="notice status-failed">{error}</p>}
      {info && (
        <div className="card">
          <p>{info.text}</p>
          <p>
            <button onClick={accept} disabled={busy}>
              I understand and accept
            </button>
          </p>
        </div>
      )}
    </main>
  );
}
