"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { api, post } from "@/lib/api";
import { DEPTH_TEXT, when } from "@/lib/format";
import type { Job, SiteConfig, User } from "@/lib/types";
import { Problem, RequireUser } from "../RequireUser";

export function DashboardView() {
  return <RequireUser>{(user, session) => <Dashboard user={user} config={session.config} />}</RequireUser>;
}

function Dashboard({ user, config }: { user: User; config: SiteConfig }) {
  const router = useRouter();
  const [jobs, setJobs] = useState<Job[] | null>(null);
  const [remaining, setRemaining] = useState<number | null>(user.remaining);
  const [company, setCompany] = useState("");
  const [competitors, setCompetitors] = useState("");
  const [questions, setQuestions] = useState("");
  const [depth, setDepth] = useState(config.depths[0] ?? "quick");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const d = await api<{ jobs: Job[]; remaining: number | null }>("/api/analyses");
      setJobs(d.jobs);
      setRemaining(d.remaining);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't load your analyses.");
    }
  }, []);

  useEffect(() => {
    void load();
    const again = new URLSearchParams(window.location.search).get("company");
    if (again) setCompany(again.slice(0, 300));
  }, [load]);

  const active = jobs?.some((j) => j.status === "queued" || j.status === "running");
  useEffect(() => {
    if (!active) return;
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [active, load]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const r = await post<{ run_id: string }>("/api/analyses", { company, competitors, questions, depth });
      router.push(`/analysis?id=${encodeURIComponent(r.run_id)}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong.");
      setBusy(false);
    }
  }

  const out = remaining === 0;
  return (
    <div className="wrap page">
      <header className="page-head">
        <h1>Which company should we research?</h1>
        <p className="meta">
          {remaining === null ? "Admin account: no daily limit."
            : `${remaining} of ${config.runs_per_user_day} analyses left today.`}
        </p>
      </header>

      <form onSubmit={submit}>
        <Problem message={error || (user.paused ? "Your account is paused for 24 hours because several requests were blocked by the safety checks." : "")} />
        <div className="field">
          <label htmlFor="company">Company, product, market or website</label>
          <div className="big-input">
            <input id="company" type="text" required maxLength={300} placeholder="For example: Notion or linear.app"
              autoComplete="off" value={company} onChange={(e) => setCompany(e.target.value)} />
            <button className="btn" type="submit" disabled={busy || out}>{busy ? "Starting…" : "Start research"}</button>
          </div>
          {out && <p className="hint" style={{ marginTop: 8 }}>You have used today&apos;s analyses. More tomorrow.</p>}
        </div>
        <details className="more" open={Boolean(competitors || questions)}>
          <summary>More options</summary>
          <div className="field">
            <label htmlFor="competitors">Competitors to include</label>
            <p className="hint">Optional. Separate with commas. Leave empty and Resurge picks the closest ones.</p>
            <input id="competitors" type="text" maxLength={400} autoComplete="off" value={competitors}
              onChange={(e) => setCompetitors(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="questions">Questions you want answered</label>
            <p className="hint">Optional. One per line, up to three.</p>
            <textarea id="questions" maxLength={900} value={questions} onChange={(e) => setQuestions(e.target.value)} />
          </div>
          <fieldset className="field" style={{ border: 0, padding: 0, margin: "0 0 18px" }}>
            <legend className="label" style={{ fontWeight: 600, marginBottom: 6, padding: 0 }}>Depth</legend>
            <div className="choice">
              {config.depths.map((d) => (
                <label key={d}>
                  <input type="radio" name="depth" value={d} checked={depth === d} onChange={() => setDepth(d)} />
                  {DEPTH_TEXT[d] ?? d}
                </label>
              ))}
            </div>
          </fieldset>
        </details>
      </form>

      <section aria-labelledby="yours">
        <h2 id="yours">Your analyses</h2>
        {jobs === null ? <p className="muted">Loading…</p> : jobs.length === 0 ? (
          <p className="muted">Nothing yet. Your reports will be listed here.</p>
        ) : (
          <div className="table-wrap">
            <table className="t">
              <thead><tr><th>Company</th><th>Started</th><th>Status</th><th><span className="sr-only">Link</span></th></tr></thead>
              <tbody>
                {jobs.map((j) => (
                  <tr key={j.run_id}>
                    <th scope="row">{j.target}</th>
                    <td className="small">{when(j.created_at)}</td>
                    <td className="small">
                      {j.status === "running" || j.status === "queued" ? (
                        <span className="status"><span className="pulse" aria-hidden="true" />{j.status_text}</span>
                      ) : <span className={`state-${j.status}`}>{j.status_text}</span>}
                      {j.status === "running" && j.step && j.steps ? (
                        <span className="mini-bar" aria-hidden="true">
                          <span style={{ width: `${Math.round(((j.step - 0.5) / j.steps) * 100)}%` }} />
                        </span>
                      ) : null}
                    </td>
                    <td className="small">
                      <Link href={`/analysis?id=${encodeURIComponent(j.run_id)}`}>
                        {j.status === "done" ? "View analysis" : j.status === "failed" ? "Details" : "View progress"}
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
