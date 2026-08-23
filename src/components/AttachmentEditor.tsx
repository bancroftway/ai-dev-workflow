"use client";

import { useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import type { UseAttachmentsReturn } from "@copilotkit/react-core/v2";

/** Shared by every `useAttachments({ config })` call in this app (currently RequirementsView.tsx
 * and the New Ticket form) so their accept/maxSize can't silently drift apart -- the New Ticket
 * handoff (new-ticket-attachment-handoff.ts) re-feeds attachments from one call site into the
 * other's queue via the same `processFiles` validation path, which only stays correct if both
 * sides agree on what's acceptable. */
export const SHARED_ATTACHMENTS_CONFIG = {
  accept: "image/*,application/pdf,.doc,.docx,.txt,.md",
  maxSize: 20 * 1024 * 1024,
} as const;

/** Markdown textarea + Edit/Preview toggle + inline-attachment affordance (file picker,
 * paste-to-attach, drag-and-drop) -- originally built inline in RequirementsView.tsx (the
 * workflow page's requirements editor) and extracted here so the New Ticket form's Description
 * field can reuse the exact same capability instead of a second implementation. Deliberately not
 * a rich WYSIWYG editor: Preview is a separate pane, never inline-rendered images while typing --
 * matches the shape that already shipped in this codebase, nothing fancier.
 *
 * The `useAttachments` hook call itself stays with each caller (each page's own submit handler
 * needs to call `consumeAttachments()` on it directly), so this component takes that hook's full
 * return value as a prop rather than calling the hook internally.
 */
export function AttachmentEditor({
  value,
  onChange,
  attachmentsApi,
  disabled = false,
  placeholder,
  minHeightClassName = "min-h-[240px]",
  uploadError,
}: {
  value: string;
  onChange: (value: string) => void;
  attachmentsApi: UseAttachmentsReturn;
  disabled?: boolean;
  placeholder?: string;
  minHeightClassName?: string;
  uploadError?: string | null;
}) {
  const [mode, setMode] = useState<"edit" | "preview">("edit");
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  const {
    attachments,
    containerRef,
    fileInputRef,
    handleFileUpload,
    handleDragOver,
    handleDragLeave,
    handleDrop,
    removeAttachment,
    processFiles,
  } = attachmentsApi;

  /** Pasted images upload through the normal attachment queue AND insert a markdown ref at the
   * cursor. Each pasted file is renamed to a unique name first -- clipboard images all arrive as
   * "image.png", and the `attachment:` ref resolves by filename, so uniqueness is what keeps two
   * pastes from resolving to the same image. Non-image pastes (text) proceed untouched. */
  function handlePaste(event: React.ClipboardEvent<HTMLTextAreaElement>) {
    const images = Array.from(event.clipboardData?.files ?? []).filter((f) => f.type.startsWith("image/"));
    if (images.length === 0) return;
    event.preventDefault();
    const renamed = images.map(
      (f, i) => new File([f], `pasted-${Date.now()}-${i + 1}.${(f.type.split("/")[1] ?? "png").split("+")[0]}`, { type: f.type }),
    );
    void processFiles(renamed);
    const refs = renamed.map((f) => `![screenshot](attachment:${f.name})`).join("\n");
    const el = textareaRef.current;
    const start = el?.selectionStart ?? value.length;
    const end = el?.selectionEnd ?? value.length;
    const next = `${value.slice(0, start)}${refs}${value.slice(end)}`;
    onChange(next);
    requestAnimationFrame(() => {
      el?.focus();
      el?.setSelectionRange(start + refs.length, start + refs.length);
    });
  }

  /** Resolves attachment:<filename> refs in the preview to a renderable URL from the live
   * attachment queue. Refs whose attachment is gone (e.g. after submit -- consumeAttachments
   * clears the queue) render as the alt text, which is acceptable: the image itself already
   * reached the pipeline as an InputContent part. */
  function resolveUrl(url: string): string {
    if (!url.startsWith("attachment:")) return url;
    const name = decodeURIComponent(url.slice("attachment:".length));
    const att = attachments.find((a) => a.filename === name);
    if (!att) return "";
    if (att.thumbnail) return att.thumbnail;
    if (att.source.type === "data") return `data:${att.source.mimeType};base64,${att.source.value}`;
    return att.source.value;
  }

  return (
    <div
      ref={containerRef}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
      className="flex min-h-0 flex-1 flex-col rounded-lg border border-neutral-300 focus-within:ring-1 focus-within:ring-neutral-400"
    >
      <div className="flex items-center gap-1 border-b border-neutral-200 px-2 py-1">
        <ModeButton label="Edit" active={mode === "edit"} onClick={() => setMode("edit")} />
        <ModeButton label="Preview" active={mode === "preview"} onClick={() => setMode("preview")} />
      </div>

      {mode === "edit" ? (
        <textarea
          ref={textareaRef}
          className={`${minHeightClassName} w-full flex-1 resize-none p-3 text-sm outline-none`}
          placeholder={placeholder}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          onPaste={handlePaste}
          disabled={disabled}
        />
      ) : (
        <div
          className={`prose prose-sm ${minHeightClassName} max-w-none flex-1 overflow-y-auto p-3 [&_img]:max-h-80 [&_img]:rounded-md [&_img]:border`}
        >
          {value.trim() ? (
            <ReactMarkdown urlTransform={resolveUrl}>{value}</ReactMarkdown>
          ) : (
            <p className="text-sm text-neutral-400">Nothing to preview yet.</p>
          )}
        </div>
      )}

      {attachments.length > 0 && (
        <ul className="flex flex-wrap gap-2 border-t border-neutral-200 p-2">
          {attachments.map((att) => (
            <li
              key={att.id}
              className="flex items-center gap-1.5 rounded-full border border-neutral-200 bg-neutral-50 px-2.5 py-1 text-xs text-neutral-700"
            >
              <span>{att.filename ?? "attachment"}</span>
              <span className="text-neutral-400">({att.status})</span>
              <button
                type="button"
                className="ml-0.5 text-neutral-400 hover:text-neutral-700"
                onClick={() => removeAttachment(att.id)}
                aria-label={`Remove ${att.filename ?? "attachment"}`}
              >
                ×
              </button>
            </li>
          ))}
        </ul>
      )}

      <div className="flex items-center justify-between gap-2 border-t border-neutral-200 p-2">
        <input ref={fileInputRef} type="file" multiple className="hidden" onChange={handleFileUpload} />
        <button
          type="button"
          className="rounded-md border border-neutral-300 px-3 py-1.5 text-xs font-medium text-neutral-700 hover:bg-neutral-100"
          onClick={() => fileInputRef.current?.click()}
          disabled={disabled}
        >
          Attach screenshot/document
        </button>
        {uploadError && <span className="text-xs text-red-600">{uploadError}</span>}
      </div>
    </div>
  );
}

function ModeButton({ label, active, onClick }: { label: string; active: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      className={[
        "rounded px-2 py-0.5 text-xs font-medium",
        active ? "bg-neutral-900 text-white" : "text-neutral-500 hover:bg-neutral-100",
      ].join(" ")}
      onClick={onClick}
    >
      {label}
    </button>
  );
}
