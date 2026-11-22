"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { post } from "@/lib/api";
import type { Session, User } from "@/lib/types";
import { Problem, RequireUser } from "../RequireUser";
import { useSession } from "../SessionProvider";

export function AccountView() {
  const [deleted, setDeleted] = useState("");
  if (deleted) {
    return (
      <div className="wrap page">
        <header className="page-head"><h1>Account deleted</h1><p className="lede">{deleted}</p></header>
        <p><Link href="/">Back to Resurge</Link></p>
      </div>
    );
  }
  return <RequireUser>{(user) => <Account user={user} onDeleted={setDeleted} />}</RequireUser>;
}

function Account({ user, onDeleted }: { user: User; onDeleted: (message: string) => void }) {
  const { session, setSession } = useSession();
  const router = useRouter();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [delPassword, setDelPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  const loggedOut = () => session && setSession({ ...session, user: null } as Session);

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await action();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Something went wrong.");
    } finally {
      setBusy(false);
    }
  }

  const changePassword = (e: React.FormEvent) => {
    e.preventDefault();
    void run(async () => {
      const r = await post<{ message: string }>("/api/account/password", { current, new: next });
      setNotice(r.message);
      setCurrent("");
      setNext("");
    });
  };

  const everywhere = () => run(async () => {
    await post("/api/account/logout-everywhere");
    loggedOut();
    router.push("/login");
  });

  const remove = (e: React.FormEvent) => {
    e.preventDefault();
    void run(async () => {
      const r = await post<{ message: string }>("/api/account/delete", { password: delPassword, confirm });
      onDeleted(r.message);
      loggedOut();
    });
  };

  return (
    <div className="wrap page">
      <header className="page-head">
        <h1>Your account</h1>
        <p className="meta">{user.email}</p>
      </header>
      <Problem message={error} />
      {notice && <p className="notice" role="status">{notice}</p>}

      <section aria-labelledby="pw">
        <h2 id="pw">Change password</h2>
        <form onSubmit={changePassword} className="measure">
          <div className="field">
            <label htmlFor="cur">Current password</label>
            <input id="cur" type="password" required maxLength={200} autoComplete="current-password" value={current}
              onChange={(e) => setCurrent(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="new">New password</label>
            <p className="hint">At least 10 characters. Changing it logs you out on every other device.</p>
            <input id="new" type="password" required minLength={10} maxLength={200} autoComplete="new-password"
              value={next} onChange={(e) => setNext(e.target.value)} />
          </div>
          <button className="btn" type="submit" disabled={busy}>Change password</button>
        </form>
      </section>

      <section aria-labelledby="sessions">
        <h2 id="sessions">Sessions</h2>
        <p className="measure">If you logged in on a computer you no longer use, end every session, including this one.</p>
        <button className="btn secondary" type="button" onClick={everywhere} disabled={busy}>Log out everywhere</button>
      </section>

      <section aria-labelledby="del">
        <h2 id="del">Delete account</h2>
        <p className="measure">Deletes your account, your analyses and their reports. This cannot be undone.</p>
        <form onSubmit={remove} className="measure">
          <div className="field">
            <label htmlFor="delpw">Password</label>
            <input id="delpw" type="password" required maxLength={200} autoComplete="current-password"
              value={delPassword} onChange={(e) => setDelPassword(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="confirm">Type DELETE to confirm</label>
            <input id="confirm" type="text" required maxLength={10} autoComplete="off" value={confirm}
              onChange={(e) => setConfirm(e.target.value)} />
          </div>
          <button className="btn danger" type="submit" disabled={busy}>Delete my account</button>
        </form>
      </section>
    </div>
  );
}
