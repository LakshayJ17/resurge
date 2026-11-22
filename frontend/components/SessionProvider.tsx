"use client";

import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { Session } from "@/lib/types";

type Ctx = {
  session: Session | null; // null while loading
  error: string;
  refresh: () => Promise<Session | null>;
  setSession: (s: Session) => void;
};

const SessionContext = createContext<Ctx>({
  session: null, error: "", refresh: async () => null, setSession: () => {},
});

export function SessionProvider({ children }: { children: React.ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      const s = await api<Session>("/api/session");
      setSession(s);
      setError("");
      return s;
    } catch (e) {
      setError(e instanceof Error ? e.message : "Can't reach the server.");
      return null;
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return <SessionContext.Provider value={{ session, error, refresh, setSession }}>{children}</SessionContext.Provider>;
}

export const useSession = () => useContext(SessionContext);
