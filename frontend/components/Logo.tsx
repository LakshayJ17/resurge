// A small mark: three bars rising, the last one in the accent colour (research that builds to a decision).
export function Logo() {
  return (
    <svg className="brand-mark" viewBox="0 0 24 24" aria-hidden="true">
      <rect x="2" y="13" width="5" height="9" rx="1.2" fill="currentColor" opacity=".45" />
      <rect x="9.5" y="8" width="5" height="14" rx="1.2" fill="currentColor" opacity=".7" />
      <rect x="17" y="2" width="5" height="20" rx="1.2" fill="var(--accent)" />
    </svg>
  );
}
