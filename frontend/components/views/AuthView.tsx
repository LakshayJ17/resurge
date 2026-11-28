"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { post, safeNext } from "@/lib/api";
import { PuzzleCache } from "@/lib/pow";
import type { Session } from "@/lib/types";
import { Problem } from "../RequireUser";
import { useSession } from "../SessionProvider";
import { Turnstile } from "../Turnstile";

export function AuthView({ mode }: { mode: "signup" | "login" }) {
  const { session, setSession } = useSession();
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [website, setWebsite] = useState(""); // honeypot
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const onToken = useCallback((t: string) => setToken(t), []);
  const config = session?.config;
  const signup = mode === "signup";
  const botCheck = !!config?.captcha;
  const puzzles = useRef(new PuzzleCache());

  useEffect(() => {
    if (session?.user) router.replace(safeNext(new URLSearchParams(window.location.search).get("next")));
  }, [session, router]);

  // solve the bot-check puzzle while the person types, so sending the form does not wait for it
  useEffect(() => {
    if (botCheck) puzzles.current.prepare();
  }, [botCheck]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const captcha = botCheck ? await puzzles.current.take() : "";
      const body = signup ? { email, password, website, turnstile: token, captcha } : { email, password, captcha };
      const s = await post<Session>(signup ? "/api/signup" : "/api/login", body);
      setSession(s);
      router.replace(safeNext(new URLSearchParams(window.location.search).get("next")));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong.");
      setBusy(false);
      if (botCheck) puzzles.current.prepare(); // each puzzle works once: get the next one ready
    }
  }

  const closed = signup && config && !config.signup_open;
  return (
    <div className="wrap page">
      <div className="auth">
        <header className="page-head">
          <h1>{signup ? "Create your account" : "Log in"}</h1>
          {signup && config && (
            <p className="meta">Free. Each account can run {config.runs_per_user_day} analyses a day.</p>
          )}
        </header>
        <Problem message={error} />
        {closed ? (
          <p className="muted">Sign-up is closed. Ask the owner of this site for an account.</p>
        ) : (
          <form onSubmit={submit} autoComplete="on">
            <div className="field">
              <label htmlFor="email">Email</label>
              <input id="email" type="email" required maxLength={254} autoComplete="email" value={email}
                onChange={(e) => setEmail(e.target.value)} />
            </div>
            <div className="field">
              <label htmlFor="password">Password</label>
              {signup && <p className="hint">At least 10 characters.</p>}
              <input id="password" type="password" required minLength={signup ? 10 : undefined} maxLength={200}
                autoComplete={signup ? "new-password" : "current-password"} value={password}
                onChange={(e) => setPassword(e.target.value)} />
            </div>
            {signup && (
              <div className="hp" aria-hidden="true">
                <label htmlFor="website">Website</label>
                <input id="website" type="text" tabIndex={-1} autoComplete="off" value={website}
                  onChange={(e) => setWebsite(e.target.value)} />
              </div>
            )}
            {signup && config?.turnstile_site_key && <Turnstile siteKey={config.turnstile_site_key} onToken={onToken} />}
            <button className="btn" type="submit" disabled={busy}>
              {busy ? "Please wait..." : signup ? "Create account" : "Log in"}
            </button>
          </form>
        )}
        <p className="switch">
          {signup
            ? <>Already have an account? <Link href="/login">Log in</Link></>
            : <>New here? <Link href="/signup">Create a free account</Link></>}
        </p>
        {signup && (
          <p className="note">
            Research uses public web pages only. Don&apos;t enter confidential information: free AI providers may keep
            what they are sent. See <Link href="/privacy">privacy</Link>.
          </p>
        )}
      </div>
    </div>
  );
}
