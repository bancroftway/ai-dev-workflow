/**
 * Shared content-area shape for every stage view (Requirements/Specification/Plan/Build/Quality/
 * Report/Overview) -- one place to change width/padding for all of them at once, instead of each
 * view hand-rolling its own mx-auto/max-w-* (which is exactly how they drifted out of sync).
 * Full width/height on purpose: AppShell's own `<main className="flex-1 overflow-y-auto">`
 * already owns scrolling, so this never adds a second scroll container.
 */
export function ViewContainer({ children }: { children: React.ReactNode }) {
  // px-4 aligns content with the tab row above; py-3/gap-3 over the old p-6/gap-4 -- the fat
  // top gap between the tabs and each view's heading read as wasted space (user, 2026-08-31).
  return <div className="flex h-full w-full flex-col gap-3 px-4 py-3">{children}</div>;
}
