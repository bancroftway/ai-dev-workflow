"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

type Provider = "copilot" | "claude";

type OrgSettings = {
  provider: Provider;
  credential_configured: boolean;
  updated_at: string | null;
  updated_by: string | null;
};

type SaveState =
  | { kind: "idle" }
  | { kind: "saving" }
  | { kind: "saved" }
  | { kind: "error"; detail: string };

const PROVIDER_LABELS: Record<Provider, string> = {
  copilot: "GitHub Copilot",
  claude: "Claude Code",
};

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
  const [credentialConfigured, setCredentialConfigured] = useState(false);
  const [editingCredential, setEditingCredential] = useState(false);
  const [credentialInput, setCredentialInput] = useState("");
  const [updatedAt, setUpdatedAt] = useState<string | null>(null);
  const [updatedBy, setUpdatedBy] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [save, setSave] = useState<SaveState>({ kind: "idle" });

  useEffect(() => {
    fetch("/api/settings/organization")
      .then((res) => (res.ok ? res.json() : null))
      .then((data: OrgSettings | null) => {
        if (!data) return;
        setProvider(data.provider);
        setCredentialConfigured(data.credential_configured);
        setEditingCredential(!data.credential_configured);
        setUpdatedAt(data.updated_at);
        setUpdatedBy(data.updated_by);
      })
      .finally(() => setLoaded(true));
  }, []);

  async function saveSettings() {
    setSave({ kind: "saving" });
    const res = await fetch("/api/settings/organization", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider, credential: credentialInput.trim() || null }),
    });
    const body = (await res.json()) as OrgSettings & { detail?: string };
    if (res.ok) {
      setCredentialConfigured(body.credential_configured ?? false);
      setEditingCredential(!body.credential_configured);
      setCredentialInput("");
      setUpdatedAt(body.updated_at ?? null);
      setUpdatedBy(body.updated_by ?? null);
      setSave({ kind: "saved" });
    } else {
      if (res.status === 422) {
        // The one 422 case (sessions_api.py's put_org_settings_endpoint): switching provider
        // with no new credential supplied. The masked "configured" display would otherwise hide
        // the exact field the user needs to fill in to fix this -- reveal it.
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

        <label className="flex flex-col gap-1">
          <span className="text-sm font-medium text-neutral-700">{PROVIDER_LABELS[provider]} credential</span>
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
              placeholder={credentialConfigured ? "New credential" : `${PROVIDER_LABELS[provider]} API key or token`}
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
    </div>
  );
}
