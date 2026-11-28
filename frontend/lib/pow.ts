// The bot check on the sign-up and log-in forms (server side: src/resurge/web/captcha.py). The server sends a
// signed puzzle: find the number n (0..maxnumber) whose SHA-256 of `salt + n` equals `challenge`. People never see
// it; the browser tries numbers in batches, which takes well under a second on a laptop.

import { api, ApiError } from "./api";

type Puzzle = { algorithm: string; salt: string; challenge: string; maxnumber: number; signature: string };

const BATCH = 500;
const USABLE_MS = 8 * 60 * 1000; // the server accepts a puzzle for 10 minutes

function hexBytes(hex: string): Uint8Array {
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  return out;
}

function same(a: Uint8Array, b: Uint8Array): boolean {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}

/** Fetches and solves a puzzle; returns the JSON the form sends as `captcha` ("" when the check is off). */
export async function solvePuzzle(): Promise<string> {
  const puzzle = await api<Puzzle | { enabled: false }>("/api/challenge");
  if (!("challenge" in puzzle)) return "";
  const subtle = globalThis.crypto?.subtle;
  if (!subtle) throw new ApiError(0, "Open this site with an https:// address to sign in.");
  const target = hexBytes(puzzle.challenge);
  const text = new TextEncoder();
  for (let start = 0; start <= puzzle.maxnumber; start += BATCH) {
    const numbers: number[] = [];
    for (let n = start; n <= Math.min(puzzle.maxnumber, start + BATCH - 1); n++) numbers.push(n);
    const digests = await Promise.all(numbers.map((n) => subtle.digest("SHA-256", text.encode(puzzle.salt + n))));
    const hit = digests.findIndex((d) => same(new Uint8Array(d), target));
    if (hit >= 0) return JSON.stringify({ ...puzzle, number: numbers[hit] });
  }
  throw new ApiError(0, "The automatic check could not finish. Please reload the page and try again.");
}

/** Solves a puzzle ahead of time so the form submits without waiting, and hands out each solution once. */
export class PuzzleCache {
  private pending: Promise<string> | null = null;
  private startedAt = 0;

  prepare(): void {
    this.startedAt = Date.now();
    this.pending = solvePuzzle();
    this.pending.catch(() => undefined); // reported when the form is sent
  }

  /** A fresh solution for one submission (solved now if none is ready or it is about to expire). */
  take(): Promise<string> {
    const ready = this.pending && Date.now() - this.startedAt < USABLE_MS ? this.pending : solvePuzzle();
    this.pending = null;
    return ready;
  }
}
