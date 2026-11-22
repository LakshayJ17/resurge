export type Depth = "quick" | "standard" | "deep";

export type User = {
  email: string;
  is_admin: boolean;
  remaining: number | null; // null = no daily limit (admin)
  paused: boolean;
};

export type SiteConfig = {
  signup_open: boolean;
  turnstile_site_key: string;
  depths: Depth[];
  runs_per_user_day: number;
  chat_per_user_day: number;
  private_mode: boolean;
};

export type Session = { user: User | null; config: SiteConfig };

export type JobStatus = "queued" | "running" | "done" | "failed";

export type Job = {
  run_id: string;
  target: string;
  status: JobStatus;
  status_text: string;
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
  error: string | null;
  report_url: string | null;
  step?: number; // running jobs: which of `steps` it is on
  steps?: number;
};

export type PhaseState = "pending" | "active" | "done" | "failed";

export type Phase = {
  title: string;
  state: PhaseState;
  started: number | null;
  ended: number | null;
  step: string; // what it is doing now, in words
  result: string; // what it produced, once done
  expected_s: number;
};

export type FeedEvent = { id: number; ts: number; agent: string; kind: string; tech: boolean; message: string };

export type AnalysisDetail = Job & {
  position: number;
  now?: number; // server clock, to correct the viewer's
  depth?: Depth;
  typical?: string;
  slow_after_s?: number;
  progress?: { phases: Phase[]; counts: { pages: number; facts: number; ai_calls: number } };
  events: FeedEvent[];
  summary: { headline: string; summary: string; competitors: string[]; facts: number; quality: number } | null;
  files: { label: string; href: string }[];
  missing: boolean;
  ask_url?: string | null;
  pdf_url?: string | null;
};

export type AdminData = {
  stats: { users: number; today: number; queued: number; cap: number };
  jobs: (Job & { email: string })[];
  users: { id: number; email: string; is_admin: boolean; created_at: number; failed_logins: number;
    disabled: boolean; locked: boolean; you: boolean }[];
  events: { ts: number; event: string; email: string; ip: string; detail: string }[];
};
