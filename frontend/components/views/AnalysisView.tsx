"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { DEPTH_NAME, duration, ordinal, when } from "@/lib/format";
import type { AnalysisDetail } from "@/lib/types";
import { Problem, RequireUser } from "../RequireUser";
import { Count, Feed, Meter, PhaseList, useServerNow } from "../RunProgress";

export function AnalysisView() {
  return <RequireUser>{() => <Analysis />}</RequireUser>;
}

function Analysis() {
  const [id, setId] = useState<string | null>(null);
  const [data, setData] = useState<AnalysisDetail | null>(null);
  const [error, setError] = useState("");
  const [gone, setGone] = useState(false);

  useEffect(() => {
    setId(new URLSearchParams(window.location.search).get("id") ?? "");
  }, []);

  useEffect(() => {
    if (!id) return;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let stopped = false;
    const tick = async () => {
      try {
        const d = await api<AnalysisDetail>(`/api/analyses/${encodeURIComponent(id)}`);
        if (stopped) return;
        setData(d);
        setError("");
        if (d.status === "queued" || d.status === "running") timer = setTimeout(tick, 2500);
      } catch (e) {
        if (stopped) return;
        if (e instanceof ApiError && e.status === 404) setGone(true);
        else {
          setError(e instanceof Error ? e.message : "Couldn't load the analysis.");
          timer = setTimeout(tick, 6000);
        }
      }
    };
    void tick();
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [id]);

  useEffect(() => {
    if (data) document.title = `${data.target} · Resurge`;
  }, [data]);

  const live = data?.status === "queued" || data?.status === "running";
  const now = useServerNow(data?.now, live);

  if (id === "" || gone) {
    return (
      <div className="wrap page">
        <header className="page-head"><h1>Analysis not found</h1></header>
        <p>It doesn&apos;t exist or belongs to another account. <Link href="/dashboard">Back to your analyses</Link></p>
      </div>
    );
  }
  if (!data) return <div className="wrap page"><Problem message={error} />{!error && <p className="muted">Loading…</p>}</div>;

  const depth = data.depth ? DEPTH_NAME[data.depth] : "";
  const phases = data.progress?.phases ?? [];
  const counts = data.progress?.counts;
  const took = data.started_at && data.finished_at ? data.finished_at - data.started_at : null;
  const deck = data.files.find((f) => f.href.endsWith(".pptx"));

  if (live) {
    const waiting = data.status === "queued";
    const since = waiting ? data.created_at : (data.started_at ?? data.created_at);
    const elapsed = now - since;
    const slow = !waiting && elapsed > (data.slow_after_s ?? 1500);
    const active = phases.findIndex((p) => p.state === "active");
    return (
      <div className="wrap page run">
        <header className="page-head">
          <p className="eyebrow">Competitor analysis{depth && ` · ${depth}`}</p>
          <h1>{data.target}</h1>
          <p className="live" aria-live="polite">
            <span className="live-dot" aria-hidden="true" />
            {waiting
              ? (data.position > 1 ? `${ordinal(data.position)} in line` : "Next in line")
              : active >= 0 ? `Step ${active + 1} of ${phases.length}: ${phases[active].title}` : data.status_text}
          </p>
        </header>
        <Problem message={error} />

        <Meter elapsed={elapsed} waiting={waiting} slow={slow} phases={phases} now={now} counts={counts}
          runKey={data.run_id}
          label={waiting ? "waiting · it starts when the analysis ahead finishes"
            : `elapsed · ${data.typical ?? "usually 10–20 minutes"}`} />

        <div className="run-grid">
          <section aria-labelledby="steps-title" className="run-steps">
            <h2 id="steps-title" className="sr-only">Steps</h2>
            {phases.length > 0 && <PhaseList phases={phases} now={now} live />}
            <p className="note">
              You can close this page. The research keeps running and the report appears on{" "}
              <Link href="/dashboard">your dashboard</Link> when it is ready.
            </p>
          </section>
          <Feed events={data.events} />
        </div>
      </div>
    );
  }

  return (
    <div className="wrap page run">
      <header className="page-head">
        <p className="eyebrow">Competitor analysis{depth && ` · ${depth}`}</p>
        <h1>{data.target}</h1>
        {data.status === "done" && data.finished_at && (
          <p className="meta">
            Finished {when(data.finished_at)}{took !== null && ` · took ${duration(took)}`}
          </p>
        )}
      </header>
      <Problem message={error} />

      {data.status === "done" && data.summary && (
        <section aria-label="Result" className="result-block">
          {data.summary.headline && <p className="verdict">{data.summary.headline}</p>}
          {data.summary.summary.split(/\n\s*\n/).filter(Boolean).map((para, i) => (
            <p key={i} className="measure summary-p">{para}</p>
          ))}
          <dl className="facts-row">
            <div><dt>Compared with</dt><dd>{data.summary.competitors.join(", ") || "—"}</dd></div>
            <div><dt>Verified facts</dt><dd><Count value={data.summary.facts} /></dd></div>
            <div><dt>Quality score</dt><dd>{data.summary.quality.toFixed(0)} / 100</dd></div>
          </dl>
          <div className="btn-row">
            {data.report_url && <a className="btn" href={data.report_url}>Open the report</a>}
            {data.pdf_url && <a className="btn secondary" href={data.pdf_url} download>Download the PDF</a>}
            {deck && <a className="btn secondary" href={deck.href} download>Download the slides</a>}
            {data.ask_url && <a className="btn secondary" href={data.ask_url}>Ask a question</a>}
          </div>
        </section>
      )}

      {data.status === "done" && data.missing && (
        <section>
          <Problem message="The report files for this analysis are no longer on this server." />
          <p><Link href="/dashboard">Start a new analysis</Link></p>
        </section>
      )}

      {data.status === "failed" && (
        <section>
          <Problem message={data.error || "The research could not finish."} />
          <div className="btn-row">
            <Link className="btn" href={`/dashboard?company=${encodeURIComponent(data.target)}`}>Try again</Link>
            <Link className="btn secondary" href="/dashboard">Back to your analyses</Link>
          </div>
        </section>
      )}

      {phases.some((p) => p.started) && (
        <details className="more how-ran" open={data.status === "failed"}>
          <summary>How this analysis ran</summary>
          <PhaseList phases={phases} now={now} live={false} />
          {counts && (
            <p className="note">
              {counts.pages} pages read · {counts.facts} facts verified · {counts.ai_calls} AI calls
            </p>
          )}
        </details>
      )}
    </div>
  );
}

