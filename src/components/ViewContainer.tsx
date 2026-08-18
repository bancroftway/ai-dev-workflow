/**
 * Shared content-area shape for every stage view (Requirements/Specification/Plan/Build/Quality/
 * Report/Overview) -- one place to change width/padding for all of them at once, instead of each
 * view hand-rolling its own mx-auto/max-w-* (which is exactly how they drifted out of sync).
 * Full width/height on purpose: AppShell's own `<main className="flex-1 overflow-y-auto">`
 * already owns scrolling, so this never adds a second scroll container.
 */
export function ViewContainer({ children }: { children: React.ReactNode }) {
  return <div className="flex h-full w-full flex-col gap-4 p-6">{children}</div>;
}
