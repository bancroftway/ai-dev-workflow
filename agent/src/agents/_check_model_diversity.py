#!/usr/bin/env python
"""Guardrail: assert draft/audit stage pairs have different models.

When models.yaml retired, model selection moved into each agent file's own `model:` field.
This script runs as a pre-commit check to ensure every <stage>-draft.md / <stage>-audit.md
pair declares different models — enforcing the "adversarial second opinion, different vendor"
design principle at authoring time, not deployment time.

Exit code 0: all pairs pass.
Exit code 1: any pair uses the same model, or audit file missing when draft exists.
"""

import os
import sys
from pathlib import Path

import yaml


def load_agent_model(agent_path: Path) -> str | None:
    """Load model field from agent file's YAML frontmatter.

    Returns model name or None if file doesn't exist or has no model field.
    Raises ValueError on YAML parse error.
    """
    if not agent_path.exists():
        return None

    with open(agent_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Extract YAML frontmatter between --- fences
    if not content.startswith("---"):
        return None

    try:
        end_marker = content.find("---", 3)
        if end_marker == -1:
            return None
        yaml_block = content[3:end_marker].strip()
        data = yaml.safe_load(yaml_block)
        return data.get("model") if isinstance(data, dict) else None
    except yaml.YAMLError as e:
        raise ValueError(f"Failed to parse YAML in {agent_path}: {e}")


def main():
    agents_dir = Path(__file__).parent
    errors = []

    # Find all draft agents
    draft_agents = sorted(agents_dir.glob("*-draft.md"))

    for draft_path in draft_agents:
        draft_name = draft_path.stem  # e.g., "specification-draft"
        stage_name = draft_name.replace("-draft", "")  # e.g., "specification"
        audit_path = agents_dir / f"{stage_name}-audit.md"

        # Load models
        try:
            draft_model = load_agent_model(draft_path)
        except ValueError as e:
            errors.append(f"ERROR {draft_path.name}: {e}")
            continue

        if not draft_model:
            errors.append(f"ERROR {draft_path.name}: no model field found in frontmatter")
            continue

        # If audit exists, check model diversity
        if audit_path.exists():
            try:
                audit_model = load_agent_model(audit_path)
            except ValueError as e:
                errors.append(f"ERROR {audit_path.name}: {e}")
                continue

            if not audit_model:
                errors.append(f"ERROR {audit_path.name}: no model field found in frontmatter")
                continue

            if draft_model == audit_model:
                errors.append(
                    f"FAIL {draft_name}/{stage_name}-audit: "
                    f"draft and audit both use '{draft_model}' — must differ"
                )
        # else: no audit for this stage is OK (not all stages have audits)

    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1

    print(f"OK: All {len(draft_agents)} draft/audit pairs have different models")
    return 0


if __name__ == "__main__":
    sys.exit(main())
