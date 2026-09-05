# Per-session dev-tool sandbox (architecture plan Section C).
#
# BASE_IMAGE defaults to a curated image (dotnet SDK + Node/npm + Python + gh), not
# mcr.microsoft.com/devcontainers/universal:linux -- the universal image bundles a toolchain per
# supported language (dotnet, node, python, java, ruby, go, ...) and is tens of GB as a result;
# this team's repos only ever need dotnet/.NET 10, npm, python, and gh, so building a purpose-fit
# image instead avoids that bloat entirely. mcr.microsoft.com/dotnet/sdk:10.0 was also
# dramatically faster to pull than mcr.microsoft.com/devcontainers/* in testing (~38s vs. an
# unbounded stall past 60s+) -- MCR itself isn't uniformly slow, just those particular images.
# Once devcontainer.json detection lands (plan Section A/C), a repo's own image gets this same
# thin wrapper layered on top via `docker build --build-arg BASE_IMAGE=<repo's image>` instead of
# this default -- that path still assumes an apt-based (Debian/Ubuntu) base, same as before.
ARG BASE_IMAGE=mcr.microsoft.com/dotnet/sdk:10.0

# ── Global version ARGs (declared pre-FROM so both stages can redeclare and inherit them) ──────
# copilot_chat_model.py drives this CLI by per-turn exec, not an SDK (Task 3's CLI-exec rewrite
# retired the old github-copilot-sdk / `copilot --server` mechanism this pin used to check
# against) -- so what this pin actually buys today is flag/output-shape stability: the exact
# --mode/--no-ask-user/--secret-env-vars/--additional-mcp-config flags and the --output-format
# json fields (result/is_error/usage/session_id/total_cost_usd) that module depends on, confirmed
# against this one version rather than whatever an unpinned `@latest` ships next.
# 2026-09-04: bumped 1.0.79 -> 1.0.83-5 (a pre-release track, not the 1.0.82 stable release) for
# v1.0.83-4's fix: "Host-provided plugin customizations can be read without redundant path
# permission prompts" -- plugin skill/reference files under /opt/ai-dev-workflow-plugins were
# silently unreadable under headless --no-ask-user before this, since that flag suppresses the
# interactive approval prompt a file-read outside /workspace/repo would otherwise need. 1.0.83-5
# (latest pre-release as of this bump) still carries -4's fix -- pre-release changelogs are
# cumulative, confirmed by reading both release pages. No CLI flag changed alongside this bump;
# the fix is entirely CLI-side. This is a real compatibility surface, not just the one plugin-read
# fix -- re-verify the flags/JSONL-fields list above against a rebuilt image before trusting a
# run (see agent/sandbox-image/README.md's spike procedure). Rollback: 1.0.79, the prior pin, if
# 1.0.83-5 shows a compatibility blocker.
ARG COPILOT_CLI_VERSION=1.0.83-5
# Claude Code CLI -- npm-installed (final stage's Node/npm layer), not curl+tar like Copilot
# above. Same reasoning as COPILOT_CLI_VERSION now: claude_chat_model.py drives this CLI by
# per-turn exec, not an SDK, so the pin here IS the compatibility contract for the flags
# (--resume, --permission-mode, --json-schema, --plugin-dir, --mcp-config, and -- Task 4 --
# --output-format stream-json plus the --verbose it requires) and the terminal NDJSON line's
# fields (session_id/usage/total_cost_usd/is_error) that module depends on -- an unpinned
# `@latest` install could silently reshape either one out from under it. Task 4 (task-4-report.md)
# verified the --verbose requirement and the real stream-json/tool_use shape against 2.1.126;
# bumped to 2.1.241 (npm latest 2026-08-23, user directive 2026-08-24) for its bundled built-in
# skills (security-review/code-review/simplify) -- the same contract points MUST be re-verified
# against a rebuilt image before trusting a run (see plan's verification section).
ARG CLAUDE_CODE_CLI_VERSION=2.1.241
# P8/P10/P11/P14 deterministic tool versions -- pinned so a gate's pass/fail is reproducible
# across sessions, same reasoning as COPILOT_CLI_VERSION above. NOT yet exercised end-to-end
# against a rebuilt image -- see each gate module's own docstring.
# 2026-09-01: bumped scc/gitleaks/trivy/osv-scanner/syft to their latest releases -- the deploy
# pipeline's tightened trivy gate found real CVEs in each one's own compiled-in Go dependencies
# (these are static Go binaries; a vulnerable transitive dep only gets fixed by a newer release of
# the binary itself, there's no separate package to bump).
ARG SCC_VERSION=4.0.0
ARG GITLEAKS_VERSION=8.30.1
ARG TRIVY_VERSION=0.74.0
# osv-scanner: a second vulnerability database (OSV.dev) alongside Trivy's. Deliberately both --
# they disagree at the edges, and src/repo_scan.py reconciles their advisory ids via OSV's alias
# lists so the overlap collapses to one finding rather than two. Pinned to a v2 release because
# repo_scan.py's command uses the v2 `scan source --offline-vulnerabilities` subcommand form;
# v1's flags differ and would silently produce no output.
# STAYING at 2.0.2 deliberately, unlike scc/gitleaks/trivy/syft above: `--local-db-path` (which
# repo_scan.py:2270 depends on for its whole offline-scan contract) was removed from osv-scanner
# almost immediately after this release -- verified empirically against v2.0.3 through v2.5.1,
# none of them have it. The binary's own CVEs (52 findings, all HIGH/CRITICAL, its own compiled-in
# Go deps) are real and unfixed, tracked in this dir's .trivyignore -- fixing them for real means
# migrating repo_scan.py's osv-scanner invocation to whatever v2.5+'s fixed-location cache
# (~/.cache/osv-scalibr/) replaced --local-db-path with, a real app-level change, not a version bump.
ARG OSV_SCANNER_VERSION=2.0.2
# Anchore's dedicated SBOM cataloger -- repo_scan.py's own docstring explains why this replaced the
# earlier "redundant with Trivy" call. Same goreleaser asset-naming convention as scc/gitleaks/trivy
# above (no "v" prefix in the filename, unlike the release tag).
ARG SYFT_VERSION=1.51.1
# mise (multi-language version manager) is what lets this immutable image serve a repo needing a
# toolchain it doesn't ship -- Node 18, a second JDK, a Go version. See the /opt/aidw layout below
# and bootstrap.sh for how it's driven. Pinned like every other tool here.
ARG MISE_VERSION=2025.1.0

# ── fetch stage: the seven curl|tar binary installs, COPY'd into the final stage in one layer ───
# Size win is ~0 (each RUN already deleted its tarball in-layer); the gains are parallel fetching
# under BuildKit and seven layers collapsing to one. All fetches stay arch-aware via
# `dpkg --print-architecture`, hence ${BASE_IMAGE} (cached anyway) rather than a smaller fetcher.
FROM ${BASE_IMAGE} AS fetch
ARG COPILOT_CLI_VERSION
ARG SCC_VERSION
ARG GITLEAKS_VERSION
ARG TRIVY_VERSION
ARG OSV_SCANNER_VERSION
ARG MISE_VERSION
ARG SYFT_VERSION
RUN set -eux; \
    mkdir -p /out/bin; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
        amd64) copilot_asset="copilot-linux-x64.tar.gz"; \
               mise_asset="mise-v${MISE_VERSION}-linux-x64.tar.gz"; \
               scc_asset="scc_Linux_x86_64.tar.gz"; \
               gitleaks_asset="gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz"; \
               trivy_asset="trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz"; \
               osv_asset="osv-scanner_linux_amd64"; \
               syft_asset="syft_${SYFT_VERSION}_linux_amd64.tar.gz" ;; \
        arm64) copilot_asset="copilot-linux-arm64.tar.gz"; \
               mise_asset="mise-v${MISE_VERSION}-linux-arm64.tar.gz"; \
               scc_asset="scc_Linux_arm64.tar.gz"; \
               gitleaks_asset="gitleaks_${GITLEAKS_VERSION}_linux_arm64.tar.gz"; \
               trivy_asset="trivy_${TRIVY_VERSION}_Linux-ARM64.tar.gz"; \
               osv_asset="osv-scanner_linux_arm64"; \
               syft_asset="syft_${SYFT_VERSION}_linux_arm64.tar.gz" ;; \
        *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://github.com/github/copilot-cli/releases/download/v${COPILOT_CLI_VERSION}/${copilot_asset}" -o /tmp/copilot.tar.gz; \
    tar -xzf /tmp/copilot.tar.gz -C /out/bin copilot; \
    curl -fsSL "https://github.com/jdx/mise/releases/download/v${MISE_VERSION}/${mise_asset}" -o /tmp/mise.tar.gz; \
    tar -xzf /tmp/mise.tar.gz -C /tmp; \
    install -m 0755 /tmp/mise/bin/mise /out/bin/mise; \
    curl -fsSL "https://github.com/boyter/scc/releases/download/v${SCC_VERSION}/${scc_asset}" -o /tmp/scc.tar.gz; \
    tar -xzf /tmp/scc.tar.gz -C /out/bin scc; \
    curl -fsSL "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/${gitleaks_asset}" -o /tmp/gitleaks.tar.gz; \
    tar -xzf /tmp/gitleaks.tar.gz -C /out/bin gitleaks; \
    curl -fsSL "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/${trivy_asset}" -o /tmp/trivy.tar.gz; \
    tar -xzf /tmp/trivy.tar.gz -C /out/bin trivy; \
    curl -fsSL "https://github.com/google/osv-scanner/releases/download/v${OSV_SCANNER_VERSION}/${osv_asset}" -o /out/bin/osv-scanner; \
    curl -fsSL "https://github.com/anchore/syft/releases/download/v${SYFT_VERSION}/${syft_asset}" -o /tmp/syft.tar.gz; \
    tar -xzf /tmp/syft.tar.gz -C /out/bin syft; \
    chmod +x /out/bin/copilot /out/bin/scc /out/bin/gitleaks /out/bin/trivy /out/bin/osv-scanner /out/bin/syft; \
    rm -rf /tmp/copilot.tar.gz /tmp/mise /tmp/mise.tar.gz /tmp/scc.tar.gz /tmp/gitleaks.tar.gz /tmp/trivy.tar.gz /tmp/syft.tar.gz

# ── final stage ────────────────────────────────────────────────────────────────────────────────
FROM ${BASE_IMAGE}

# Piped RUN commands below (curl | bash, the nodesource setup script) need pipefail or a failing
# curl can silently pass.
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# PowerShell 7.6.2 ships baked into BASE_IMAGE itself (mcr.microsoft.com/dotnet/sdk:10.0's own
# Dockerfile) and bundles System.Security.Cryptography.Xml 10.0.6 -- 5 HIGH CVEs, fixed in
# 10.0.10. Nothing in this codebase invokes pwsh/powershell (grepped agent/src and
# agent/sandbox-image, zero hits); dead weight carrying real CVEs, not a tool this image needs.
RUN rm -rf /usr/share/powershell /usr/bin/pwsh

ARG NODE_MAJOR=22
# Redeclared here (default lives with COPILOT_CLI_VERSION above) so the npm install in the
# Node/npm/gh layer below can see it -- same cross-stage pattern as COPILOT_CLI_VERSION's own
# redeclaration in the fetch stage.
ARG CLAUDE_CODE_CLI_VERSION
# Azure Functions Core Tools: `func` is what actually builds and runs a Functions project, and
# app discovery (agent/src/app_discovery.py) classifies Azure Functions repos as in scope. Was
# pinned to a major only ("nothing parses its output") until 2026-08-24, when 4.14.0 shipped
# 401-broken within the hour AND 4.13.2/4.13.1's postinstall CDN blobs went 404 -- so the pin
# must be a version whose cdn.functions.azure.com zip actually still exists, verified by real
# install: 4.13.0 OK, 4.12.1 OK, 4.13.1/4.13.2 404, 4.14.0 401. Exact pin now, same reasoning as
# every other tool here: a build must not fail because upstream published (or unpublished) today.
ARG FUNC_CORE_TOOLS_VERSION=4.13.0
# The @playwright/mcp server was REMOVED, not left dormant: nothing attached it once ac-to-tests
# stopped (graph.py's "Deleted rather than left dormant" note explains why -- that stage runs before
# any app exists, and selectors are authored via data-testid rather than discovered). Keeping an
# unused MCP server also FORCED this version pin: PLAYWRIGHT_VERSION had to equal @playwright/mcp's
# own `playwright` dependency, and the MCP team pins alphas deliberately, which is the only reason
# this image runs a 1.63.0-alpha instead of a stable release. That constraint is now gone. Moving to
# a stable Playwright is a free choice but must be made deliberately: this literal is duplicated in
# the ac-to-tests SKILL.md and in two prompts that pin `@playwright/test` to it, so all four change
# together or the browser revision stops matching the baked build.
ARG PLAYWRIGHT_VERSION=1.63.0-alpha-2026-08-05
# Lighthouse: e2e_nodes.py runs it against the booted app (performance + accessibility categories
# only) inside e2e's live-app window -- deliberately NOT part of repo_scan (whose contract is
# offline, no running app). Pinned like every gate tool: a score is only comparable across runs if
# the auditor didn't move underneath it. It drives the SAME baked headless shell as everything
# else via CHROME_PATH (no second browser download) -- compatibility verified against a rebuilt
# image before any run trusts the numbers; if the headless shell proves insufficient, bake full
# chromium here instead of ever fetching at session time.
ARG LIGHTHOUSE_VERSION=13.4.1
# Everything below was unpinned ("neither gate reads a specific tool version, just parses
# whatever JSON/CSV shape the latest release emits") until 2026-09-01, when the deploy pipeline's
# security gate was tightened to block on hadolint's DL3008/13/16 (pin every apt/pip/npm install)
# with no exceptions -- pinned like every other tool in this file now, same reproducibility
# reasoning, even for the ones whose own output format is version-agnostic.
ARG MERMAID_CLI_VERSION=11.16.0
ARG JSCPD_VERSION=5.1.1
ARG LIZARD_VERSION=1.24.0
ARG SEMGREP_VERSION=1.175.0
ARG CHECKOV_VERSION=3.3.16
ARG INTERROGATE_VERSION=1.7.0
ARG BANDIT_VERSION=1.9.4
ARG PNPM_VERSION=11.25.0
ARG YARN_VERSION=1.22.22
ARG TYPESCRIPT_VERSION=7.0.2
ARG ESLINT_GLOBAL_VERSION=10.9.1
ARG PRETTIER_VERSION=3.9.6
ARG ANGULAR_CLI_VERSION=22.1.6
ARG RUFF_VERSION=0.16.5
ARG MYPY_VERSION=2.3.1

USER root

COPY --from=fetch /out/bin/ /usr/local/bin/

# ── /opt/aidw: where anything installed at *runtime* goes ──────────────────────────────────────
# The image is immutable; a container is not. A repo that needs a toolchain this image doesn't
# ship installs it here, never into /workspace/repo (which is the user's actual source tree) and
# never via apt (the container runs as non-root `vscode`, so that isn't possible by construction
# -- a genuine OS-package need is a BASE_IMAGE change instead).
#
#   /opt/aidw/tools -- executables: mise-installed SDKs, npm's global prefix, go install bins.
#                      Lives in the container's writable layer and dies with the session, on
#                      purpose: an executable on PATH is the artifact that would carry an attack
#                      between two sessions running different customers' untrusted repos.
#   /opt/aidw/cache -- package caches: npm, NuGet, pip, uv, mise downloads. Content-addressed
#                      data, safe to share, and mounted as a per-owner named volume by
#                      agent/src/sandbox/local_docker.py. Playwright browsers are NOT here --
#                      see /opt/playwright-browsers below (baked at build time, never a runtime
#                      install, so there is nothing version-drifted or partially-extracted for
#                      concurrent sessions of the same owner to race on).
#
# Both are created AND declared here rather than only at mount time, so the container behaves
# identically with or without a volume attached -- the mount is purely an accelerator, never a
# correctness dependency.
ENV AIDW_TOOLS_DIR=/opt/aidw/tools \
    AIDW_CACHE_DIR=/opt/aidw/cache \
    MISE_DATA_DIR=/opt/aidw/tools/mise \
    MISE_CACHE_DIR=/opt/aidw/cache/mise \
    npm_config_cache=/opt/aidw/cache/npm \
    NUGET_PACKAGES=/opt/aidw/cache/nuget \
    PIP_CACHE_DIR=/opt/aidw/cache/pip \
    UV_CACHE_DIR=/opt/aidw/cache/uv \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers \
    DOTNET_CLI_TELEMETRY_OPTOUT=1
# mise's shims live under MISE_DATA_DIR, not under a bare tools/shims -- getting this wrong means
# every mise-installed toolchain is invisible at runtime.
ENV PATH=/opt/aidw/tools/bin:/opt/aidw/tools/mise/shims:/opt/dotnet-tools:$PATH

# dotnet-ef, baked read-only at /opt/dotnet-tools (--tool-path, not --global: global tools land in
# the installing user's home, and this layer runs as root while the container runs as vscode).
# Observed live (run 8be5b4eb, PS-4): with no dotnet-ef, the model swapped the plan's "generate and
# apply the initial EF Core migration" for Database.EnsureCreatedAsync() and documented the
# workaround -- delivered repos then carry no migrations at all. "10.*" tracks the SDK major this
# image is built on; per-project Microsoft.EntityFrameworkCore.Design stays a repo-level
# PackageReference the model adds (runtime NuGet, cached).
RUN set -eux; \
    dotnet tool install --tool-path /opt/dotnet-tools --version "10.*" dotnet-ef; \
    chmod -R a+rX /opt/dotnet-tools; \
    dotnet-ef --version

RUN set -eux; \
    mkdir -p /opt/aidw/tools/bin /opt/aidw/cache; \
    chmod -R a+rwX /opt/aidw

# Node.js/npm (NodeSource) + Python + the GitHub CLI + the Claude Code CLI. dotnet, git, and curl
# already come from the base dotnet/sdk image (verified: Ubuntu 24.04, git/curl preinstalled,
# `dotnet --version` reports 10.0.x).
#
# Claude Code CLI is npm-installed here, not fetched via the curl+tar pattern used for the Copilot
# binary above -- that pattern is for GitHub-releases-hosted prebuilt binaries, and
# @anthropic-ai/claude-code is a plain npm package, the same install path as eslint/typescript/
# playwright elsewhere in this file. Both CLIs are baked into every image regardless of which
# AGENT_PROVIDER a given deployment runs -- neither is started by entrypoint.sh at container
# start (it only picks which credential to warn about when empty; see its own header comment).
# The per-turn exec launched from OUTSIDE the container, driven by AGENT_PROVIDER, is what
# actually picks which CLI runs, one turn at a time.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates=20260601~24.04.1 \
        gnupg=2.4.4-2ubuntu17.6 \
        python3=3.12.3-0ubuntu2.1 \
        python3-pip=24.0+dfsg-1ubuntu1.3; \
    curl -fsSL --retry 5 --retry-all-errors "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash -; \
    apt-get install -y --no-install-recommends nodejs=22.23.2-1nodesource1; \
    # NodeSource's bundled npm carries its own vendored deps (tar, pacote, brace-expansion, ...)
    # with real HIGH/CRITICAL CVEs, same root cause as the frontend image's -- there npm is unused
    # at runtime and gets removed entirely; here a target repo's own npm install/run genuinely
    # needs it, so upgrade instead.
    npm install -g npm@12.0.2; \
    npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_CLI_VERSION}"; \
    curl -fsSL --retry 5 --retry-all-errors https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg; \
    chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg; \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends gh=2.100.0; \
    rm -rf /var/lib/apt/lists/*

# Playwright MCP + its exact-matching browser build, baked at BUILD time into
# /opt/playwright-browsers (root-owned, world-readable) -- this is now the ONE browser path for
# everything that runs Playwright in this image: target repos' own e2e test runs (e2e_nodes.py,
# via the global
# PLAYWRIGHT_BROWSERS_PATH env set above). No runtime `playwright install` happens anywhere in
# this image -- deterministic version, no runtime npm/browser fetch in a container running
# untrusted repos, and no concurrent-extraction race between sessions of the same owner (the
# problem a writable, per-owner cache volume had).
# chromium-headless-shell, not full chromium: --headless only is what both the MCP and e2e_nodes'
# `playwright test` runs need, and the headless shell is roughly half the size.
# The stable /usr/local/bin/aidw-headless-shell symlink exists for mermaid-cli's Puppeteer (next
# layer), which reuses THIS browser read-only via PUPPETEER_EXECUTABLE_PATH instead of downloading
# a second full Chromium (~450 MB saved).
# `@playwright/test` is installed globally ALONGSIDE `playwright`, at the same pinned version. It is
# the package a generated repo's specs and config actually import, and it was previously absent:
# /usr/lib/node_modules/@playwright held only `mcp`, so an idiomatic
# `import { test } from '@playwright/test'` died with MODULE_NOT_FOUND and e2e_nodes had to write a
# compatibility shim into the target repo at run time. Installing it here makes that shim a fallback
# rather than the mechanism, and keeps repo and image on one Playwright version -- which is also what
# keeps the baked browser revision correct.
RUN set -eux; \
    npm install -g "playwright@${PLAYWRIGHT_VERSION}" \
        "@playwright/test@${PLAYWRIGHT_VERSION}" \
        "lighthouse@${LIGHTHOUSE_VERSION}"; \
    playwright install chromium-headless-shell; \
    chmod -R a+rX /opt/playwright-browsers; \
    ln -s "$(ls /opt/playwright-browsers/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell)" \
        /usr/local/bin/aidw-headless-shell
ENV PUPPETEER_EXECUTABLE_PATH=/usr/local/bin/aidw-headless-shell
# Lighthouse's chrome-launcher honors CHROME_PATH the same way Puppeteer honors the line above --
# one baked browser serves Playwright e2e, mermaid-cli, and lighthouse.
ENV CHROME_PATH=/usr/local/bin/aidw-headless-shell

# P3's diagram-validation gate (agent/src/gates/diagram_gate.py) renders Mermaid source via
# @mermaid-js/mermaid-cli, which needs a headless Chromium (Puppeteer). Real finding from a live
# build: Ubuntu 24.04's `chromium` apt package is a snap-only stub with no actual browser inside
# (`exec /snap/bin/chromium "$@"`, which doesn't exist in a container -- no snapd here) -- do NOT
# apt-install `chromium` expecting a working binary. Instead: install the shared libs any headless
# Chromium needs (Puppeteer's own troubleshooting docs list these as commonly missing) and point
# Puppeteer at the Playwright headless shell baked above (PUPPETEER_EXECUTABLE_PATH env +
# "headless": "shell" in mermaid-puppeteer-config.json). Both skip-download spellings are set:
# PUPPETEER_SKIP_DOWNLOAD is current, PUPPETEER_SKIP_CHROMIUM_DOWNLOAD covers older bundled
# puppeteers. libatk1.0-0/libatk-bridge2.0-0/libcups2 pin under their real Ubuntu 24.04 t64-transition
# names (apt resolves the old names to these transitively; pinning the old name has no candidate).
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        libnss3=2:3.98-1ubuntu0.2 \
        libatk1.0-0t64=2.52.0-1build1 \
        libatk-bridge2.0-0t64=2.52.0-1build1 \
        libcups2t64=2.4.7-1.2ubuntu7.14 \
        libdrm2=2.4.125-1ubuntu0.1~24.04.2 \
        libxkbcommon0=1.6.0-1build1 \
        libxcomposite1=1:0.4.5-1build3 \
        libxdamage1=1:1.1.6-1build1 \
        libxfixes3=1:6.0.0-2build1 \
        libxrandr2=2:1.5.2-2build1 \
        libgbm1=25.2.8-0ubuntu0.24.04.2 \
        libasound2t64=1.2.11-1ubuntu0.3 \
        libpango-1.0-0=1.52.1+ds-1build1 \
        libcairo2=1.18.0-3build1; \
    PUPPETEER_SKIP_DOWNLOAD=1 PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=1 \
        npm install -g "@mermaid-js/mermaid-cli@${MERMAID_CLI_VERSION}"; \
    rm -rf /var/lib/apt/lists/*

# P8's quality scan: jscpd (duplication) via npm, lizard (per-function complexity) via pip.
# checkov (IaC misconfig), interrogate (Python docstring coverage) and bandit (Python SAST --
# Apache-2.0, the licence-clean load-bearer for Python once the official semgrep rules left, see
# the rule-pack block below) ride the same pip line as lizard/semgrep -- all six are pure-Python
# static analyzers with no other runtime dependency.
# azure-functions-core-tools rides along in the same layer: npm rather than the
# packages.microsoft.com apt feed, since the package resolves its own linux-x64/linux-arm64
# payload (no new arch case block, no third apt keyring). Its postinstall runs fine as root here
# without --unsafe-perm -- npm 12 (bumped above) dropped that flag entirely; running as actual
# root already gets postinstall scripts run without npm's non-root privilege-drop dance. Nothing
# in the pipeline invokes `func` yet -- it earns its size for Node-based Functions projects
# downstream, in R's build and P13's test runs. Its bundled PowerShell-language worker is real
# dead weight, not just unused-for-now: this pipeline is scoped to Node-based Functions projects
# only (see above), never PowerShell ones, and that worker's own bundled
# System.Security.Cryptography.Xml carries real HIGH CVEs with no upstream fix in any 4.x release
# yet -- removed outright, `func` and its Node worker are untouched.
RUN set -eux; \
    npm install -g "jscpd@${JSCPD_VERSION}" "azure-functions-core-tools@${FUNC_CORE_TOOLS_VERSION}"; \
    rm -rf /usr/lib/node_modules/azure-functions-core-tools/bin/workers/powershell \
           /usr/lib/node_modules/azure-functions-core-tools/bin/*/workers/powershell; \
    pip install --break-system-packages --no-cache-dir --default-timeout=120 --retries=5 \
        "lizard==${LIZARD_VERSION}" "semgrep==${SEMGREP_VERSION}" "checkov==${CHECKOV_VERSION}" \
        "interrogate==${INTERROGATE_VERSION}" "bandit==${BANDIT_VERSION}"

# Front-end and Python quality toolchain. These are the deterministic signal for non-.NET repos:
# preflight_nodes.apply_stack_conventions writes eslint.config.mjs / ruff.toml / mypy.ini into a
# target repo, and the rebuild verifier session runs the repo's own build/lint/typecheck commands
# (`tsc --noEmit --strict`, `ruff check`, `mypy`, the repo's eslint where it has one) so a
# violation fails the build the same way an analyzer warning does under `dotnet build
# -warnaserror`. JS/TS SECURITY linting no longer rides that path at all: repo_scan.py's
# `eslint-security` adapter runs /opt/aidw/lint's config (security + sonarjs rules only,
# --no-config-lookup) on every repo with a package.json, own-config or not.
#
# Deliberately NOT installed globally: React and Vue. Neither has a global CLI worth having (they
# are scaffolded per-project via `npm create`), and a repo's own React/Vue lint plugins are
# installed into that repo as devDependencies by the conventions hook -- a global copy would only
# drift from the version the repo actually resolves. Angular is the exception: `ng` genuinely is a
# global CLI.
#
# eslint/typescript/prettier are here for repos that have no package.json of their own to install
# into; a repo that does has its own pinned copies, and `npx` prefers those. npm_config_prefix is
# deliberately left at its default so these land in /usr/local (root-owned, immutable at runtime)
# rather than in the runtime-writable /opt/aidw/tools -- nothing in the pipeline installs global
# npm packages at runtime, and keeping the two separate means an untrusted repo cannot shadow a
# tool a gate depends on.
RUN set -eux; \
    npm install -g "pnpm@${PNPM_VERSION}" "yarn@${YARN_VERSION}" "typescript@${TYPESCRIPT_VERSION}" \
        "eslint@${ESLINT_GLOBAL_VERSION}" "prettier@${PRETTIER_VERSION}" "@angular/cli@${ANGULAR_CLI_VERSION}"; \
    pip install --break-system-packages --no-cache-dir --default-timeout=120 --retries=5 \
        "ruff==${RUFF_VERSION}" "mypy==${MYPY_VERSION}"

# ── /opt/aidw/lint: the pipeline-owned ESLint toolchain, resolved AGAINST ITS OWN node_modules ─
# Linting a target repo must never mutate that repo's dependency graph: installing our lint
# devDependencies into a pnpm workspace root once re-resolved the whole peer graph, forked
# drizzle-orm into two incompatible peer-variant instances, and broke the repo's own build.
# So the toolchain lives here, pinned at image build, and the rebuild gate invokes
#   /opt/aidw/lint/node_modules/.bin/eslint --config /opt/aidw/lint/eslint.config.mjs .
# Root-owned and immutable at runtime for the same reason as the global npm tools above.
COPY lint/ /opt/aidw/lint/
# WORKDIR persists for every later Dockerfile instruction; this cd is scoped to just this RUN's
# shell, deliberately, so it can't shift where a later COPY/RUN in this 550+ line file resolves
# relative paths -- not worth the risk for a pure layer-caching nicety.
# hadolint ignore=DL3003
RUN set -eux; \
    cd /opt/aidw/lint; \
    npm install --no-audit --no-fund; \
    test -x node_modules/.bin/eslint; \
    node -e "import('/opt/aidw/lint/eslint.config.mjs').then(m => { if (!Array.isArray(m.default)) throw new Error('config not an array'); })"

# ── /opt/aidw/test: pipeline-owned vitest + coverage provider, same isolation rationale ────────
# vitest resolves @vitest/coverage-v8 from its own node_modules (they're siblings here), so the
# coverage gate never installs a provider into the target repo. A c8 wrapper does NOT work as an
# alternative: vitest's pool workers never inherit the wrapper's V8 coverage collection
# (observed live: a real suite measured a hard 0.0%).
COPY test/ /opt/aidw/test/
# hadolint ignore=DL3003
RUN set -eux; \
    cd /opt/aidw/test; \
    npm install --no-audit --no-fund; \
    test -x node_modules/.bin/vitest

# ── /opt/aidw/fakeidp: pipeline-owned fake OIDC identity provider, same isolation rationale ─────
# A local oidc-provider stood up during e2e for OIDC apps (Entra/Google/generic) so sign-in can be
# tested with declared test users -- no real tenant, secret, or consent. Resolves oidc-provider from
# its own node_modules; agent/src/fake_idp.py boots it as `node /opt/aidw/fakeidp/server.js`.
COPY fakeidp/ /opt/aidw/fakeidp/
# hadolint ignore=DL3003
RUN set -eux; \
    cd /opt/aidw/fakeidp; \
    npm install --no-audit --no-fund; \
    test -d node_modules/oidc-provider; \
    node -e "require('/opt/aidw/fakeidp/package.json')"

# mise / scc / gitleaks / trivy / osv-scanner / copilot all arrive via the fetch stage's
# COPY --from at the top of this stage. mise usage note: driven at container start by
# bootstrap.sh from files the repo itself declares (.tool-versions, mise.toml, .nvmrc,
# .node-version) -- never from an LLM's guess -- and restricted there to mise's own tool
# registry, because an arbitrary asdf plugin URL in a repo-supplied mise.toml is third-party
# shell that would otherwise run automatically before anything else in the container starts.

# ── Offline scan data: the vulnerability databases and semgrep rules, baked at BUILD time ──────
# Nothing in src/repo_scan.py touches the network at scan time -- with ONE deliberate exception:
# the `outdated` staleness probe (metrics_compute only), which is fail-open and excluded from
# content_hash for exactly the reproducibility reason below. Two reasons, both load-bearing:
#
#   1. Reproducibility. A scan is only comparable against a baseline taken days earlier if the
#      database did not move underneath it -- otherwise the delta reports advisory-feed churn as
#      if it were a code change. Both reports record their db_version and the delta flags drift.
#   2. The container runs untrusted repos. Fewer outbound fetches at scan time is strictly better.
#
# The trade, stated plainly: vulnerability data goes stale between image rebuilds. Rebuild on a
# schedule; that is the cost of a reproducible number.
ENV AIDW_OSV_DB_DIR=/opt/aidw/osv-db \
    AIDW_SEMGREP_RULES_DIR=/opt/aidw/semgrep-rules \
    TRIVY_CACHE_DIR=/opt/aidw/trivy-cache
# The two `test -n` lines fail the build loudly rather than shipping an image whose "offline" scan
# silently finds nothing -- an empty database and a clean repository produce identical output.
# java-db (~1 GB) deliberately not baked -- no Java repos in scope (this team is dotnet/node/
# python, see the BASE_IMAGE header), and repo_scan.py runs trivy with --skip-java-db-update, so
# trivy just skips JAR analysis with a warning instead of fetching.
# osv-scanner downloads a database ONLY for an ecosystem it actually finds a lockfile for, so the
# seed directory below is not decoration -- this step used to run in a bare `/tmp`, found no
# lockfiles, downloaded nothing, and swallowed it with `|| true`. Every scan since then failed at
# runtime with `could not load db for npm ecosystem: unable to fetch OSV database: no offline
# version of the OSV database is available` the moment a repo grew a package-lock.json (observed
# live, blazor-dotnet: osv-scanner exit 127, dependency-CVE coverage silently single-sourced to
# trivy for the whole run). One seed lockfile per ecosystem in scope -- npm, NuGet, PyPI, matching
# the dotnet/node/python scope named in the BASE_IMAGE header.
# The one `cd` below is already subshell-scoped ( cd X && ... ) specifically so it can't leak into
# the rest of this RUN -- a WORKDIR here would be the regression, not the fix.
# hadolint ignore=DL3003
RUN set -eux; \
    mkdir -p "$TRIVY_CACHE_DIR" "$AIDW_OSV_DB_DIR" /tmp/osv-seed; \
    trivy image --download-db-only --cache-dir "$TRIVY_CACHE_DIR"; \
    printf '%s' '{"name":"seed","version":"1.0.0","lockfileVersion":3,"requires":true,"packages":{"":{"name":"seed","version":"1.0.0","dependencies":{"lodash":"4.17.20"}},"node_modules/lodash":{"version":"4.17.20","resolved":"https://registry.npmjs.org/lodash/-/lodash-4.17.20.tgz"}}}' \
        > /tmp/osv-seed/package-lock.json; \
    printf '%s' '{"version":1,"dependencies":{".NETCoreApp,Version=v8.0":{"Newtonsoft.Json":{"type":"Direct","requested":"[13.0.1, )","resolved":"13.0.1","contentHash":"seed"}}}}' \
        > /tmp/osv-seed/packages.lock.json; \
    echo 'requests==2.31.0' > /tmp/osv-seed/requirements.txt; \
    ( cd /tmp/osv-seed && osv-scanner scan source --recursive --download-offline-databases \
        --local-db-path "$AIDW_OSV_DB_DIR" --offline-vulnerabilities . || true ); \
    rm -rf /tmp/osv-seed; \
    chmod -R a+rX "$TRIVY_CACHE_DIR" "$AIDW_OSV_DB_DIR"; \
    test -n "$(ls -A "$TRIVY_CACHE_DIR")"; \
    # Assert the ECOSYSTEM DATABASES exist, not merely that the parent directory is non-empty --
    # the old `test -n "$(ls -A "$AIDW_OSV_DB_DIR")"` passed on an empty `osv-scanner/` subdir,
    # which is exactly how an image with a completely empty OSV database shipped while claiming to
    # "fail the build loudly rather than ship an image whose offline scan silently finds nothing".
    for ecosystem in npm NuGet PyPI; do \
        test -s "$AIDW_OSV_DB_DIR/osv-scanner/$ecosystem/all.zip" \
            || { echo "OSV offline db missing for $ecosystem" >&2; exit 1; }; \
    done

# Semgrep rule packs, fetched at SHA pins at BUILD time rather than via `--config auto` at scan
# time. `auto` means a network fetch on every run, rules that change underneath a baseline
# comparison, and the hosted registry's own terms attaching to what it serves.
#
# NOT the official semgrep/semgrep-rules pack any more. This image is redistributed (SaaS + on-prem
# customers), and the Semgrep Rules License v1.0 forbids exactly that: "You may use the rules only
# for your own internal business purposes. This license does not allow you to distribute the rules,
# or to make them available to others as a service." The packs below are MIT -- listed, SHA-pinned
# and licence-asserted in semgrep-rule-packs.txt (the rule-pack equivalent of
# plugins/vendor/vendor-lock.json). The semgrep ENGINE stays: LGPL-2.1, invoked as an unmodified
# subprocess, recorded as `permissive: false` in every scan report's tools[] block.
#
# Each pack's url+sha+spdx is written to MANIFEST so a scan report traces to the exact rule set
# that produced it. The pin is the built image, same as for every other tool here: rules only
# move when the image is rebuilt, never between two scans compared against each other.
COPY semgrep-rule-packs.txt /tmp/semgrep-rule-packs.txt
RUN set -eux; \
    mkdir -p "$AIDW_SEMGREP_RULES_DIR"; \
    : > "$AIDW_SEMGREP_RULES_DIR/MANIFEST"; \
    grep -v '^\s*#' /tmp/semgrep-rule-packs.txt | grep -v '^\s*$' | \
    while read -r url sha spdx license_grep subdirs; do \
        pack="$(basename "$url" .git)"; \
        git clone --filter=blob:none "$url" "/tmp/pack-$pack"; \
        git -C "/tmp/pack-$pack" checkout --quiet "$sha"; \
        head -5 "/tmp/pack-$pack/LICENSE" | grep -qi "$license_grep" \
            || { echo "rule pack $pack LICENSE does not read as $spdx -- refusing to ship" >&2; exit 1; }; \
        mkdir -p "$AIDW_SEMGREP_RULES_DIR/$pack"; \
        cp "/tmp/pack-$pack/LICENSE" "$AIDW_SEMGREP_RULES_DIR/$pack/LICENSE"; \
        echo "$subdirs" | tr ',' '\n' | while read -r subdir; do \
            [ -d "/tmp/pack-$pack/$subdir" ] || { echo "rule pack $pack has no $subdir" >&2; exit 1; }; \
            cp -r "/tmp/pack-$pack/$subdir" "$AIDW_SEMGREP_RULES_DIR/$pack/"; \
        done; \
        echo "$url $sha $spdx" >> "$AIDW_SEMGREP_RULES_DIR/MANIFEST"; \
        rm -rf "/tmp/pack-$pack"; \
    done; \
    find "$AIDW_SEMGREP_RULES_DIR" -name '*.test.*' -delete; \
    find "$AIDW_SEMGREP_RULES_DIR" \( -name '*.yaml' -o -name '*.yml' \) -exec grep -lEi 'Semgrep Rules License|Commons Clause' {} + \
        | grep . && { echo "non-permissive rule text made it into the image" >&2; exit 1; } || true; \
    test -n "$(find "$AIDW_SEMGREP_RULES_DIR" \( -name '*.yaml' -o -name '*.yml' \) -print -quit)"; \
    test -s "$AIDW_SEMGREP_RULES_DIR/MANIFEST"; \
    rm -f /tmp/semgrep-rule-packs.txt; \
    chmod -R a+rX "$AIDW_SEMGREP_RULES_DIR"
# ── Third-party licence texts: /opt/aidw/licenses/<tool>/LICENSE* ──────────────────────────────
# This image is REDISTRIBUTED (SaaS + on-prem customers), so every binary baked above ships with
# its licence text -- see agent/sandbox-image/THIRD-PARTY-NOTICES.md for the full inventory,
# SPDX ids, and source pointers (the compliance obligation for the copyleft ones: semgrep engine
# LGPL-2.1, eslint-plugin-sonarjs LGPL-3.0). Fetched at the SAME version pins as the binaries.
# Copilot CLI's licence §2 makes including its LICENSE.md a CONDITION of redistribution.
# pip/npm-installed packages already carry their licence files in *.dist-info/ and
# node_modules/<pkg>/ inside the image; the two below are the exceptions worth surfacing at a
# stable path (the proprietary grant, and the copyleft lint plugin).
# Global ARGs are per-stage in Docker: redeclared here or every ${*_VERSION} below is EMPTY and
# the raw.githubusercontent URLs 404 (curl -f makes that a loud build failure, but still).
ARG SCC_VERSION
ARG GITLEAKS_VERSION
ARG TRIVY_VERSION
ARG OSV_SCANNER_VERSION
ARG SYFT_VERSION
ARG MISE_VERSION
RUN set -eux; \
    for tool_ref in \
        "scc boyter/scc v${SCC_VERSION} LICENSE" \
        "gitleaks gitleaks/gitleaks v${GITLEAKS_VERSION} LICENSE" \
        "trivy aquasecurity/trivy v${TRIVY_VERSION} LICENSE" \
        "osv-scanner google/osv-scanner v${OSV_SCANNER_VERSION} LICENSE" \
        "syft anchore/syft v${SYFT_VERSION} LICENSE" \
        "mise jdx/mise v${MISE_VERSION} LICENSE" \
        "copilot-cli github/copilot-cli main LICENSE.md" \
    ; do \
        set -- $tool_ref; tool="$1"; repo="$2"; ref="$3"; file="$4"; \
        mkdir -p "/opt/aidw/licenses/$tool"; \
        curl -fsSL "https://raw.githubusercontent.com/$repo/$ref/$file" \
            -o "/opt/aidw/licenses/$tool/$file" \
            || { echo "licence fetch failed for $tool -- refusing to ship without it" >&2; exit 1; }; \
    done; \
    mkdir -p /opt/aidw/licenses/eslint-plugin-sonarjs /opt/aidw/licenses/semgrep-rules; \
    cp /opt/aidw/lint/node_modules/eslint-plugin-sonarjs/LICENSE* /opt/aidw/licenses/eslint-plugin-sonarjs/ \
        || { echo "eslint-plugin-sonarjs (LGPL-3.0) ships without its licence text" >&2; exit 1; }; \
    cp "$AIDW_SEMGREP_RULES_DIR/MANIFEST" /opt/aidw/licenses/semgrep-rules/MANIFEST; \
    for d in /opt/aidw/licenses/*/; do \
        find "$d" -maxdepth 1 \( -name 'LICENSE*' -o -name 'MANIFEST' \) -print -quit | grep -q . \
            || { echo "empty licence dir $d" >&2; exit 1; }; \
    done; \
    chmod -R a+rX /opt/aidw/licenses

# ponytail: nuget-license (P11d's .NET license scan) intentionally NOT installed here -- it's a
# `dotnet tool` pulled from NuGet at scan time in a *target* repo's own container, not something
# this base image can usefully pre-install (no target repo/lockfile exists yet at image-build
# time). p11_gates.py's command already tolerates it being absent (best-effort, non-fatal).

# dotnet/sdk's base image has no non-root user; devcontainer images conventionally run as
# "vscode" and this repo's entrypoint.sh/provisioning code doesn't need to care which base
# provided the user, so create one here for parity either way.
RUN useradd -m -s /bin/bash vscode

COPY entrypoint.sh /usr/local/bin/ai-dev-workflow-entrypoint.sh
COPY bootstrap.sh /usr/local/bin/ai-dev-workflow-bootstrap.sh
# Agent Plugin content (skills/commands/MCP servers for GitHub Copilot CLI) -- see
# agent/sandbox-image/README.md and agent/src/config.py's COPILOT_PLUGIN_DIRECTORIES for
# the matching in-container path this must stay in sync with.
#
# ai-dev-workflow/ is this project's own first-party skill pack -- committed to git, copied
# directly like everything else in this stage.
COPY plugins/ai-dev-workflow/ /opt/ai-dev-workflow-plugins/ai-dev-workflow/
COPY plugins/.claude-plugin/ /opt/ai-dev-workflow-plugins/.claude-plugin/
# vendor/ is nine third-party skill packs -- NOT committed to git (see vendor-lock.json's own
# "_readme" and this repo's .gitignore): fetched here, at build time, from their pinned commits,
# with the exact curation (stripped files, patched paths, extra license files)
# fetch-vendor-plugins.sh and vendor-lock.json document. Build-time, never session-runtime, for
# the same reason Playwright's browser and the semgrep/OSV/Trivy databases are baked above instead
# of fetched per-session: a live sandbox session runs untrusted repos and must stay
# network-independent. Only vendor-lock.json/fetch-vendor-plugins.sh/wrappers/ are ever committed
# -- copied first, before the RUN below, so this layer's build cache only invalidates when one of
# those three actually changes, not on an unrelated Dockerfile edit.
COPY plugins/vendor/vendor-lock.json plugins/vendor/fetch-vendor-plugins.sh /opt/ai-dev-workflow-plugins/vendor/
COPY plugins/vendor/wrappers/ /opt/ai-dev-workflow-plugins/vendor/wrappers/
RUN set -eux; \
    chmod +x /opt/ai-dev-workflow-plugins/vendor/fetch-vendor-plugins.sh; \
    sh /opt/ai-dev-workflow-plugins/vendor/fetch-vendor-plugins.sh /opt/ai-dev-workflow-plugins/vendor
# The puppeteer config the mermaid-cli comment far above (and diagram_gate._render_one's own `-p`
# flag) has always named, but which nothing ever created: mmdc aborted with `Configuration file
# "/opt/ai-dev-workflow-plugins/mermaid-puppeteer-config.json" doesn't exist` on EVERY diagram, so
# verify_plan_diagrams failed the plan stage on every lap and spent its whole max_verify_cycles
# budget redrafting a plan whose Mermaid was fine (observed live, blazor-dotnet s01: two wasted
# laps; the identical source rendered the moment this file existed). Written here, last, rather
# than beside the mermaid install: this is a leaf artifact nothing above depends on, so a change to
# it rebuilds one cheap layer instead of every expensive install below it. "headless": "shell"
# matches the Playwright headless shell PUPPETEER_EXECUTABLE_PATH points at; --no-sandbox is
# required to run it as a non-root container user without --cap-add=SYS_ADMIN. The chown below
# covers this file too.
RUN printf '%s\n' \
      '{' \
      '  "headless": "shell",' \
      '  "args": ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]' \
      '}' \
      > /opt/ai-dev-workflow-plugins/mermaid-puppeteer-config.json

RUN chmod +x /usr/local/bin/ai-dev-workflow-entrypoint.sh /usr/local/bin/ai-dev-workflow-bootstrap.sh && \
    mkdir -p /workspace && chown -R vscode:vscode /workspace /opt/aidw && \
    # Plugin content is root-owned and read-only, not vscode-writable like /workspace/aidw above:
    # the sandboxed session runs as vscode and should be able to READ curated skills/commands (via
    # --plugin-dir, and since the 1.0.83-5 bump, direct file reads of a skill's own reference files
    # too) but never write or tamper with them -- a self-modifying plugin mid-run is a supply-chain
    # risk this doesn't need to accept just because the directory happened to be group-writable.
    chown -R root:root /opt/ai-dev-workflow-plugins && chmod -R a+rX /opt/ai-dev-workflow-plugins && \
    # /opt/aidw/tools/bin is first on PATH (see the ENV PATH above) but nothing installs into it --
    # at build or at runtime (runtime toolchains land in /opt/aidw/tools/dotnet and mise's own shims
    # dir). Left vscode-writable it is a free shadowing slot: an autopilot session could drop
    # /opt/aidw/tools/bin/semgrep (or trivy/gitleaks/git) and silence every later gate. Re-assert
    # root ownership, no group/other write, so a PATH-first empty dir can't be weaponised. mise's
    # shims stay vscode-writable by necessity, but mise only shims repo-declared toolchains, not the
    # root-owned gate binaries in /usr/local/bin.
    chown root:root /opt/aidw/tools/bin && chmod 0755 /opt/aidw/tools/bin

USER vscode
WORKDIR /workspace

ENTRYPOINT ["/usr/local/bin/ai-dev-workflow-entrypoint.sh"]
