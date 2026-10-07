"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { api, ApiError, type User } from "@/lib/api";

/**
 * Loads the signed-in user, redirecting to /login when there is no session, and to /disclaimer
 * until the user has accepted the current disclaimer (FR-4). Returns null while it redirects, so
 * no page shows data before the disclaimer is accepted.
 */
export function useUser(): User | null {
  const router = useRouter();
  const [user, setUser] = useState<User | null>(null);
  useEffect(() => {
    api<User>("/api/auth/me")
      .then((u) => {
        if (u.disclaimer_accepted) setUser(u);
        else router.replace("/disclaimer");
      })
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) router.replace("/login");
      });
  }, [router]);
  return user;
}
