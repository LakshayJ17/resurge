"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";
import type { Session, User } from "@/lib/types";
import { useSession } from "./SessionProvider";

/** Renders its children only for a logged-in user; otherwise sends the visitor to log in and come back. */
export function RequireUser({ children }: { children: (user: User, session: Session) => React.ReactNode }) {
  const { session, error } = useSession();
  const router = useRouter();

  useEffect(() => {
    if (session && !session.user) {
      const here = window.location.pathname + window.location.search;
      router.replace("/login?next=" + encodeURIComponent(here));
    }
  }, [session, router]);

  if (error && !session) return <p className="problem" role="alert"><strong>Problem:</strong> {error}</p>;
  if (!session?.user) return <p className="muted">Loading…</p>;
  return <>{children(session.user, session)}</>;
}

export function Problem({ message }: { message: string }) {
  if (!message) return null;
  return <p className="problem" role="alert"><strong>Problem:</strong> {message}</p>;
}
