/** The one spinner glyph, shared by every screen (user requirement 2026-08-31: progress must be
 * visible globally, not re-invented per view). Pair it with a short label at the call site. */
export function Spinner({ className = "h-4 w-4" }: { className?: string }) {
  return (
    <svg className={`${className} shrink-0 animate-spin text-neutral-400`} viewBox="0 0 24 24" fill="none" aria-hidden>
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" />
    </svg>
  );
}

/** Same glyph as `Spinner`, but blue -- the "this specific stage is running right now" signal
 * (AppShell's tab pill, SessionOverview's per-stage table, BuildView's per-stage card), distinct
 * from `Spinner`'s neutral-gray generic "something is loading". A separate component rather than
 * `<Spinner className="text-blue-500" />`: Tailwind's generated-CSS order (not JSX class order)
 * decides ties between two color utilities on the same element, so stacking a color override in
 * `className` on top of `Spinner`'s own hardcoded `text-neutral-400` is not reliable. */
export function RunningSpinner({ className = "h-3.5 w-3.5" }: { className?: string }) {
  return (
    <svg className={`${className} shrink-0 animate-spin text-blue-500`} viewBox="0 0 24 24" fill="none" aria-hidden>
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" />
    </svg>
  );
}
