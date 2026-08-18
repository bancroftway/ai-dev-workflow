"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";

type SaveState =
  | { kind: "idle" }
  | { kind: "saving" }
  | { kind: "saved"; secretCount: number }
  | { kind: "error"; detail: string };

/**
 * Per-repo settings. v1 section: Key Vault -- saving test-reads the vault on-behalf-of the
 * signed-in user (the agent's OBO exchange), so a successful save is proof the grant works. A
 * 403 comes back with the provider's own detail plus the role-assignment command to fix it.
 */
export default function RepoSettingsPage() {
  const params = useParams<{ owner: string; repo: string }>();
  const owner = decodeURIComponent(params.owner);
  const repo = decodeURIComponent(params.repo);

  const [vaultUri, setVaultUri] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [save, setSave] = useState<SaveState>({ kind: "idle" });

  useEffect(() => {
    const query = new URLSearchParams({ owner, repo });
    fetch(`/api/repos/vault?${query}`)
      .then((res) => (res.ok ? res.json() : null))
      .then((data: { vault_uri?: string } | null) => {
        if (data?.vault_uri) setVaultUri(data.vault_uri);
      })
      .finally(() => setLoaded(true));
  }, [owner, repo]);

  async function saveVault() {
    setSave({ kind: "saving" });
    const res = await fetch("/api/repos/vault", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ owner, repo, vaultUri }),
    });
    const body = (await res.json()) as { secret_count?: number; detail?: string };
    if (res.ok) {
      setSave({ kind: "saved", secretCount: body.secret_count ?? 0 });
    } else {
      setSave({ kind: "error", detail: body.detail ?? `save failed (${res.status})` });
    }
  }

  return (
    <div className="flex h-full w-full flex-col gap-6 p-6">
      <div>
        <Link href="/select" className="text-sm text-neutral-500 hover:text-neutral-800">
          ← Back to repositories
        </Link>
        <h1 className="mt-2 text-lg font-semibold">
          Settings — {owner}/{repo}
        </h1>
        <p className="text-sm text-neutral-500">These settings apply to your sessions on this repository.</p>
      </div>

      <section className="flex max-w-2xl flex-col gap-3 rounded-lg border border-neutral-200 p-4">
        <div>
          <h2 className="font-medium">Azure Key Vault</h2>
          <p className="mt-1 text-sm text-neutral-500">
            Secrets from this vault are injected into your app&apos;s environment when sessions run it
            (E2E tests, migrations). Access is checked as <em>you</em> — the vault must grant your
            account <code className="rounded bg-neutral-100 px-1">Key Vault Secrets User</code>; the
            service itself gets no standing access.
          </p>
        </div>
        <label className="flex flex-col gap-1">
          <span className="text-sm font-medium text-neutral-700">Vault URI</span>
          <input
            type="url"
            className="rounded-md border border-neutral-300 px-3 py-2 text-sm"
            placeholder="https://my-vault.vault.azure.net/"
            value={vaultUri}
            onChange={(event) => setVaultUri(event.target.value)}
            disabled={!loaded}
          />
        </label>
        <div className="flex items-center gap-3">
          <button
            type="button"
            className="self-start rounded-lg bg-neutral-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-40"
            onClick={saveVault}
            disabled={!loaded || !vaultUri || save.kind === "saving"}
          >
            {save.kind === "saving" ? "Checking access…" : "Save & test access"}
          </button>
          {save.kind === "saved" && (
            <span className="text-sm text-green-700">
              ✓ Vault readable as you — {save.secretCount} secret{save.secretCount === 1 ? "" : "s"} found
            </span>
          )}
        </div>
        {save.kind === "error" && (
          <div className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-800">
            <p className="font-medium">Could not read the vault as you</p>
            <p className="mt-1 break-words">{save.detail}</p>
            <p className="mt-2">
              If this is a permissions problem, have the vault owner run:
            </p>
            <pre className="mt-1 overflow-x-auto rounded bg-red-100 p-2 text-xs">
              {`az role assignment create --role "Key Vault Secrets User" \\\n  --assignee <your-upn> --scope <vault-resource-id>`}
            </pre>
          </div>
        )}
        <p className="text-xs text-neutral-400">
          Secret names map to env vars: <code>connection-string-main</code> becomes{" "}
          <code>CONNECTION_STRING_MAIN</code>.
        </p>
      </section>
    </div>
  );
}
