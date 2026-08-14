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
FROM ${BASE_IMAGE}

# Pinned to match agent/.venv's installed github-copilot-sdk build (agent/.venv/Lib/site-packages/
# copilot/_cli_version.py's CLI_VERSION) so the SDK's minimum protocol version check
# (client.py's _MIN_PROTOCOL_VERSION) never rejects this server on a stale binary.
ARG COPILOT_CLI_VERSION=1.0.78
ARG NODE_MAJOR=22
# P8/P10/P11/P14 deterministic tool versions -- pinned so a gate's pass/fail is reproducible
# across sessions, same reasoning as COPILOT_CLI_VERSION above. NOT yet exercised end-to-end
# against a rebuilt image -- see each gate module's own docstring.
# Azure Functions Core Tools: `func` is what actually builds and runs a Functions project, and
# app discovery (agent/src/app_discovery.py) classifies Azure Functions repos as in scope. Pinned
# to a major only -- nothing parses its output, same reasoning as jscpd/lizard below.
ARG FUNC_CORE_TOOLS_MAJOR=4
ARG SCC_VERSION=3.5.0
ARG GITLEAKS_VERSION=8.21.2
ARG TRIVY_VERSION=0.73.0
# osv-scanner: a second vulnerability database (OSV.dev) alongside Trivy's. Deliberately both --
# they disagree at the edges, and src/repo_scan.py reconciles their advisory ids via OSV's alias
# lists so the overlap collapses to one finding rather than two. Pinned to a v2 release because
# repo_scan.py's command uses the v2 `scan source --offline-vulnerabilities` subcommand form;
# v1's flags differ and would silently produce no output.
ARG OSV_SCANNER_VERSION=2.0.2
# mise (multi-language version manager) is what lets this immutable image serve a repo needing a
# toolchain it doesn't ship -- Node 18, a second JDK, a Go version. See the /opt/aidw layout below
# and bootstrap.sh for how it's driven. Pinned like every other tool here.
ARG MISE_VERSION=2025.1.0

USER root

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
#                      agent/src/sandbox/local_docker.py.
#
# Both are created AND declared here rather than only at mount time, so the container behaves
# identically with or without a volume attached -- the mount is purely an accelerator, never a
# correctness dependency.
#
# Do NOT move PUPPETEER_CACHE_DIR (below) under /opt/aidw/cache: it is populated at *build* time,
# and mounting a volume over a build-populated directory hides its contents -- the same class of
# bug this file already records for /root/.cache/puppeteer.
ENV AIDW_TOOLS_DIR=/opt/aidw/tools \
    AIDW_CACHE_DIR=/opt/aidw/cache \
    MISE_DATA_DIR=/opt/aidw/tools/mise \
    MISE_CACHE_DIR=/opt/aidw/cache/mise \
    npm_config_cache=/opt/aidw/cache/npm \
    NUGET_PACKAGES=/opt/aidw/cache/nuget \
    PIP_CACHE_DIR=/opt/aidw/cache/pip \
    UV_CACHE_DIR=/opt/aidw/cache/uv \
    PLAYWRIGHT_BROWSERS_PATH=/opt/aidw/cache/playwright \
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
    curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash -; \
    apt-get install -y --no-install-recommends nodejs; \
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg; \
    chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg; \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends gh; \
    rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
        amd64) asset="copilot-linux-x64.tar.gz" ;; \
        arm64) asset="copilot-linux-arm64.tar.gz" ;; \
        *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://github.com/github/copilot-cli/releases/download/v${COPILOT_CLI_VERSION}/${asset}" \
        -o /tmp/copilot.tar.gz; \
    tar -xzf /tmp/copilot.tar.gz -C /usr/local/bin copilot; \
    chmod +x /usr/local/bin/copilot; \
    rm -f /tmp/copilot.tar.gz

# P3's diagram-validation gate (agent/src/gates/diagram_gate.py) renders Mermaid source via
# @mermaid-js/mermaid-cli, which needs a headless Chromium (Puppeteer). Real finding from a live
# build: Ubuntu 24.04's `chromium` apt package is a snap-only stub with no actual browser inside
# (`exec /snap/bin/chromium "$@"`, which doesn't exist in a container -- no snapd here) -- do NOT
# apt-install `chromium` expecting a working binary. Instead: install the shared libs any headless
# Chromium needs (Puppeteer's own troubleshooting docs list these as commonly missing) plus
# `unzip` (puppeteer's browser installer needs it to extract its own download), and let Puppeteer
# download and manage its own bundled Chromium build -- the actually-supported path, and the one
# mermaid-puppeteer-config.json's args (not executablePath) now assumes.
# PUPPETEER_CACHE_DIR fixed to a world-readable path, not the default $HOME/.cache/puppeteer --
# this RUN executes as root (USER root above), but the container runs as `vscode` at runtime
# (USER vscode below); a real end-to-end test caught puppeteer's downloaded Chromium landing in
# /root/.cache/puppeteer, invisible to vscode's own $HOME/.cache/puppeteer at runtime. Setting it
# via ENV (not just for this RUN) means every later user/process sees the same path.
ENV PUPPETEER_CACHE_DIR=/opt/puppeteer-cache
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        unzip libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
        libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2t64 libpango-1.0-0 \
        libcairo2; \
    npm install -g @mermaid-js/mermaid-cli; \
    chmod -R a+rX "$PUPPETEER_CACHE_DIR"; \
    rm -rf /var/lib/apt/lists/*

# P8's quality scan: jscpd (duplication) via npm, lizard (per-function complexity) via pip.
# ponytail: no version pin here -- neither gate reads a specific tool version, just parses
# whatever JSON/CSV shape the latest release emits, so pinning buys nothing over always-latest.
# azure-functions-core-tools rides along in the same layer: npm rather than the
# packages.microsoft.com apt feed, since the package resolves its own linux-x64/linux-arm64
# payload (no new arch case block, no third apt keyring). --unsafe-perm because its postinstall
# runs as root here. Nothing in the pipeline invokes `func` yet -- it earns its size for
# Node-based Functions projects downstream, in R's build and P13's test runs.
RUN set -eux; \
    npm install -g jscpd "azure-functions-core-tools@${FUNC_CORE_TOOLS_MAJOR}" --unsafe-perm true; \
    pip install --break-system-packages --no-cache-dir --default-timeout=120 --retries 5 lizard semgrep

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

# mise: the answer to "this image cannot ship every toolchain every repo needs". Driven at
# container start by bootstrap.sh from files the repo itself declares (.tool-versions, mise.toml,
# .nvmrc, .node-version) -- never from an LLM's guess -- and restricted there to mise's own tool
# registry, because an arbitrary asdf plugin URL in a repo-supplied mise.toml is third-party shell
# that would otherwise run automatically before anything else in the container starts.
# Arch-aware release fetch, same pattern as scc/gitleaks/trivy above.
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
        amd64) asset="mise-v${MISE_VERSION}-linux-x64.tar.gz" ;; \
        arm64) asset="mise-v${MISE_VERSION}-linux-arm64.tar.gz" ;; \
        *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://github.com/jdx/mise/releases/download/v${MISE_VERSION}/${asset}" -o /tmp/mise.tar.gz; \
    tar -xzf /tmp/mise.tar.gz -C /tmp; \
    install -m 0755 /tmp/mise/bin/mise /usr/local/bin/mise; \
    rm -rf /tmp/mise /tmp/mise.tar.gz

# P8/P14: scc (LOC/complexity). Go binary release, arch-aware like the Copilot CLI fetch above.
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
        amd64) asset="scc_Linux_x86_64.tar.gz" ;; \
        arm64) asset="scc_Linux_arm64.tar.gz" ;; \
        *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://github.com/boyter/scc/releases/download/v${SCC_VERSION}/${asset}" -o /tmp/scc.tar.gz; \
    tar -xzf /tmp/scc.tar.gz -C /usr/local/bin scc; \
    chmod +x /usr/local/bin/scc; \
    rm -f /tmp/scc.tar.gz

# P10: gitleaks (secrets). Binary release, arch-aware.
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
        amd64) asset="gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" ;; \
        arm64) asset="gitleaks_${GITLEAKS_VERSION}_linux_arm64.tar.gz" ;; \
        *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/${asset}" -o /tmp/gitleaks.tar.gz; \
    tar -xzf /tmp/gitleaks.tar.gz -C /usr/local/bin gitleaks; \
    chmod +x /usr/local/bin/gitleaks; \
    rm -f /tmp/gitleaks.tar.gz

# P10: trivy (vuln/misconfig/license scan + SBOM). Binary release, arch-aware.
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
        amd64) asset="trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz" ;; \
        arm64) asset="trivy_${TRIVY_VERSION}_Linux-ARM64.tar.gz" ;; \
        *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/${asset}" -o /tmp/trivy.tar.gz; \
    tar -xzf /tmp/trivy.tar.gz -C /usr/local/bin trivy; \
    chmod +x /usr/local/bin/trivy; \
    rm -f /tmp/trivy.tar.gz

# P10/P14: osv-scanner (OSV.dev vulnerability database). Binary release, arch-aware.
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
        amd64) asset="osv-scanner_linux_amd64" ;; \
        arm64) asset="osv-scanner_linux_arm64" ;; \
        *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://github.com/google/osv-scanner/releases/download/v${OSV_SCANNER_VERSION}/${asset}" \
        -o /usr/local/bin/osv-scanner; \
    chmod +x /usr/local/bin/osv-scanner

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
RUN set -eux; \
    mkdir -p "$TRIVY_CACHE_DIR" "$AIDW_OSV_DB_DIR"; \
    trivy image --download-db-only --cache-dir "$TRIVY_CACHE_DIR"; \
    trivy image --download-java-db-only --cache-dir "$TRIVY_CACHE_DIR"; \
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
