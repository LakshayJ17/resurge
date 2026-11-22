// How one analysis flows through Resurge. Wide screens get a drawn flowchart (inline SVG, themed by CSS variables);
// narrow screens and screen readers get the same steps as an ordered list.

type Step = { title: string; lines: [string, string, string]; text: string };

const STEPS: Step[] = [
  { title: "Your request", lines: ["A company name or website,", "plus optional competitors", "and your own questions"],
    text: "A company name or its website, plus optional competitors and your own questions." },
  { title: "Find competitors", lines: ["A market scout picks the", "closest 3 to 6 rivals from", "live search results"],
    text: "A market scout picks the closest 3 to 6 rivals from live search results." },
  { title: "Plan the research", lines: ["Questions per competitor:", "pricing, features, buyers,", "news and risks"],
    text: "Questions for each competitor: pricing, features, buyers, news and risks." },
  { title: "Read public pages", lines: ["Search, then fetch pages", "robots.txt allows; hidden", "instructions are stripped"],
    text: "Searches the web, then fetches only pages that robots.txt allows. Hidden instructions are stripped before any AI reads them." },
  { title: "Extract facts", lines: ["Specialist agents pull out", "facts, each with the exact", "sentence it came from"],
    text: "Specialist agents pull out facts, each kept with the exact sentence it came from." },
  { title: "Check every fact", lines: ["The quote must match the", "page, then an AI model from", "another family verifies it"],
    text: "The quote must match the page, then an AI model from a different family verifies the claim." },
  { title: "Analyse and review", lines: ["Positioning, pricing and", "battlecards; a reviewer", "sends weak parts back"],
    text: "Positioning, pricing comparisons and battlecards. A reviewer scores the draft and sends weak parts back." },
  { title: "Your report", lines: ["A full report page, a PDF", "and a slide deck, plus", "answers with sources"],
    text: "A full report page, a PDF and a slide deck. Ask questions and get answers with sources." },
];

const LOOP = "Standard and deep runs: gaps and weak parts go back for another round";

const W = 220, H = 120, GAP = 40, ROW1 = 10, ROW2 = 250;
const X = [0, W + GAP, 2 * (W + GAP), 3 * (W + GAP)];

function Node({ step, n, x, y }: { step: Step; n: number; x: number; y: number }) {
  return (
    <g>
      <rect className={n === STEPS.length ? "node end" : "node"} x={x} y={y} width={W} height={H} rx={10} />
      <circle className="badge" cx={x + 24} cy={y + 26} r={12} />
      <text className="badge-n" x={x + 24} y={y + 26} textAnchor="middle" dominantBaseline="central">{n}</text>
      <text className="title" x={x + 44} y={y + 31}>{step.title}</text>
      {step.lines.map((line, i) => (
        <text key={i} className="desc" x={x + 16} y={y + 62 + i * 19}>{line}</text>
      ))}
    </g>
  );
}

export function Flowchart() {
  const mid1 = ROW1 + H / 2, mid2 = ROW2 + H / 2, between = (ROW1 + H + ROW2) / 2;
  return (
    <figure className="flow">
      <svg className="flow-svg" viewBox="-4 0 1008 450" aria-hidden="true" focusable="false">
        <defs>
          <marker id="fc-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7"
            orient="auto-start-reverse">
            <path className="head" d="M0,0 L10,5 L0,10 z" />
          </marker>
        </defs>
        {[0, 1, 2].map((i) => (
          <line key={`a${i}`} className="edge" x1={X[i] + W + 3} y1={mid1} x2={X[i + 1] - 5} y2={mid1}
            markerEnd="url(#fc-arrow)" />
        ))}
        <path className="edge" markerEnd="url(#fc-arrow)"
          d={`M ${X[3] + W / 2} ${ROW1 + H + 3} V ${between} H ${X[0] + W / 2} V ${ROW2 - 5}`} />
        {[0, 1, 2].map((i) => (
          <line key={`b${i}`} className="edge" x1={X[i] + W + 3} y1={mid2} x2={X[i + 1] - 5} y2={mid2}
            markerEnd="url(#fc-arrow)" />
        ))}
        <path className="edge loop" markerEnd="url(#fc-arrow)"
          d={`M ${X[2] + W / 2} ${ROW2 + H + 3} C ${X[2] + W / 2} ${ROW2 + H + 52}, ${X[0] + W / 2} ${ROW2 + H + 52}, ${X[0] + W / 2} ${ROW2 + H + 6}`} />
        <text className="loop-label" x={(X[0] + X[2]) / 2 + W / 2} y={ROW2 + H + 66} textAnchor="middle">{LOOP}</text>
        {STEPS.map((s, i) => (
          <Node key={s.title} step={s} n={i + 1} x={X[i % 4]} y={i < 4 ? ROW1 : ROW2} />
        ))}
      </svg>
      <ol className="flow-list">
        {STEPS.map((s, i) => (
          <li key={s.title} data-n={i + 1}>
            <h3>{s.title}</h3>
            <p>{s.text}</p>
            {i === 6 && <p className="loop">{LOOP}.</p>}
          </li>
        ))}
      </ol>
    </figure>
  );
}
