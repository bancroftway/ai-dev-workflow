"""Deterministic detection of an app's auth kind and the config keys it reads.

Runs host-side in the tech-stack stage (and its hydration backfill): reads appsettings*.json and a
grep of the source through the sandbox, then applies the PURE functions below. The LLM tech-stack
draft contributes its own guesses; the union is what the human approves and what seeds
repo_test_config. Nothing here executes app code -- it is text analysis only.

Pure halves (flatten_appsettings / detect_auth_kind / extract_config_keys) are self-checked offline:
`cd agent && uv run python -m src.config_inventory`.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from . import repo_files
from .sandbox.provider import SandboxProvider

logger = logging.getLogger(__name__)

# Auth-kind vocabulary, most-specific first (first hit wins). Kept as (kind, [needles]) so the
# order is the precedence: an Entra app also matches generic OIDC, so Entra must be tested first.
_AUTH_SIGNATURES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("entra", ("Microsoft.Identity.Web", "AddMicrosoftIdentityWebApp", "AddMicrosoftIdentityWebApi",
               "login.microsoftonline.com", "AzureAd", "@azure/msal")),
    ("google", (".apps.googleusercontent.com", "AddGoogle", "GoogleDefaults", "accounts.google.com")),
    ("generic-oidc", ("AddOpenIdConnect", "OpenIdConnectDefaults", "openid-client", "next-auth",
                      "@auth/core", "authug/openidconnect")),
    ("custom", ("AddIdentityCore", "AddIdentity", "UserManager", "SignInManager", "PasswordHasher",
                "IPasswordHasher", "AddAuthentication(\"Cookies\"", "CookieAuthenticationDefaults")),
)

# Config-read call shapes across .NET and JS. Each captures the key in group 1.
_CONFIG_KEY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"""Configuration\[\s*["']([^"']+)["']\s*\]"""),
    re.compile(r"""GetSection\(\s*["']([^"']+)["']\s*\)"""),
    re.compile(r"""GetValue<[^>]+>\(\s*["']([^"']+)["']\s*\)"""),
    re.compile(r"""Environment\.GetEnvironmentVariable\(\s*["']([^"']+)["']\s*\)"""),
    re.compile(r"""process\.env\.([A-Za-z_][A-Za-z0-9_]*)"""),
    re.compile(r"""process\.env\[\s*["']([A-Za-z_][A-Za-z0-9_]*)["']\s*\]"""),
)

# appsettings sections that are framework noise, not app config a tester would supply.
_APPSETTINGS_SKIP_ROOTS = frozenset({"Logging", "AllowedHosts", "Kestrel", "ConnectionStrings"})
# ...except ConnectionStrings, which IS test-relevant -- keep its children but not the bare root.
_APPSETTINGS_KEEP_CHILDREN_OF = frozenset({"ConnectionStrings"})


def flatten_appsettings(obj: Any, prefix: str = "") -> list[str]:
    """Nested appsettings JSON -> ordered leaf paths as `Section:Key`. Arrays are treated as leaves
    (their contents aren't independent config keys). Framework-noise roots are dropped."""
    keys: list[str] = []
    if not isinstance(obj, dict):
        return keys
    for k, v in obj.items():
        path = f"{prefix}:{k}" if prefix else k
        root = path.split(":", 1)[0]
        if not prefix and root in _APPSETTINGS_SKIP_ROOTS and root not in _APPSETTINGS_KEEP_CHILDREN_OF:
            continue
        if isinstance(v, dict) and v:
            keys.extend(flatten_appsettings(v, path))
        else:
            keys.append(path)
    return keys


def detect_auth_kind(texts: dict[str, str]) -> str:
    """First matching signature over the concatenated source/config text, precedence by order."""
    blob = "\n".join(texts.values())
    for kind, needles in _AUTH_SIGNATURES:
        if any(n in blob for n in needles):
            return kind
    return "none"


def extract_config_keys(texts: dict[str, str]) -> list[str]:
    """Distinct config keys read anywhere in the source, in first-seen order."""
    seen: list[str] = []
    have: set[str] = set()
    for content in texts.values():
        for pattern in _CONFIG_KEY_PATTERNS:
            for m in pattern.finditer(content):
                key = m.group(1)
                if key and key not in have:
                    have.add(key)
                    seen.append(key)
    return seen


async def inventory(provider: SandboxProvider, thread_id: str) -> tuple[str, list[str]]:
    """(auth_kind, config_keys) for the repo. appsettings keys + source-read keys, unioned; auth
    kind from source+config signatures. Best-effort: any read failure yields ("none", [])."""
    try:
        listing = await provider.exec_in_sandbox(
            thread_id,
            # appsettings + a bounded slice of source likely to carry auth wiring / config reads.
            r"cd /workspace/repo && (find . -maxdepth 4 \( -name 'appsettings*.json' -o -name 'Program.cs' "
            r"-o -name 'Startup.cs' -o -name '*.csproj' \) -not -path '*/bin/*' -not -path '*/obj/*' "
            r"-not -path '*/node_modules/*'; grep -rlE 'process\.env|AddAuthentication|AddOpenIdConnect|"
            r"Microsoft.Identity|next-auth|AddGoogle' --include='*.ts' --include='*.tsx' --include='*.cs' "
            r". 2>/dev/null | grep -v node_modules | head -40) 2>/dev/null | sort -u | head -60",
        )
    except Exception:  # noqa: BLE001
        logger.warning("config inventory listing failed for %s", thread_id, exc_info=True)
        return "none", []
    paths = [p.strip().lstrip("./") for p in (listing.stdout or "").splitlines() if p.strip()]
    texts: dict[str, str] = {}
    appsettings_keys: list[str] = []
    for path in paths[:60]:
        content = await repo_files.read_repo_file(provider, thread_id, path)
        if content is None:
            continue
        texts[path] = content
        if path.rsplit("/", 1)[-1].startswith("appsettings") and path.endswith(".json"):
            try:
                appsettings_keys.extend(flatten_appsettings(json.loads(content)))
            except json.JSONDecodeError:
                pass
    auth_kind = detect_auth_kind(texts)
    # Union appsettings keys (first) with code-read keys, de-duped.
    keys: list[str] = []
    have: set[str] = set()
    for key in appsettings_keys + extract_config_keys(texts):
        if key not in have:
            have.add(key)
            keys.append(key)
    return auth_kind, keys


def _demo() -> None:
    """`cd agent && uv run python -m src.config_inventory`."""
    flat = flatten_appsettings({
        "Logging": {"LogLevel": {"Default": "Information"}},   # noise root, dropped
        "AzureAd": {"TenantId": "", "ClientId": "", "Instance": ""},
        "ConnectionStrings": {"Ledger": ""},                   # kept (children)
        "FeatureFlags": {"NewUi": True},
    })
    assert "AzureAd:TenantId" in flat and "ConnectionStrings:Ledger" in flat and "FeatureFlags:NewUi" in flat, flat
    assert not any(k.startswith("Logging") for k in flat), flat

    assert detect_auth_kind({"Program.cs": "builder.Services.AddMicrosoftIdentityWebApi(...)"}) == "entra"
    assert detect_auth_kind({"a.ts": "GoogleProvider clientId .apps.googleusercontent.com"}) == "google"
    assert detect_auth_kind({"Program.cs": "AddOpenIdConnect(...)"}) == "generic-oidc"
    assert detect_auth_kind({"Program.cs": "services.AddIdentityCore<AppUser>()"}) == "custom"
    assert detect_auth_kind({"x": "nothing here"}) == "none"
    # entra beats generic-oidc when both present.
    assert detect_auth_kind({"p": "AddOpenIdConnect Microsoft.Identity.Web"}) == "entra"

    keys = extract_config_keys({
        "Program.cs": 'Configuration["Stripe:Key"]; builder.Configuration.GetSection("Smtp");',
        "api.ts": "const u = process.env.API_BASE_URL; const k = process.env['STRIPE_KEY'];",
    })
    assert keys == ["Stripe:Key", "Smtp", "API_BASE_URL", "STRIPE_KEY"], keys
    print("config_inventory self-check: ok")


if __name__ == "__main__":  # pragma: no cover
    _demo()
