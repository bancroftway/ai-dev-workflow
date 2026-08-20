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
# Pinned to match agent/.venv's installed github-copilot-sdk build (agent/.venv/Lib/site-packages/
# copilot/_cli_version.py's CLI_VERSION) so the SDK's minimum protocol version check
# (client.py's _MIN_PROTOCOL_VERSION) never rejects this server on a stale binary.
ARG COPILOT_CLI_VERSION=1.0.79
# P8/P10/P11/P14 deterministic tool versions -- pinned so a gate's pass/fail is reproducible
# across sessions, same reasoning as COPILOT_CLI_VERSION above. NOT yet exercised end-to-end
# against a rebuilt image -- see each gate module's own docstring.
ARG SCC_VERSION=3.5.0
ARG GITLEAKS_VERSION=8.21.2
ARG TRIVY_VERSION=0.73.0
# osv-scanner: a second vulnerability database (OSV.dev) alongside Trivy's. Deliberately both --
# they disagree at the edges, and src/repo_scan.py reconciles their advisory ids via OSV's alias
# lists so the overlap collapses to one finding rather than two. Pinned to a v2 release because
# repo_scan.py's command uses the v2 `scan source --offline-vulnerabilities` subcommand form;
# v1's flags differ and would silently produce no output.
ARG OSV_SCANNER_VERSION=2.0.2
# Anchore's dedicated SBOM cataloger -- repo_scan.py's own docstring explains why this replaced the
# earlier "redundant with Trivy" call. Same goreleaser asset-naming convention as scc/gitleaks/trivy
# above (no "v" prefix in the filename, unlike the release tag).
ARG SYFT_VERSION=1.51.0
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

ARG NODE_MAJOR=22
# Azure Functions Core Tools: `func` is what actually builds and runs a Functions project, and
# app discovery (agent/src/app_discovery.py) classifies Azure Functions repos as in scope. Pinned
# to a major only -- nothing parses its output, same reasoning as jscpd/lizard below.
ARG FUNC_CORE_TOOLS_MAJOR=4
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
ENV PATH=/opt/aidw/tools/bin:/opt/aidw/tools/mise/shims:$PATH

RUN set -eux; \
    mkdir -p /opt/aidw/tools/bin /opt/aidw/cache; \
    chmod -R a+rwX /opt/aidw

# Node.js/npm (NodeSource) + Python + the GitHub CLI. dotnet, git, and curl already come from
# the base dotnet/sdk image (verified: Ubuntu 24.04, git/curl preinstalled, `dotnet --version`
# reports 10.0.x).
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates gnupg python3 python3-pip; \
    curl -fsSL --retry 5 --retry-all-errors "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash -; \
    apt-get install -y --no-install-recommends nodejs; \
    curl -fsSL --retry 5 --retry-all-errors https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg; \
    chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg; \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends gh; \
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
        "@playwright/test@${PLAYWRIGHT_VERSION}"; \
    playwright install chromium-headless-shell; \
    chmod -R a+rX /opt/playwright-browsers; \
    ln -s "$(ls /opt/playwright-browsers/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell)" \
        /usr/local/bin/aidw-headless-shell
ENV PUPPETEER_EXECUTABLE_PATH=/usr/local/bin/aidw-headless-shell

# P3's diagram-validation gate (agent/src/gates/diagram_gate.py) renders Mermaid source via
# @mermaid-js/mermaid-cli, which needs a headless Chromium (Puppeteer). Real finding from a live
# build: Ubuntu 24.04's `chromium` apt package is a snap-only stub with no actual browser inside
# (`exec /snap/bin/chromium "$@"`, which doesn't exist in a container -- no snapd here) -- do NOT
# apt-install `chromium` expecting a working binary. Instead: install the shared libs any headless
# Chromium needs (Puppeteer's own troubleshooting docs list these as commonly missing) and point
# Puppeteer at the Playwright headless shell baked above (PUPPETEER_EXECUTABLE_PATH env +
# "headless": "shell" in mermaid-puppeteer-config.json). Both skip-download spellings are set:
# PUPPETEER_SKIP_DOWNLOAD is current, PUPPETEER_SKIP_CHROMIUM_DOWNLOAD covers older bundled
# puppeteers -- mermaid-cli is installed unpinned.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
        libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2t64 libpango-1.0-0 \
        libcairo2; \
    PUPPETEER_SKIP_DOWNLOAD=1 PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=1 npm install -g @mermaid-js/mermaid-cli; \
    rm -rf /var/lib/apt/lists/*

# P8's quality scan: jscpd (duplication) via npm, lizard (per-function complexity) via pip.
# checkov (IaC misconfig) and interrogate (Python docstring coverage) ride the same pip line as
# lizard/semgrep -- all five are pure-Python static analyzers with no other runtime dependency.
# ponytail: no version pin here -- neither gate reads a specific tool version, just parses
# whatever JSON/CSV shape the latest release emits, so pinning buys nothing over always-latest.
# azure-functions-core-tools rides along in the same layer: npm rather than the
# packages.microsoft.com apt feed, since the package resolves its own linux-x64/linux-arm64
# payload (no new arch case block, no third apt keyring). --unsafe-perm because its postinstall
# runs as root here. Nothing in the pipeline invokes `func` yet -- it earns its size for
# Node-based Functions projects downstream, in R's build and P13's test runs.
RUN set -eux; \
    npm install -g jscpd "azure-functions-core-tools@${FUNC_CORE_TOOLS_MAJOR}" --unsafe-perm true; \
    pip install --break-system-packages --no-cache-dir --default-timeout=120 --retries 5 lizard semgrep checkov interrogate

# Front-end and Python quality toolchain. These are the deterministic signal for non-.NET repos:
# preflight_nodes.apply_stack_conventions writes eslint.config.mjs / ruff.toml / mypy.ini into a
# target repo, and rebuild.py runs `eslint --max-warnings=0`, `tsc --noEmit --strict`, `ruff check`
# and `mypy` so a violation fails the build the same way an analyzer warning does under
# `dotnet build -warnaserror`.
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
    npm install -g pnpm yarn typescript eslint prettier @angular/cli --unsafe-perm true; \
    pip install --break-system-packages --no-cache-dir --default-timeout=120 --retries 5 ruff mypy

# ── /opt/aidw/lint: the pipeline-owned ESLint toolchain, resolved AGAINST ITS OWN node_modules ─
# Linting a target repo must never mutate that repo's dependency graph: installing our lint
# devDependencies into a pnpm workspace root once re-resolved the whole peer graph, forked
# drizzle-orm into two incompatible peer-variant instances, and broke the repo's own build.
# So the toolchain lives here, pinned at image build, and the rebuild gate invokes
#   /opt/aidw/lint/node_modules/.bin/eslint --config /opt/aidw/lint/eslint.config.mjs .
# Root-owned and immutable at runtime for the same reason as the global npm tools above.
COPY lint/ /opt/aidw/lint/
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
RUN set -eux; \
    cd /opt/aidw/test; \
    npm install --no-audit --no-fund; \
    test -x node_modules/.bin/vitest

# mise / scc / gitleaks / trivy / osv-scanner / copilot all arrive via the fetch stage's
# COPY --from at the top of this stage. mise usage note: driven at container start by
# bootstrap.sh from files the repo itself declares (.tool-versions, mise.toml, .nvmrc,
# .node-version) -- never from an LLM's guess -- and restricted there to mise's own tool
# registry, because an arbitrary asdf plugin URL in a repo-supplied mise.toml is third-party
# shell that would otherwise run automatically before anything else in the container starts.

# ── Offline scan data: the vulnerability databases and semgrep rules, baked at BUILD time ──────
# Nothing in src/repo_scan.py touches the network at scan time. Two reasons, both load-bearing:
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
RUN set -eux; \
    mkdir -p "$TRIVY_CACHE_DIR" "$AIDW_OSV_DB_DIR"; \
    trivy image --download-db-only --cache-dir "$TRIVY_CACHE_DIR"; \
    ( cd /tmp && osv-scanner scan source --download-offline-databases \
        --local-db-path "$AIDW_OSV_DB_DIR" --offline-vulnerabilities . || true ); \
    chmod -R a+rX "$TRIVY_CACHE_DIR" "$AIDW_OSV_DB_DIR"; \
    test -n "$(ls -A "$AIDW_OSV_DB_DIR")"; \
    test -n "$(ls -A "$TRIVY_CACHE_DIR")"

# Semgrep rules, fetched from a pinned tag at BUILD time rather than via `--config auto` at scan
# time. `auto` means a network fetch on every run, rules that change underneath a baseline
# comparison, and the hosted registry's own terms attaching to what it serves.
#
# semgrep-rules is LGPL-2.1, and semgrep itself is LGPL-2.1 -- the one non-permissive dependency in
# this image, kept deliberately (it is the only free multi-language SAST engine) and recorded as
# `permissive: false` in every scan report's tools[] block. Individual rule files that declare a
# different licence in their own metadata are deleted below, so only the LGPL-2.1 set ships.
#
# The resolved commit is written to REVISION so a scan report can be traced back to the exact rule
# set that produced it. The pin is the built image, same as for every other tool here: rules only
# move when the image is rebuilt, never between two scans compared against each other.
ARG SEMGREP_RULES_REF=develop
RUN set -eux; \
    git clone --depth 1 --branch "${SEMGREP_RULES_REF}" \
        https://github.com/semgrep/semgrep-rules.git /tmp/semgrep-rules; \
    mkdir -p "$AIDW_SEMGREP_RULES_DIR"; \
    git -C /tmp/semgrep-rules rev-parse HEAD > "$AIDW_SEMGREP_RULES_DIR/REVISION"; \
    cp /tmp/semgrep-rules/LICENSE "$AIDW_SEMGREP_RULES_DIR/LICENSE"; \
    for lang in c cpp csharp dockerfile generic go java javascript json kotlin php python ruby rust scala terraform typescript yaml; do \
        if [ -d "/tmp/semgrep-rules/$lang" ]; then cp -r "/tmp/semgrep-rules/$lang" "$AIDW_SEMGREP_RULES_DIR/"; fi; \
    done; \
    find "$AIDW_SEMGREP_RULES_DIR" -name '*.yaml' -exec grep -lEi 'Semgrep Rules License|Commons Clause' {} + \
        | xargs -r rm -f; \
    find "$AIDW_SEMGREP_RULES_DIR" -name '*.test.*' -delete; \
    rm -rf /tmp/semgrep-rules; \
    test -n "$(find "$AIDW_SEMGREP_RULES_DIR" -name '*.yaml' -print -quit)"; \
    chmod -R a+rX "$AIDW_SEMGREP_RULES_DIR"
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
COPY plugins/ /opt/ai-dev-workflow-plugins/
RUN chmod +x /usr/local/bin/ai-dev-workflow-entrypoint.sh /usr/local/bin/ai-dev-workflow-bootstrap.sh && \
    mkdir -p /workspace && chown -R vscode:vscode /workspace /opt/ai-dev-workflow-plugins /opt/aidw && \
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

EXPOSE 3000

ENTRYPOINT ["/usr/local/bin/ai-dev-workflow-entrypoint.sh"]
