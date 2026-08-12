# Python FastAPI/LangGraph agent image (architecture plan Section D).
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# AzureContainerInstanceProvider (agent/src/sandbox/azure_aci.py, SANDBOX_PROVIDER=azure) shells
# out to `az container ...` to provision/tear down per-session ACI sandboxes -- required in any
# real deployment, not just for local testing against this provider. Authenticates via the
# Container App's managed identity (`az login --identity`, wired up separately, not baked into
# this image) rather than a credential shipped in the image.
RUN curl -sL https://aka.ms/InstallAzureCLIDeb | bash

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . .
RUN chmod +x docker-entrypoint.sh

# SANDBOX_PROVIDER=local (agent/src/sandbox/factory.py) needs Docker itself to provision
# sandboxes -- meaningless inside this container; local-only, for a developer's own machine.
# Deployed, this image is the persistent agent service only, using SANDBOX_PROVIDER=azure; it is
# not itself a sandbox.
EXPOSE 8123
ENV PORT=8123

ENTRYPOINT ["./docker-entrypoint.sh"]
