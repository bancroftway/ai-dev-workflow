import type { Attachment } from "@copilotkit/react-core/v2";

/**
 * One-shot, in-memory handoff for the New Ticket form's attachment queue, from
 * src/app/(boxed)/tickets/new/page.tsx to src/components/RequirementsView.tsx (via
 * AttachmentEditor.tsx on both ends). Title/description already ride across in sessionStorage
 * (tiny strings, unaffected by this) -- this module exists only because attachments don't fit
 * there: they're base64-inline (no `onUpload` backend is configured anywhere in this codebase --
 * see docs/superpowers/plans/part-3-attachments-research-notes.md section 3), so a single
 * attachment at the shared config's own 20MB maxSize is ~27MB of base64 text once inflated, well
 * past sessionStorage's real per-origin quota (typically 5-10MB, shared across every key on the
 * origin).
 *
 * A plain module-level Map is safe here because /tickets/new -> /workflow/... is a same-tab
 * Next.js App Router client-side transition, not a full page reload -- confirmed, not assumed:
 * both routes render under the single root layout (src/app/layout.tsx is the only layout.tsx
 * under src/app), and the New Ticket form navigates via `router.push` (next/navigation) rather
 * than an <a> tag or window.location. This app does have a request gate -- src/proxy.ts (Next
 * 16 renamed Middleware to "Proxy"; grep for middleware.ts and you will find nothing, which is
 * itself the trap -- see AGENTS.md on this repo's Next.js having broken from upstream
 * conventions) -- but it only ever redirects an UNAUTHENTICATED request to "/"; both routes here
 * require auth to reach in the first place, so for this navigation it's a pass-through on both
 * ends, same as having no proxy at all. Proxy/middleware execution is server-side regardless
 * (it decides what response a request gets, on every navigation including client-side ones), so
 * even a non-pass-through proxy wouldn't by itself change whether the *client's* JS runtime
 * survives -- only an actual redirect response would, and none happens on this path. Next's own
 * docs (node_modules/next/dist/docs/01-app/01-getting-started/04-linking-and-navigating.md,
 * "Client-side transitions") describe exactly this router.push/<Link> path as keeping shared
 * layouts -- and therefore the JS module registry -- alive rather than reloading the document.
 * If any of this ever stops being true (a second root layout appears, src/proxy.ts starts
 * redirecting between these two routes, or the navigation switches to a real document load),
 * this Map silently stops working and would need revisiting alongside it.
 *
 * One accepted trade-off versus sessionStorage: a hard refresh landing between the navigation
 * committing and RequirementsView's mount effect running (a sub-frame window) would lose the
 * attachments, where sessionStorage would have survived it. Given that window's size and that
 * this handoff is already a best-effort, one-shot convenience (same as the existing text handoff,
 * which is equally lost once already consumed), that's an acceptable trade for having no
 * attachment size ceiling at all.
 */
// ponytail: entries for a ticket the user never actually opens (nav away, closed tab before
// RequirementsView mounts) sit here until the tab closes -- bounded by how many tickets one tab
// creates in a session, add an eviction policy if that ever shows up as a real leak.
const pendingAttachmentsBySessionId = new Map<string, Attachment[]>();

/** Called by the New Ticket form right before it navigates to the new session's workflow page. */
export function stashHandoffAttachments(sessionId: string, attachments: Attachment[]): void {
  if (attachments.length === 0) return;
  pendingAttachmentsBySessionId.set(sessionId, attachments);
}

/** Called once by RequirementsView on mount. Returns each stashed attachment re-materialized as a
 * File, ready to feed through the same `processFiles` path a real file-picker/drop/paste selection
 * already goes through (so it re-validates against this session's own attachment config and
 * populates the queue identically) -- and clears the entry so a remount never reapplies it. Empty
 * for every session not created via the New Ticket form, and for one that was but had no
 * attachments queued. */
export function takeHandoffAttachments(sessionId: string): File[] {
  const attachments = pendingAttachmentsBySessionId.get(sessionId);
  if (!attachments) return [];
  pendingAttachmentsBySessionId.delete(sessionId);
  return attachments.map(attachmentToFile).filter((file): file is File => file !== null);
}

/** Only "data" (base64-inline) sources exist anywhere in this codebase today -- confirmed by the
 * Part 3 attachments research notes, no call site configures `onUpload`. A "url" source can't
 * happen in practice; skipped defensively rather than guessed at. */
function attachmentToFile(att: Attachment): File | null {
  if (att.source.type !== "data") return null;
  const bytes = Uint8Array.from(atob(att.source.value), (c) => c.charCodeAt(0));
  return new File([bytes], att.filename ?? "attachment", { type: att.source.mimeType });
}
