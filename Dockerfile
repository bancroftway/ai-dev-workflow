# Next.js frontend image (architecture plan Section D). Multi-stage build using
# `output: "standalone"` (next.config.ts) so the final image doesn't need full node_modules.
FROM node:22-slim AS deps
WORKDIR /app
COPY package.json package-lock.json ./
# `npm install`, not `npm ci`: package-lock.json (generated on Windows/macOS dev machines) doesn't
# fully resolve optional wasm32 fallback binaries some transitive deps only need on other
# platforms (e.g. sharp's @img/sharp-wasm32 -> @emnapi/runtime), which trips npm ci's strict
# lockfile-completeness check on this Linux build image even though nothing here is missing for
# real. `npm install` reconciles it the same way a local `npm install` already would.
RUN npm install

FROM node:22-slim AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM node:22-slim AS runner
WORKDIR /app
ENV NODE_ENV=production
RUN groupadd --system --gid 1001 nodejs && useradd --system --uid 1001 --gid nodejs nextjs
# npm/npx/corepack are never invoked at runtime (CMD is `node server.js` only) -- standalone
# output has no need for them, and their bundled deps (tar, pacote, sigstore, ...) are what trivy
# keeps flagging in every node:*-slim image.
RUN rm -rf /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/corepack \
    /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/corepack

COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs
EXPOSE 3000
ENV PORT=3000
ENV HOSTNAME=0.0.0.0

CMD ["node", "server.js"]
