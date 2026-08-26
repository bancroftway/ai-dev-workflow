"""Deterministic README structure check (W7) -- standard-readme, split hard vs advisory.

HARD requirements (returned by readme_problems; a run still failing them after the readme leg's
in-node retry laps blocks the merge via exit's readiness downgrade): README.md exists, is
non-empty, has an H1 title, has Install, Usage and License sections, and License is the LAST
section. These are the standard-readme requirements a generated repo has no excuse to miss.

ADVISORY findings (readme_advisories; reported in the leg's feedback, NEVER blocking): short
description length, ToC anchor resolution, optional-section ordering. Blocking a merge on GitHub
slugification artifacts (emoji, code spans, duplicate headings, `&` -> `--`) is disproportionate
-- three LLM laps chasing a double hyphen, then a blocked merge over a formatting nit.

Pure halves are offline self-checked: `cd agent && uv run python -m src.gates.readme_gate`.
"""

from __future__ import annotations

import re
from typing import Any

# Section titles that satisfy each hard requirement (case-insensitive H2 match). standard-readme
# fixes the names; "Installation" is accepted for Install because refusing a merge over that one
# word helps nobody.
_HARD_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Install", ("install", "installation")),
    ("Usage", ("usage",)),
    ("License", ("license", "licence")),
)

_H1_RE = re.compile(r"^# \S", re.MULTILINE)
_H2_RE = re.compile(r"^## +(.+?)\s*$", re.MULTILINE)


def _h2_titles(markdown: str) -> list[str]:
    """H2 titles outside fenced code blocks, in order."""
    titles: list[str] = []
    in_fence = False
    for line in markdown.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _H2_RE.match(line)
        if match:
            titles.append(match.group(1).strip())
    return titles


def readme_problems(markdown: str | None) -> list[str]:
    """HARD standard-readme violations -- empty list means the README passes the blocking bar."""
    if not markdown or not markdown.strip():
        return ["README.md is missing or empty"]
    problems: list[str] = []
    if not _H1_RE.search(markdown):
        problems.append("README.md has no H1 title (`# <project name>` on the first line)")
    titles = _h2_titles(markdown)
    lowered = [t.lower().strip("`*_ ") for t in titles]
    for label, accepted in _HARD_SECTIONS:
        if not any(any(t.startswith(a) for a in accepted) for t in lowered):
            problems.append(f"README.md is missing a `## {label}` section (standard-readme requires it)")
    license_indexes = [
        i for i, t in enumerate(lowered) if any(t.startswith(a) for a in ("license", "licence"))
    ]
    if license_indexes and license_indexes[-1] != len(lowered) - 1:
        problems.append("the `## License` section must be the LAST section of README.md")
    return problems


def _github_slug(heading: str) -> str:
    """GitHub's anchor slugification: lowercase, strip everything but word chars/spaces/hyphens,
    spaces to hyphens. (Duplicate suffixing is handled by the caller.)"""
    text = re.sub(r"`([^`]*)`", r"\1", heading)  # code spans contribute their text
    text = re.sub(r"[^\w\- ]", "", text.lower())
    return text.replace(" ", "-")


def readme_advisories(markdown: str | None) -> list[str]:
    """Non-blocking polish findings, reported into the leg's feedback only."""
    if not markdown or not markdown.strip():
        return []
    advisories: list[str] = []
    lines = markdown.splitlines()

    # Short description: standard-readme wants one <120-char line right after the title block.
    h1_index = next((i for i, line in enumerate(lines) if line.startswith("# ")), None)
    if h1_index is not None:
        description = next(
            (line for line in lines[h1_index + 1:] if line.strip() and not line.startswith(("#", "[", "!", ">"))),
            None,
        )
        if description is not None and len(description.strip()) >= 120:
            advisories.append("the short description line after the title should be under 120 characters")

    # ToC anchors that resolve to no heading. Duplicate-heading suffixes (-1, -2) accepted.
    slugs: dict[str, int] = {}
    known: set[str] = set()
    in_fence = False
    for line in lines:
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not re.match(r"^#{1,6} ", line):
            continue
        slug = _github_slug(re.sub(r"^#{1,6} ", "", line).strip())
        count = slugs.get(slug, 0)
        slugs[slug] = count + 1
        known.add(slug if count == 0 else f"{slug}-{count}")
    for anchor in re.findall(r"\]\(#([^)]+)\)", markdown):
        if anchor not in known:
            advisories.append(f"table-of-contents link `#{anchor}` resolves to no heading")
    return advisories


async def check_readme(provider: Any, thread_id: str) -> tuple[list[str], list[str]]:
    """(hard_problems, advisories) for the repo's README.md. An empty file reads as `\"\"`, not
    None (repo_files returns \"\" for existing-but-empty), so the falsy check covers both."""
    from .. import repo_files

    raw = await repo_files.read_repo_file(provider, thread_id, "README.md")
    return readme_problems(raw), readme_advisories(raw)


def _demo() -> None:
    """Self-check: `cd agent && uv run python -m src.gates.readme_gate`."""
    good = (
        "# widget-api\n\nA tiny widget service.\n\n"
        "## Table of Contents\n\n- [Install](#install)\n- [Usage](#usage)\n- [License](#license)\n\n"
        "## Install\n\n```sh\nnpm install\n```\n\n"
        "## Usage\n\n```sh\nnpm start\n```\n\n"
        "## Contributing\n\nPRs accepted.\n\n"
        "## License\n\nMIT\n"
    )
    assert readme_problems(good) == [], readme_problems(good)
    assert readme_advisories(good) == [], readme_advisories(good)

    assert readme_problems(None) == ["README.md is missing or empty"]
    assert readme_problems("   \n") == ["README.md is missing or empty"]
    missing = readme_problems("# x\n\n## Usage\n\ntext\n")
    assert any("Install" in p for p in missing) and any("License" in p for p in missing), missing
    # License present but not last -> hard problem.
    unordered = readme_problems("# x\n\n## Install\n\na\n\n## License\n\nMIT\n\n## Usage\n\nb\n")
    assert any("LAST" in p for p in unordered), unordered
    # "Installation" satisfies Install; headings inside code fences don't count as sections.
    assert readme_problems("# x\n\n## Installation\n\na\n\n## Usage\n\nb\n\n## License\n\nMIT\n") == []
    fenced = "# x\n\n```md\n## License\n```\n\n## Install\n\na\n\n## Usage\n\nb\n\n## License\n\nMIT\n"
    assert readme_problems(fenced) == [], readme_problems(fenced)

    # Advisory, never blocking: a broken ToC anchor and a long description.
    broken_toc = good.replace("(#usage)", "(#useage)")
    assert readme_problems(broken_toc) == []
    assert any("useage" in a for a in readme_advisories(broken_toc))
    long_desc = good.replace("A tiny widget service.", "A" * 130)
    assert any("120" in a for a in readme_advisories(long_desc))
    # Slugification edge cases resolve (code spans, &, emoji-adjacent punctuation).
    tricky = (
        "# x\n\nshort.\n\n- [Install & Run](#install--run)\n- [`npm` tips](#npm-tips)\n\n"
        "## Install & Run\n\na\n\n## `npm` tips\n\nb\n\n## Usage\n\nc\n\n## License\n\nMIT\n"
    )
    assert readme_advisories(tricky) == [], readme_advisories(tricky)
    print("readme_gate self-check: ok")


if __name__ == "__main__":  # pragma: no cover -- cd agent && uv run python -m src.gates.readme_gate
    _demo()
