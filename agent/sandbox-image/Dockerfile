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

# dotnet/sdk's base image has no non-root user; devcontainer images conventionally run as
# "vscode" and this repo's entrypoint.sh/provisioning code doesn't need to care which base
# provided the user, so create one here for parity either way.
RUN useradd -m -s /bin/bash vscode

COPY entrypoint.sh /usr/local/bin/ai-dev-workflow-entrypoint.sh
RUN chmod +x /usr/local/bin/ai-dev-workflow-entrypoint.sh && \
    mkdir -p /workspace && chown -R vscode:vscode /workspace

USER vscode
WORKDIR /workspace

EXPOSE 3000

ENTRYPOINT ["/usr/local/bin/ai-dev-workflow-entrypoint.sh"]
