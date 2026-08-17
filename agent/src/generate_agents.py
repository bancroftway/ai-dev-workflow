#!/usr/bin/env python
"""Auto-generate agent files from existing prompt files."""

from pathlib import Path
import yaml

# Model mapping from models.yaml
MODELS = {
    ("app-discovery", "draft"): "gpt-5.4-mini",
    ("tech-stack", "draft"): "gpt-5.4-mini",
    ("specification", "draft"): "gpt-5.4-mini",
    ("specification", "audit"): "gemini-3.6-flash",
    ("plan", "draft"): "gpt-5.4-mini",
    ("plan", "audit"): "gemini-3.6-flash",
    ("ac-to-tests", "draft"): "gpt-5.4-mini",
    ("minimal-code-to-green", "draft"): "gpt-5.4",
    ("minimal-code-to-green", "audit"): "gemini-3.6-flash",
    ("adversarial-audit", "draft"): "claude-haiku-4.5",
    ("dedup-simplify", "draft"): "gpt-5.4-mini",
    ("license-audit", "draft"): "gpt-5.4-mini",
    ("exit", "draft"): "claude-haiku-4.5",
    ("brownfield-baseline", "draft"): "gpt-5.4-mini",
    ("quality-remediation", "draft"): "gpt-5.4",
    ("security-remediation", "draft"): "gpt-5.4",
    ("finding-cluster-upgrade", "draft"): "gpt-5.4-mini",
    ("test-hardening-flake-triage", "draft"): "gpt-5.4-mini",
    ("metrics-report", "draft"): "gpt-5.4-mini",
    ("rebuild", "draft"): "gpt-5.4",
    ("e2e", "draft"): "gpt-5.4",
}

# Tool assignments: draft=write-capable, audit/readonly=read-only
DRAFT_TOOLS = ["builtin:view", "builtin:grep", "builtin:glob", "builtin:bash", "builtin:edit"]
AUDIT_TOOLS = ["builtin:view", "builtin:grep", "builtin:glob"]

def stage_role_from_filename(filename: str) -> tuple[str, str] | None:
    """Extract (stage, role) from filename like 'specification_draft.md'."""
    stem = filename[:-3]  # remove .md
    if "_draft" in stem:
        stage = stem.replace("_draft", "")
        return (stage, "draft")
    elif "_audit" in stem:
        stage = stem.replace("_audit", "")
        return (stage, "audit")
    elif "_fix" in stem:
        stage = stem.replace("_fix", "")
        return (stage, "fix")
    return None

def read_prompt(path: Path) -> str:
    """Read prompt file content."""
    return path.read_text(encoding="utf-8")

def create_agent_file(stage: str, role: str, prompt_content: str, agents_dir: Path) -> None:
    """Create agent file from prompt content."""
    agent_name = f"{stage}-{role}"
    model = MODELS.get((stage, role), "gpt-5.4-mini")
    tools = DRAFT_TOOLS if role == "draft" else AUDIT_TOOLS

    # Build description
    if role == "draft":
        desc_template = f"Draft {stage}"
    elif role == "audit":
        desc_template = f"Audit {stage}"
    else:
        desc_template = f"Fix {stage}"

    # YAML frontmatter
    frontmatter = f"""---
name: "{agent_name}"
description: "{desc_template}"
tools:
{chr(10).join(f'  - {tool}' for tool in tools)}
model: "{model}"
---

"""

    # Combine frontmatter + prompt
    agent_content = frontmatter + prompt_content

    # Write agent file
    agent_file = agents_dir / f"{agent_name}.md"
    agent_file.write_text(agent_content, encoding="utf-8")
    print(f"Created {agent_file}")

def main():
    prompts_dir = Path(__file__).parent / "prompts"
    agents_dir = Path(__file__).parent / "agents"
    agents_dir.mkdir(exist_ok=True)

    # List of prompt files to convert (skip already done)
    skip = {"specification_draft.md", "specification_audit.md", "plan_draft.md", "stack_discovery_data.md"}

    for prompt_file in sorted(prompts_dir.glob("*.md")):
        if prompt_file.name in skip or prompt_file.name == "README.md":
            continue

        result = stage_role_from_filename(prompt_file.name)
        if result is None:
            continue

        stage, role = result
        prompt_content = read_prompt(prompt_file)

        create_agent_file(stage, role, prompt_content, agents_dir)

if __name__ == "__main__":
    main()
