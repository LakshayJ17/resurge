"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api, ApiError, post } from "@/lib/api";
import { when } from "@/lib/format";
import type { AdminData } from "@/lib/types";
import { Problem, RequireUser } from "../RequireUser";

export function AdminView() {
  return (
    <RequireUser>
      {(user) => user.is_admin ? <Admin /> : (
        <div className="wrap page"><header className="page-head"><h1>Not found</h1></header>
          <p><Link href="/dashboard">Back to your analyses</Link></p></div>
      )}
    </RequireUser>
  );
}

function Admin() {
  const [data, setData] = useState<AdminData | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      setData(await api<AdminData>("/api/admin"));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Couldn't load usage.");
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);

  async function toggle(id: number, disabled: boolean) {
    try {
      await post(`/api/admin/users/${id}/${disabled ? "enable" : "disable"}`);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Something went wrong.");
    }
  }

  if (!data) return <div className="wrap page"><Problem message={error} />{!error && <p className="muted">Loading…</p>}</div>;
  const { stats } = data;
  return (
    <div className="wrap page">
      <header className="page-head">
        <h1>Usage</h1>
        <p className="meta">
          {stats.users} accounts; {stats.today} of {stats.cap} analyses used today; {stats.queued} waiting in line.
        </p>
      </header>
      <Problem message={error} />

      <section aria-labelledby="recent">
        <h2 id="recent">Recent analyses</h2>
        <div className="table-wrap">
          <table className="t">
            <thead><tr><th>Company</th><th>Account</th><th>Started</th><th>Status</th><th><span className="sr-only">Link</span></th></tr></thead>
            <tbody>
              {data.jobs.map((j) => (
                <tr key={j.run_id}>
                  <th scope="row">{j.target}</th>
                  <td className="small">{j.email}</td>
                  <td className="small">{when(j.created_at)}</td>
                  <td className="small">{j.status_text}{j.error && <><br /><span className="muted">{j.error}</span></>}</td>
                  <td className="small"><Link href={`/analysis?id=${encodeURIComponent(j.run_id)}`}>Open</Link></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section aria-labelledby="accounts">
        <h2 id="accounts">Accounts</h2>
        <div className="table-wrap">
          <table className="t">
            <thead><tr><th>Email</th><th>Joined</th><th className="num">Failed log-ins</th><th>State</th><th><span className="sr-only">Action</span></th></tr></thead>
            <tbody>
              {data.users.map((u) => (
                <tr key={u.id}>
                  <th scope="row">{u.email}{u.is_admin && <span className="tag"> (admin)</span>}</th>
                  <td className="small">{when(u.created_at)}</td>
                  <td className="small num">{u.failed_logins}</td>
                  <td className="small">{u.disabled ? "Disabled" : u.locked ? "Locked for now" : "Active"}</td>
                  <td className="small">
                    {!u.you && (
                      <button className="link-btn" type="button" onClick={() => toggle(u.id, u.disabled)}>
                        {u.disabled ? "Enable" : "Disable"}
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section aria-labelledby="events">
        <h2 id="events">Security events</h2>
        <div className="table-wrap">
          <table className="t">
            <thead><tr><th>Time</th><th>Event</th><th>Account</th><th>IP</th><th>Detail</th></tr></thead>
            <tbody>
              {data.events.map((e, i) => (
                <tr key={`${e.ts}-${i}`}>
                  <td className="small">{when(e.ts)}</td>
                  <td className="small">{e.event.replaceAll("_", " ")}</td>
                  <td className="small">{e.email}</td>
                  <td className="small">{e.ip}</td>
                  <td className="small muted">{e.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
