"use client";

import Link from "next/link";
import { useSession } from "./SessionProvider";

export function HeroActions({ secondary = true }: { secondary?: boolean }) {
  const { session } = useSession();
  const user = session?.user;
  return (
    <div className="btn-row">
      {user
        ? <Link className="btn" href="/dashboard">Start a new analysis</Link>
        : <Link className="btn" href="/signup">Start free research</Link>}
      {secondary && <a className="btn secondary" href="#how">How it works</a>}
    </div>
  );
}
