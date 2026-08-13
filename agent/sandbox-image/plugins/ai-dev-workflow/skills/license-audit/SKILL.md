---
name: license-audit
description: Guides classifying dependency licenses against an allow/review/deny policy, with explicit low-confidence flagging for dual-licensed or exception-carrying packages that automated classifiers commonly get wrong. Use this skill whenever asked to audit dependency licenses, check for GPL/AGPL/commercial licenses in a dependency tree, generate third-party notices, or decide whether a package's license is acceptable to use. Also trigger on "check our licenses," "are any of our dependencies GPL," or "review this license report."
---

# License Audit

A wrong license classification doesn't fail loudly -- it sits there until legal review finds it,
by which point the dependency may be deeply integrated. Automated license detection tools are
good at reading a declared `license` field, and much worse at catching dual licensing, license
exceptions, and packages that changed license between versions. Your job is to classify
confidently where the evidence is clean, and to say "I'm not sure" clearly where it isn't --
never to round an ambiguous case up to a comfortable answer.

## Classify against the policy, but distrust an easy answer

Given an allowlist (e.g. MIT/Apache-2.0/BSD/ISC), a review-required list (e.g. MPL/LGPL), and a
denylist (e.g. commercial/AGPL/source-available), most packages will have one clearly declared
license that maps directly onto one bucket -- classify those with high confidence. But watch for:

- **Dual-licensed packages**: a package offered under, say, either MIT or a commercial license
  depending on how it's used (self-hosted vs. hosted-service, open-source vs. proprietary
  integration). The declared "license" field in a manifest often only shows one side of this. If
  a package's actual repository documents dual licensing, split licensing by directory/component,
  or a "different terms for commercial use" clause, this is never a high-confidence allow, even
  if the manifest says MIT -- flag it for human review with the specifics of what you found.
- **License exceptions and carve-outs**: some packages are technically GPL/AGPL but ship an
  explicit linking exception, or are MIT except for one vendored subdirectory under a different
  license. A classifier that only reads the top-level `LICENSE` file misses this -- check for a
  `LICENSING.md`, per-directory license notes, or exception language in the license text itself
  before concluding a package is uniformly one license.
- **License changes between versions**: a package can relicense between the version you audited
  last time and the version currently pinned. Don't assume last time's classification still holds
  without checking the currently-installed version's own license metadata.

## When to flag low confidence instead of guessing

If you can't find a clear, current, complete license declaration -- conflicting information
between the manifest and the actual repository, a license file that doesn't match what the
package registry states, or genuinely ambiguous dual/split licensing -- report `confidence: low`
and say specifically what's ambiguous. Never auto-accept a low-confidence classification into the
allowlist bucket just because most packages turn out fine; the packages that actually cause legal
problems are disproportionately the ones that were never confidently classified in the first
place, precisely because ambiguity is what got missed.

## Reporting your findings

For every package: the license you found declared, the license you'd classify it as after
checking for dual-licensing/exceptions, which policy bucket it falls into (allow/review/deny/
unknown), your confidence level, whether it's dual-licensed or carries an exception, and your
reasoning. For anything not high-confidence-and-allowed, be explicit that it needs a human
decision -- don't soften a `review`/`deny`/`low-confidence` classification into something that
reads like it's already been cleared.
