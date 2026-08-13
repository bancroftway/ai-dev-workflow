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
ARG SCC_VERSION=3.5.0
ARG GITLEAKS_VERSION=8.21.2
ARG TRIVY_VERSION=0.73.0

USER root

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
RUN set -eux; \
    npm install -g jscpd; \
    pip install --break-system-packages --no-cache-dir --default-timeout=120 --retries 5 lizard semgrep

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
# ponytail: nuget-license (P11d's .NET license scan) intentionally NOT installed here -- it's a
# `dotnet tool` pulled from NuGet at scan time in a *target* repo's own container, not something
# this base image can usefully pre-install (no target repo/lockfile exists yet at image-build
# time). p11_gates.py's command already tolerates it being absent (best-effort, non-fatal).

# dotnet/sdk's base image has no non-root user; devcontainer images conventionally run as
# "vscode" and this repo's entrypoint.sh/provisioning code doesn't need to care which base
# provided the user, so create one here for parity either way.
RUN useradd -m -s /bin/bash vscode

COPY entrypoint.sh /usr/local/bin/ai-dev-workflow-entrypoint.sh
# Agent Plugin content (skills/commands/MCP servers for GitHub Copilot CLI) -- see
# agent/sandbox-image/README.md and agent/src/config.py's COPILOT_PLUGIN_DIRECTORIES for
# the matching in-container path this must stay in sync with.
COPY plugins/ /opt/ai-dev-workflow-plugins/
RUN chmod +x /usr/local/bin/ai-dev-workflow-entrypoint.sh && \
    mkdir -p /workspace && chown -R vscode:vscode /workspace /opt/ai-dev-workflow-plugins

USER vscode
WORKDIR /workspace

EXPOSE 3000

ENTRYPOINT ["/usr/local/bin/ai-dev-workflow-entrypoint.sh"]
