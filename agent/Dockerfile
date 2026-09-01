# Python FastAPI/LangGraph agent image (architecture plan Section D).
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# session_store.py imports aioodbc/pyodbc at module load time (agent/pyproject.toml), which link
# against the ODBC driver manager -- without it the app crashes on every boot, not just before the
# first-bootstrap vault seed. Mirrors the MS driver install deploy.yml does on the runner.
RUN curl -sSL -o /tmp/packages-microsoft-prod.deb \
        "https://packages.microsoft.com/config/debian/$(. /etc/os-release && echo "$VERSION_ID")/packages-microsoft-prod.deb" \
    && dpkg -i /tmp/packages-microsoft-prod.deb \
    && rm -f /tmp/packages-microsoft-prod.deb \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
    && rm -rf /var/lib/apt/lists/*

# AzureContainerInstanceProvider (agent/src/sandbox/azure_aci.py, SANDBOX_PROVIDER=azure) shells
# out to `az container ...` to provision/tear down per-session ACI sandboxes -- required in any
# real deployment, not just for local testing against this provider. Authenticates via the
# Container App's managed identity (`az login --identity`, wired up separately, not baked into
# this image) rather than a credential shipped in the image.
RUN curl -sL https://aka.ms/InstallAzureCLIDeb | bash

# az CLI's bundled venv pins an old cryptography (2 HIGH CVEs); bump it directly, it's a stable
# leaf dependency. pip itself (both az CLI's copy and the base image's, neither ever invoked --
# this app only runs through uv's own .venv, and the az commands actually used (login, container,
# containerapp) don't touch pip) vendors its own old setuptools/msgpack that we can't patch
# in place, so remove pip entirely instead of carrying findings for code that's never reachable.
RUN /opt/az/bin/python3 -m pip install --no-cache-dir --upgrade "cryptography>=50.0.0" \
    && rm -rf /usr/local/lib/python*/site-packages/pip* /opt/az/lib/python*/site-packages/pip*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . .
RUN chmod +x docker-entrypoint.sh

# Non-root: az CLI's managed-identity login (docker-entrypoint.sh) writes its cache under $HOME,
# so the user needs a real home dir, not just a UID.
RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app
USER appuser

# SANDBOX_PROVIDER=local (agent/src/sandbox/factory.py) needs Docker itself to provision
# sandboxes -- meaningless inside this container; local-only, for a developer's own machine.
# Deployed, this image is the persistent agent service only, using SANDBOX_PROVIDER=azure; it is
# not itself a sandbox.
EXPOSE 8123
ENV PORT=8123

ENTRYPOINT ["./docker-entrypoint.sh"]
