"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { post } from "@/lib/api";
import type { Session } from "@/lib/types";
import { Logo } from "./Logo";
import { useSession } from "./SessionProvider";

export function SiteHeader() {
  const { session, setSession } = useSession();
  const router = useRouter();
  const path = usePathname();
  const user = session?.user;
  const here = (href: string) => (path === href ? "page" : undefined);

  async function logout() {
    try {
      await post("/api/logout");
    } finally {
      if (session) setSession({ ...session, user: null } as Session);
      router.push("/");
    }
  }

  return (
    <header className="site-head">
      <div className="wrap">
        <Link className="brand" href="/">
          <Logo />
          Resurge
        </Link>
        <nav className="nav" aria-label="Main">
          {user ? (
            <>
              <Link href="/dashboard" aria-current={here("/dashboard")}>New analysis</Link>
              {user.is_admin && <Link href="/admin" aria-current={here("/admin")} className="hide-sm">Admin</Link>}
              <Link href="/account" aria-current={here("/account")} className="hide-sm">Account</Link>
              <button className="link-btn" type="button" onClick={logout}>Log out</button>
            </>
          ) : (
            <>
              <Link href="/#how" className="hide-sm">How it works</Link>
              <Link href="/login" aria-current={here("/login")}>Log in</Link>
              <Link href="/signup" className="btn small">Sign up</Link>
            </>
          )}
        </nav>
      </div>
    </header>
  );
}
