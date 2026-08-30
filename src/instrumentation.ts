/**
 * Boot-time configuration from Azure Key Vault (docs/superpowers/specs/2026-08-30-keyvault-config-design.md).
 *
 * Next.js calls `register()` once per server start and awaits it before serving any request, so
 * every module that reads `process.env` at import time (src/auth.ts, src/lib/agent-client.ts,
 * src/proxy.ts -- Node runtime in Next 16) sees the injected values. Every enabled secret in the
 * vault named by AZURE_CONFIG_VAULT_URI becomes an env var (`AUTH-SECRET` -> `AUTH_SECRET`);
 * a variable already present in the process environment wins, so platform-set values and shell
 * overrides stay authoritative. Unset AZURE_CONFIG_VAULT_URI = nothing happens (plain env/.env).
 * Unreachable vault = the server refuses to start, with Azure's own error text.
 *
 * The agent has the same loader in agent/src/env_bootstrap.py; keep the two in step.
 */

/** `AUTH-SECRET` -> `AUTH_SECRET` (Key Vault names allow only [A-Za-z0-9-]). */
export const envName = (secretName: string) => secretName.toUpperCase().replace(/-/g, "_");

export async function register() {
  if (process.env.NEXT_RUNTIME !== "nodejs") return;
  const vaultUri = process.env.AZURE_CONFIG_VAULT_URI;
  if (!vaultUri) return;

  // Dynamic imports keep the Azure SDK out of the edge bundle.
  const [{ DefaultAzureCredential }, { SecretClient }] = await Promise.all([
    import("@azure/identity"),
    import("@azure/keyvault-secrets"),
  ]);
  const client = new SecretClient(vaultUri, new DefaultAzureCredential());

  try {
    const names: string[] = [];
    for await (const props of client.listPropertiesOfSecrets()) {
      if (props.enabled !== false) names.push(props.name);
    }
    const pending = names.filter((name) => process.env[envName(name)] === undefined);
    const values = await Promise.all(pending.map((name) => client.getSecret(name)));
    let loaded = 0;
    for (const secret of values) {
      if (secret.value === undefined) continue;
      process.env[envName(secret.name)] = secret.value;
      loaded += 1;
    }
    const kept = names.length - pending.length;
    console.log(`[config vault] ${loaded} values loaded from ${vaultUri}${kept ? ` (${kept} already set in env, kept)` : ""}`);
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    throw new Error(
      `config vault ${vaultUri}: ${message} (locally: az login; in Azure: grant this identity 'Key Vault Secrets User' on the vault)`,
      { cause: err },
    );
  }
}
