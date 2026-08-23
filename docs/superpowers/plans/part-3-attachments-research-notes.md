# Part 3 attachments research notes: ground truth before drafting a task plan

Branch `feature/claude-support`, read-only inspection of the working tree at
`d:\Projects\bancroftway\ai-dev-workflow`. Every citation below is `file:line` against a real file
read in full or in the relevant range; anything not found is stated as a grep-with-no-matches, not
inferred.

One file-consistency note, not a finding: `SPECIFICATION.md` (which `graph.py`'s own module
docstring and `workflow_persistence.py`'s "architecture plan Section B" comment both cite as an
external reference) does not exist at this checkout's repo root — the only copy on disk is inside
a different session's isolated worktree (`.claude\worktrees\agent-a3f7e1b1e1f03b935\SPECIFICATION.md`).
That worktree belongs to the concurrent agent mentioned in this task's own brief, so it was not read
as ground truth for this checkout; wherever "the architecture plan" is cited below (e.g.
`workflow_persistence.py`'s `attachments/` comment) it is quoted as this repo's own in-code
description of that plan, not verified against the plan document itself.

---

## 1. `GraphState.requirements_attachments`

**Declaration**, `agent/src/graph.py:222-226`, exact quote:
```python
    raw_requirements_text: str
    # Non-text InputContent parts (screenshots/documents) from the latest submission's
    # HumanMessage, if any -- only ever consumed by the specification stage's draft prompt
    # (BR-2: the plan stage's input is the approved Specification, never raw attachments).
    requirements_attachments: list[dict[str, Any]]
```
Type is `list[dict[str, Any]]` — an untyped list of dicts, not a Pydantic/TypedDict-shaped model.

**Every WRITE site (2 total, both in `intake_node`).** `agent/src/graph.py:1435-1451`, the splitter:
```python
def _split_text_and_attachments(content: Any) -> tuple[str, list[dict[str, Any]]]:
    """Split a HumanMessage's content into its text and any non-text (AG-UI InputContent)
    parts. A plain string (every submission before multimodal attachments existed, and every
    text-only submission since) passes through unchanged with no attachments.
    """
    if isinstance(content, str):
        return content, []
    if isinstance(content, list):
        text_parts: list[str] = []
        attachments: list[dict[str, Any]] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text_parts.append(str(part.get("text", "")))
            elif isinstance(part, dict):
                attachments.append(part)
        return "\n".join(text_parts), attachments
    return str(content), []
```
Called from `intake_node`, `agent/src/graph.py:1498-1505`:
```python
    raw_requirements_text = state.get("raw_requirements_text", "") if not is_new_submission else ""
    requirements_attachments: list[dict[str, Any]] = []
    if is_new_submission:
        assert latest_human_message is not None  # narrows for the type checker
        raw_requirements_text, requirements_attachments = _split_text_and_attachments(
            latest_human_message.content
        )
        consumed_message_id = latest_human_message.id
```
and returned into state at `agent/src/graph.py:1597-1601`:
```python
    return {
        "stages": stages,
        "run_id": uuid.uuid4().hex[:8],
        "raw_requirements_text": raw_requirements_text,
        "requirements_attachments": requirements_attachments,
```
On a textless/resume run (`is_new_submission` False), `requirements_attachments` is reset to `[]`
every time (it is never carried forward from a prior run/checkpoint the way `raw_requirements_text`
is) — confirmed by the unconditional `requirements_attachments: list[dict[str, Any]] = []`
initializer at line 1499, only overwritten inside the `if is_new_submission:` branch.

**The one READ site.** `agent/src/graph.py:368-383`, `_build_specification_prompt`:
```python
def _build_specification_prompt(state: GraphState) -> list[BaseMessage]:
    stage = state["stages"]["specification"]
    requirements_text = f"Raw Requirements Text:\n\n{state['raw_requirements_text']}"
    attachments = state.get("requirements_attachments") or []
    # Attachments (screenshots/documents) ride alongside the text as a multimodal content list --
    # but neither current provider's CLI-exec path actually has an attachment mechanism to forward
    # them through: both claude_chat_model.py's and copilot_chat_model.py's own
    # `_messages_to_prompt` flatten this list to a single prompt string and DROP every non-text
    # part (logged via logger.warning), a real capability loss versus the old SDK-based Copilot
    # session that is not restored yet -- see either module's own `_messages_to_prompt` docstring.
    # Still built as a list rather than joined to plain text here, so a future CLI-level attachment
    # mechanism (a per-part --file flag, e.g.) has the original structure to work from instead of
    # information already lost upstream.
    requirements_content: str | list[dict[str, Any]] = (
        [{"type": "text", "text": requirements_text}, *attachments] if attachments else requirements_text
    )
    messages: list[BaseMessage] = [
        SystemMessage(content=SPEC_SYSTEM_PROMPT),
        HumanMessage(content=requirements_content),
    ]
```
Grepped `requirements_attachments` across the entire repo (excluding this file itself and the
older `part-3-research-notes.md`, which merely quotes the same `GraphState` declaration as prior
research) — no other reader or writer exists anywhere: not in any prompt-builder for any other
stage, not in `workflow_persistence.py`, not in the frontend's TypeScript types.

**Does a value actually get in today, in practice?** Yes — this is not a dead/unused field. Any
real submission from `RequirementsView.tsx` (see §6) that includes an attachment produces exactly
the list-of-dicts `HumanMessage.content` shape `_split_text_and_attachments` expects, and
`intake_node` correctly splits it every time. The field is populated by a live, already-shipped
frontend path; what's missing is purely downstream (§2).

**Exact dict shape it carries.** Each dict is whatever the frontend put in the `HumanMessage`
content list that isn't `{"type": "text", ...}` — AG-UI's `InputContent` union, from
`node_modules/@ag-ui/core/dist/index.d.mts` (types, not runtime validation reached at this layer —
`graph.py` does no shape-checking beyond "is a dict"):
```
{ type: "text"; text: string }                                        // filtered OUT before reaching attachments
{ type: "image" | "audio" | "video" | "document";
  source:
    | { type: "data"; value: string /* base64 */; mimeType: string }
    | { type: "url"; value: string; mimeType?: string };
  metadata?: unknown }
{ type: "binary"; mimeType: string; id?: string; data?: string; url?: string; filename?: string }
```
`src/components/RequirementsView.tsx:152-165` is what actually constructs these on submit (see §6)
— every attachment it sends today carries `source.type === "data"` (base64-inline), never `"url"`,
because no upload backend is configured (§3).

**Persistence:** `agent/src/workflow_persistence.py:9-11`, exact quote:
```
Not implemented here, an explicit known gap: `attachments/` from the file layout this plan
describes -- requirements_attachments are still ephemeral, per-run-only, same as before this
module existed.
```
So this field never survives past the in-memory `GraphState` checkpoint (`InMemorySaver`, per
`graph.py`'s own comments elsewhere) — no repo file, no DB row, nothing durable, confirmed as a
named, deliberate, still-open gap rather than an oversight.

---

## 2. The two provider CLI-exec modules' drop points

**`agent/src/claude_chat_model.py:121-156`**, `_messages_to_prompt`, exact quote (docstring +
body):
```python
def _messages_to_prompt(messages: list[BaseMessage]) -> str:
    """Flatten a LangChain message list into a single Claude CLI prompt string.

    Mirrors copilot_chat_model._messages_to_prompt's text-handling exactly (SystemMessage gets an
    "Instructions:" prefix, everything else passes through verbatim, parts joined with a blank
    line), but drops multimodal content instead of translating it to an attachment -- the `claude`
    CLI's -p mode takes a single stdin string, and no stage in this pipeline currently sends Claude
    an image. Not attempted here as a ponytail-style deliberate cut, not an oversight: the upgrade
    path, if a stage ever needs it, is to mirror write_scratch_file for binary payloads and pass
    the result via a --file flag per part instead of dropping it.
    """
    parts: list[str] = []
    for message in messages:
        content = message.content
        if isinstance(content, list):
            text_parts: list[str] = []
            dropped = 0
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text_parts.append(str(item.get("text", "")))
                else:
                    dropped += 1
            if dropped:
                logger.warning(
                    "dropped %d non-text content part(s) -- ClaudeChatModel has no multimodal support",
                    dropped,
                )
```
**`agent/src/copilot_chat_model.py:68-99`**, identical shape, exact quote:
```python
def _messages_to_prompt(messages: list[BaseMessage]) -> str:
    """Flatten a LangChain message list into a single Copilot CLI prompt string.

    Mirrors claude_chat_model._messages_to_prompt exactly (SystemMessage gets an "Instructions:"
    prefix, everything else passes through verbatim, parts joined with a blank line) and, like that
    function, drops multimodal content instead of translating it to an attachment. The old SDK
    version translated image_url parts into a Copilot Attachment over the live session
    (_content_part_to_attachment, now deleted along with the rest of the SDK plumbing); the
    verified Copilot CLI flags table (task-3-brief.md) has no attachment/file flag to translate one
    into over `-p`'s stdin-string interface, and no stage in this pipeline currently sends Copilot
    an image. Not attempted here as a ponytail-style deliberate cut, not an oversight -- same
    upgrade path as Claude's: mirror write_scratch_file for binary payloads and pass the result via
    a flag per part, if a real Copilot CLI flag for it is ever confirmed.
    """
    ...
            if dropped:
                logger.warning(
                    "dropped %d non-text content part(s) -- CopilotChatModel has no multimodal "
                    "support over the CLI",
                    dropped,
                )
```
Both are called from each class's own `_agenerate_inner` (`claude_chat_model.py:267`,
`copilot_chat_model.py:292`) as `prompt = _messages_to_prompt(messages)` — the only call site each
way. The dispatcher in front of both, `agent/src/chat_model.py`, does no attachment handling of any
kind (grepped `content|list|attachment` case-insensitively across the whole file: zero matches) —
`get_chat_model_for_thread` just forwards to whichever provider module's own constructor
(`chat_model.py:302-309`), so the drop happens exactly once, inside whichever provider's
`_messages_to_prompt`, never before.

**Real `claude` CLI, checked against a real binary in this environment.** `which claude` resolves
to `/c/Users/jblis/.local/bin/claude`, and `claude --version` reports `2.1.126 (Claude Code)` —
this is an exact version match against `agent/sandbox-image/Dockerfile:29`,
`ARG CLAUDE_CODE_CLI_VERSION=2.1.126`, so this `--help` output is the same CLI version the sandbox
image bakes in (not merely "some claude binary"). Full `claude --help` was captured; the relevant
flag, not mentioned anywhere in this codebase's own comments about Claude's CLI surface:
```
--file <specs...>                                 File resources to download at startup. Format: file_id:relative_path (e.g., --file file_abc:doc.txt file_def:img.png)
```
This flag genuinely exists on the exact pinned CLI version. Its help text alone does not establish
whether `file_id` means an Anthropic Files-API upload id (which would require a separate upload
step to that API first) versus an arbitrary local path/blob this pipeline could hand it directly —
that is exactly what `--help` says and no more; nothing else in this repo or in the CLI's own
`--help` output resolves the ambiguity, so it is reported here as an unverified real flag, not as a
confirmed mechanism. No `--attachment`, `--image`, or other multimodal-input flag exists in the
output. Also present and not currently used anywhere in this codebase: `--input-format stream-json`
(realtime streaming input, `--print`-only) — a second, entirely different mechanism (structured
stdin messages rather than a single prompt string) that might carry richer content than the plain
stdin string `run_turn` feeds today, also unverified against this use case.

**Real `copilot` CLI.** `which copilot` finds nothing in this environment (`command not found`) —
this matches `copilot_chat_model.py`'s own module docstring (lines 21-25), which already states "no
`copilot` binary is installed or authenticated in this dev environment: confirmed empirically:
`which copilot` finds nothing here." So there is no way to independently verify Copilot's real CLI
flags in this environment; per the task's own instruction, only this repo's own documentation is
reported. That documentation is `.superpowers\sdd\part-1-provider-unification-tasks\task-3-brief.md`
(the "verified Copilot CLI flags table" `copilot_chat_model.py` cites repeatedly) — its flags table
(lines 16-21) lists Mode, Tool allow/deny, Model, and a small number of others; grepped the entire
file case-insensitively for `attach|image|multimodal|--file|file_id|vision|picture|pdf` — zero
matches for any of them. So this repo's own most-authoritative record of Copilot's real CLI surface
documents no multimodal/attachment/file-input flag of any kind, consistent with both
`copilot_chat_model.py`'s in-code claim and the module's own `secret_env_names`/flags handling
never referencing one.

---

## 3. Existing file-storage/upload mechanism anywhere in this codebase

**Frontend attachment queue is 100% client-side today — no upload ever happens.** CopilotKit's
`useAttachments` hook (`@copilotkit/react-core/v2`, the only place attachments are handled in the
frontend, see §6) has two strategies per its own bundled reference doc,
`node_modules/@copilotkit/react-core/skills/react-core/references/attachments.md:113-138`:
```
### Custom upload backend (S3 / presigned URL)

`onUpload` replaces the default base64-inline strategy. Return an
`Attachment.source` describing where the file lives.
```
`RequirementsView.tsx`'s own call (`src/components/RequirementsView.tsx:39-46`) configures
`enabled`, `accept`, `maxSize`, and `onUploadFailed` — **no `onUpload`** — so it runs the *default*
base64-inline strategy. Every attachment that reaches `requirements_attachments` today therefore
has `source.type === "data"` (a base64 string plus `mimeType`, held entirely in browser memory and
then in the in-process `GraphState` checkpoint) — nothing is ever written to any backend storage,
confirmed by the absence of any `onUpload`/network call in the one component that builds these.
This is also why `workflow_persistence.py`'s "ephemeral, per-run-only" framing (§1) is accurate: not
only is there no `.ai-dev-workflow/attachments/` persistence step, there is no upload endpoint
upstream of that either.

**No backend upload primitives anywhere in `agent/src` or `src/`.** Grepped
`upload|multipart|FormData` (case-insensitive) across `src/`: the only match is
`src/components/RequirementsView.tsx` itself (the `handleFileUpload`/`onUploadFailed` names that
are part of the client-only `useAttachments` wiring already covered above — not a network upload).
Grepped `UploadFile|File\(\.\.\.\)|blob|BlobServiceClient|azure.storage` (case-insensitive) across
all of `agent/`: 4 files matched, and every one is a false positive on the English word "blob," not
a storage feature —
- `agent/src/telemetry.py:33` — "`git_ops.push_head` embeds its credential-helper script as one
  such blob" (a comment about span attribute size limits).
- `agent/src/gates/test_coverage_gate.py:210,216,231,237,251,252,254` — a local variable named
  `blob` holding `"\n".join(project_texts.values())`, a joined-text scratch string for keyword
  scanning, unrelated to storage.
- `agent/src/app_discovery.py:245` — "a capped blob -- a prompt-grounding artifact," prose only.
- `agent/src/git_ops.py` — the single hit is `blob-report/` (line 304 of the file listing shown by
  an earlier context view), a Playwright test-artifact directory name inside a `.gitignore`-entries
  list, nothing to do with Azure Blob Storage.
There is no FastAPI `UploadFile`/`File(...)` parameter anywhere in `agent/src` (grep for those
literal tokens returned nothing beyond the false positives above).

**No Azure Blob Storage config anywhere.** Grepped `blob` (case-insensitive) across
`infra/main.bicep`: zero matches. No storage-account/blob-container resource of any kind is
declared there.

**`repo_vaults` (the closest thing to "existing per-user storage") stores a pointer, not files.**
`agent/db/migrations/0002_create_repo_vaults.sql:1-13`:
```sql
-- Per user-repo Azure Key Vault pointer (agent/src/keyvault.py). A row here grants NOTHING by
-- itself: secrets are fetched on-behalf-of the signed-in user (Entra OBO), so Azure's own RBAC on
-- the vault is the enforcement -- a wrong or malicious vault_uri can't expose anything the user
-- couldn't already read themselves.
CREATE TABLE dbo.repo_vaults (
    owner       NVARCHAR(255) NOT NULL,
    repo        NVARCHAR(255) NOT NULL,
    user_login  NVARCHAR(255) NOT NULL,
    vault_uri   NVARCHAR(256) NOT NULL,               -- https://<name>.vault.azure.net/
    created_at  DATETIME2(0)  NOT NULL DEFAULT SYSUTCDATETIME(),
    updated_at  DATETIME2(0)  NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT PK_repo_vaults PRIMARY KEY (owner, repo, user_login)
);
```
This is a string pointer to an externally-managed, per-user-authorized Key Vault instance — no file
bytes, secret or otherwise, are ever stored by this codebase; the vault holds them, fetched live via
Entra On-Behalf-Of. Not a reusable pattern for storing uploaded file content.

**The one real "the pipeline writes a binary artifact somewhere" pattern: e2e screenshots,
committed to git, not stored in any blob service.** `agent/src/exit_nodes.py:28`:
`HISTORY_DIR = ".ai-dev-workflow/history"`. `agent/src/e2e_nodes.py:752`:
`screens_dir = f"{HISTORY_DIR}/{run_id}-screens"`. Screenshots are written into that path inside the
already-running sandbox's own git working tree (via `provider.exec_in_sandbox`) and then committed
to the repo through the pipeline's ordinary git-commit machinery (`agent/src/git_ops.py`'s
`commit_paths`/`commit_all`) — i.e., "storage" for a pipeline-produced binary today means "write to
the sandbox filesystem, `git add`, `git commit`," never an object-storage/blob service. This pattern
is real and reusable in principle, but it only exists once a sandbox and a git repo are already
running — the New Ticket form's "+ New Project" path (§4) has neither at the moment a user would
attach a file, since project/repo scaffolding happens only after ticket submission
(`src/app/(boxed)/tickets/new/page.tsx:130-153`).

**Conclusion:** there is no general-purpose file-upload/storage backend anywhere in this codebase
today — client-side base64-inline is the only mechanism that exists, nothing is durably stored, and
the only durable-storage pattern in the whole repo (git-commit inside a sandbox) does not apply
before a session/sandbox exists. A storage decision for durable, pre-session file uploads would be
new.

---

## 4. The New Ticket form

**File:** `src/app/(boxed)/tickets/new/page.tsx`, read in full (320 lines).

**Description field — exact quote, lines 289-298:**
```tsx
        <label className="flex flex-col gap-1">
          <span className="text-sm font-medium text-neutral-700">Description</span>
          <textarea
            className="min-h-[160px] rounded-md border border-neutral-300 px-3 py-2 text-sm"
            placeholder="Describe what you want built. You can refine this further once the session opens."
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            disabled={busy}
          />
        </label>
```
A plain, uncontrolled-markdown `<textarea>` — no markdown rendering, no preview, no file input, no
paste/drag handling, no attachment affordance of any kind. The Title field just above it
(lines 277-287) is likewise a plain `<input type="text">`.

**What happens to that text on submit.** `handleAssign` (lines 90-180) never sends title/description
to the agent/backend at all during ticket creation — it only creates/reuses a project row and
provisions a sandbox session. The text is handed off client-side only, via `sessionStorage`
(lines 172-175):
```tsx
      sessionStorage.setItem(
        `aidw:new-ticket:${sessionId}`,
        JSON.stringify({ title: title.trim(), description: description.trim() }),
      );
      router.push(`/workflow/${owner}/${repo}/${sessionId}/${branch}`);
```
Only `{title, description}` strings travel through this handoff — there is no attachment field in
that payload, and no code path from this page reaches `requirements_attachments` or
`GraphState` at all. `RequirementsView.tsx` (§6) is what consumes this payload, once the user lands
on the workflow page.

**No markdown-editor dependency exists in `package.json`** (read in full, 37 lines):
```json
  "dependencies": {
    "@ag-ui/a2ui-middleware": "^0.0.10",
    "@ag-ui/langgraph": "^0.0.42",
    "@copilotkit/a2ui-renderer": "^1.66.4",
    "@copilotkit/react-core": "^1.66.4",
    "@copilotkit/runtime": "^1.66.4",
    "next": "16.3.0",
    "next-auth": "^5.0.0-beta.32",
    "octokit": "^5.0.5",
    "react": "19.2.8",
    "react-dom": "19.2.8",
    "react-markdown": "^10.1.0",
    "server-only": "^0.0.1",
    "zod": "^3.25.76"
  },
```
`react-markdown` is a markdown **renderer** (turns markdown text into React elements for display),
not an editor. None of `@uiw/react-md-editor`, `tiptap`, `lexical`, `slate`, `@mdxeditor`,
`milkdown`, `codemirror`, or `monaco` appear anywhere in `dependencies`/`devDependencies`.

**However — a markdown-editor-with-inline-attachments UX already exists, just on a different
page.** `src/components/RequirementsView.tsx` (the main workflow page's requirements surface, a
wholly separate component the New Ticket form does not use) already implements: a `<textarea>` +
Edit/Preview mode toggle rendered via `react-markdown` in Preview mode
(`RequirementsView.tsx:201-219`); inline attachment references via a `![screenshot](attachment:filename)`
convention resolved through a custom `urlTransform` (`resolveUrl`, lines 136-144); automatic
insertion of such a reference on image paste (`handlePaste`, lines 112-130); drag-and-drop; and an
explicit "Attach screenshot/document" file-picker button (lines 251-258) — all built on CopilotKit's
`useAttachments` hook configured with `accept: "image/*,application/pdf,.doc,.docx,.txt,.md"` and
`maxSize: 20 * 1024 * 1024` (lines 39-46). It is not a rich WYSIWYG editor (no formatting toolbar,
no inline-rendered images while typing — Preview is a separate pane) but it already covers "a
markdown field with inline attachment support" as a working pattern in this same codebase, on the
one page that is not the New Ticket form.

---

## 5. Specification and Plan stages' real current prompts

**Filenames, confirmed via `graph.py`'s own `load_prompt` calls (not guessed):**
`agent/src/graph.py:326`: `SPEC_SYSTEM_PROMPT = load_prompt("specification_draft")` →
`agent/src/prompts/specification_draft.md`. `agent/src/graph.py:333`:
`PLAN_SYSTEM_PROMPT = load_prompt("plan_draft")` → `agent/src/prompts/plan_draft.md`. Cross-checked
against `agent/src/prompts/README.md`'s own stage table, lines 19 and 21:
`| `specification_draft.md` / `specification_audit.md` | specification | user stories + acceptance criteria |`
and `| `plan_draft.md` / `plan_audit.md` | plan | implementation plan, diagrams, wireframes |`.

**Neither prompt mentions attachments, screenshots, or images — at all.** Both files were read in
full (47 and 57 lines respectively) and grepped case-insensitively for
`attachment|screenshot|image`: zero matches in either file. Full text of `specification_draft.md`
covers: use the `brainstorming` skill, read the Raw Requirements Text, produce a Specification (User
Stories/ACs), ask Clarifying Questions when insufficient, use the `spec-sync` skill for identity
preservation, and the hard rule about never inventing `US-####`/`AC-####.#` ids. Full text of
`plan_draft.md` covers: use the `writing-plans` skill, read the approved Specification, produce an
Implementation Plan (Plan Steps + Risk Notes), Clarifying Questions, identity preservation, Mermaid
diagram rules, and self-contained-HTML wireframe rules. Neither says one word about how (or whether)
to use any non-text input.

**How attachments do/don't reach each stage's prompt-builder — traced directly, not inferred.**
`_build_specification_prompt` (`agent/src/graph.py:368-383`, quoted in full in §1) is the *only*
function that reads `requirements_attachments`, and it inlines the parts directly into the
`HumanMessage.content` list alongside a `{"type": "text", ...}` part for the requirements text —
attachments are passed as literal parts of one multimodal message, not as a separate message and
not stringified into the text. `_build_plan_prompt` (`agent/src/graph.py:405-430`) never reads
`requirements_attachments` or `raw_requirements_text` at all — its `HumanMessage`s are built purely
from the approved Specification JSON (`spec_stage["approved_content"]`), the greenfield/UI-framework
segments, the ticket-mode segment, Plan's own prior draft, and used ids. This matches
`requirements_attachments`'s own declaration comment (`graph.py:223-225`, quoted in §1): "only ever
consumed by the specification stage's draft prompt (BR-2: the plan stage's input is the approved
Specification, never raw attachments)." So today's design is that Plan is categorically excluded
from ever seeing raw attachments, by the same architectural rule that keeps it from seeing raw
requirements text — not an oversight parallel to the CLI-drop issue in §2, but a separate, deliberate
exclusion one level higher up.

---

## 6. How intake actually captures requirements today

**`intake_node`** (`agent/src/graph.py:1454-1611`) is the sole place a fresh `HumanMessage` is
consumed. It picks the latest `HumanMessage` off `state["messages"]`
(`graph.py:1492-1494`), decides "is this a new submission" by comparing that message's `id` against
`consumed_message_id` (`graph.py:1495-1496`), and — only when new — calls
`_split_text_and_attachments(latest_human_message.content)` (`graph.py:1502-1504`, quoted in §1) to
produce both `raw_requirements_text` and `requirements_attachments` together, from the same message,
in one step.

**The frontend flow that feeds it is `RequirementsView.tsx`, and it is NOT text-only — it already
has full attachment affordance.** `src/components/RequirementsView.tsx:28-46` wires up CopilotKit's
`useAttachments` hook:
```tsx
  const {
    attachments,
    containerRef,
    fileInputRef,
    handleFileUpload,
    handleDragOver,
    handleDragLeave,
    handleDrop,
    removeAttachment,
    consumeAttachments,
    processFiles,
  } = useAttachments({
    config: {
      enabled: true,
      accept: "image/*,application/pdf,.doc,.docx,.txt,.md",
      maxSize: 20 * 1024 * 1024,
      onUploadFailed: ({ file, message }) => setUploadError(`${file.name}: ${message}`),
    },
  });
```
and its submit handler builds exactly the AG-UI `InputContent[]` shape `_split_text_and_attachments`
expects (`RequirementsView.tsx:146-171`):
```tsx
  async function handleSubmit() {
    const trimmed = text.trim();
    if (!trimmed) return;
    setSubmitting(true);
    try {
      const ready = consumeAttachments();
      const content: string | InputContent[] =
        ready.length === 0
          ? trimmed
          : [
              { type: "text", text: trimmed },
              ...ready.map(
                (att) =>
                  ({
                    type: att.type,
                    source: att.source,
                    metadata: { ...(att.filename ? { filename: att.filename } : {}), ...att.metadata },
                  }) as InputContent,
              ),
            ];
      agent.addMessage({ id: crypto.randomUUID(), role: "user", content });
      await copilotkit.runAgent({ agent });
    } finally {
      setSubmitting(false);
    }
  }
```
So the ONE existing frontend entry point that actually calls `agent.addMessage`/`runAgent` (the only
thing `intake_node` ever sees) already has: a file-picker button ("Attach screenshot/document",
lines 251-258), paste-to-attach for clipboard images (`handlePaste`, lines 112-130, which both queues
the file via `processFiles` and inserts a `![screenshot](attachment:<name>)` markdown reference at
the cursor), and drag-and-drop (`containerRef`/`handleDragOver`/`handleDragLeave`/`handleDrop`,
wired onto the surrounding `<div>` at lines 189-194). This is a real, working, already-shipped
attachment path that round-trips correctly into `GraphState.requirements_attachments` today — the
gap is entirely downstream of intake (§2's CLI-level drop, and §5's prompts never mentioning
attachments), not at the intake/capture layer itself.

The New Ticket form (§4) is the genuinely text-only entry point, but it is a *different* component
that never calls `agent.addMessage` itself — it hands off plain `{title, description}` strings via
`sessionStorage`, which `RequirementsView.tsx`'s own `parseNewTicketHandoff`
(lines 298-311) reads back once, on mount, purely to seed its own `text` state
(`RequirementsView.tsx:65-91`):
```tsx
  useEffect(() => {
    if (syncedRef.current) return;
    let pending: string | null;
    try {
      const key = `aidw:new-ticket:${threadId}`;
      pending = sessionStorage.getItem(key);
      if (pending) sessionStorage.removeItem(key);
    } catch {
      return;
    }
    if (!pending) return;
    const combined = parseNewTicketHandoff(pending);
    if (combined) {
      setText(combined);
      syncedRef.current = true;
    }
  }, [threadId]);
```
By the time that handoff runs, the user is looking at the same fully attachment-capable
`RequirementsView` UI described above — so any attachment added at that point (post-handoff, before
first Submit) already flows through the working path. There is no stub/placeholder attachment
affordance on the New Ticket page itself; it is simply absent there, confirmed by the full-file read
in §4.
