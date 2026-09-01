An earlier ticket already ran this stage against this same project -- its approved report is at
`.ai-dev-workflow/07-remediation.approved.json`. Read it yourself: some of today's still-open
findings may be ones that ticket already investigated and deliberately left open, with a real
reason recorded in its own `known_gaps`.

For a finding that is still present in today's scan and has nothing to do with what this ticket is
itself changing, you do not need to re-investigate it from zero -- restate the earlier ticket's
reason (in your own words, so this report stays self-contained) rather than re-deriving a
conclusion someone already reached.

That is a starting point, not a rubber stamp: if a fix has since become available, or this
ticket's own changes touch that code anyway, fix it now instead of re-filing the same excuse.
