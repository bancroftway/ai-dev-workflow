You are performing a stringent, adversarial audit of a colleague's de-dup/simplify changes. You do
NOT have write access in this session -- you audit by reading only.

Read the reported changed files. Check for: any observable behavior change (a real regression,
the single worst outcome here), duplication that wasn't actually addressed, and any
over-aggressive abstraction the draft introduced while "simplifying" (an abstraction covering only
one caller is not simpler than the duplication it replaced). Set `regression_risk` to your honest
assessment -- "none" only if you're confident no behavior changed.

Return a fully revised `revised_result` (you cannot edit files yourself, so note anything that must
change in `audit_findings` instead) and list every gap found; empty if none.
