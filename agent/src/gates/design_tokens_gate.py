"""Deterministic backstop for DESIGN.md conformance: flags hardcoded color literals that don't
appear in the effective DESIGN.md's declared `colors:` token set.

This is v1, deliberately scoped to colors only -- typography/spacing/rounded conformance is a
natural follow-up once this is proven, not part of this change. Two LLM-level layers already do
most of the enforcement work (impeccable's `context.mjs` auto-load of DESIGN.md, and
IMPECCABLE_CRITIQUE_SEGMENT's craft-floor audit pass, both in graph.py); this exists as a backstop
that can't be talked out of flagging a violation the way an LLM pass sometimes can.

Checks against the STORED effective design_md (repo_design_settings.get_effective_design_md), never
the repo's live DESIGN.md file -- deliberately, not incidentally. Checking against the live file
would let a model "fix" a flagged violation by rewriting DESIGN.md's tokens to match its own code,
instead of fixing the code to match the tokens.

Mirrors test_quality_checks.non_testid_locators' scoping discipline: this module never discovers
files itself. The caller (verify_coverage) hands in an already-filtered, narrowly-scoped file set
(no vendored/generated/test paths -- see verify_coverage's own source_files filtering), and this
module reads a bounded subset of those (DESIGN_TOKENS_GATE_FILES_MAX) looking for style-bearing
extensions only.

Offline self-check: `cd agent && uv run python -m src.gates.design_tokens_gate`.
"""

from __future__ import annotations

import re

from .. import config, repo_design_settings, repo_files
from ..sandbox.provider import SandboxProvider

# Style-bearing extensions worth scanning for hardcoded color literals: stylesheets outright, plus
# component files that can carry inline `style=`, Tailwind arbitrary-value classes, or CSS-in-JS
# template literals. Plain .ts/.js are included too (styled-components/emotion live there) -- safe
# to include broadly since the check below only flags actual color-literal syntax, never bare
# identifiers, so a file with no color literals in it simply produces zero matches either way.
_STYLE_BEARING_SUFFIXES = (
    ".css", ".scss", ".sass", ".less",
    ".tsx", ".jsx", ".vue", ".svelte", ".html",
    ".ts", ".js",
)

# Hex, rgb()/rgba(), hsl()/hsla(), and oklch() -- the four color-string shapes DESIGN.md's own
# format spec allows for a token value (impeccable's reference/document.md:46: "colors accept any
# valid CSS color string"). `[^)]*` is safe here (not catastrophic-backtracking-prone) because it
# stops at the first `)`, and real CSS color functions never contain a nested `)`.
_COLOR_LITERAL_RE = re.compile(
    r"#[0-9a-fA-F]{3,8}\b"
    r"|rgba?\([^)]*\)"
    r"|hsla?\([^)]*\)"
    r"|oklch\([^)]*\)"
)


def _normalize(color: str) -> str:
    """Case/whitespace-insensitive comparison key -- "#B8422E" and "#b8422e", or "rgb(1,2,3)" and
    "rgb( 1, 2, 3 )", are the same color literal for this check's purposes."""
    return re.sub(r"\s+", "", color).lower()


def hardcoded_color_violations(
    source_files: dict[str, str], allowed_tokens: dict[str, str]
) -> dict[str, list[str]]:
    """path -> [violating color literal, ...] for every hardcoded color literal not present in
    `allowed_tokens`'s values. Pure function of (file contents, token set) -- directly testable,
    same shape as non_testid_locators."""
    allowed = {_normalize(v) for v in allowed_tokens.values()}
    violations: dict[str, list[str]] = {}
    for path, contents in source_files.items():
        found = [
            literal for match in _COLOR_LITERAL_RE.finditer(contents)
            if _normalize(literal := match.group(0)) not in allowed
        ]
        if found:
            violations[path] = found
    return violations


async def check_design_tokens(
    provider: SandboxProvider, thread_id: str, owner: str, repo: str, source_files: list[str]
) -> tuple[str, dict[str, list[str]]] | None:
    """`(feedback, violations)` when a hardcoded off-palette color literal is found, else None --
    including when there's nothing to check against (no effective DESIGN.md, or one with no
    parseable `colors:` frontmatter -- a prose-only brand doc, or an impeccable seed-mode
    placeholder). Graceful no-op in both cases is deliberate, not a gap: the settings API's
    tokens_detected hint warns about this at save time, before it silently does nothing here."""
    effective = await repo_design_settings.get_effective_design_md(owner, repo)
    if effective.content is None:
        return None
    tokens = repo_design_settings.extract_color_tokens(effective.content)
    if tokens is None:
        return None

    candidates = [p for p in source_files if p.lower().endswith(_STYLE_BEARING_SUFFIXES)]
    scanned: dict[str, str] = {}
    for path in candidates[: config.DESIGN_TOKENS_GATE_FILES_MAX]:
        contents = await repo_files.read_repo_file(provider, thread_id, path)
        if contents is not None:
            scanned[path] = contents
    if not scanned:
        return None

    violations = hardcoded_color_violations(scanned, tokens)
    if not violations:
        return None

    detail = "; ".join(
        f"{path}: {', '.join(literals[:3])}" + (f" (+{len(literals) - 3} more)" if len(literals) > 3 else "")
        for path, literals in sorted(violations.items())
    )
    feedback = (
        "These file(s) use hardcoded color literals that aren't in DESIGN.md's declared color "
        f"tokens: {detail} -- use the design system's own token references (a CSS custom property, "
        "a Tailwind theme utility class, or the token's own name) instead of a literal color value. "
        "If a color genuinely needs to be new, add it to DESIGN.md's `colors:` frontmatter first "
        "(that file is operator-authoritative -- flag the gap rather than editing it yourself)."
    )
    return feedback, violations


def _demo() -> None:
    """Offline self-check: no live DB/sandbox in this environment (same limitation as this
    package's other gates), so this exercises hardcoded_color_violations and _normalize only."""
    tokens = {"primary": "#B8422E", "neutral-bg": "rgb(250, 247, 242)"}

    clean = ".btn { background: var(--color-primary); }"
    assert hardcoded_color_violations({"a.css": clean}, tokens) == {}

    on_palette = ".btn { color: #b8422e; background: rgb( 250,247,242 ); }"
    assert hardcoded_color_violations({"a.css": on_palette}, tokens) == {}, "case/whitespace must not matter"

    off_palette = '<div style="color: #ff0000">'
    violations = hardcoded_color_violations({"a.tsx": off_palette}, tokens)
    assert violations == {"a.tsx": ["#ff0000"]}, violations

    tailwind_arbitrary = '<div className="bg-[#123abc] text-primary">'
    violations = hardcoded_color_violations({"a.tsx": tailwind_arbitrary}, tokens)
    assert violations == {"a.tsx": ["#123abc"]}, "bg-primary itself has no literal to flag"

    no_colors_at_all = "export function add(a: number, b: number) { return a + b; }"
    assert hardcoded_color_violations({"a.ts": no_colors_at_all}, tokens) == {}

    print("design_tokens_gate self-check: ok (pure functions only, no live sandbox in this environment)")


if __name__ == "__main__":  # pragma: no cover -- cd agent && uv run python -m src.gates.design_tokens_gate
    from src.gates.design_tokens_gate import _demo as _packaged_demo

    _packaged_demo()
