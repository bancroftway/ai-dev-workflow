"""Boot a local fake OpenID Connect provider for an OIDC app's e2e, and hand back the env overrides
that point the app at it.

For an app whose auth_kind is entra/google/generic-oidc AND that has declared test users, this
replaces the real identity provider during e2e: the app's authority config is overridden to a local
`oidc-provider` (agent/sandbox-image/fakeidp) preloaded with the test users, so Playwright completes
real redirect logins per persona with no tenant, client secret, consent, or MFA. Custom-auth apps
use the seam path instead (no IdP); apps with no test users are unaffected.

The pure halves (should_run / deterministic_oid / build_config / env_overrides / issuer_url) are
self-checked offline: `cd agent && uv run python -m src.fake_idp`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import shlex
from typing import Any

from .sandbox.provider import SandboxProvider

logger = logging.getLogger(__name__)

SERVER_JS = "/opt/aidw/fakeidp/server.js"
CONFIG_PATH = "/home/vscode/.aidw-fakeidp.json"           # outside the repo tree, like APP_ENV_PATH
ISSUER_PATH = "/aidw/v2.0"                                 # where MS-style config composes discovery
CLIENT_ID = "aidw-test-client"
CLIENT_SECRET = "aidw-test-secret"
# Named to match e2e's existing service-pid kill glob (agent-work/e2e-service-*.pid), so teardown
# reaps the IdP with no extra code.
PID_PATH = "agent-work/e2e-service-idp.pid"
LOG_PATH = "agent-work/e2e-service-idp.log"

# auth kinds that mean "the app delegates sign-in to an OIDC provider we can stand in for".
OIDC_KINDS = frozenset({"entra", "google", "generic-oidc"})

_READY_ATTEMPTS = 20
_READY_SLEEP_SECONDS = 1


def should_run(auth_kind: str, users: list[dict[str, Any]]) -> bool:
    return auth_kind in OIDC_KINDS and bool(users)


def deterministic_oid(name: str) -> str:
    """A stable GUID per user name, so a persona's oid is the same across boots/resumes (the app
    may store it). Derived from the name -- not random -- and shaped as a v4-looking GUID."""
    h = hashlib.sha1(name.encode("utf-8")).hexdigest()
    return f"{h[0:8]}-{h[8:12]}-4{h[13:16]}-a{h[17:20]}-{h[20:32]}"


def issuer_url(port: int) -> str:
    # host is localhost (NOT 127.0.0.1) to match the browsed BASE_URL origin -- see server.js.
    return f"http://localhost:{port}{ISSUER_PATH}"


def build_config(users: list[dict[str, Any]]) -> dict[str, Any]:
    """The users.json server.js reads: one entry per declared user with a stable oid."""
    return {
        "issuerPath": ISSUER_PATH,
        "clientId": CLIENT_ID,
        "clientSecret": CLIENT_SECRET,
        "users": [
            {
                "oid": deterministic_oid(u["name"]),
                "name": u["name"],
                "email": u.get("email") or "",
                "roles": u.get("roles") or [],
            }
            for u in users if u.get("name")
        ],
    }


def env_overrides(auth_kind: str, port: int) -> dict[str, str]:
    """Env vars that repoint the app's auth at the fake IdP. entra apps read the AzureAd section
    (Instance + TenantId compose to the issuer); other OIDC apps read a generic authority. AIDW_IDP_URL
    is always set so the Playwright suite knows where to drive the login."""
    authority = issuer_url(port)
    common = {"AIDW_IDP_URL": authority}
    if auth_kind == "entra":
        return {
            **common,
            # Instance + "/" + TenantId + "/v2.0" == authority (see ISSUER_PATH = /aidw/v2.0).
            "AzureAd__Instance": f"http://localhost:{port}/",
            "AzureAd__TenantId": "aidw",
            "AzureAd__ClientId": CLIENT_ID,
            "AzureAd__ClientSecret": CLIENT_SECRET,
        }
    return {
        **common,
        "OIDC_AUTHORITY": authority,
        "OIDC_CLIENT_ID": CLIENT_ID,
        "OIDC_CLIENT_SECRET": CLIENT_SECRET,
    }


async def start(
    provider: SandboxProvider, thread_id: str, users: list[dict[str, Any]], auth_kind: str, port: int
) -> dict[str, str]:
    """Write the users config, boot the IdP backgrounded, wait for its discovery doc, and return the
    env overrides. Raises on a boot/readiness failure so the caller can fall back to the seam path."""
    config_json = json.dumps(build_config(users))
    encoded = base64.b64encode(config_json.encode("utf-8")).decode("ascii")
    write = await provider.exec_in_sandbox(
        thread_id, f"umask 077 && echo {encoded} | base64 -d > {shlex.quote(CONFIG_PATH)}"
    )
    if not write.ok:
        raise RuntimeError(f"could not write fake IdP config: {write.stderr}")

    boot_cmd = (
        f"setsid nohup node {shlex.quote(SERVER_JS)} --port {port} --config {shlex.quote(CONFIG_PATH)} "
        f"> {shlex.quote(LOG_PATH)} 2>&1 & echo $! > {shlex.quote(PID_PATH)}"
    )
    launch = await provider.exec_in_sandbox(thread_id, boot_cmd)
    if not launch.ok:
        raise RuntimeError(f"could not launch fake IdP: {launch.stderr}")

    discovery = f"{issuer_url(port)}/.well-known/openid-configuration"
    for _ in range(_READY_ATTEMPTS):
        probe = await provider.exec_in_sandbox(
            thread_id, f"curl -s -o /dev/null -w '%{{http_code}}' --max-time 5 {shlex.quote(discovery)} || true"
        )
        if (probe.stdout or "").strip() == "200":
            logger.info("fake IdP ready for %s at %s", thread_id, issuer_url(port))
            return env_overrides(auth_kind, port)
        import asyncio
        await asyncio.sleep(_READY_SLEEP_SECONDS)
    tail = await provider.exec_in_sandbox(thread_id, f"tail -c 1500 {shlex.quote(LOG_PATH)} 2>/dev/null || true")
    raise RuntimeError(f"fake IdP never answered discovery at {discovery} -- log: {tail.stdout}")


def _demo() -> None:
    """`cd agent && uv run python -m src.fake_idp`."""
    assert should_run("entra", [{"name": "A"}]) and not should_run("entra", [])
    assert not should_run("custom", [{"name": "A"}]) and not should_run("none", [{"name": "A"}])
    oid = deterministic_oid("Ada Admin")
    assert oid == deterministic_oid("Ada Admin") and len(oid) == 36 and oid[14] == "4", oid
    cfg = build_config([{"name": "Ada", "email": "ada@test.local", "roles": ["Admin"]}, {"name": "", "roles": []}])
    assert len(cfg["users"]) == 1 and cfg["users"][0]["oid"] == deterministic_oid("Ada"), cfg
    entra = env_overrides("entra", 9400)
    assert entra["AzureAd__Instance"] == "http://localhost:9400/" and entra["AzureAd__TenantId"] == "aidw"
    assert entra["AIDW_IDP_URL"] == "http://localhost:9400/aidw/v2.0"
    generic = env_overrides("generic-oidc", 9400)
    assert generic["OIDC_AUTHORITY"] == "http://localhost:9400/aidw/v2.0" and "AzureAd__Instance" not in generic
    print("fake_idp self-check: ok")


if __name__ == "__main__":  # pragma: no cover
    _demo()
