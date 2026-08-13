"""P3's deterministic diagram-validation gate: renders every ImplementationPlan.diagrams entry to
SVG via the mermaid CLI (`mmdc`) inside the sandbox, using rendering itself as the syntax check --
a non-zero exit is a concrete, machine-checkable failure fed back to the draft node, exactly like
every other deterministic_verify in this pipeline. Never trusts the LLM's own claim that Mermaid
source is valid.

Known limitation, stated plainly: mmdc needs a headless Chromium (via Puppeteer) inside the
sandbox image (agent/sandbox-image/Dockerfile installs it) -- this is a heavier, more
failure-prone dependency than any other gate in this pipeline, and unlike P0/P1/P2's gates, this
one has not been exercised against a rebuilt sandbox image end-to-end. A render failure caused by
a broken/missing Chromium install (not a real diagram syntax problem) is distinguished from a
genuine syntax failure where possible (see _looks_like_infra_failure) so it can be surfaced
differently, but this distinction itself is unverified in practice.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .. import git_ops, repo_files
from ..sandbox.provider import SandboxProvider

if TYPE_CHECKING:
    from ..graph import VerificationResult

DIAGRAMS_DIR = ".ai-dev-workflow/plan/diagrams"

_INFRA_FAILURE_MARKERS = (
    "command not found",
    "cannot find module",
    "failed to launch the browser process",
    "error while loading shared libraries",
)


@dataclass(frozen=True)
class DiagramRenderOutcome:
    name: str
    ok: bool
    is_infra_failure: bool
    stderr_tail: str


def _looks_like_infra_failure(stderr: str) -> bool:
    lowered = stderr.lower()
    return any(marker in lowered for marker in _INFRA_FAILURE_MARKERS)


_SAFE_DIAGRAM_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


async def _render_one(provider: SandboxProvider, thread_id: str, diagram: dict[str, Any]) -> DiagramRenderOutcome:
    name = diagram.get("name") or "diagram"
    if not _SAFE_DIAGRAM_NAME_RE.match(name):
        # diagram["name"] is model-reported (PlanDiagram.name) -- a real command-injection gap,
        # found by automated security review, if it were interpolated unquoted into the mmdc
        # shell command below without validation first. Rejected as a render failure (not a
        # syntax problem -- the draft node's feedback should ask for a filename-safe name), never
        # silently sanitized/truncated, so the failure is visible rather than papered over.
        return DiagramRenderOutcome(
            name=name,
            ok=False,
            is_infra_failure=False,
            stderr_tail=f"diagram name {name!r} must match {_SAFE_DIAGRAM_NAME_RE.pattern} (letters, digits, _, - only)",
        )

    source = diagram.get("mermaid_source") or ""
    mmd_path = f"{DIAGRAMS_DIR}/{name}.mmd"
    svg_path = f"{DIAGRAMS_DIR}/{name}.svg"

    await repo_files.write_repo_file(provider, thread_id, mmd_path, source)

    # -p points mmdc at a bundled no-sandbox Puppeteer config (see the Dockerfile) -- required to
    # run headless Chromium as a non-root container user without --cap-add=SYS_ADMIN. Paths are
    # shell-quoted even though `name` is now validated above -- DIAGRAMS_DIR is a fixed constant,
    # but quoting is cheap defense-in-depth against a future change to that constant.
    command = (
        f"npx --yes @mermaid-js/mermaid-cli -i {shlex.quote(mmd_path)} -o {shlex.quote(svg_path)} "
        f"-p /opt/ai-dev-workflow-plugins/mermaid-puppeteer-config.json 2>&1"
    )
    result = await provider.exec_in_sandbox(thread_id, command)
    stderr_tail = (result.stdout or result.stderr or "")[-2000:]
    return DiagramRenderOutcome(
        name=name, ok=result.ok, is_infra_failure=(not result.ok and _looks_like_infra_failure(stderr_tail)), stderr_tail=stderr_tail
    )


async def verify_plan_diagrams(
    thread_id: str, content_dict: dict[str, Any], _run_id: str, _baseline_commit: str | None, provider: SandboxProvider
) -> "VerificationResult":
    from ..graph import VerificationResult  # local import: graph.py imports this module

    diagrams = content_dict.get("diagrams") or []
    if not diagrams:
        return VerificationResult(passed=True, feedback="No diagrams in this draft -- nothing to validate.", report={})

    outcomes = [await _render_one(provider, thread_id, diagram) for diagram in diagrams]
    failures = [o for o in outcomes if not o.ok]

    if not failures:
        await git_ops.commit_paths(provider, thread_id, [DIAGRAMS_DIR], "ai-dev-workflow: render plan diagrams")
        return VerificationResult(
            passed=True, feedback=f"All {len(diagrams)} diagram(s) rendered successfully.", report={"rendered": [o.name for o in outcomes]}
        )

    infra_failures = [o for o in failures if o.is_infra_failure]
    if infra_failures:
        # Distinct from a real syntax problem -- the draft node retrying with "fix your Mermaid
        # syntax" feedback would be nonsensical here since the syntax was never actually checked.
        feedback = (
            f"Diagram rendering infrastructure failure (mermaid-cli/Chromium), not a diagram syntax "
            f"problem -- affected: {[o.name for o in infra_failures]}. First error: "
            f"{infra_failures[0].stderr_tail}"
        )
    else:
        feedback = "; ".join(f"{o.name}: {o.stderr_tail[-400:]}" for o in failures)

    return VerificationResult(
        passed=False,
        feedback=feedback,
        report={"failed": [o.name for o in failures], "infra_failure": bool(infra_failures)},
    )
