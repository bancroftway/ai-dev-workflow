---
name: security-triage
description: Guides triaging security-scanner findings (Semgrep, Trivy, gitleaks, and similar deterministic security scanners, plus reasoning-based security-review output) into fix-or-suppress decisions with real, exploitability-grounded justification. Use this skill whenever asked to review, triage, or resolve security scan findings, decide whether a vulnerability finding is a false positive, judge whether a dependency CVE needs an immediate upgrade, or write a justification for suppressing a security finding. Also trigger on "go through these security findings," "is this vulnerability exploitable," or "review this SARIF security report."
---

# Security Triage

A security finding you dismiss incorrectly becomes an incident later; a secret you fail to flag
becomes a breach. Unlike code-quality triage, the cost of getting this wrong is asymmetric --
being too cautious costs some engineering time, being too permissive can cost far more. Bias your
decisions accordingly, and never let a plausible-sounding justification substitute for actually
tracing whether the vulnerable code path is reachable.

## One category never gets a rubber-stamp suppression: secrets

Any finding that a secrets scanner (gitleaks or similar) flags as a hardcoded credential, API key,
or private key is **always fix** -- rotate or remove it -- unless you can prove the specific value
is a non-functional placeholder or an already-rotated test fixture, and your justification names
exactly why you're confident of that (e.g. "this is the literal string from the library's own
example docs, confirmed by comparing byte-for-byte" -- not "looks like a test key"). This is the
single highest-risk category for a rubber-stamp suppression, because a real leaked secret and a
harmless placeholder can look identical from the finding alone. When genuinely uncertain, treat it
as real and fix it -- the cost of over-reacting to a placeholder is far lower than the cost of
under-reacting to a real leaked credential.

## Exploitability framework by finding category

For everything else, ground your fix-vs-suppress decision in whether the vulnerable path is
actually reachable from untrusted input, not just whether the pattern matches:

- **Injection (SQL, command, template, etc.)**: is user-controlled input actually concatenated or
  interpolated into the dangerous sink, with no parameterization/escaping in between? If yes,
  fix -- this is close to always exploitable when the pattern matches. If the "user input" is
  actually a hardcoded internal value, say so specifically.
- **SSRF**: can an external caller influence the URL/host being requested, even indirectly (a
  redirect chain, a webhook config)? If the URL is always a fixed, internal, or allowlisted value,
  say so.
- **Path traversal**: can user input reach a filesystem path with no normalization/allowlist
  check? Check for `..`-stripping or a base-directory containment check before concluding it's
  safe.
- **Insecure deserialization**: is the deserialized payload ever attacker-controlled, or always
  internally generated?
- **Broken auth/access control**: does the code path actually skip an authorization check that
  exists elsewhere in the codebase for equivalent operations, or is this genuinely an
  intentionally-public endpoint?

## Dependency vulnerabilities: upgrade first

Default to upgrading the flagged package. Only suppress when no fixed version exists yet *and* you
can name the specific code path that would need to call the vulnerable function/API for the CVE to
matter -- "we don't use that function" is only a valid justification if you actually checked, not
assumed. Attach a `review_by` date (default roughly 90 days out) to any dependency suppression so
it doesn't become permanent by default -- a fixed version may ship before then.

## Trust the scanner's severity unless you have a specific reason not to

Don't downgrade a scanner-reported CRITICAL/HIGH severity in your own reasoning without documenting
exactly why (e.g. "confirmed unreachable, see above") -- the severity field itself isn't yours to
edit, only the fix-or-suppress decision and its justification are.

## Justification evidence bar

A justified suppression names the *specific* precondition that makes exploitation impossible in
this codebase, as it exists right now -- not a general statement that the pattern is "usually
fine" or "a known false positive for this rule." "Seems fine" and "false positive" alone, with no
supporting detail, are not acceptable justifications regardless of how confident they sound.

## Reporting your findings

For every finding: your decision (fix or suppress), your justification (naming the specific
reachability/exploitability reasoning, not a generic statement), and for suppressions, the exact
marker text plus a `review_by` date where applicable.
