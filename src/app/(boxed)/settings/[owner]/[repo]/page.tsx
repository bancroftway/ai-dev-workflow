"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";

type SaveState =
  | { kind: "idle" }
  | { kind: "saving" }
  | { kind: "saved"; secretCount: number }
  | { kind: "error"; detail: string };

type SelectionEntry = { name: string; env_name: string | null };

/** Advisory client-side mirror of the agent's ENV_NAME_RE -- the agent re-validates on save. */
const ENV_NAME_RE = /^[A-Za-z_][A-Za-z0-9_]{0,127}$/;

/** Mirror of keyvault.secret_name_to_env: `connection-string-main` -> `CONNECTION_STRING_MAIN`. */
function autoMapEnvName(name: string): string {
  const env = name.toUpperCase().replace(/-/g, "_");
  return /^[0-9]/.test(env) ? `_${env}` : env;
}

/**
 * Per-repo settings. Key Vault -- saving test-reads the vault on-behalf-of the signed-in user
 * (the agent's OBO exchange), so a successful save is proof the grant works; after that the
 * secret picker chooses which secrets reach the sandbox and under which env names. Application
 * authentication is repo-scoped (property of the codebase, shared by teammates) and enforced by
 * the e2e auth gate only when the vault actually supplies auth secrets.
 */
export default function RepoSettingsPage() {
  const params = useParams<{ owner: string; repo: string }>();
  const owner = decodeURIComponent(params.owner);
  const repo = decodeURIComponent(params.repo);

  const [vaultUri, setVaultUri] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [save, setSave] = useState<SaveState>({ kind: "idle" });

  // Secret picker: names from the vault + the user's checked subset with env-name overrides.
  const [secretNames, setSecretNames] = useState<string[] | null>(null);
  const [secretsError, setSecretsError] = useState<string | null>(null);
  const [selection, setSelection] = useState<Map<string, string>>(new Map()); // name -> env override ("" = automap)
  const [hasSavedSelection, setHasSavedSelection] = useState(false);
  const [selectionSave, setSelectionSave] = useState<SaveState>({ kind: "idle" });

  // Application authentication.
  const [authMode, setAuthMode] = useState<"required" | "anonymous_list" | "none">("required");
  const [anonRoutes, setAnonRoutes] = useState("");
  const [authSave, setAuthSave] = useState<SaveState>({ kind: "idle" });

  const loadSecrets = useCallback(() => {
    setSecretsError(null);
    fetch("/api/repos/vault/secrets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ owner, repo }),
    })
      .then(async (res) => {
        const body = (await res.json()) as {
          names?: string[];
          selection?: SelectionEntry[] | null;
          detail?: string;
        };
        if (!res.ok) {
          // 404 = no vault saved yet -- the picker simply stays hidden.
          if (res.status !== 404) setSecretsError(body.detail ?? `could not list secrets (${res.status})`);
          return;
        }
        setSecretNames(body.names ?? []);
        if (body.selection != null) {
          setHasSavedSelection(true);
          setSelection(new Map(body.selection.map((entry) => [entry.name, entry.env_name ?? ""])));
        } else {
          // No selection saved = everything is exposed today; reflect that as all-checked.
          setSelection(new Map((body.names ?? []).map((name) => [name, ""])));
        }
      })
      .catch(() => setSecretsError("could not reach the server"));
  }, [owner, repo]);

  useEffect(() => {
    const query = new URLSearchParams({ owner, repo });
    fetch(`/api/repos/vault?${query}`)
      .then((res) => (res.ok ? res.json() : null))
      .then((data: { vault_uri?: string } | null) => {
        if (data?.vault_uri) {
          setVaultUri(data.vault_uri);
          loadSecrets();
        }
      })
      .finally(() => setLoaded(true));
    fetch(`/api/repos/auth-settings?${query}`)
      .then((res) => (res.ok ? res.json() : null))
      .then((data: { auth_mode?: string; anonymous_routes?: string[] } | null) => {
        if (data?.auth_mode === "required" || data?.auth_mode === "anonymous_list" || data?.auth_mode === "none") {
          setAuthMode(data.auth_mode);
        }
        if (data?.anonymous_routes?.length) setAnonRoutes(data.anonymous_routes.join("\n"));
      })
      .catch(() => undefined);
  }, [owner, repo, loadSecrets]);

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
      loadSecrets();
    } else {
      setSave({ kind: "error", detail: body.detail ?? `save failed (${res.status})` });
    }
  }

  function toggleSecret(name: string) {
    setSelection((prev) => {
      const next = new Map(prev);
      if (next.has(name)) next.delete(name);
      else next.set(name, "");
      return next;
    });
  }

  function setOverride(name: string, value: string) {
    setSelection((prev) => {
      const next = new Map(prev);
      if (next.has(name)) next.set(name, value);
      return next;
    });
  }

  const invalidOverrides = [...selection.entries()].filter(
    ([, override]) => override !== "" && !ENV_NAME_RE.test(override),
  );

  async function saveSelection() {
    setSelectionSave({ kind: "saving" });
    const payload = [...selection.entries()].map(([name, override]) => ({
      name,
      env_name: override || null,
    }));
    const res = await fetch("/api/repos/vault/selection", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ owner, repo, selection: payload }),
    });
    const body = (await res.json()) as { secret_count?: number; detail?: string };
    if (res.ok) {
      setHasSavedSelection(true);
      setSelectionSave({ kind: "saved", secretCount: body.secret_count ?? payload.length });
    } else {
      setSelectionSave({ kind: "error", detail: body.detail ?? `save failed (${res.status})` });
    }
  }

  async function saveAuth() {
    setAuthSave({ kind: "saving" });
    const routes = anonRoutes
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
    const res = await fetch("/api/repos/auth-settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        owner,
        repo,
        authMode,
        anonymousRoutes: authMode === "anonymous_list" ? routes : [],
      }),
    });
    const body = (await res.json()) as { detail?: string };
    if (res.ok) {
      setAuthSave({ kind: "saved", secretCount: 0 });
    } else {
      setAuthSave({ kind: "error", detail: body.detail ?? `save failed (${res.status})` });
    }
  }

  return (
    <div className="flex h-full w-full flex-col gap-6 overflow-y-auto p-6">
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

        {secretsError && (
          <p className="text-sm text-red-700">{secretsError}</p>
        )}
        {secretNames && (
          <div className="flex flex-col gap-2 border-t border-neutral-200 pt-3">
            <div>
              <h3 className="text-sm font-medium text-neutral-700">Secrets exposed to the sandbox</h3>
              <p className="mt-0.5 text-xs text-neutral-500">
                Checked secrets become env vars in the app&apos;s environment. Env name is editable —
                e.g. map <code>client-id</code> to <code>AzureAd__ClientId</code> so .NET config
                binding picks it up without touching appsettings.json.
                {!hasSavedSelection && " No selection saved yet: every secret is currently exposed."}
              </p>
            </div>
            {secretNames.length === 0 && <p className="text-sm text-neutral-400">The vault has no enabled secrets.</p>}
            <ul className="flex flex-col gap-1">
              {secretNames.map((name) => {
                const checked = selection.has(name);
                const override = selection.get(name) ?? "";
                const invalid = checked && override !== "" && !ENV_NAME_RE.test(override);
                return (
                  <li key={name} className="flex items-center gap-2">
                    <label className="flex min-w-0 flex-1 items-center gap-2 text-sm">
                      <input type="checkbox" checked={checked} onChange={() => toggleSecret(name)} />
                      <code className="truncate text-xs">{name}</code>
                    </label>
                    <span className="text-xs text-neutral-400">→</span>
                    <input
                      type="text"
                      className={`w-56 rounded border px-2 py-1 font-mono text-xs ${invalid ? "border-red-400" : "border-neutral-300"}`}
                      placeholder={autoMapEnvName(name)}
                      value={override}
                      onChange={(event) => setOverride(name, event.target.value)}
                      disabled={!checked}
                    />
                  </li>
                );
              })}
            </ul>
            <div className="flex items-center gap-3">
              <button
                type="button"
                className="self-start rounded-lg bg-neutral-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-40"
                onClick={saveSelection}
                disabled={selectionSave.kind === "saving" || invalidOverrides.length > 0}
              >
                {selectionSave.kind === "saving" ? "Saving…" : "Save selection"}
              </button>
              {selectionSave.kind === "saved" && (
                <span className="text-sm text-green-700">✓ Selection saved ({selectionSave.secretCount} exposed)</span>
              )}
              {invalidOverrides.length > 0 && (
                <span className="text-xs text-red-700">
                  Env names must match [A-Za-z_][A-Za-z0-9_]* (e.g. AzureAd__ClientId)
                </span>
              )}
            </div>
            {selectionSave.kind === "error" && (
              <p className="text-sm text-red-700">{selectionSave.detail}</p>
            )}
          </div>
        )}
      </section>

      <section className="flex max-w-2xl flex-col gap-3 rounded-lg border border-neutral-200 p-4">
        <div>
          <h2 className="font-medium">Application authentication</h2>
          <p className="mt-1 text-sm text-neutral-500">
            Repo-wide (shared with teammates). When authentication is required, generated apps must
            protect every route and API endpoint with Entra ID sign-in, and the e2e auth gate
            verifies it by probing the running app unauthenticated before a merge is allowed.
            Enforcement activates only when the Key Vault above supplies the app&apos;s auth secrets
            (ClientId, ClientSecret, TenantId).
          </p>
        </div>
        <label className="flex items-start gap-2 text-sm">
          <input
            type="radio"
            name="authMode"
            checked={authMode === "required"}
            onChange={() => setAuthMode("required")}
            className="mt-0.5"
          />
          <span>
            <span className="font-medium">Require authentication on all routes</span>{" "}
            <span className="text-neutral-500">(default — enterprise posture)</span>
          </span>
        </label>
        <label className="flex items-start gap-2 text-sm">
          <input
            type="radio"
            name="authMode"
            checked={authMode === "anonymous_list"}
            onChange={() => setAuthMode("anonymous_list")}
            className="mt-0.5"
          />
          <span className="flex-1">
            <span className="font-medium">Allow specific anonymous routes</span>
            {authMode === "anonymous_list" && (
              <textarea
                className="mt-2 w-full rounded-md border border-neutral-300 px-3 py-2 font-mono text-xs"
                rows={4}
                placeholder={"/\n/health*"}
                value={anonRoutes}
                onChange={(event) => setAnonRoutes(event.target.value)}
              />
            )}
            {authMode === "anonymous_list" && (
              <span className="mt-1 block text-xs text-neutral-400">
                One pattern per line, must start with /. Use <code>/health*</code> for a subtree — a
                bare <code>*</code> is rejected (use &quot;No enforcement&quot; instead).
              </span>
            )}
          </span>
        </label>
        <label className="flex items-start gap-2 text-sm">
          <input
            type="radio"
            name="authMode"
            checked={authMode === "none"}
            onChange={() => setAuthMode("none")}
            className="mt-0.5"
          />
          <span>
            <span className="font-medium">No authentication enforcement</span>{" "}
            <span className="text-neutral-500">(public apps, demos, network-level auth)</span>
          </span>
        </label>
        <div className="flex items-center gap-3">
          <button
            type="button"
            className="self-start rounded-lg bg-neutral-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-40"
            onClick={saveAuth}
            disabled={authSave.kind === "saving"}
          >
            {authSave.kind === "saving" ? "Saving…" : "Save"}
          </button>
          {authSave.kind === "saved" && <span className="text-sm text-green-700">✓ Saved</span>}
        </div>
        {authSave.kind === "error" && <p className="text-sm text-red-700">{authSave.detail}</p>}
      </section>
    </div>
  );
}
