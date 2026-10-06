"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { api, type Notifications, post, type User } from "@/lib/api";

export function Header({ user }: { user: User }) {
  const router = useRouter();
  const [unread, setUnread] = useState(0);
  useEffect(() => {
    // The page that changes alerts sends this event, so the count stays right without a reload.
    const load = () =>
      api<Notifications>("/api/notifications?limit=1")
        .then((body) => setUnread(body.unread))
        .catch(() => undefined);
    load();
    window.addEventListener("qt-notifications", load);
    return () => window.removeEventListener("qt-notifications", load);
  }, []);
  const logout = async () => {
    await post("/api/auth/logout");
    router.replace("/login");
  };
  return (
    <header className="top">
      <strong>QuantTrading</strong>
      <nav>
        <Link href="/">Dashboard</Link>
        <Link href="/import">Import</Link>
        <Link href="/holdings">Holdings</Link>
        <Link href="/tax">Tax</Link>
        <Link href="/costs">Costs</Link>
        <Link href="/prices">Prices</Link>
        <Link href="/notifications">Alerts{unread > 0 ? ` (${unread})` : ""}</Link>
        {user.role === "admin" && <Link href="/admin/invites">Invites</Link>}
        <Link href="/settings">Settings</Link>
        <button className="link" onClick={logout}>
          Sign out
        </button>
      </nav>
    </header>
  );
}
