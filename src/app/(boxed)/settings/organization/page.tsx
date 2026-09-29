"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { PROVIDER_LABELS, invalidateOrgProvider } from "@/lib/use-org-provider";

type Provider = "copilot" | "claude";
// C-1 (whole-branch review): the two Claude billing modes. Meaningless for provider === "copilot"
// (the backend records credential_kind=null there). "api_key" matches what every credential saved
// before this column existed necessarily was -- see BILLING_MODE_LABELS' own default handling
// below for where that matters in this UI.
type BillingMode = "api_key" | "oauth";

type OrgSettings = {
  provider: Provider;
  // Whether a credential is SAVED (vault has a secret under the org's one fixed slot) -- NOT
  // whether one would actually work right now. This page keys its masked-dots-vs-input display
  // off this field; SettingsBanner/settings-checks.ts instead key off the response's
  // `session_ready` field (a different, deliberately narrower question: "would a session
  // provisioned right now get a usable credential", which also covers the env-var-fallback and
  // last-probe-failed cases this field says nothing about). Documented on both sides
  // (agent/src/sessions_api.py's `_org_settings_response()` docstring is the backend original) so
  // a reader who notices this page and the banner check two different fields doesn't mistake it
  // for drift -- it's deliberate.
  credential_configured: boolean;
  // null means "no credential saved" OR "saved before this column existed" -- read as "api_key"
  // in the UI below (BILLING_MODE_LABELS' own default), never as "unknown".
  credential_kind: BillingMode | null;
  updated_at: string | null;
  updated_by: string | null;
  // "owner/repo" of the TOOL's own support repo, where failed-run issues are filed (never the
  // customer repo). null = not configured; the support-issue button explains and links here.
  support_repo: string | null;
  // Deployment-wide default DESIGN.md -- the fallback a repo uses when it has no override of its
  // own (../[owner]/[repo]/page.tsx). null = no default set.
  design_md: string | null;
  // Org-wide extra gitleaks allowlist entries (repo_scan.py's org_gitleaks_allowlist), one
  // pattern per line. null = no override, every scan's default behavior.
  gitleaks_extra_stopwords: string | null;
  gitleaks_extra_allow_paths: string | null;
};

const BILLING_MODE_LABELS: Record<BillingMode, string> = {
  oauth: "Subscription (Pro / Max / Team)",
  api_key: "API key (metered)",
};

// Shown as placeholder text only (never submitted unless the operator types over it) -- the
// machine-readable shape the deterministic design-token gate needs, per impeccable's own
// DESIGN.md format spec (reference/document.md). A plain prose brand doc still works for the two
// LLM-level enforcement layers; only the automated color-token check needs this frontmatter.
const DESIGN_MD_PLACEHOLDER = `---
name: <project title>
description: <one-line tagline>
colors:
  primary: "#b8422e"
  neutral-bg: "#faf7f2"
  # ...one entry per brand color; key = descriptive slug
typography:
  display:
    fontFamily: "Cormorant Garamond, Georgia, serif"
  body:
    fontFamily: "Inter, system-ui, sans-serif"
---

# Design System: <project title>

## Overview

...

## Colors

...`;

type SaveState =
  | { kind: "idle" }
  | { kind: "saving" }
  | { kind: "saved" }
  | { kind: "error"; detail: string };

/**
 * Org-wide active coding-agent provider + credential. Sibling of the per-repo settings page
 * (../[owner]/[repo]/page.tsx) -- same SaveState shape, raw Tailwind, loading/saved/error
 * rendering -- but there's no repo to scope this to: it changes what every NEW session across the
 * org runs (an in-flight session keeps whatever it was pinned to at intake). The credential is
 * write-only once saved (Part 4 Spec's own explicit resolution, matching the vault page's own
 * convention): GET never returns the value, only credential_configured.
 *
 * Authorization note: this codebase has no admin/role concept beyond "signed in" (checked
 * src/lib/session-access.ts -- its only gate, hasRepoAccess, is repo-scoped and doesn't apply to
 * an org-wide setting). So today, any signed-in user can reach and change this page -- see the BFF
 * route's own comment for the same note, flagged rather than silently assumed.
 */
export default function OrganizationSettingsPage() {
  const [provider, setProvider] = useState<Provider>("copilot");
  // Defaults to "api_key" -- matches every credential saved before this control existed, and the
  // GET response's own credential_kind: null default (see OrgSettings' own comment above).
  const [billingMode, setBillingMode] = useState<BillingMode>("api_key");
  const [credentialConfigured, setCredentialConfigured] = useState(false);
  const [editingCredential, setEditingCredential] = useState(false);
  const [credentialInput, setCredentialInput] = useState("");
  const [updatedAt, setUpdatedAt] = useState<string | null>(null);
  const [updatedBy, setUpdatedBy] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [save, setSave] = useState<SaveState>({ kind: "idle" });
  const [supportRepo, setSupportRepo] = useState("");
  const [supportSave, setSupportSave] = useState<SaveState>({ kind: "idle" });
  const [designMd, setDesignMd] = useState("");
  const [designMdSave, setDesignMdSave] = useState<SaveState>({ kind: "idle" });
  const [designMdTokensDetected, setDesignMdTokensDetected] = useState(true);
  const [gitleaksStopwords, setGitleaksStopwords] = useState("");
  const [gitleaksAllowPaths, setGitleaksAllowPaths] = useState("");
  const [gitleaksSave, setGitleaksSave] = useState<SaveState>({ kind: "idle" });

  useEffect(() => {
    fetch("/api/settings/organization")
      .then((res) => (res.ok ? res.json() : null))
      .then((data: OrgSettings | null) => {
        if (!data) return;
        setProvider(data.provider);
        setBillingMode(data.credential_kind === "oauth" ? "oauth" : "api_key");
        setCredentialConfigured(data.credential_configured);
        setEditingCredential(!data.credential_configured);
        setUpdatedAt(data.updated_at);
        setUpdatedBy(data.updated_by);
        setSupportRepo(data.support_repo ?? "");
        setDesignMd(data.design_md ?? "");
        setGitleaksStopwords(data.gitleaks_extra_stopwords ?? "");
        setGitleaksAllowPaths(data.gitleaks_extra_allow_paths ?? "");
      })
      .finally(() => setLoaded(true));
  }, []);

  async function saveSettings() {
    setSave({ kind: "saving" });
    const res = await fetch("/api/settings/organization", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        provider,
        credential: credentialInput.trim() || null,
        // Only meaningful for claude -- the backend ignores/records null for copilot anyway, but
        // sending it unconditionally means this component never has to special-case the omission.
        credential_kind: provider === "claude" ? billingMode : null,
      }),
    });
    const body = (await res.json().catch(() => ({}))) as OrgSettings & { detail?: string };
    if (res.ok) {
      setCredentialConfigured(body.credential_configured ?? false);
      setBillingMode(body.credential_kind === "oauth" ? "oauth" : "api_key");
      setEditingCredential(!body.credential_configured);
      setCredentialInput("");
      setUpdatedAt(body.updated_at ?? null);
      setUpdatedBy(body.updated_by ?? null);
      invalidateOrgProvider();
      setSave({ kind: "saved" });
    } else {
      if (res.status === 422) {
        // The 422 cases (sessions_api.py's put_org_settings_endpoint): switching provider, or
        // switching Claude billing mode (C-1), with no new credential supplied either way. The
        // masked "configured" display would otherwise hide the exact field the user needs to
        // fill in to fix this -- reveal it.
        setEditingCredential(true);
      }
      setSave({ kind: "error", detail: body.detail ?? `save failed (${res.status})` });
    }
  }

  return (
    <div className="flex h-full w-full flex-col gap-6 p-6">
      <div>
        <Link href="/select" className="text-sm text-neutral-500 hover:text-neutral-800">
          ← Back to repositories
        </Link>
        <h1 className="mt-2 text-lg font-semibold">Organization Settings</h1>
        <p className="text-sm text-neutral-500">
          Applies to every new session across the organization. A session already running keeps
          whatever provider it started with.
        </p>
      </div>

      <section className="flex max-w-2xl flex-col gap-4 rounded-lg border border-neutral-200 p-4">
        <div>
          <h2 className="font-medium">Coding agent provider</h2>
          <p className="mt-1 text-sm text-neutral-500">
            Which coding agent new sessions run on. Takes effect for the next session provisioned
            -- no redeploy needed.
          </p>
        </div>

        <div className="flex flex-col gap-2">
          {(Object.keys(PROVIDER_LABELS) as Provider[]).map((value) => (
            <label key={value} className="flex items-center gap-2 text-sm">
              <input
                type="radio"
                name="provider"
                value={value}
                checked={provider === value}
                onChange={() => setProvider(value)}
                disabled={!loaded}
              />
              {PROVIDER_LABELS[value]}
            </label>
          ))}
        </div>

        {provider === "claude" && (
          <div className="flex flex-col gap-2 border-t border-neutral-100 pt-3">
            <span className="text-sm font-medium text-neutral-700">Claude billing mode</span>
            {(Object.keys(BILLING_MODE_LABELS) as BillingMode[]).map((value) => (
              <label key={value} className="flex items-center gap-2 text-sm">
                <input
                  type="radio"
                  name="billing-mode"
                  value={value}
                  checked={billingMode === value}
                  onChange={() => setBillingMode(value)}
                  disabled={!loaded}
                />
                {BILLING_MODE_LABELS[value]}
              </label>
            ))}
          </div>
        )}

        <label className="flex flex-col gap-1">
          <span className="text-sm font-medium text-neutral-700">
            {provider === "claude" ? (billingMode === "oauth" ? "Subscription token" : "API key") : PROVIDER_LABELS[provider]} credential
          </span>
          {credentialConfigured && !editingCredential ? (
            <div className="flex items-center gap-3">
              <span className="rounded-md border border-neutral-300 bg-neutral-50 px-3 py-2 text-sm text-neutral-500">
                •••••••••••••••• configured
              </span>
              <button
                type="button"
                className="text-sm text-neutral-600 underline hover:text-neutral-900"
                onClick={() => setEditingCredential(true)}
              >
                Change
              </button>
            </div>
          ) : (
            <input
              type="password"
              autoComplete="off"
              className="rounded-md border border-neutral-300 px-3 py-2 text-sm"
              placeholder={
                credentialConfigured
                  ? "New credential"
                  : provider === "claude" && billingMode === "oauth"
                    ? "Generate with `claude setup-token`, paste here"
                    : `${PROVIDER_LABELS[provider]} API key or token`
              }
              value={credentialInput}
              onChange={(event) => setCredentialInput(event.target.value)}
              disabled={!loaded}
            />
          )}
        </label>

        <div className="flex items-center gap-3">
          <button
            type="button"
            className="self-start rounded-lg bg-neutral-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-40"
            onClick={saveSettings}
            disabled={!loaded || save.kind === "saving"}
          >
            {save.kind === "saving" ? "Saving…" : "Save"}
          </button>
          {save.kind === "saved" && <span className="text-sm text-green-700">✓ Saved</span>}
        </div>

        {save.kind === "error" && (
          <div className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-800">
            <p className="font-medium">Could not save organization settings</p>
            <p className="mt-1 break-words">{save.detail}</p>
          </div>
        )}

        {updatedAt && (
          <p className="text-xs text-neutral-400">
            Last changed by {updatedBy ?? "unknown"} on {new Date(updatedAt).toLocaleString()}
          </p>
        )}
      </section>

      <section className="flex max-w-2xl flex-col gap-4 rounded-lg border border-neutral-200 p-4">
        <div>
          <h2 className="font-medium">Support repo</h2>
          <p className="mt-1 text-sm text-neutral-500">
            Failed-run issues are filed here (this tool&apos;s own support repository — never the
            repo a run worked on). Leave blank to disable the &quot;Open support issue&quot; action.
          </p>
        </div>

        <label className="flex flex-col gap-1">
          <span className="text-sm font-medium text-neutral-700">Repository (owner/repo)</span>
          <input
            type="text"
            autoComplete="off"
            className="rounded-md border border-neutral-300 px-3 py-2 text-sm"
            placeholder="my-org/ai-dev-workflow-support"
            value={supportRepo}
            onChange={(event) => setSupportRepo(event.target.value)}
            disabled={!loaded}
          />
        </label>

        <div className="flex items-center gap-3">
          <button
            type="button"
            className="self-start rounded-lg bg-neutral-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-40"
            onClick={async () => {
              setSupportSave({ kind: "saving" });
              const res = await fetch("/api/settings/organization/support-repo", {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ support_repo: supportRepo.trim() || null }),
              });
              const body = (await res.json().catch(() => ({}))) as OrgSettings & { detail?: string };
              if (res.ok) {
                setSupportRepo(body.support_repo ?? "");
                setSupportSave({ kind: "saved" });
              } else {
                setSupportSave({ kind: "error", detail: body.detail ?? `save failed (${res.status})` });
              }
            }}
            disabled={!loaded || supportSave.kind === "saving"}
          >
            {supportSave.kind === "saving" ? "Saving…" : "Save"}
          </button>
          {supportSave.kind === "saved" && <span className="text-sm text-green-700">✓ Saved</span>}
        </div>

        {supportSave.kind === "error" && (
          <div className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-800">
            <p className="font-medium">Could not save support repo</p>
            <p className="mt-1 break-words">{supportSave.detail}</p>
          </div>
        )}
      </section>

      <section className="flex max-w-2xl flex-col gap-4 rounded-lg border border-neutral-200 p-4">
        <div>
          <h2 className="font-medium">Default design system (DESIGN.md)</h2>
          <p className="mt-1 text-sm text-neutral-500">
            Seeded into every repo that has no design system of its own (per-repo overrides live on
            that repo&apos;s own settings page). Overwrites the repo&apos;s copy on every run — this
            is the deployment&apos;s canonical value, not a one-time suggestion. Leave blank to fall
            back to each repo&apos;s own reverse-engineered design system.
          </p>
        </div>

        <label className="flex flex-col gap-1">
          <span className="text-sm font-medium text-neutral-700">DESIGN.md content</span>
          <textarea
            rows={12}
            className="rounded-md border border-neutral-300 px-3 py-2 font-mono text-xs"
            placeholder={DESIGN_MD_PLACEHOLDER}
            value={designMd}
            onChange={(event) => setDesignMd(event.target.value)}
            disabled={!loaded}
          />
        </label>

        <div className="flex items-center gap-3">
          <button
            type="button"
            className="self-start rounded-lg bg-neutral-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-40"
            onClick={async () => {
              setDesignMdSave({ kind: "saving" });
              const res = await fetch("/api/settings/organization/design-md", {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ design_md: designMd.trim() || null }),
              });
              const body = (await res.json().catch(() => ({}))) as OrgSettings & {
                tokens_detected?: boolean;
                detail?: string;
              };
              if (res.ok) {
                setDesignMd(body.design_md ?? "");
                setDesignMdTokensDetected(body.tokens_detected ?? true);
                setDesignMdSave({ kind: "saved" });
              } else {
                setDesignMdSave({ kind: "error", detail: body.detail ?? `save failed (${res.status})` });
              }
            }}
            disabled={!loaded || designMdSave.kind === "saving"}
          >
            {designMdSave.kind === "saving" ? "Saving…" : "Save"}
          </button>
          {designMdSave.kind === "saved" && <span className="text-sm text-green-700">✓ Saved</span>}
        </div>

        {designMdSave.kind === "saved" && designMd.trim() && !designMdTokensDetected && (
          <div className="rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800">
            No machine-readable <code>colors:</code> frontmatter detected. The agent will still read
            and follow this document, but the automated color-conformance check has nothing to
            check against until it includes a YAML frontmatter block like the placeholder above.
          </div>
        )}

        {designMdSave.kind === "error" && (
          <div className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-800">
            <p className="font-medium">Could not save default design system</p>
            <p className="mt-1 break-words">{designMdSave.detail}</p>
          </div>
        )}
      </section>

      <section className="flex max-w-2xl flex-col gap-4 rounded-lg border border-neutral-200 p-4">
        <div>
          <h2 className="font-medium">Gitleaks allowlist</h2>
          <p className="mt-1 text-sm text-neutral-500">
            Suppresses known-false-positive secret findings across every scanned repo in this org
            (e.g. e2e/smoke-test fixture values that merely look like secrets). Applies on top of
            the platform&apos;s own built-in allowlist -- leave both blank to keep today&apos;s
            default behavior.
          </p>
        </div>

        <label className="flex flex-col gap-1">
          <span className="text-sm font-medium text-neutral-700">Extra stopwords (one per line)</span>
          <p className="text-xs text-neutral-500">
            A literal substring matched against a flagged secret&apos;s VALUE (not the file or
            variable name) -- if the substring appears anywhere in what gitleaks flagged, that
            finding is allowed. Case-sensitive, no regex.
          </p>
          <ul className="list-disc space-y-0.5 pl-4 text-xs text-neutral-500">
            <li>
              <code>e2e-smoke</code> — matches any value containing this, e.g.{" "}
              <code>e2e-smoke-internal-secret</code>
            </li>
            <li>
              <code>dummy</code>, <code>mock</code>, <code>fake</code> — common placeholder-value
              prefixes
            </li>
            <li>
              <code>sandbox-</code>, <code>staging-</code> — env-scoped test credentials that
              aren&apos;t real production secrets
            </li>
            <li>
              <code>fixture</code> — generic test-fixture marker some repos prefix every seeded
              value with
            </li>
          </ul>
          <textarea
            rows={4}
            className="rounded-md border border-neutral-300 px-3 py-2 font-mono text-xs"
            placeholder={"e2e-smoke\nfixture-only"}
            value={gitleaksStopwords}
            onChange={(event) => setGitleaksStopwords(event.target.value)}
            disabled={!loaded}
          />
        </label>

        <label className="flex flex-col gap-1">
          <span className="text-sm font-medium text-neutral-700">Extra allow-path patterns (one per line, regex)</span>
          <p className="text-xs text-neutral-500">
            Excludes whole files/dirs from secret scanning entirely, by path. Coarser than a
            stopword: suppresses ALL findings in a matching file, not just one known-safe value --
            reach for this when a whole file/folder is test-only, not just one value in it.
          </p>
          <ul className="list-disc space-y-0.5 pl-4 text-xs text-neutral-500">
            <li>
              <code>**/e2e/**</code> — any file under an <code>e2e/</code> directory, at any depth
            </li>
            <li>
              <code>**/*\.(spec|test)\.[jt]sx?$</code> — Jest/Vitest/Playwright test files by
              extension
            </li>
            <li>
              <code>**/fixtures/**</code>, <code>**/mocks/**</code> — fixture or mock data
              directories
            </li>
            <li>
              <code>playwright\.config\..*</code>, <code>cypress\.config\..*</code> — e2e-runner
              config files (where the value in your example lives)
            </li>
            <li>
              <code>docker-compose\.test\.yml</code> — a test-only compose file with seeded
              env vars
            </li>
          </ul>
          <textarea
            rows={4}
            className="rounded-md border border-neutral-300 px-3 py-2 font-mono text-xs"
            placeholder={"**/e2e/**\nplaywright\\.config\\..*"}
            value={gitleaksAllowPaths}
            onChange={(event) => setGitleaksAllowPaths(event.target.value)}
            disabled={!loaded}
          />
        </label>

        <div className="flex items-center gap-3">
          <button
            type="button"
            className="self-start rounded-lg bg-neutral-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-40"
            onClick={async () => {
              setGitleaksSave({ kind: "saving" });
              const res = await fetch("/api/settings/organization/gitleaks-allowlist", {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                  gitleaks_extra_stopwords: gitleaksStopwords.trim() || null,
                  gitleaks_extra_allow_paths: gitleaksAllowPaths.trim() || null,
                }),
              });
              const body = (await res.json().catch(() => ({}))) as OrgSettings & { detail?: string };
              if (res.ok) {
                setGitleaksStopwords(body.gitleaks_extra_stopwords ?? "");
                setGitleaksAllowPaths(body.gitleaks_extra_allow_paths ?? "");
                setGitleaksSave({ kind: "saved" });
              } else {
                setGitleaksSave({ kind: "error", detail: body.detail ?? `save failed (${res.status})` });
              }
            }}
            disabled={!loaded || gitleaksSave.kind === "saving"}
          >
            {gitleaksSave.kind === "saving" ? "Saving…" : "Save"}
          </button>
          {gitleaksSave.kind === "saved" && <span className="text-sm text-green-700">✓ Saved</span>}
        </div>

        {gitleaksSave.kind === "error" && (
          <div className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-800">
            <p className="font-medium">Could not save gitleaks allowlist</p>
            <p className="mt-1 break-words">{gitleaksSave.detail}</p>
          </div>
        )}
      </section>
    </div>
  );
}
