"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { api, ApiError, type User } from "@/lib/api";

/** Loads the signed-in user, redirecting to /login when there is no session. */
export function useUser(): User | null {
  const router = useRouter();
  const [user, setUser] = useState<User | null>(null);
  useEffect(() => {
    api<User>("/api/auth/me")
      .then(setUser)
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) router.replace("/login");
      });
  }, [router]);
  return user;
}
