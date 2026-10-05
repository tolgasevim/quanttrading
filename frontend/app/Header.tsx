"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { post, type User } from "@/lib/api";

export function Header({ user }: { user: User }) {
  const router = useRouter();
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
        {user.role === "admin" && <Link href="/admin/invites">Invites</Link>}
        <Link href="/settings">Settings</Link>
        <button className="link" onClick={logout}>
          Sign out
        </button>
      </nav>
    </header>
  );
}
