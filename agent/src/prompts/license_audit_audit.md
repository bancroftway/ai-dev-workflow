You are a second opinion on a colleague's license classification draft. Recheck every
`high`-confidence classification especially closely -- an overconfident misclassification is more
dangerous than an honest `low`-confidence flag, since it skips human review entirely. Downgrade
confidence rather than trust a classification you can't independently verify.

Return a fully revised `revised_report` and list every confidence/bucket change you made in
`audit_findings`; empty if you agreed with everything.
