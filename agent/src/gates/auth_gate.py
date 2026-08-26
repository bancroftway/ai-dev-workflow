"""Deterministic application-auth gate (W4): probes the RUNNING app unauthenticated and verifies
the repo's auth posture is actually enforced -- never LLM self-attestation.

Runs inside e2e_run_node while the app is still up (the only window it exists), when
repo_auth_settings' posture requires auth AND Key Vault delivered auth secrets (graph.auth_enforced
-- the same predicate that injected the auth prompt segments, so the gate never demands what the
prompts never asked for). Violations land in e2e["failed_tests"] and feed the SAME fix loop the
suite does; this gate is deliberately NOT fail-open (unlike lighthouse) -- an unauthenticated 200
on a protected route is a verified defect, not noise.

Classification is allowlist-of-proof, not blocklist:
  * protected route/API: PASS only on 401/403, or a redirect chain that leaves the app for an
    external identity provider. 2xx served locally = VIOLATION. 404/405/500 = INCONCLUSIVE,
    reported but non-blocking (ASP.NET answers 405 from routing BEFORE auth; a hallucinated
    discovered route 404s; neither proves auth either way).
  * allowlisted anonymous route: PASS on locally-served 2xx; a redirect to the IdP is a VIOLATION
    (the user explicitly required it reachable anonymously).
  * SPA dev servers (Vite, Blazor WASM, ...) serve 200 + index.html for EVERY path by design --
    detected behaviorally via a catch-all probe, after which HTML-200 on a page route is
    NOT-APPLICABLE (auth lives on the API there) and only api_routes carry the verdict.

The probe deliberately does NOT set AIDW_TEST_AUTH (the test-only sign-in seam the Playwright
suite uses): the seam being honored here would defeat the point.

Offline self-check of the pure classifier: `cd agent && uv run python -m src.gates.auth_gate`.
"""

from __future__ import annotations

import logging
import shlex
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:  # pragma: no cover -- import cycle guard (graph imports the gates)
    from ..graph import VerificationResult

logger = logging.getLogger(__name__)

_LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "[::1]", "::1")
_CURL_TIMEOUT_SECONDS = 15
_MAX_PROBES = 40  # routes + api_routes combined -- a discovery pass gone wild must not stall e2e
# The catch-all probe path: no real app serves this; a 200 for it means the server answers 200
# for EVERYTHING (an SPA shell / dev-server fallback), so page-route 200s prove nothing there.
_CATCHALL_PROBE_PATH = "/__aidw_auth_probe__/no-such-route"


def is_allowlisted(route: str, anonymous_routes: list[str]) -> bool:
    """fnmatchCASE, not fnmatch: fnmatch normalizes case per-OS, so a win32 dev-box self-check
    would pass case-insensitively while the Linux sandbox matches exactly."""
    return any(fnmatchcase(route, pattern) for pattern in anonymous_routes)


def classify_response(
    status: int,
    final_url: str,
    *,
    allowlisted: bool,
    spa_shell: bool = False,
) -> str:
    """Pure verdict for one probed route: 'protected' | 'anonymous_ok' | 'violation' |
    'inconclusive' | 'not_applicable'. `final_url` is curl's %{url_effective} after -L."""
    final_host = (urlsplit(final_url).hostname or "").lower() if final_url else ""
    left_app = bool(final_host) and final_host not in _LOCAL_HOSTS

    if allowlisted:
        if left_app:
            return "violation"  # the user required this route reachable WITHOUT sign-in
        return "anonymous_ok" if 200 <= status < 300 else "inconclusive"

    if left_app:
        return "protected"  # redirect chain left for the IdP -- exactly what enforcement looks like
    if status in (401, 403):
        return "protected"
    if 300 <= status < 400:
        # -L follows redirects, so landing here on a 3xx means the chain ended locally on one --
        # curl's --max-redirs exhausted or a redirect loop. Not proof either way.
        return "inconclusive"
    if 200 <= status < 300:
        return "not_applicable" if spa_shell else "violation"
    # 404/405/500/0: routing/method/crash answered before auth could -- reported, never a pass.
    return "inconclusive"


async def _probe(provider: Any, thread_id: str, url: str) -> tuple[int, str]:
    """(status, final_url) for one unauthenticated GET, following redirects. status 0 = no answer."""
    result = await provider.exec_in_sandbox(
        thread_id,
        f"curl -s -L --max-redirs 5 --max-time {_CURL_TIMEOUT_SECONDS} "
        f"-o /dev/null -w '%{{http_code}} %{{url_effective}}' {shlex.quote(url)} 2>/dev/null || true",
    )
    parts = ((result.stdout or "").strip() or "0").split(None, 1)
    try:
        status = int(parts[0])
    except ValueError:
        status = 0
    return status, parts[1] if len(parts) > 1 else url


async def check_auth(
    provider: Any,
    thread_id: str,
    *,
    ui_port: int,
    routes: list[str],
    api_routes: list[str],
    service_urls: list[str],
    anonymous_routes: list[str],
) -> "VerificationResult":
    """Probes every discovered page route (UI port) and API route (each booted service, falling
    back to the UI port when the stack serves both from one process). See module docstring for the
    verdict rules. `report` carries per-route verdicts for the exit report and the compliance
    auditor; `feedback` names the violations the fix loop must repair."""
    from ..graph import VerificationResult

    base = f"http://127.0.0.1:{ui_port}"
    catchall_status, catchall_final = await _probe(provider, thread_id, f"{base}{_CATCHALL_PROBE_PATH}")
    # A catch-all 200 answered locally = SPA shell. A catch-all that redirects to the IdP is
    # server-side auth over everything -- the strongest possible pass signal, not an SPA.
    catchall_host = (urlsplit(catchall_final).hostname or "").lower() if catchall_final else ""
    spa_shell = 200 <= catchall_status < 300 and (not catchall_host or catchall_host in _LOCAL_HOSTS)

    page_routes = [r for r in routes if isinstance(r, str) and r.startswith("/")]
    api_paths = [r for r in api_routes if isinstance(r, str) and r.startswith("/")]
    # API probes go to every booted service (Blazor-style absolute API base means the UI port
    # would 404 them -- a false pass); one-process stacks fall back to the UI port itself.
    api_bases = service_urls or [base]

    verdicts: list[dict[str, Any]] = []
    probes: list[tuple[str, str, bool]] = []  # (label, url, allowlisted)
    for route in page_routes:
        probes.append((route, f"{base}{route}", is_allowlisted(route, anonymous_routes)))
    for path in api_paths:
        for api_base in api_bases:
            probes.append((path, f"{api_base}{path}", is_allowlisted(path, anonymous_routes)))
    dropped = max(0, len(probes) - _MAX_PROBES)
    probes = probes[:_MAX_PROBES]

    for label, url, allowlisted in probes:
        status, final_url = await _probe(provider, thread_id, url)
        verdict = classify_response(status, final_url, allowlisted=allowlisted, spa_shell=spa_shell)
        verdicts.append({
            "route": label, "url": url, "status": status, "final_url": final_url,
            "allowlisted": allowlisted, "verdict": verdict,
        })

    violations = [v for v in verdicts if v["verdict"] == "violation"]
    inconclusive = [v for v in verdicts if v["verdict"] == "inconclusive"]
    report = {
        "spa_shell": spa_shell,
        "probed": len(verdicts),
        "dropped_over_cap": dropped,
        "anonymous_routes": anonymous_routes,
        "verdicts": verdicts,
    }
    if violations:
        lines = "; ".join(
            (
                f"{v['route']} answered {v['status']} unauthenticated (must demand sign-in)"
                if not v["allowlisted"]
                else f"{v['route']} is on the anonymous allowlist but redirected to sign-in ({v['final_url']})"
            )
            for v in violations[:8]
        )
        return VerificationResult(
            passed=False,
            feedback=(
                f"authentication enforcement check failed on {len(violations)} route(s): {lines}. "
                "Every non-allowlisted route and API endpoint must answer 401/403 or redirect to "
                "the identity provider for unauthenticated requests; allowlisted anonymous routes "
                "must stay reachable. Enforce centrally (middleware/authorization filter), and keep "
                "the AIDW_TEST_AUTH test seam out of the default configuration."
            ),
            report=report,
        )
    feedback = f"auth enforcement verified on {len(verdicts)} probe(s)"
    if spa_shell:
        feedback += " (SPA shell detected -- page-route 200s not applicable, APIs carried the verdict)"
    if inconclusive:
        feedback += f"; {len(inconclusive)} inconclusive (404/405/5xx before auth) -- reported, not blocking"
    return VerificationResult(passed=True, feedback=feedback, report=report)


def _demo() -> None:
    """Self-check of the pure classifier: `cd agent && uv run python -m src.gates.auth_gate`."""
    # Protected route, properly enforced.
    assert classify_response(401, "http://127.0.0.1:3000/x", allowlisted=False) == "protected"
    assert classify_response(403, "http://localhost:3000/x", allowlisted=False) == "protected"
    # Redirect chain that LEFT the app for the IdP (curl followed it; final host is external).
    assert classify_response(200, "https://login.microsoftonline.com/common/oauth2/v2.0/authorize?...", allowlisted=False) == "protected"
    # The two false-pass traps the adversarial audit named: 405 from routing-before-auth, 404 from
    # a hallucinated discovered route -- inconclusive, never a silent pass.
    assert classify_response(405, "http://127.0.0.1:5000/api/orders", allowlisted=False) == "inconclusive"
    assert classify_response(404, "http://127.0.0.1:5000/api/nope", allowlisted=False) == "inconclusive"
    assert classify_response(500, "http://127.0.0.1:5000/api/boom", allowlisted=False) == "inconclusive"
    assert classify_response(0, "", allowlisted=False) == "inconclusive"
    # THE violation: unauthenticated 2xx served locally on a protected route.
    assert classify_response(200, "http://127.0.0.1:3000/expenses", allowlisted=False) == "violation"
    # ...unless the server is an SPA shell, where a page-route 200 proves nothing.
    assert classify_response(200, "http://127.0.0.1:5173/expenses", allowlisted=False, spa_shell=True) == "not_applicable"
    # Allowlisted anonymous routes: 2xx is the requirement; a redirect to sign-in breaks it.
    assert classify_response(200, "http://127.0.0.1:3000/health", allowlisted=True) == "anonymous_ok"
    assert classify_response(200, "https://login.microsoftonline.com/...", allowlisted=True) == "violation"

    # Allowlist semantics: fnmatchCASE (Linux prod), subtree spelled explicitly.
    assert is_allowlisted("/health", ["/health"])
    assert is_allowlisted("/health/live", ["/health*"])
    assert not is_allowlisted("/health/live", ["/health"]), "bare /health must NOT cover the subtree"
    assert not is_allowlisted("/Admin", ["/admin"]), "matching must be case-sensitive like the Linux sandbox"
    print("auth_gate self-check: ok")


if __name__ == "__main__":  # pragma: no cover -- cd agent && uv run python -m src.gates.auth_gate
    _demo()
