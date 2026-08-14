Second opinion on a colleague's as-built baseline. Re-verify every high/medium-confidence entry
against the grounding context you were given — downgrade to low if you can't confirm it yourself.
Flag any AC claiming a backing test that doesn't actually look like it tests that behavior.

Return `revised_baseline` with everything fixed, and `audit_findings` listing what changed; empty
if none.
