// Times come from the server as Unix seconds and are shown in the viewer's own time zone.

const dateTime = new Intl.DateTimeFormat(undefined, {
  day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit",
});
const clockTime = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });

export const when = (ts: number) => dateTime.format(new Date(ts * 1000));
export const clock = (ts: number) => clockTime.format(new Date(ts * 1000));

/** 75 -> "1:15", 3725 -> "1:02:05" (a running clock). */
export function stopwatch(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
  const mm = h ? String(m).padStart(2, "0") : String(m);
  return `${h ? `${h}:` : ""}${mm}:${String(r).padStart(2, "0")}`;
}

/** 593 -> "9 min 53 s", 42 -> "42 s", 3720 -> "1 h 2 min" (a finished duration). */
export function duration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s} s`;
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h} h ${m} min` : s % 60 ? `${m} min ${s % 60} s` : `${m} min`;
}

const ordinals = new Intl.PluralRules("en", { type: "ordinal" });
const SUFFIX: Record<string, string> = { one: "st", two: "nd", few: "rd", other: "th" };
export const ordinal = (n: number) => `${n}${SUFFIX[ordinals.select(n)]}`;

export const DEPTH_TEXT: Record<string, string> = {
  quick: "Quick: 3 competitors, about 10 to 20 minutes",
  standard: "Standard: 4 competitors, about 25 to 40 minutes",
  deep: "Deep: 6 competitors, about an hour",
};

export const DEPTH_NAME: Record<string, string> = { quick: "Quick", standard: "Standard", deep: "Deep" };
