"use client";

// The live view of one analysis: a running clock, a progress bar that keeps moving, the seven steps with the
// current one animated, counts that tick up, and the agents' activity as it happens.

import { useEffect, useRef, useState } from "react";
import { clock, duration, stopwatch } from "@/lib/format";
import type { FeedEvent, Phase } from "@/lib/types";

/** The current time in seconds, on the server's clock, updated every second while `live`. */
export function useServerNow(serverNow: number | undefined, live: boolean): number {
  const offset = useRef(0);
  useEffect(() => {
    if (serverNow) offset.current = serverNow - Date.now() / 1000;
  }, [serverNow]);
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    if (!live) return;
    const t = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(t);
  }, [live]);
  return now + offset.current;
}

const reducedMotion = () =>
  typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/** A number that counts up to its new value instead of jumping. */
export function Count({ value }: { value: number }) {
  const [shown, setShown] = useState(value);
  const from = useRef(value);
  useEffect(() => {
    const start = from.current;
    if (start === value || reducedMotion()) {
      from.current = value;
      setShown(value);
      return;
    }
    let frame = 0;
    const t0 = performance.now();
    const step = (t: number) => {
      const k = Math.min(1, (t - t0) / 900);
      const eased = 1 - Math.pow(1 - k, 3);
      const v = Math.round(start + (value - start) * eased);
      setShown(v);
      from.current = v;
      if (k < 1) frame = requestAnimationFrame(step);
    };
    frame = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frame);
  }, [value]);
  return <>{shown.toLocaleString()}</>;
}

/** Share of the work done, from each step's typical length. The current step creeps forward but never claims to be
 *  finished, so the bar keeps moving without promising a time. */
export function percentDone(phases: Phase[], now: number): number {
  const total = phases.reduce((s, p) => s + p.expected_s, 0) || 1;
  let done = 0;
  for (const p of phases) {
    if (p.state === "done") done += p.expected_s;
    else if (p.state === "active" && p.started) {
      const t = Math.max(0, now - p.started);
      done += p.expected_s * Math.min(0.92, 1 - Math.exp(-t / p.expected_s));
    }
  }
  return Math.min(99, (done / total) * 100);
}

function useMonotonic(value: number, reset: unknown): number {
  const best = useRef(0);
  const key = useRef(reset);
  if (key.current !== reset) {
    key.current = reset;
    best.current = 0;
  }
  best.current = Math.max(best.current, value);
  return best.current;
}

export function Meter({ elapsed, label, slow, phases, now, counts, waiting, runKey }: {
  elapsed: number; label: string; slow: boolean; phases: Phase[]; now: number; waiting: boolean; runKey: string;
  counts: { pages: number; facts: number; ai_calls: number } | undefined;
}) {
  const pct = useMonotonic(waiting ? 0 : percentDone(phases, now), runKey);
  return (
    <div className="meter">
      <div className="clock">
        <span className="clock-n" aria-hidden="true">{stopwatch(elapsed)}</span>
        <span className="clock-l">{label}</span>
      </div>
      <div className="meter-main">
        <div className={waiting ? "bar waiting" : "bar"} role="progressbar" aria-label="Progress"
          aria-valuemin={0} aria-valuemax={100} aria-valuenow={waiting ? undefined : Math.round(pct)}>
          <span style={waiting ? undefined : { width: `${Math.max(2, pct)}%` }} />
        </div>
        {waiting ? (
          <p className="note queue-note">Analyses run one at a time to stay within the free AI limits.</p>
        ) : (
          <div className="meter-foot">
            <dl className="counts">
              <div><dt>Pages read</dt><dd><Count value={counts?.pages ?? 0} /></dd></div>
              <div><dt>Facts verified</dt><dd><Count value={counts?.facts ?? 0} /></dd></div>
              <div><dt>AI calls</dt><dd><Count value={counts?.ai_calls ?? 0} /></dd></div>
            </dl>
            <span className="pct">{Math.floor(pct)}%</span>
          </div>
        )}
        {slow && (
          <p className="note slow">Taking longer than usual: the free AI models are busy. It will keep going.</p>
        )}
      </div>
    </div>
  );
}

function Mark({ state }: { state: Phase["state"] }) {
  if (state === "done") {
    return (
      <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 8.5 6.5 11.5 12.5 4.5" fill="none"
        stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" /></svg>
    );
  }
  if (state === "failed") {
    return (
      <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4.5 4.5l7 7m0-7-7 7" fill="none" stroke="currentColor"
        strokeWidth="2" strokeLinecap="round" /></svg>
    );
  }
  return null;
}

const STATE_TEXT: Record<Phase["state"], string> = {
  done: "done", active: "in progress", pending: "not started", failed: "stopped here",
};

export function PhaseList({ phases, now, live }: { phases: Phase[]; now: number; live: boolean }) {
  return (
    <ol className="phases">
      {phases.map((p, i) => {
        const took = p.started ? (p.ended ?? (live ? now : p.started)) - p.started : null;
        return (
          <li key={p.title} className={`phase ${p.state}`}>
            <span className="dot"><Mark state={p.state} /><span className="n">{i + 1}</span></span>
            <div className="phase-body">
              <h3>{p.title}<span className="sr-only">: {STATE_TEXT[p.state]}</span></h3>
              {p.state === "active" && p.step && <p className="step">{p.step}</p>}
              {p.state !== "active" && p.result && <p className="result">{p.result}</p>}
            </div>
            <span className="dur">
              {took !== null && p.state !== "pending" ? (p.state === "active" ? stopwatch(took) : duration(took)) : ""}
            </span>
          </li>
        );
      })}
    </ol>
  );
}

export function Feed({ events }: { events: FeedEvent[] }) {
  const [tech, setTech] = useState(false);
  const [all, setAll] = useState(false);
  // The first batch appears as is; entries that arrive with a later poll slide in. Worked out once per poll (not
  // per render: the clock re-renders every second and would cut the animation short).
  const memo = useRef<{ events: FeedEvent[] | null; fresh: Set<number> }>({ events: null, fresh: new Set() });
  if (memo.current.events !== events) {
    const before = memo.current.events ? new Set(memo.current.events.map((e) => e.id)) : null;
    memo.current = { events, fresh: new Set(before ? events.filter((e) => !before.has(e.id)).map((e) => e.id) : []) };
  }
  const fresh = (id: number) => memo.current.fresh.has(id);
  const visible = events.filter((e) => tech || !e.tech);
  const shown = visible.slice(all ? -60 : -12).reverse();
  const hidden = visible.length - shown.length;

  return (
    <section className="feed" aria-labelledby="feed-title">
      <div className="feed-head">
        <h2 id="feed-title">Live activity</h2>
        <label className="toggle">
          <input type="checkbox" checked={tech} onChange={(e) => setTech(e.target.checked)} />
          Show AI model calls
        </label>
      </div>
      {shown.length === 0 ? <p className="muted small">Waiting for the first step…</p> : (
        <ol className="feed-list">
          {shown.map((e) => (
            <li key={e.id} className={`${fresh(e.id) ? "fresh " : ""}${e.tech ? "tech " : ""}k-${e.kind}`}>
              <time dateTime={new Date(e.ts * 1000).toISOString()}>{clock(e.ts)}</time>
              <span className="k" aria-hidden="true" />
              <span className="msg">{e.agent && <strong>{e.agent}</strong>}{e.message}</span>
            </li>
          ))}
        </ol>
      )}
      {(hidden > 0 || all) && (
        <button type="button" className="link-btn more-feed" onClick={() => setAll(!all)}>
          {all ? "Show only the latest" : `Show earlier activity (${hidden} more)`}
        </button>
      )}
    </section>
  );
}
