# Third-party notices — sandbox image

This image is redistributed (SaaS deployments AND on-prem customer installs), so everything baked
into it ships under its own licence. Ground rule: tools are invoked **unmodified as subprocesses**
and shipped as binaries inside a container image — mere aggregation. Copyleft is a compliance
obligation (licence text in the image, notices kept, pinned upstream source named here), not
contamination. What excludes a component: no redistribution grant, or terms forbidding
"as a service" use.

Licence texts live at `/opt/aidw/licenses/<tool>/` in the image (asserted non-empty at build);
pip packages carry theirs in `site-packages/*.dist-info/`, npm packages in `node_modules/<pkg>/`.
Version pins are the Dockerfile `ARG`s unless noted.

## Scan / build tools (binaries fetched at build)

| Component | Version pin | SPDX | Upstream source |
|---|---|---|---|
| scc | `SCC_VERSION` | MIT | https://github.com/boyter/scc |
| gitleaks | `GITLEAKS_VERSION` | MIT | https://github.com/gitleaks/gitleaks |
| trivy | `TRIVY_VERSION` | Apache-2.0 | https://github.com/aquasecurity/trivy |
| osv-scanner | `OSV_SCANNER_VERSION` | Apache-2.0 | https://github.com/google/osv-scanner |
| syft | `SYFT_VERSION` | Apache-2.0 | https://github.com/anchore/syft |
| mise | `MISE_VERSION` | MIT | https://github.com/jdx/mise |
| GitHub Copilot CLI | `COPILOT_CLI_VERSION` | proprietary (redistribution grant, licence §2) | https://github.com/github/copilot-cli |

Copilot CLI's licence §2 permits redistribution only: unmodified, "as part of an application or
service that provides material functionality beyond the Software itself", never standalone, **with
a copy of the licence and all notices included** (that copy is
`/opt/aidw/licenses/copilot-cli/LICENSE.md`), and with this application licensed independently.
This image relies on exactly those conditions.

## pip-installed analyzers

| Component | SPDX | Notes |
|---|---|---|
| semgrep (engine + bundled `semgrep-core` binary) | LGPL-2.1 | unmodified subprocess; recorded `permissive: false` in every scan report's `tools[]`; source: https://github.com/semgrep/semgrep |
| lizard | MIT | |
| checkov | Apache-2.0 | |
| interrogate | MIT | |
| bandit | Apache-2.0 | |
| ruff / mypy | MIT | |

## Semgrep rule packs (vendored, `/opt/aidw/semgrep-rules`)

The **official semgrep/semgrep-rules pack is deliberately absent**: the Semgrep Rules License v1.0
allows use "only for your own internal business purposes" and "does not allow you to distribute the
rules, or to make them available to others as a service" — incompatible with this image's
distribution. Shipped packs are SHA-pinned and licence-asserted at build via
`semgrep-rule-packs.txt`; the exact set is recorded in the image at
`/opt/aidw/semgrep-rules/MANIFEST` (mirrored to `/opt/aidw/licenses/semgrep-rules/MANIFEST`):

| Pack | SPDX |
|---|---|
| elttam/semgrep-rules (`rules/generic`, `rules/yaml`) | MIT |
| apiiro/malicious-code-ruleset (`dynamic_execution`, `obfuscation`) | MIT |

## npm-installed toolchain

| Component | SPDX | Notes |
|---|---|---|
| jscpd, pnpm, yarn, typescript, prettier, @angular/cli, @mermaid-js/mermaid-cli | MIT | |
| eslint, @eslint/js, typescript-eslint | MIT | pipeline-owned toolchain at `/opt/aidw/lint` |
| eslint-plugin-security | Apache-2.0 | |
| **eslint-plugin-sonarjs** | **LGPL-3.0-only** | copyleft: licence text at `/opt/aidw/licenses/eslint-plugin-sonarjs/`; source: https://github.com/SonarSource/SonarJS |
| eslint-plugin-react-hooks, eslint-plugin-jsx-a11y, angular-eslint, eslint-plugin-vue | MIT | |
| lighthouse | Apache-2.0 | |
| oidc-provider | MIT | pipeline-owned fake OIDC identity provider at `/opt/aidw/fakeidp` (e2e test-login only; never in a delivered app) |
| azure-functions-core-tools | MIT | its npm postinstall downloads the platform binary from Microsoft's CDN at IMAGE BUILD time — the shipped binary is Microsoft's, version pinned by `FUNC_CORE_TOOLS_VERSION` |
| Claude Code CLI (`@anthropic-ai/claude-code`, `CLAUDE_CODE_CLI_VERSION`) | "© Anthropic PBC. All rights reserved. Use is subject to Anthropic's Commercial Terms of Service." | **no redistribution grant in the package licence** — kept as-is by decision (2026-08-29); on-prem distribution pending confirmation with Anthropic. The Claude *Agent SDK* is not a way out: its MIT licence covers the SDK wrapper only, and it bundles this same CLI under these same terms |

## Browsers

| Component | SPDX | Notes |
|---|---|---|
| Playwright / playwright-core | Apache-2.0 | |
| chromium-headless-shell (via `playwright install`) | BSD-3-Clause + bundled third-party licences | Chromium's licence and per-component credits: https://chromium.googlesource.com/chromium/src/+/refs/heads/main/LICENSE — one baked browser serves Playwright e2e, mermaid-cli and lighthouse |

## Vulnerability databases (baked offline data)

Redistributed unmodified; the data aggregates sources under their own licences — attribution:

- **Trivy DB** (aquasecurity/trivy-db, Apache-2.0) aggregating NVD (US government work, attribution
  requested), GitHub Advisory Database (CC-BY-4.0), Red Hat security data (CC-BY-4.0), Debian,
  Alpine SecDB and other distro trackers.
- **OSV database** (osv.dev) — per-source: GitHub Advisory Database, PyPA Advisory Database,
  Go Vulnerability Database, PSF database, OSS-Fuzz (CC-BY-4.0); Ubuntu (CC-BY-SA-4.0);
  RustSec, Global Security Database, opam, Haskell (CC0-1.0); AlmaLinux, Drupal (MIT);
  Rocky Linux (BSD); RConsortium, OpenSSF Malicious Packages, Bitnami (Apache-2.0).

## Base image and OS packages

`mcr.microsoft.com/dotnet/sdk` (`BASE_IMAGE`): .NET is MIT; the underlying Debian packages
(git GPL-2.0, bash/coreutils GPL-3.0, …) carry their licences in
`/usr/share/doc/*/copyright`, with source available via Debian's archives. Microsoft's container
image terms govern the MS-built layers — verify before FURTHER redistributing a re-based image.
Node.js arrives via NodeSource (MIT); the GitHub CLI `gh` is MIT.

## Written into TARGET repos (not shipped in this image)

`agent/src/templates/dotnet/Directory.Build.props` references NuGet analyzers the customer's own
build restores; each carries its licence in a comment there. One is not OSI-approved:
**SonarAnalyzer.CSharp — Sonar Source-Available License v1.0** (free as a build-time analyzer;
restricts use in tools competing with SonarQube). Kept by decision (2026-08-29); recorded here
because SaaS runs execute it and customer repos inherit the reference.

## Vendored skill packs

Curated third-party Claude/Copilot skill packs under `plugins/vendor/` — provenance, pins,
licence scoping and per-pack notes live in `plugins/vendor/vendor-lock.json` (that file is the
authority; packs ship their own LICENSE files where upstream provides one).

## Maintenance

Adding any binary/tool to this image = one row here + a licence file under `/opt/aidw/licenses/`
(the Dockerfile's licence step asserts every dir is non-empty; extend its list when you add a
fetched binary).
